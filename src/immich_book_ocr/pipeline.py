"""Download, split, recognize, and assemble a book, with resumable page results."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import cv2

from . import vision
from .imaging import prepare_spread
from .immich import download_album
from .ocr import configuration_signature, ensure_models, language_codes, recognize_page
from .imaging import TextGeometry
from .ocr import text_angle as tesseract_text_angle

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic", ".heif", ".avif"}
Progress = Callable[[str], None]


@dataclass
class PipelineOptions:
    language: str = "eng+deu"
    order: str = "date"
    reading_order: str = "ltr"
    single_page: bool = False
    psm: int = 3
    models: str = "best"
    engine: str = "auto"
    workers: int = 0
    rotation: str = "auto"
    crop_paper: bool = True
    split_overrides: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        language_codes(self.language)
        if self.order not in {"album", "filename", "date"}:
            raise ValueError("Order must be album, filename, or date.")
        if self.reading_order not in {"ltr", "rtl"}:
            raise ValueError("Reading order must be ltr or rtl.")
        if self.psm not in {3, 4, 6, 11} or self.models not in {"best", "system"}:
            raise ValueError("Invalid OCR mode or model selection.")
        if self.engine not in {"auto", "vision", "tesseract"}:
            raise ValueError("OCR engine must be auto, vision, or tesseract.")
        if str(self.rotation) not in {"auto", "none", "0", "90", "180", "270"}:
            raise ValueError("Rotation must be auto, none, 0, 90, 180, or 270.")
        if not isinstance(self.crop_paper, bool):
            raise ValueError("Paper cropping must be true or false.")
        if isinstance(self.workers, bool) or not isinstance(self.workers, int) or not 0 <= self.workers <= 64:
            raise ValueError("Workers must be a whole number between 0 (automatic) and 64.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.05 <= v <= 0.95 for v in self.split_overrides.values()):
            raise ValueError("Each split override must be a fraction between 0.05 and 0.95.")


def models_dir() -> Path:
    """Where downloaded tessdata_best models are cached (BOOK_OCR_MODELS overrides)."""
    configured = os.environ.get("BOOK_OCR_MODELS")
    return Path(configured) if configured else Path.cwd() / ".models" / "best"


def _prepare_engine(options: PipelineOptions, progress: Progress) -> tuple[str, Path | None, str]:
    """Pick the OCR engine and check its prerequisites before a long download.

    ``auto`` prefers Apple Vision: it reads rotated and skewed handheld book
    photos that Tesseract mangles, and it is several times faster per page.
    The resolved name is written back so cache keys and the report record it.
    """
    engine = options.engine
    if engine == "auto":
        engine = "vision" if vision.available(options.language) else "tesseract"
        if engine == "tesseract":
            progress("Apple Vision is unavailable; using Tesseract.")
    if engine == "vision":
        options.engine = "vision"
        return "vision", None, vision.configuration_signature(options.language)
    options.engine = "tesseract"
    tessdata = ensure_models(options.language, options.models, models_dir(), progress)
    return "tesseract", tessdata, configuration_signature(options.language, tessdata)


def _worker_count(options: PipelineOptions, engine: str) -> int:
    if options.workers:
        return options.workers
    cpus = os.cpu_count() or 4
    # Vision dispatches to the Neural Engine, so a handful of concurrent
    # requests saturates it; Tesseract is pure CPU and scales with cores.
    return max(1, min(6, cpus)) if engine == "vision" else max(1, cpus - 1)


def _natural(value: str) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_child(root: Path, relative: str) -> Path:
    child = (root / relative).resolve()
    if not child.is_relative_to(root.resolve()):
        raise ValueError("Invalid cached file path.")
    return child


def run_album(url: str, output: Path, *, options: PipelineOptions | None = None,
              api_key: str | None = None, password: str | None = None,
              progress: Progress | None = None) -> dict:
    options = options or PipelineOptions()
    options.validate()
    progress = progress or (lambda _: None)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Fail early on missing OCR prerequisites before downloading a large album.
    engine, tessdata, model_signature = _prepare_engine(options, progress)
    result = download_album(url, output / "originals", api_key=api_key, password=password,
                            order=options.order, progress=progress)
    sources = [{"source": asset.filename, "file": asset.path.name, "id": asset.id} for asset in result.assets]
    manifest = {"title": result.album_name, "sources": sources, "order": options.order,
                "warnings": result.warnings, "skipped_videos": result.skipped_videos}
    _write_json(output / "sources.json", manifest)
    return _process([(item["source"], output / "originals" / item["file"]) for item in sources],
                    output, options, result.album_name, list(result.warnings), progress,
                    engine, tessdata, model_signature)


def run_local(input_dir: Path, output: Path, *, options: PipelineOptions | None = None,
              progress: Progress | None = None) -> dict:
    options = options or PipelineOptions()
    options.validate()
    progress = progress or (lambda _: None)
    input_dir, output = Path(input_dir).resolve(), Path(output).resolve()
    if not input_dir.is_dir():
        raise ValueError("The scan directory does not exist.")
    output.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    title = input_dir.name
    manifest_path = output / "sources.json"
    if input_dir == output / "originals" and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sources = [(item["source"], _safe_child(input_dir, item["file"])) for item in manifest["sources"]]
        title = manifest["title"]
        warnings = list(manifest.get("warnings", []))
        if manifest.get("order", options.order) != options.order:
            warnings.append("Saved album order was retained for this correction run. Start a new album job to change the scan order.")
    else:
        files = [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        if options.order == "date":
            files.sort(key=lambda p: (p.stat().st_mtime_ns, _natural(p.name)))
            warnings.append("Local date order uses file modification time. Check the scan order in the review.")
        else:
            files.sort(key=lambda p: _natural(p.name))
        sources = [(p.name, p) for p in files]
    engine, tessdata, model_signature = _prepare_engine(options, progress)
    return _process(sources, output, options, title, warnings, progress, engine, tessdata, model_signature)


def _cache_key(path: Path, source: str, options: PipelineOptions, model_signature: str) -> str:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    values = asdict(options)
    # Thread count changes throughput, never output: it must not evict cached pages.
    values.pop("workers", None)
    values["split_overrides"] = options.split_overrides.get(source)
    # Bump this whenever splitting, rotation, or cropping changes: cached pages
    # are only reusable while the code that produced them would produce them again.
    return hashlib.sha256(json.dumps(["pipeline-v3", digest, source, values, model_signature], sort_keys=True).encode()).hexdigest()


def _angle_probe(engine: str, options: PipelineOptions, tessdata: Path | None):
    """Read a reduced spread once, for both its orientation and its layout.

    Only an OCR pass can tell an upright page from an upside-down one, and the
    same pass reports where the text blocks sit, which is what locates the
    binding. Returns None when the user pinned the rotation, leaving no
    question worth a pass.
    """
    if options.rotation != "auto":
        return None

    def probe(image):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "layout.png"
            image.save(candidate)
            if engine == "vision":
                return vision.text_geometry(candidate, options.language)
            # OSD reports a quarter turn and nothing about the layout, so the
            # pixel gutter detector stays in charge for the Tesseract path.
            measured = tesseract_text_angle(candidate, tessdata)
            return None if measured is None else TextGeometry(measured[0], [], measured[1])

    return probe


def _spread_rotation(options: PipelineOptions) -> int | str:
    if options.rotation == "auto":
        return "auto"
    return 0 if options.rotation == "none" else int(options.rotation)


def _recognize(engine: str, image: Path, stem_base: Path, options: PipelineOptions, tessdata: Path | None):
    if engine == "vision":
        return vision.recognize_page(image, stem_base, language=options.language)
    return recognize_page(image, stem_base, language=options.language, tessdata=tessdata, psm=options.psm)


def _run_scan(index: int, source: str, path: Path, total: int, output: Path, options: PipelineOptions,
              previous: dict, model_signature: str, engine: str, tessdata: Path | None) -> dict:
    """Process one scan end to end. Runs on a worker thread, so it raises nothing:
    a failure is reported in the result and the remaining scans continue."""
    entry = {"source": source, "preview": f"previews/{index:05d}.jpg", "split_fraction": None,
             "split_confidence": 0.0, "warnings": [], "pages": []}
    messages: list[str] = [f"Scan {index}/{total}: {source}"]
    try:
        # Include position: cached numbered artifact paths must never be reused
        # after a reordered scan has overwritten them during the current run.
        key = f"{index}:" + _cache_key(path, source, options, model_signature)
        artifact_stem = f"{index:05d}-{key.split(':', 1)[1][:20]}"
        entry["preview"] = f"previews/{artifact_stem}.jpg"
        cached = previous.get(source)
        cache_file = output / "cache" / (key.replace(":", "-") + ".json")
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if cached and cached.get("cache_key") == key and not cached.get("error"):
            required = [cached["preview"]] + [page[field] for page in cached["pages"] for field in ("text_file", "tsv_file", "image")]
            if cached["pages"] and all(_safe_child(output, relative).is_file() for relative in required):
                texts = [_safe_child(output, page["text_file"]).read_text(encoding="utf-8").strip() for page in cached["pages"]]
                messages.append("Reused cached OCR for this scan.")
                return {"entry": cached, "texts": texts, "messages": messages, "failed": False}
        spread = prepare_spread(path, split_fraction=options.split_overrides.get(source),
                                single_page=options.single_page, reading_order=options.reading_order,
                                rotation=_spread_rotation(options),
                                angle_probe=_angle_probe(engine, options, tessdata),
                                crop_paper=options.crop_paper)
        entry.update(split_fraction=spread.split_fraction, split_confidence=spread.confidence,
                     warnings=list(spread.warnings), cache_key=key, rotation=spread.rotation,
                     orientation_confidence=spread.orientation_confidence, cropped=spread.cropped)
        spread.preview.convert("RGB").save(output / entry["preview"], quality=85)
        texts = []
        for page_index, page in enumerate(spread.pages, 1):
            # Different settings never overwrite artifacts referenced by a
            # committed report, even if a rerun is interrupted mid-page.
            stem = f"{artifact_stem}-{page_index}"
            image_relative = f"pages/{stem}.png"
            page.save(output / image_relative, dpi=(300, 300))
            recognized = _recognize(engine, output / image_relative, output / "text" / stem, options, tessdata)
            record = {"image": image_relative, "text_file": f"text/{stem}.txt", "tsv_file": f"text/{stem}.tsv",
                      "confidence": recognized.confidence, "words": recognized.words,
                      "low_confidence_words": recognized.low_confidence_words, "warnings": recognized.warnings}
            entry["pages"].append(record)
            texts.append(recognized.text)
        _write_json(cache_file, entry)
        return {"entry": entry, "texts": texts, "messages": messages, "failed": False}
    except (OSError, ValueError, RuntimeError, cv2.error) as exc:
        entry["error"] = str(exc)
        entry["warnings"].append(f"Scan could not be fully processed: {exc}")
        messages.append(f"Scan {index} failed; continuing with the remaining scans.")
        return {"entry": entry, "texts": [f"[OCR INCOMPLETE: {source}. See report.json for details.]"],
                "messages": messages, "failed": True}


def _process(sources: list[tuple[str, Path]], output: Path, options: PipelineOptions, title: str,
             warnings: list[str], progress: Progress, engine: str, tessdata: Path | None,
             model_signature: str) -> dict:
    if not sources:
        raise ValueError("No images were found to OCR.")
    for subdir in ("pages", "previews", "text", "cache"):
        (output / subdir).mkdir(exist_ok=True)
    previous = {}
    try:
        old = json.loads((output / "report.json").read_text(encoding="utf-8"))
        previous = {entry["source"]: entry for entry in old["entries"]}
    except (OSError, ValueError, KeyError):
        pass
    unknown = set(options.split_overrides) - {source for source, _ in sources}
    if unknown:
        raise ValueError("Split overrides refer to unknown scans: " + ", ".join(sorted(unknown)))
    workers = _worker_count(options, engine)
    report = {"title": title, "complete": False, "scans": len(sources), "pages": 0,
              "warnings": warnings, "entries": [], "text_file": "book.txt",
              "options": asdict(options), "engine": engine, "workers": workers}
    text_parts: list[str] = []
    failures = 0
    # Invalidate the published completion flag before replacing any book text.
    # The previous report remains available in memory for verified page reuse.
    _write_json(output / "report.json", report)
    progress(f"Recognizing with {'Apple Vision' if engine == 'vision' else 'Tesseract'} across {workers} worker(s).")
    total = len(sources)
    remaining = iter(list(enumerate(sources, 1)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def submit_next() -> bool:
            for index, (source, path) in remaining:
                pending.append(pool.submit(_run_scan, index, source, path, total, output, options,
                                           previous, model_signature, engine, tessdata))
                return True
            return False

        pending: deque = deque()
        # Keep only a small lead over the consumer: scans hold full-size page
        # images in memory, so an unbounded queue would exhaust RAM on a big book.
        for _ in range(workers * 2):
            if not submit_next():
                break
        while pending:
            result = pending.popleft().result()
            for message in result["messages"]:
                progress(message)
            entry = result["entry"]
            text_parts.extend(result["texts"])
            failures += result["failed"]
            report["entries"].append(entry)
            report["pages"] += len(entry["pages"])
            _save_progress(output, report, text_parts)
            submit_next()
    report["complete"] = failures == 0
    report["failed_scans"] = failures
    if failures:
        report["warnings"].append(f"{failures} scans failed. The text is incomplete; inspect the report and rerun.")
    _save_progress(output, report, text_parts)
    _write_review(output, report)
    progress(f"Finished: {report['pages']} pages from {report['scans']} scans. " + ("Text is ready." if not failures else "Some scans need attention."))
    return report


def _save_progress(output: Path, report: dict, parts: list[str]) -> None:
    temporary = output / "book.txt.tmp"
    # Form feeds preserve physical page boundaries; do not invent missing words or remove hyphens.
    temporary.write_text("\n\n\f\n\n".join(parts) + "\n", encoding="utf-8")
    temporary.replace(output / "book.txt")
    _write_json(output / "report.json", report)


def _write_review(output: Path, report: dict) -> None:
    esc = html.escape
    sections = []
    for index, entry in enumerate(report["entries"], 1):
        notes = entry["warnings"] + [f"Page {i}: {warning}" for i, page in enumerate(entry["pages"], 1) for warning in page["warnings"]]
        preview = f'<img loading="lazy" src="{esc(entry["preview"])}" alt="Detected split for scan {index}">' if (output / entry["preview"]).exists() else ""
        split = f'{entry["split_fraction"]:.4f}' if entry["split_fraction"] is not None else "single page"
        if entry.get("rotation"):
            split += f' · rotated {entry["rotation"]}°'
        pages = " · ".join(f'<a href="{esc(p["text_file"])}">Page {i}</a> (confidence {p["confidence"]})' for i, p in enumerate(entry["pages"], 1))
        sections.append(f'<section><h2>{index}. {esc(entry["source"])}</h2><p>Split: {split} · {pages}</p><ul>' + "".join(f"<li>{esc(note)}</li>" for note in notes) + f"</ul>{preview}</section>")
    document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>OCR review</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px;background:#f5f3ed;color:#24332d}section{background:white;padding:24px;margin:24px 0;border-radius:12px}img{width:100%;height:auto}a{color:#17694e}li{margin:8px 0}</style>'
    document += f'<h1>{esc(report["title"])} — OCR review</h1><p>{report["scans"]} scans · {report["pages"]} pages · ' + ("All scans processed" if report["complete"] else "INCOMPLETE") + '</p><p><a href="book.txt">Open book.txt</a> · <a href="report.json">Detailed report</a></p><p>Confidence is an OCR heuristic, not measured accuracy. Check reading order and gutter positions. Use the browser app or a split-overrides JSON file to correct splits.</p>'
    document += "".join(f"<p>{esc(w)}</p>" for w in report["warnings"]) + "".join(sections) + "</html>"
    (output / "review.html").write_text(document, encoding="utf-8")
