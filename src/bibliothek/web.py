"""Web-Oberfläche der Bibliothek (nur lokal, 127.0.0.1).

Seiten werden auf dem Server gerendert, Formulare sind gewöhnliche POSTs mit
CSRF-Token. JavaScript gibt es nur für Komfort: Job-Fortschritt, Vorschau der
Seitenzählung, Felder hinzufügen, Schnell-Upload. Die JSON-API unter /api/
bedient die CLI.

Mit verbundener Nextcloud gleicht die App beim Start, alle fünf Minuten und
kurz nach jeder Änderung ab (auto_sync, nur in der laufenden App).
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for)
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from . import ablage, config, dateien, extract, index, ingest, pagenums, sync
from .jobs import JobRunner
from .store import (STATUS_AUTO, STATUS_BESTAETIGT, STATUS_KEINE, STATUS_PDF, STATUS_UNBEKANNT, Item, Library,
                    Page)

STATUS_TEXT = {
    STATUS_BESTAETIGT: "geprüft",
    STATUS_PDF: "aus PDF",
    STATUS_AUTO: "geschätzt",
    STATUS_UNBEKANNT: "ungeklärt",
    STATUS_KEINE: "Abschnitte",
}
EINTRAGSTYPEN = ["book", "article", "incollection", "inbook", "collection", "online", "thesis", "report",
                 "inproceedings", "misc"]
PFLICHT = ("author", "title", "year")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_\-]*$")
PAGES_PER_VIEW = 20
ABGLEICH_INTERVALL = 300   # Sekunden zwischen automatischen Abgleichen
ABGLEICH_VERZOEGERUNG = 10  # nach einer Änderung kurz warten, damit mehrere Änderungen zusammen hochgehen
UPLOAD_ACCEPT = ",".join(sorted(extract.SUPPORTED))


def version() -> str:
    try:
        from importlib.metadata import version as v
        return v("bibliothek")
    except Exception:
        return "0.1.0"


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,;\n]", value or "") if v.strip()]


def _missing(item: Item) -> list[str]:
    f = item.felder
    return [k for k in PFLICHT if not f.get(k) and not (k == "author" and f.get("editor"))
            and not (k == "year" and f.get("date"))]


def _filter_keys(lib: Library, tag: str = "", kategorie: str = "", projekt: str = "", typ: str = "") -> set[str] | None:
    if not (tag or kategorie or projekt or typ):
        return None
    keys = set()
    for it in lib.items():
        if tag and tag not in it.tags:
            continue
        if kategorie and not any(k == kategorie or k.startswith(kategorie + "/") for k in it.kategorien):
            continue
        if projekt and projekt not in it.projekte:
            continue
        if typ and it.eintrag != typ:
            continue
        keys.add(it.citekey)
    return keys


def _first_last(p: Page) -> tuple[str, str]:
    lines = [x.strip() for x in p.text.splitlines() if x.strip()]
    return (lines[0], lines[-1]) if lines else ("", "")


def _samples(pages: list[Page], n: int) -> list[Page]:
    """n gleichmäßig verteilte Seiten mit nennenswertem Text."""
    usable = [p for p in pages if len(p.text.strip()) > 80]
    if len(usable) <= n:
        return usable
    step = (len(usable) - 1) / (n - 1)
    return [usable[round(i * step)] for i in range(n)]


def _segment_rows(item: Item, pages: list[Page]) -> list[dict]:
    """Abschnitte der Seitenzählung mit Beispielseiten zum Gegenprüfen (Anfang, Mitte, Ende)."""
    segs = item.segments()
    status = (item.seitenzaehlung or {}).get("status")
    last = pages[-1].scan if pages else 0
    rows = []
    for i, seg in enumerate(segs):
        end = segs[i + 1]["ab_scan"] - 1 if i + 1 < len(segs) else max(last, seg["ab_scan"])
        inside = [p for p in pages if seg["ab_scan"] <= p.scan <= end]
        rows.append({
            "ab_scan": seg["ab_scan"], "bis_scan": end, "versatz": seg["versatz"],
            "geprueft": bool(seg.get("geprueft")) or status == STATUS_BESTAETIGT,
            "beispiele": [{"scan": p.scan, "ref": item.page_ref(p), "zeilen": _first_last(p)}
                          for p in _samples(inside, 3)],
        })
    return rows


def bib_executable() -> Path | None:
    """Das `bib`-Programm neben dem laufenden Interpreter bzw. im App-Paket."""
    name = "bib.exe" if sys.platform == "win32" else "bib"
    here = Path(sys.executable).resolve().parent
    for cand in (here / name, here.parent / "MacOS" / name, here / "Scripts" / name):
        if cand.exists():
            return cand
    found = shutil.which("bib")
    return Path(found) if found else None


def _mb(n: int | float) -> str:
    return f"{n / 1e6:,.1f} MB".replace(",", "\u202f") if n < 1e9 else f"{n / 1e9:,.2f} GB"


def _uhrzeit(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def create_app(lib: Library, *, runner: JobRunner | None = None, auto_sync: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=4 * 1024 ** 3,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SHUTDOWN=None,   # wird von app.py gesetzt
    )
    jobs = runner or JobRunner()
    app.extensions["bibliothek_jobs"] = jobs
    app.extensions["bibliothek"] = lib

    # --- Schutz und Kopfzeilen -------------------------------------------------------
    @app.before_request
    def protect_local_app():
        try:
            hostname = urlsplit(f"http://{request.host}").hostname
        except ValueError:
            abort(400, "Ungültiger Host.")
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            abort(403, "Die Bibliothek ist nur lokal erreichbar.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != f"{request.scheme}://{request.host}":
                abort(403, "Die Anfrage muss aus der Bibliothek selbst kommen.")
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(403, "Sitzung abgelaufen — Seite neu laden und noch einmal versuchen.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if "Content-Security-Policy" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; "
                "object-src 'self'"
            )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(fehler=error.description), error.code
        return render_template("fehler.html", code=error.code, meldung=error.description), error.code

    @app.context_processor
    def globals_():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf": session["csrf_token"], "STATUS_TEXT": STATUS_TEXT, "aktive_jobs": jobs.active(),
                "version": version(), "nas_anzeige": nas_anzeige(), "upload_accept": UPLOAD_ACCEPT}

    @app.template_filter("markiert")
    def markiert(snippet: str) -> Markup:
        text = str(escape(snippet or ""))
        return Markup(text.replace("«", "<mark>").replace("»", "</mark>"))

    def get_item(key: str) -> Item:
        try:
            return lib.get(key)
        except KeyError:
            abort(404, f"Kein Titel {key}")

    def after_change(keys: list[str] | None = None, *, embed: bool = True, reindex: bool = True) -> None:
        if reindex:
            index.update(lib, keys=keys)
        lib.write_refs_bib()
        if embed:
            submit_embeddings()
        schedule_sync()

    def submit_embeddings():
        def run(job, progress):
            n = index.embed_missing(lib, progress=progress)
            return {"neu": n}
        return jobs.submit("Embeddings berechnen", run, art="embeddings", einmalig=True)

    app.extensions["bibliothek_embed"] = submit_embeddings

    # --- Abgleich mit der Nextcloud ------------------------------------------------------------
    # Nur hier wird die Erreichbarkeit bestimmt; Seiten lesen den gemerkten Zustand (kein Netz beim Rendern).
    abgleich = {"laeuft": False, "online": None, "zuletzt": None, "ausstehend": None,
                "meldung": "", "konflikte": [], "fehler": [], "job": None}
    login = {"status": "", "meldung": "", "url": "", "abbrechen": False}
    timer_lock = threading.Lock()
    timer: dict = {"t": None}
    app.extensions["bibliothek_abgleich"] = abgleich

    def ausstehend_zaehlen() -> None:
        try:
            a = dateien.ausstehend(lib)
            abgleich["ausstehend"] = {"originale": a["originale"], "bytes": a["bytes"], "ocr": len(a["ocr"])}
        except Exception:
            pass

    def submit_sync(grund: str = ""):
        if not dateien.nas_aktiv():
            return None

        def run(job, progress):
            abgleich["laeuft"] = True
            abgleich["job"] = job.id
            if grund:
                progress(f"Anlass: {grund}")
            try:
                bericht = sync.abgleichen(lib, fortschritt=progress)
            except sync.Gesperrt:
                progress("Ein anderer Abgleich (Kommandozeile) läuft gerade — übersprungen.")
                return {"uebersprungen": True}
            except ablage.NichtErreichbar as exc:
                abgleich["online"] = False
                abgleich["meldung"] = str(exc)
                progress(str(exc))
                return {"status": "offline"}
            except ablage.AblageFehler as exc:
                abgleich["meldung"] = str(exc)
                raise
            finally:
                abgleich["laeuft"] = False
                ausstehend_zaehlen()
            status = bericht.get("status")
            abgleich["online"] = status != "offline"
            if status == "ok":
                abgleich["zuletzt"] = time.time()
                abgleich["meldung"] = ""
            elif status == "offline":
                abgleich["meldung"] = "Nextcloud nicht erreichbar — Änderungen bleiben vorgemerkt."
            abgleich["konflikte"] = list(bericht.get("konflikte") or [])
            abgleich["fehler"] = list(bericht.get("fehler") or [])
            for k in abgleich["konflikte"]:
                progress(f"Konflikt: andere Fassung gesichert als {k}")
            for f in abgleich["fehler"]:
                progress(f"Fehler: {f}")
            for e in bericht.get("eingang") or []:
                progress(f"Eingang: {e}")
            progress(f"Fertig: {bericht.get('hoch', 0)} hoch, {bericht.get('runter', 0)} runter")
            if bericht.get("geholt") or bericht.get("eingang"):
                submit_embeddings()
            return {k: v for k, v in bericht.items() if k != "ausstehend"}

        return jobs.submit("Abgleich mit der Nextcloud", run, art="abgleich", einmalig=True)

    def schedule_sync(delay: float = ABGLEICH_VERZOEGERUNG) -> None:
        """Abgleich kurz nach einer Änderung; weitere Änderungen verschieben ihn (debounce)."""
        if not app.config.get("AUTO_SYNC") or not dateien.nas_aktiv():
            return
        with timer_lock:
            if timer["t"] is not None:
                timer["t"].cancel()
            t = threading.Timer(delay, lambda: submit_sync("Änderung"))
            t.daemon = True
            timer["t"] = t
            t.start()

    def periodisch() -> None:
        time.sleep(3)
        while True:
            try:
                submit_sync("regelmäßig")
            except Exception:
                pass
            time.sleep(ABGLEICH_INTERVALL)

    app.config["AUTO_SYNC"] = auto_sync
    app.extensions["bibliothek_sync"] = submit_sync
    if auto_sync:
        threading.Thread(target=periodisch, name="bibliothek-abgleich", daemon=True).start()

    def nas_anzeige() -> dict:
        """Statusanzeige für die Kopfzeile — ohne Netzzugriff."""
        if not dateien.nas_aktiv():
            return {"klasse": "aus", "text": "kein NAS"}
        if abgleich["laeuft"]:
            return {"klasse": "laeuft", "text": "gleiche ab …"}
        if abgleich["ausstehend"] is None:
            ausstehend_zaehlen()
        a = abgleich["ausstehend"] or {}
        offen = (a.get("originale") or 0) + (a.get("ocr") or 0)
        if abgleich["online"] is False:
            return {"klasse": "offline", "text": "offline" + (f" · {offen} ausstehend" if offen else "")}
        zuletzt = abgleich["zuletzt"] or (sync.status(lib).get("zuletzt") or None)
        if zuletzt:
            text = f"abgeglichen {_uhrzeit(zuletzt)}" + (f" · {offen} ausstehend" if offen else "")
            return {"klasse": "warn" if offen or abgleich["konflikte"] else "ok", "text": text}
        return {"klasse": "aus", "text": "noch nicht abgeglichen"}

    # --- Übersicht -------------------------------------------------------------------------
    @app.get("/")
    def liste():
        items = list(lib.items())
        facets = {"tags": {}, "kategorien": {}, "projekte": {}, "typen": {}}
        for it in items:
            for t in it.tags:
                facets["tags"][t] = facets["tags"].get(t, 0) + 1
            for k in it.kategorien:
                facets["kategorien"][k] = facets["kategorien"].get(k, 0) + 1
            for p in it.projekte:
                facets["projekte"][p] = facets["projekte"].get(p, 0) + 1
            facets["typen"][it.eintrag] = facets["typen"].get(it.eintrag, 0) + 1
        f = {k: request.args.get(k, "") for k in ("tag", "kategorie", "projekt", "typ", "q", "unvollstaendig")}
        keys = _filter_keys(lib, f["tag"], f["kategorie"], f["projekt"], f["typ"])
        shown = [it for it in items if keys is None or it.citekey in keys]
        if f["q"]:
            needle = f["q"].lower()
            shown = [it for it in shown if needle in it.title.lower() or needle in it.citekey
                     or needle in it.authors_short.lower()]
        if f["unvollstaendig"]:
            shown = [it for it in shown if it.unvollstaendig]
        shown.sort(key=lambda it: (it.authors_short.lower(), it.year))
        facets = {k: sorted(v.items()) for k, v in facets.items()}
        return render_template("liste.html", items=shown, gesamt=len(items), facets=facets, f=f)

    # --- Titel ----------------------------------------------------------------------------------
    @app.get("/titel/<key>")
    def titel(key):
        item = get_item(key)
        pages = lib.pages(key)
        review = bool(dateien.ocr_ort(item))
        orte = {d: ("lokal" if (lib.folder(key) / d).is_file() else "nas") for d in item.dateien}
        return render_template("titel.html", item=item, seitenzahl=len(pages), typen=EINTRAGSTYPEN, review=review,
                               orte=orte, nas=dateien.nas_aktiv(),
                               abschnitte=_segment_rows(item, pages),
                               beispiele=[] if item.segments() else _samples(pages, 6))

    @app.post("/titel/<key>/metadaten")
    def metadaten(key):
        item = get_item(key)
        names = request.form.getlist("feld_name")
        values = request.form.getlist("feld_wert")
        felder: dict[str, str] = {}
        for name, value in zip(names, values):
            name = name.strip().lower()
            value = value.strip()
            if not name or not value:
                continue
            if not FIELD_NAME.match(name):
                flash(f"Feldname „{name}“ ist ungültig (nur Kleinbuchstaben, Ziffern, - und _).", "fehler")
                return redirect(url_for("titel", key=key))
            felder[name] = value.replace("\r\n", "\n")
        item.felder = felder
        typ = request.form.get("eintrag", item.eintrag).strip()
        if typ:
            item.eintrag = typ
        item.tags = _split_list(request.form.get("tags", ""))
        item.kategorien = _split_list(request.form.get("kategorien", ""))
        item.projekte = _split_list(request.form.get("projekte", ""))
        item.notiz = request.form.get("notiz", "").replace("\r\n", "\n").strip()
        item.unvollstaendig = _missing(item)
        lib.save(item)
        after_change(embed=False, reindex=False)
        flash("Gespeichert.", "ok")
        return redirect(url_for("titel", key=key))

    @app.post("/titel/<key>/seitenzaehlung")
    def seitenzaehlung(key):
        """Wie `bib seiten`: Scan N zeigt Druckseite S → Versatz S−N für den Abschnitt, in dem N liegt."""
        item = get_item(key)
        sz = dict(item.seitenzaehlung or {})
        if sz.get("status") == STATUS_KEINE:
            abort(400, "Dieses Format hat keine Seiten.")
        segs = [dict(x) for x in item.segments()]
        aktion = request.form.get("aktion", "setzen")
        back = redirect(url_for("titel", key=key) + "#seiten")

        def zahl(name: str, *, pflicht: bool = True) -> int | None:
            raw = (request.form.get(name) or "").strip()
            if not raw and not pflicht:
                return None
            return int(raw)   # ValueError → unten abgefangen

        try:
            if aktion == "setzen":
                scan, druckseite = zahl("scan"), zahl("druckseite")
                ab = zahl("ab_scan", pflicht=False)
                if scan < 1 or (ab is not None and ab < 1):
                    raise ValueError
                off = druckseite - scan
                if ab:
                    start = ab
                elif segs:
                    start = max([x["ab_scan"] for x in segs if x["ab_scan"] <= scan] or [scan])
                else:
                    start = max(1, 1 - off)
                segs = pagenums.set_segment(segs, start, off)
                next(x for x in segs if x["ab_scan"] == start)["geprueft"] = True
                meldung = f"Ab Scan {start}: Druckseite = Scan {off:+d} (geprüft)."
            elif aktion == "abschnitt_bestaetigen":
                ab = zahl("ab_scan")
                seg = next((x for x in segs if x["ab_scan"] == ab), None)
                if seg is None:
                    abort(400, "Diesen Abschnitt gibt es nicht.")
                seg["geprueft"] = True
                meldung = f"Abschnitt ab Scan {ab} bestätigt."
            elif aktion == "alles":
                if not segs and sz.get("status") != STATUS_PDF:
                    flash("Nichts zu bestätigen — erst eine Druckseite für einen Scan angeben.", "fehler")
                    return back
                for x in segs:
                    x["geprueft"] = True
                meldung = "Seitenzählung vollständig bestätigt."
            elif aktion == "abschnitt_loeschen":
                ab = zahl("ab_scan")
                if not any(x["ab_scan"] == ab for x in segs):
                    abort(400, "Diesen Abschnitt gibt es nicht.")
                segs = [x for x in segs if x["ab_scan"] != ab]
                meldung = f"Abschnitt ab Scan {ab} gelöscht."
            else:
                abort(400, "Unbekannte Aktion.")
        except ValueError:
            flash("Scan und Druckseite müssen ganze Zahlen sein (Scan ab 1).", "fehler")
            return back

        sz.pop("versatz", None)
        sz.pop("ab_scan", None)
        if segs:
            sz["segmente"] = segs
        else:
            sz.pop("segmente", None)
        if aktion == "alles":
            sz["status"] = STATUS_BESTAETIGT
        elif segs:
            sz["status"] = STATUS_BESTAETIGT if all(x.get("geprueft") for x in segs) else STATUS_AUTO
        else:
            sz["status"] = STATUS_PDF if any(p.label for p in lib.pages(key)) else STATUS_UNBEKANNT
        if sz["status"] == STATUS_BESTAETIGT:
            sz.pop("schaetzung", None)
        item.seitenzaehlung = sz
        lib.save(item)   # schreibt text.md mit den neuen Seitenmarken
        index.update(lib, keys=[item.citekey])
        schedule_sync()
        flash(meldung, "ok")
        return back

    @app.post("/titel/<key>/umbenennen")
    def umbenennen(key):
        item = get_item(key)
        neu = request.form.get("citekey", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-:.]+", neu):
            flash("Der Schlüssel darf nur Buchstaben, Ziffern und - _ : . enthalten.", "fehler")
            return redirect(url_for("titel", key=key))
        if neu == key:
            return redirect(url_for("titel", key=key))
        try:
            lib.rename(item, neu)
        except ValueError as exc:
            flash(str(exc), "fehler")
            return redirect(url_for("titel", key=key))
        after_change()
        flash(f"Umbenannt in {neu}. Verweise in Arbeiten (\\cite{{{key}}}) bitte anpassen.", "ok")
        return redirect(url_for("titel", key=neu))

    @app.post("/titel/<key>/loeschen")
    def loeschen(key):
        item = get_item(key)
        if request.form.get("bestaetigung", "").strip() != key:
            flash("Zum Löschen den Schlüssel genau eintippen.", "fehler")
            return redirect(url_for("titel", key=key))
        lib.delete(item.citekey)
        after_change(embed=False)
        flash(f"{key} gelöscht.", "ok")
        return redirect(url_for("liste"))

    @app.post("/titel/<key>/neu-erkennen")
    def neu_erkennen(key):
        item = get_item(key)

        def run(job, progress):
            ingest.reocr(lib, lib.get(item.citekey), progress=progress)
            after_change([item.citekey])
            return {"citekey": item.citekey}
        job = jobs.submit(f"OCR neu: {key}", run, art="ocr")
        return redirect(url_for("job_seite", job_id=job.id))

    @app.get("/titel/<key>/text")
    def text(key):
        item = get_item(key)
        pages = lib.pages(key)
        scan = request.args.get("scan", type=int)
        start = request.args.get("ab", type=int)
        if start is None:
            start = max(1, (scan or 1) - 2)
        start = max(1, min(start, max(1, len(pages) - PAGES_PER_VIEW + 1)))
        shown = [p for p in pages if start <= p.scan < start + PAGES_PER_VIEW]
        return render_template("text.html", item=item, pages=shown, gesamt=len(pages), start=start,
                               schritt=PAGES_PER_VIEW, ziel=scan)

    def nas_fehlerseite(exc: Exception):
        if isinstance(exc, ablage.NichtErreichbar):
            meldung = ("Nextcloud nicht erreichbar — die Datei liegt auf dem NAS. Im Heimnetz erneut versuchen. "
                       "Suche und Text funktionieren auch ohne Verbindung.")
            return render_template("fehler.html", code=503, meldung=meldung), 503
        return render_template("fehler.html", code=502, meldung=f"Nextcloud: {exc}"), 502

    @app.get("/titel/<key>/datei/<path:name>")
    def datei(key, name):
        item = get_item(key)
        if name not in item.dateien:
            abort(404, "Datei gehört nicht zu diesem Titel.")
        try:
            path = dateien.original(lib, item, name)
        except FileNotFoundError:
            abort(404, "Datei nicht gefunden — weder lokal noch auf der Nextcloud.")
        except ablage.AblageFehler as exc:
            return nas_fehlerseite(exc)
        resp = send_file(path, as_attachment=bool(request.args.get("laden")), download_name=name)
        if name.lower().endswith((".html", ".htm", ".xhtml", ".svg")):
            # Gespeicherte Webseiten dürfen im Kontext der Bibliothek keine Skripte ausführen
            resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        else:
            # PDF-Betrachter des Browsers nicht durch eine strenge Richtlinie aussperren
            resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        return resp

    @app.get("/titel/<key>/ocr/<path:name>")
    def ocr_datei(key, name):
        item = get_item(key)
        if not dateien.ocr_ort(item) or not (name == "review.html" or name.startswith(("previews/", "pages/"))):
            abort(404)
        try:
            path = dateien.ocr_datei(lib, item, name)
        except FileNotFoundError:
            abort(404, "Diese Datei gibt es im OCR-Lauf nicht.")
        except ablage.AblageFehler as exc:
            return nas_fehlerseite(exc)
        resp = send_file(path)
        # review.html bringt eigenes <style> mit, aber keine Skripte
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'self'")
        return resp

    # --- Suche --------------------------------------------------------------------------------
    def run_search(args) -> tuple[list[dict], str]:
        q = (args.get("q") or "").strip()
        if not q:
            return [], ""
        modus = args.get("modus", "hybrid")
        if modus not in ("hybrid", "text", "semantisch"):
            abort(400, "modus muss hybrid, text oder semantisch sein.")
        limit = max(1, min(int(args.get("limit", 20) or 20), 200))
        keys = _filter_keys(lib, args.get("tag", ""), args.get("kategorie", ""), args.get("projekt", ""),
                            args.get("typ", ""))
        hinweis = ""
        if keys is not None and not keys:
            return [], ""   # Filter passt auf keinen Titel (index.search läse leere Menge als „alle“)
        try:
            hits = index.search(lib, q, mode=modus, limit=limit, keys=keys)
        except ValueError as exc:
            abort(400, str(exc))
        except Exception as exc:  # Modell fehlt / lädt nicht: auf Volltext zurückfallen
            if modus == "text":
                raise
            hits = index.search(lib, q, mode="text", limit=limit, keys=keys)
            hinweis = f"Semantische Suche nicht verfügbar ({exc.__class__.__name__}: {exc}); nur Volltext."
        cache: dict[str, Item] = {}
        out = []
        for h in hits:
            if h.citekey not in cache:
                try:
                    cache[h.citekey] = lib.get(h.citekey)
                except KeyError:
                    continue
            it = cache[h.citekey]
            out.append({
                "citekey": h.citekey, "kurz": it.short, "titel": it.title,
                "seite": it.page_ref(Page(h.scan, h.text)), "scan": h.scan,
                "snippet": h.snippet, "text": h.text, "wege": sorted(h.wege), "score": round(h.score, 5),
            })
        return out, hinweis

    @app.get("/suche")
    def suche():
        treffer, hinweis = run_search(request.args)
        items = list(lib.items())
        facets = {
            "tags": sorted({t for it in items for t in it.tags}),
            "kategorien": sorted({k for it in items for k in it.kategorien}),
            "projekte": sorted({p for it in items for p in it.projekte}),
        }
        return render_template("suche.html", treffer=treffer, hinweis=hinweis, args=request.args, facets=facets)

    # --- Aufnehmen -------------------------------------------------------------------------------
    def common_kw(form) -> dict:
        kw: dict = {"tags": _split_list(form.get("tags", "")), "kategorien": _split_list(form.get("kategorien", "")),
              "projekte": _split_list(form.get("projekte", ""))}
        felder: dict[str, str] = {}
        for name in ("author", "title", "year"):
            if form.get(name, "").strip():
                felder[name] = form[name].strip()
        if felder:
            kw["felder"] = felder
        if form.get("eintrag"):
            kw["eintrag"] = form["eintrag"]
        if form.get("doppelt"):
            kw["allow_duplicate"] = True
        return kw

    @app.get("/aufnehmen")
    def aufnehmen():
        return render_template("aufnehmen.html", typen=EINTRAGSTYPEN)

    def start_upload(files, form):
        """Hochgeladene Dateien zwischenspeichern und als Aufnahme-Job einreihen."""
        kw = common_kw(form)
        if len(files) > 1:
            kw.pop("felder", None)   # Autor/Titel gelten nur für eine einzelne Datei
        tmp = Path(tempfile.mkdtemp(prefix="bibliothek-upload-"))
        saved = []
        for f in files:
            name = secure_filename(f.filename) or "datei"
            # secure_filename entfernt Umlaute; den Originalnamen für die Herkunft merken
            target = tmp / f"{len(saved):03d}-{name}"
            f.save(target)
            saved.append((f.filename, target))
        run_ocr = not form.get("ohne_ocr")

        def run(job, progress):
            neu, doppelt, fehler = [], [], []
            try:
                for original, path in saved:
                    progress(f"Nehme auf: {original}")
                    renamed = path.with_name(Path(original).name.replace("/", "_").replace("\\", "_"))
                    try:
                        path.rename(renamed)
                    except OSError:
                        renamed = path
                    try:
                        item = ingest.add_file(lib, renamed, run_ocr=run_ocr, herkunft=original,
                                               reindex=False, progress=progress, **kw)
                        neu.append(item.citekey)
                        progress(f"→ {item.citekey}")
                    except ingest.Duplicate as exc:
                        doppelt.append({"datei": original, "citekey": exc.item.citekey, "titel": exc.item.title})
                        progress(f"Schon vorhanden: {exc.item.citekey}")
                    except Exception as exc:
                        fehler.append({"datei": original, "fehler": str(exc)})
                        progress(f"Fehler bei {original}: {exc}")
                if neu:
                    after_change(neu)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return {"neu": neu, "doppelt": doppelt, "fehler": fehler}

        return jobs.submit(f"Aufnahme: {len(saved)} Datei(en)", run, art="aufnahme")

    @app.post("/aufnehmen/dateien")
    def aufnehmen_dateien():
        files = [f for f in request.files.getlist("dateien") if f and f.filename]
        if not files:
            flash("Keine Datei ausgewählt.", "fehler")
            return redirect(url_for("aufnehmen"))
        job = start_upload(files, request.form)
        return redirect(url_for("job_seite", job_id=job.id))

    @app.post("/aufnehmen/schnell")
    def aufnehmen_schnell():
        """Knopf „Literatur hinzufügen“: Dateien ohne weitere Angaben aufnehmen (JSON-Antwort)."""
        files = [f for f in request.files.getlist("dateien") if f and f.filename]
        if not files:
            abort(400, "Keine Datei ausgewählt.")
        job = start_upload(files, {})
        return jsonify(job=job.id, url=url_for("job_seite", job_id=job.id),
                       api=url_for("api_job", job_id=job.id), anzahl=len(files))

    @app.post("/aufnehmen/scans")
    def aufnehmen_scans():
        quelle = request.form.get("quelle", "").strip()
        if not quelle:
            flash("Bitte einen Ordner oder Immich-Link angeben.", "fehler")
            return redirect(url_for("aufnehmen"))
        if "://" not in quelle and not Path(quelle).expanduser().is_dir():
            flash(f"Ordner nicht gefunden: {quelle}", "fehler")
            return redirect(url_for("aufnehmen"))
        kw = common_kw(request.form)
        einzel = bool(request.form.get("einzelseiten"))
        order = request.form.get("reihenfolge", "filename")
        if order not in ("filename", "date"):
            order = "filename"

        def run(job, progress):
            try:
                item = ingest.add_scans(lib, quelle, single_page=einzel, order=order, progress=progress, **kw)
            except ingest.Duplicate as exc:
                return {"doppelt": [{"datei": quelle, "citekey": exc.item.citekey, "titel": exc.item.title}]}
            after_change([item.citekey])
            return {"neu": [item.citekey]}

        job = jobs.submit(f"OCR: {quelle[-60:]}", run, art="ocr")
        return redirect(url_for("job_seite", job_id=job.id))

    @app.post("/aufnehmen/ocr-ordner")
    def aufnehmen_ocr_ordner():
        ordner = Path(request.form.get("ordner", "").strip()).expanduser()
        if not (ordner / "report.json").exists():
            flash(f"Kein OCR-Ergebnis (report.json) in {ordner}", "fehler")
            return redirect(url_for("aufnehmen"))
        kw = common_kw(request.form)

        def run(job, progress):
            try:
                item = ingest.add_ocr_output(lib, ordner, reindex=False, **kw)
            except ingest.Duplicate as exc:
                return {"doppelt": [{"datei": str(ordner), "citekey": exc.item.citekey, "titel": exc.item.title}]}
            progress(f"→ {item.citekey}")
            after_change([item.citekey])
            return {"neu": [item.citekey]}

        job = jobs.submit(f"OCR-Ordner: {ordner.name}", run, art="aufnahme")
        return redirect(url_for("job_seite", job_id=job.id))

    # --- Jobs -------------------------------------------------------------------------------------
    @app.get("/jobs")
    def jobs_seite():
        return render_template("jobs.html", jobs=jobs.all())

    @app.get("/jobs/<job_id>")
    def job_seite(job_id):
        job = jobs.get(job_id)
        if not job:
            abort(404, "Job nicht gefunden (nach einem Neustart sind alte Jobs weg).")
        return render_template("job.html", job=job)

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id):
        job = jobs.get(job_id)
        if not job:
            abort(404, "Job nicht gefunden.")
        return jsonify(job.to_dict())

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(jobs=[j.to_dict() for j in jobs.all()])

    # --- Einstellungen ---------------------------------------------------------------------------
    @app.get("/einstellungen")
    def einstellungen():
        try:
            st = index.stats(lib)
        except Exception:
            st = {}
        local_bin = Path.home() / ".local" / "bin"
        on_path = str(local_bin) in os.environ.get("PATH", "").split(os.pathsep)
        nas = ablage.einstellungen() if dateien.nas_aktiv() else None
        info = {}
        if nas:
            if abgleich["ausstehend"] is None:
                ausstehend_zaehlen()
            a = abgleich["ausstehend"] or {}
            zuletzt = abgleich["zuletzt"] or sync.status(lib).get("zuletzt")
            info = {"zuletzt": time.strftime("%d.%m.%Y %H:%M", time.localtime(zuletzt)) if zuletzt else "",
                    "originale": a.get("originale", 0), "mb": _mb(a.get("bytes", 0)), "ocr": a.get("ocr", 0),
                    "online": abgleich["online"], "meldung": abgleich["meldung"], "konflikte": abgleich["konflikte"],
                    "fehler": abgleich["fehler"]}
        return render_template("einstellungen.html", settings=config.settings(), root=lib.root,
                               sprache=config.ocr_language(), stats=st, bib=bib_executable(),
                               windows=sys.platform == "win32", on_path=on_path,
                               settings_file=config.SETTINGS_FILE, nas=nas, nas_info=info, login=login,
                               zwischenspeicher=_mb(dateien.zwischenspeicher_groesse()))

    @app.post("/einstellungen")
    def einstellungen_speichern():
        root = request.form.get("bibliothek", "").strip()
        sprache = request.form.get("ocr_sprache", "").strip()
        if sprache and not re.fullmatch(r"[A-Za-z_]+(\+[A-Za-z_]+)*", sprache):
            flash("OCR-Sprache wie eng+deu angeben.", "fehler")
            return redirect(url_for("einstellungen"))
        values = {}
        if root:
            values["bibliothek"] = str(Path(root).expanduser())
        if sprache:
            values["ocr_sprache"] = sprache
        config.save_settings(values)
        hinweis = " Der neue Bibliotheksordner gilt nach einem Neustart." if root and Path(root).expanduser() != lib.root else ""
        flash("Einstellungen gespeichert." + hinweis, "ok")
        return redirect(url_for("einstellungen"))

    @app.post("/einstellungen/cli")
    def cli_einrichten():
        if sys.platform == "win32":
            flash("Unter Windows trägt der Installer `bib` in den PATH ein.", "ok")
            return redirect(url_for("einstellungen"))
        exe = bib_executable()
        if not exe:
            flash("Das Programm `bib` wurde neben dieser App nicht gefunden.", "fehler")
            return redirect(url_for("einstellungen"))
        link = Path.home() / ".local" / "bin" / "bib"
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if link.resolve() == exe.resolve():
                flash(f"`bib` ist bereits eingerichtet ({link}).", "ok")
                return redirect(url_for("einstellungen"))
            if not link.is_symlink():
                flash(f"{link} existiert schon und ist kein Verweis — nicht überschrieben.", "fehler")
                return redirect(url_for("einstellungen"))
            link.unlink()
        link.symlink_to(exe)
        msg = f"`bib` eingerichtet: {link} → {exe}."
        if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            msg += " ~/.local/bin steht noch nicht im PATH — in ~/.zshrc bzw. ~/.bashrc ergänzen: export PATH=\"$HOME/.local/bin:$PATH\""
        flash(msg, "ok")
        return redirect(url_for("einstellungen"))

    @app.post("/einstellungen/index")
    def index_neu():
        def run(job, progress):
            r = index.update(lib, rebuild=True, progress=progress)
            lib.write_refs_bib()
            return r
        job = jobs.submit("Index neu aufbauen", run, art="index")
        submit_embeddings()
        return redirect(url_for("job_seite", job_id=job.id))

    @app.post("/einstellungen/embeddings")
    def embeddings():
        job = submit_embeddings()
        return redirect(url_for("job_seite", job_id=job.id))

    # --- NAS / Nextcloud ---------------------------------------------------------------------------
    @app.post("/nas/verbinden")
    def nas_verbinden():
        server = request.form.get("server", "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", server):
            flash("Server-Adresse wie http://192.168.0.70:9001 angeben.", "fehler")
            return redirect(url_for("einstellungen") + "#nas")
        if login["status"] == "wartet":
            flash("Die Anmeldung läuft schon — im Browser bestätigen oder abbrechen.", "hinweis")
            return redirect(url_for("einstellungen") + "#nas")
        try:
            flow = ablage.login_starten(server)
        except ablage.AblageFehler as exc:
            flash(str(exc), "fehler")
            return redirect(url_for("einstellungen") + "#nas")
        # Kein automatisches Öffnen: Der normale Browser ist meist mit dem eigenen (Admin-)Konto
        # angemeldet. Der Link wird angezeigt und soll in einem privaten Fenster geöffnet werden.
        login.update(status="wartet", meldung="", url=flow.get("login", ""), abbrechen=False)

        def warten():
            try:
                nas = ablage.login_abwarten(flow, abbruch=lambda: login["abbrechen"])
                login.update(status="verbunden", meldung=f"Verbunden als {nas['benutzer']} @ {nas['server']}")
                abgleich.update(online=True, meldung="", ausstehend=None)
                submit_sync("neu verbunden")
            except Exception as exc:
                login.update(status="fehler", meldung=str(exc))
        # Nicht im Job-Thread: das Warten auf den Browser darf Aufnahmen nicht blockieren
        threading.Thread(target=warten, name="bibliothek-login", daemon=True).start()
        return redirect(url_for("einstellungen") + "#nas")

    @app.post("/nas/abbrechen")
    def nas_abbrechen():
        login["abbrechen"] = True
        login.update(status="", meldung="", url="")
        flash("Anmeldung abgebrochen.", "ok")
        return redirect(url_for("einstellungen") + "#nas")

    @app.get("/api/nas/login")
    def api_nas_login():
        return jsonify(status=login["status"], meldung=login["meldung"], url=login["url"])

    @app.post("/nas/abgleichen")
    def nas_abgleichen():
        job = submit_sync("von Hand")
        if not job:
            flash("Keine Nextcloud verbunden.", "fehler")
            return redirect(url_for("einstellungen") + "#nas")
        return redirect(url_for("job_seite", job_id=job.id))

    @app.post("/nas/trennen")
    def nas_trennen():
        ablage.trennen()
        login.update(status="", meldung="", url="")
        abgleich.update(online=None, zuletzt=None, ausstehend=None, meldung="", konflikte=[], fehler=[])
        flash("Nextcloud getrennt. Dateien auf dem NAS bleiben erhalten; Originale, die nur dort liegen, "
              "sind ohne Verbindung nicht zu öffnen.", "ok")
        return redirect(url_for("einstellungen") + "#nas")

    @app.post("/nas/zwischenspeicher-leeren")
    def nas_zwischenspeicher_leeren():
        frei = dateien.aufraeumen(0)
        flash(f"Zwischenspeicher geleert ({_mb(frei)} frei).", "ok")
        return redirect(url_for("einstellungen") + "#nas")

    @app.post("/beenden")
    def beenden():
        stop = app.config.get("SHUTDOWN")
        if stop:
            stop()
        return render_template("fehler.html", code="", meldung="Die Bibliothek wurde beendet. Dieses Fenster kann geschlossen werden.")

    # --- JSON-API für die CLI ------------------------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        nas = ablage.einstellungen() if dateien.nas_aktiv() else None
        if nas:
            nas = {k: nas.get(k) for k in ("server", "benutzer", "ordner")}
        stand = {k: abgleich[k] for k in ("laeuft", "online", "zuletzt", "ausstehend", "meldung", "konflikte", "fehler")}
        stand["anzeige"] = nas_anzeige()
        return jsonify(bibliothek=str(lib.root), version=version(), jobs=jobs.active(), nas=nas, abgleich=stand)

    @app.get("/api/suche")
    def api_suche():
        if not (request.args.get("q") or "").strip():
            abort(400, "Parameter q fehlt.")
        treffer, hinweis = run_search(request.args)
        return jsonify(treffer=treffer, hinweis=hinweis)

    return app
