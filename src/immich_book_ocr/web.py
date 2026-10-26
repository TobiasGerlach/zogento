"""Loopback-only browser interface for the book OCR pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import threading
from typing import Any
from urllib.parse import urlsplit
import unicodedata
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from .pipeline import PipelineOptions, run_album, run_local


@dataclass
class _Job:
    id: str
    output: Path
    options: PipelineOptions
    status: str = "running"
    events: list[dict[str, str]] = field(default_factory=list)
    report: dict[str, Any] | None = None
    error: str | None = None


def _folder_name(title: object, fallback: str) -> str:
    """Turn an album title into one safe path segment.

    The title comes from the Immich server, so it is treated as untrusted text:
    anything that is not a letter, digit, underscore or hyphen becomes an
    underscore, which leaves no way to name a parent directory or escape the
    output root. An unusable title falls back to the job id.
    """
    slug = unicodedata.normalize("NFC", str(title)).casefold()
    slug = re.sub(r"[^\w-]+", "_", slug, flags=re.UNICODE)
    slug = re.sub(r"_{2,}", "_", slug).strip("_-.")[:80].strip("_-.")
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        return fallback
    return slug


def _unused_path(root: Path, name: str, keep: Path) -> Path | None:
    """Find a free directory name, so a second copy of a book cannot overwrite the first."""
    candidate = root / name
    if candidate == keep or not candidate.exists():
        return candidate
    for suffix in range(2, 100):
        candidate = root / f"{name}-{suffix}"
        if not candidate.exists():
            return candidate
    return None


def _safe_message(value: object, hidden: tuple[str, ...] = ()) -> str:
    """Keep network credentials and album URLs out of browser status messages."""
    message = str(value)
    for secret in sorted((part for part in hidden if part), key=len, reverse=True):
        message = message.replace(secret, "[private]")
    message = re.sub(r"https?://[^\s<>\"']+", "[server]", message)
    return message[:2000]


def _clean_report(value: Any, hidden: tuple[str, ...], field_name: str = "") -> Any:
    if isinstance(value, str):
        # Filenames identify cached assets. A short password can coincidentally
        # occur in a filename; redacting it would break previews and corrections.
        if field_name in {"source", "preview", "image", "text_file", "tsv_file", "cache_key"}:
            return value
        return _safe_message(value, hidden)
    if isinstance(value, dict):
        return {str(key): _clean_report(item, hidden, str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_report(item, hidden, field_name) for item in value]
    return value


def _options(payload: dict[str, Any]) -> PipelineOptions:
    defaults = PipelineOptions()
    language = str(payload.get("language", defaults.language)).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\+[A-Za-z][A-Za-z0-9_]*)*", language):
        raise ValueError("Enter OCR language codes such as eng or deu+eng.")
    order = payload.get("order", defaults.order)
    if order not in {"album", "filename", "date"}:
        raise ValueError("Choose a valid scan order.")
    reading_order = payload.get("reading_order", "ltr")
    if reading_order not in {"ltr", "rtl"}:
        raise ValueError("Choose a valid reading direction.")
    single_page = payload.get("single_page", False)
    if not isinstance(single_page, bool):
        raise ValueError("Single-page mode must be true or false.")
    engine = payload.get("engine", "auto")
    if engine not in {"auto", "vision", "tesseract"}:
        raise ValueError("Choose a valid OCR engine.")
    rotation = str(payload.get("rotation", "auto"))
    if rotation not in {"auto", "none", "0", "90", "180", "270"}:
        raise ValueError("Choose a valid page rotation.")
    crop_paper = payload.get("crop_paper", True)
    if not isinstance(crop_paper, bool):
        raise ValueError("Background trimming must be true or false.")
    return PipelineOptions(
        language=language,
        order=order,
        reading_order=reading_order,
        single_page=single_page,
        psm=3,
        models="best",
        engine=engine,
        rotation=rotation,
        crop_paper=crop_paper,
        split_overrides={},
    )


def create_app(output_root: Path) -> Flask:
    """Create the UI. The CLI must bind its server to a loopback address."""
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    jobs: dict[str, _Job] = {}
    lock = threading.RLock()
    active: str | None = None

    @app.before_request
    def protect_local_app():
        try:
            hostname = urlsplit(f"http://{request.host}").hostname
        except ValueError:
            abort(400, "Invalid host.")
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            abort(403, "This application is available only on localhost.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != f"{request.scheme}://{request.host}":
                abort(403, "The request must come from this application.")
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(403, "Refresh this page before submitting again.")
            if not request.is_json:
                abort(415, "Send a JSON request.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def get_job(job_id: str) -> _Job:
        with lock:
            job = jobs.get(job_id)
            if job is None:
                abort(404, "This job is no longer available. Start a new album.")
            return job

    def event(job: _Job, message: object, hidden: tuple[str, ...] = ()) -> None:
        with lock:
            job.events.append(
                {"time": datetime.now(timezone.utc).isoformat(), "message": _safe_message(message, hidden)}
            )
            del job.events[:-150]

    def worker(job: _Job, url: str | None, api_key: str | None, password: str | None):
        nonlocal active
        hidden = tuple(value for value in (url, api_key, password) if value)
        try:
            progress = lambda message: event(job, message, hidden)
            if url is None:
                report = run_local(job.output / "originals", job.output, options=job.options, progress=progress)
            else:
                report = run_album(
                    url, job.output, options=job.options, api_key=api_key, password=password, progress=progress
                )
            with lock:
                # Everything inside the folder is referenced relatively, so it can
                # be renamed once the album metadata has revealed the book's title.
                _rename_to_title(job, report.get("title"))
                job.report = _clean_report(report, hidden)
                job.status = "completed"
            event(job, "Text is ready. Review the scan warnings before using it.")
        except Exception as error:
            with lock:
                job.error = _safe_message(error, hidden) or "The job could not finish."
                job.status = "failed"
            event(job, "Processing stopped. See the error below.")
        finally:
            with lock:
                active = None

    def _rename_to_title(job: _Job, title: object) -> None:
        name = _folder_name(title, job.id)
        if name == job.id:
            return
        target = _unused_path(output_root, name, job.output)
        if target is None or target == job.output:
            return
        try:
            job.output.rename(target)
        except OSError:
            # A locked or otherwise unmovable folder is not worth failing a
            # finished book over; the results stay where they are.
            return
        job.output = target

    def launch(job: _Job, url: str | None, api_key: str | None = None, password: str | None = None):
        nonlocal active
        active = job.id
        job.status = "running"
        job.error = None
        thread = threading.Thread(target=worker, args=(job, url, api_key, password), daemon=True)
        thread.start()

    @app.get("/")
    def index():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return render_template("index.html", csrf_token=session["csrf_token"])

    @app.post("/api/jobs")
    def start_job():
        payload = request.get_json()
        if not isinstance(payload, dict):
            abort(400, "Enter the album details.")
        url = payload.get("url", "")
        if not isinstance(url, str) or not url.strip():
            abort(400, "Paste an Immich album or share link.")
        url = url.strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Use an http or https album link without embedded credentials.")
            options = _options(payload)
        except (ValueError, TypeError) as error:
            abort(400, str(error))
        credentials = []
        for name in ("api_key", "password"):
            value = payload.get(name)
            if value is not None and not isinstance(value, str):
                abort(400, "Credentials must be text.")
            credentials.append(value or None)
        resume_id = payload.get("resume_job")
        if resume_id is not None and not isinstance(resume_id, str):
            abort(400, "The job to retry was not recognized.")
        with lock:
            if active is not None:
                return jsonify(error="An album is already being processed.", job_id=active), 409
            if resume_id:
                job = get_job(resume_id)
                if job.status != "failed":
                    abort(400, "Only a stopped job can be retried from the album form.")
                job.options = options
                job.report = None
                event(job, "Retrying your Immich album using saved downloads…")
            else:
                job_id = uuid.uuid4().hex
                job = _Job(job_id, output_root / job_id, options)
                job.output.mkdir()
                jobs[job_id] = job
                event(job, "Opening your Immich album…")
            launch(job, url, *credentials)
        return jsonify(id=job.id), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = get_job(job_id)
        with lock:
            return jsonify(
                id=job.id,
                status=job.status,
                events=job.events,
                error=job.error,
                report=job.report,
                single_page=job.options.single_page,
                download_url=f"/api/jobs/{job.id}/text" if job.report else None,
            )

    @app.post("/api/jobs/<job_id>/rerun")
    def rerun_job(job_id: str):
        job = get_job(job_id)
        payload = request.get_json()
        if not isinstance(payload, dict) or not isinstance(payload.get("splits", {}), dict):
            abort(400, "Send page split positions by scan filename.")
        splits = payload.get("splits", {})
        with lock:
            if active is not None:
                return jsonify(error="An album is already being processed.", job_id=active), 409
            if not job.report or not (job.output / "originals").is_dir():
                abort(400, "There are no cached scans to process. Start the album again.")
            allowed = {entry["source"] for entry in job.report.get("entries", [])}
            corrected = dict(job.options.split_overrides)
            for name, fraction in splits.items():
                if name not in allowed:
                    abort(400, "A scan filename was not recognized.")
                if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0.05 <= fraction <= 0.95:
                    abort(400, "Page splits must be between 5% and 95% of the scan width.")
                corrected[name] = float(fraction)
            job.options = replace(job.options, split_overrides=corrected)
            event(job, "Reprocessing saved scans with your page splits…")
            launch(job, None)
        return jsonify(id=job.id), 202

    def job_file(job: _Job, relative: str) -> Path:
        path = (job.output / relative).resolve()
        if not path.is_relative_to(job.output.resolve()) or not path.is_file():
            abort(404, "The requested file is unavailable.")
        return path

    @app.get("/api/jobs/<job_id>/text")
    def download_text(job_id: str):
        job = get_job(job_id)
        if job.status != "completed" or not job.report:
            abort(409, "Wait for processing to finish before downloading.")
        path = job_file(job, "book.txt")
        name = _folder_name((job.report or {}).get("title"), "book")
        return send_file(path, mimetype="text/plain; charset=utf-8", as_attachment=True,
                         download_name=f"{name}.txt")

    @app.get("/api/jobs/<job_id>/preview/<int:entry_index>")
    def preview(job_id: str, entry_index: int):
        job = get_job(job_id)
        entries = (job.report or {}).get("entries", [])
        if entry_index >= len(entries) or not entries[entry_index].get("preview"):
            abort(404, "A preview is unavailable for this scan.")
        path = job_file(job, str(entries[entry_index]["preview"]))
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            abort(404, "A preview is unavailable for this scan.")
        return send_file(path)

    return app
