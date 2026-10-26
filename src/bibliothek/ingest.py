"""Titel aufnehmen: Datei, Scan-Ordner, Immich-Album oder vorhandene OCR-Ausgabe.

Grundsatz: Metadaten werden nie geraten. Was nicht aus der Datei oder vom
Aufrufer kommt, landet in `unvollstaendig` und wird so angezeigt.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import bibtex, dateien, extract, index, ocr, pagenums
from .store import (STATUS_AUTO, STATUS_KEINE, STATUS_PDF, STATUS_UNBEKANNT, Item, Library, Page)

Progress = Callable[[str], None]
PFLICHT = ("author", "title", "year")


class Duplicate(Exception):
    def __init__(self, item: Item):
        super().__init__(f"Schon vorhanden als {item.citekey} ({item.short}: {item.title})")
        self.item = item


def _prepare(lib: Library, felder: dict[str, str], citekey: str | None, *, allow_duplicate: bool) -> tuple[str, list[str]]:
    if not allow_duplicate:
        dup = lib.find_duplicate(felder)
        if dup:
            raise Duplicate(dup)
    missing = [f for f in PFLICHT if not felder.get(f) and not (f == "author" and felder.get("editor"))
               and not (f == "year" and felder.get("date"))]
    key = citekey or lib.new_citekey(felder)
    if lib.exists(key):
        raise ValueError(f"Schlüssel {key} ist schon vergeben")
    return key, missing


def _numbering(item: Item, pages: list[Page], paginiert: bool, has_labels: bool) -> None:
    if not paginiert:
        item.seitenzaehlung = {"status": STATUS_KEINE}
        return
    if has_labels:
        item.seitenzaehlung = {"status": STATUS_PDF}
        return
    guess = pagenums.detect(pages)
    if guess:
        item.seitenzaehlung = {"status": STATUS_AUTO, "segmente": guess["segmente"],
                               "schaetzung": f"{guess['treffer']} Seiten tragen die erwartete Zahl "
                                             f"({int(guess['quote'] * 100)} %)"}
        if guess["spruenge"]:
            item.seitenzaehlung["spruenge"] = guess["spruenge"]
    else:
        item.seitenzaehlung = {"status": STATUS_UNBEKANNT}


def _finish(lib: Library, item: Item, pages: list[Page], *, reindex: bool) -> Item:
    lib.write_pages(item, pages)   # speichert auch meta.yaml und text.md
    if reindex:
        index.update(lib, keys=[item.citekey])
    return item


def add_file(lib: Library, path: Path, *, felder: dict[str, str] | None = None, eintrag: str | None = None,
             citekey: str | None = None, tags: list[str] | None = None, kategorien: list[str] | None = None,
             projekte: list[str] | None = None, run_ocr: bool = True, allow_duplicate: bool = False,
             reindex: bool = True, herkunft: str = "", progress: Progress | None = None) -> Item:
    progress = progress or (lambda _: None)
    path = Path(path).expanduser().resolve()
    summe = pruefsumme(path)
    if not allow_duplicate:
        gleich = next((it for it in lib.items() if summe in (it.text.get("sha256") or [])), None)
        if gleich:
            raise Duplicate(gleich)
    progress(f"Lese {path.name}")
    ex = extract.extract(path)
    felder = dict(felder or {})
    hinweise = list(ex.hinweise)
    if not felder.get("title"):
        felder["title"], hinweis = guess_title(ex.meta.get("title"), path)
        hinweise.append(hinweis)
    if not felder.get("author") and ex.meta.get("author"):
        # Dateieigenschaften nennen meist den Benutzer am Scanner oder in Word, nicht den Autor
        hinweise.append(f"Dateieigenschaften nennen „{ex.meta['author'].strip()}“ als Autor — nicht übernommen.")
    key, missing = _prepare(lib, felder, citekey, allow_duplicate=allow_duplicate)

    item = Item(citekey=key, eintrag=eintrag or _guess_type(ex), felder=felder, tags=tags or [],
                kategorien=kategorien or [], projekte=projekte or [], unvollstaendig=missing,
                herkunft=herkunft or str(path))
    folder = lib.folder(key)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _safe_name(path.name)
    shutil.copy2(path, target)
    item.dateien = [target.name]
    item.text = {"quelle": ex.quelle, "sha256": [summe]}

    pages = ex.pages
    if ex.ocr_noetig and run_ocr:
        try:
            if ex.quelle == "bild":
                pages = _ocr_single_image(target, dateien.ocr_workdir(lib, key), progress)
                item.text["quelle"] = "ocr"
                item.text["ocr_ordner"] = str(dateien.ocr_workdir(lib, key))
            else:
                progress(f"OCR für {len(ex.ocr_noetig)} Seite(n) ohne Textebene")
                recognized = ocr.ocr_pdf_pages(target, ex.ocr_noetig, dateien.ocr_workdir(lib, key), progress=progress)
                for p in pages:
                    if p.scan in recognized:
                        p.text, p.konfidenz = recognized[p.scan].text, recognized[p.scan].konfidenz
                item.text["quelle"] = "ocr" if len(ex.ocr_noetig) == len(pages) else "pdf+ocr"
                item.text["ocr_ordner"] = str(dateien.ocr_workdir(lib, key))
            conf = [p.konfidenz for p in pages if p.konfidenz is not None]
            if conf:
                item.text["konfidenz"] = round(sum(conf) / len(conf), 1)
            hinweise.append("Text per OCR erkannt — Zitate am Original prüfen.")
        except Exception as exc:  # OCR-Probleme sollen die Aufnahme nicht verhindern
            hinweise.append(f"OCR fehlgeschlagen: {exc}")
    elif ex.ocr_noetig:
        hinweise.append(f"{len(ex.ocr_noetig)} Seite(n) ohne Text, OCR übersprungen.")
    item.text["hinweise"] = hinweise
    has_labels = any(p.label for p in pages)
    _numbering(item, pages, ex.paginiert, has_labels)
    return _finish(lib, item, pages, reindex=reindex)


def add_ocr_output(lib: Library, ocr_dir: Path, *, felder: dict[str, str] | None = None, eintrag: str = "book",
                   citekey: str | None = None, tags: list[str] | None = None, kategorien: list[str] | None = None,
                   projekte: list[str] | None = None, allow_duplicate: bool = False, reindex: bool = True,
                   herkunft: str = "") -> Item:
    """Fertige Ausgabe des Buch-OCR übernehmen. Der OCR-Ordner bleibt, wo er ist (Scans sind groß)."""
    ocr_dir = Path(ocr_dir).expanduser().resolve()
    pages, summary = ocr.pages_from_report(ocr_dir)
    felder = dict(felder or {})
    hinweise = ["Text per OCR erkannt — Zitate am Scan prüfen (review.html im OCR-Ordner)."]
    if not felder.get("title"):
        felder["title"] = summary.get("titel") or ocr_dir.name
        hinweise.append("Titel aus dem OCR-Album — prüfen.")
    if not summary["vollstaendig"]:
        hinweise.append("OCR-Lauf unvollständig: einzelne Scans fehlen.")
    key, missing = _prepare(lib, felder, citekey, allow_duplicate=allow_duplicate)
    item = Item(citekey=key, eintrag=eintrag, felder=felder, tags=tags or [], kategorien=kategorien or [],
                projekte=projekte or [], unvollstaendig=missing, herkunft=herkunft or str(ocr_dir))
    item.text = {"quelle": "ocr", "ocr_ordner": str(ocr_dir), "konfidenz": summary["konfidenz"],
                 "ocr_warnungen": summary["warnungen"], "hinweise": hinweise}
    _numbering(item, pages, True, False)
    return _finish(lib, item, pages, reindex=reindex)


def add_scans(lib: Library, source: str, *, single_page: bool = False, order: str = "filename",
              progress: Progress | None = None, **kw) -> Item:
    """OCR starten (Ordner mit Fotos oder Immich-Link) und das Ergebnis aufnehmen."""
    progress = progress or (lambda _: None)
    work = dateien.ocr_workdir(lib, "lauf-" + bibtex.ascii_slug(Path(source).name if "://" not in source else source)[-24:])
    if "://" in source:
        ocr.ocr_album(source, work, progress=progress)
    else:
        ocr.ocr_folder(Path(source).expanduser(), work, single_page=single_page, order=order, progress=progress)
    item = add_ocr_output(lib, work, herkunft=source, **kw)
    final = dateien.ocr_workdir(lib, item.citekey)
    if not final.exists():
        work.rename(final)
        item.text["ocr_ordner"] = str(final)
        lib.save(item)
    return item


def _ocr_single_image(image: Path, out: Path, progress: Progress) -> list[Page]:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(image, Path(tmp) / image.name)
        ocr.ocr_folder(Path(tmp), out, single_page=True, progress=progress)
    pages, _ = ocr.pages_from_report(out)
    return pages


def reocr(lib: Library, item: Item, *, progress: Progress | None = None) -> Item:
    """Alle Seiten eines PDFs neu erkennen (z. B. schlechte eingebettete Textebene)."""
    pdfs = [f for f in item.dateien if f.lower().endswith(".pdf")]
    if not pdfs:
        raise ValueError("Kein PDF zum Neu-Erkennen vorhanden")
    pages = lib.pages(item.citekey)
    out = dateien.ocr_workdir(lib, item.citekey)
    pdf = dateien.original(lib, item, pdfs[0])   # mit NAS: vorher herunterladen
    recognized = ocr.ocr_pdf_pages(pdf, [p.scan for p in pages], out, progress=progress)
    for p in pages:
        if p.scan in recognized:
            p.text, p.konfidenz = recognized[p.scan].text, recognized[p.scan].konfidenz
    item.text["quelle"] = "ocr"
    item.text["ocr_ordner"] = str(out)
    return _finish(lib, item, pages, reindex=True)


_JUNK_TITLE = re.compile(
    r"^(untitled|unbenannt|dokument\d*|document\d*|scan\w*|img[_\-]?\d+)$"   # Platzhalter
    r"|^[A-Z]{1,4}[_\-]?\d{2,}"                                          # Scanner/Kopierer (KM_364e-…)
    r"|\d{8,}",                                                          # Zeitstempel
    re.I)


def guess_title(meta_title: str | None, path: Path) -> tuple[str, str]:
    """Titel aus den Dateieigenschaften, wenn plausibel, sonst aus dem Dateinamen."""
    t = (meta_title or "").strip()
    t = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", t)
    t = re.sub(r"\.(docx?|pdf|pptx?|odt|rtf)$", "", t, flags=re.I).strip()
    if len(t) > 3 and not _JUNK_TITLE.search(t):
        return t, "Titel aus den Dateieigenschaften — prüfen."
    stem = re.sub(r"[_]+", " ", path.stem)
    stem = re.sub(r"[\-\s]+\d+$", "", stem)                       # Nummer am Ende (-1, -2)
    stem = re.sub(r"\s+(copy|kopie)(\s*\d+)?$", "", stem, flags=re.I)  # Finder-Kopien
    stem = re.sub(r"^\d+([\-–]\d+)?[\s.\-_]+", "", stem)          # Kursnummerierung vorne (03-04 …)
    stem = stem.strip() or path.stem
    return stem, "Titel aus dem Dateinamen — prüfen."


def pruefsumme(path: Path) -> str:
    """SHA-256 des Dateiinhalts — erkennt dieselbe Datei unter anderem Namen."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _guess_type(ex: extract.Extracted) -> str:
    if ex.quelle in ("html",):
        return "online"
    if ex.quelle == "pdf" and len(ex.pages) <= 60:
        return "article"
    return "book" if ex.paginiert else "misc"


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "datei"
