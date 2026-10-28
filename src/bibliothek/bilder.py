"""Vorschaubild einer Scan-Seite — zum Ablesen der Druckseite und Prüfen von Zitaten.

Quelle der Reihe nach: PDF-Original (Seite rendern), OCR-Lauf (Seitenbild aus
report.json), Bilddatei. Ergebnis: verkleinertes JPEG im Zwischenspeicher.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import dateien, extract
from .store import Item, Library

BREITE = 1000


def scan_bild(lib: Library, item: Item, scan: int) -> Path | None:
    if scan < 1:
        return None
    ziel = dateien.zwischenspeicher() / "vorschau" / item.citekey / f"{scan}.jpg"
    if ziel.is_file():
        return ziel
    if item.teile() > 1:
        bild = _geteilt_aus_ocr(lib, item, scan) or _geteilt_aus_pdf(lib, item, scan)
    else:
        bild = _aus_pdf(lib, item, scan) or _aus_ocr(lib, item, scan) or _aus_bilddatei(lib, item, scan)
    if bild is None:
        return None
    from PIL import Image

    with bild:
        bild = bild.convert("RGB")
        if bild.width > BREITE:
            bild = bild.resize((BREITE, round(bild.height * BREITE / bild.width)))
        ziel.parent.mkdir(parents=True, exist_ok=True)
        bild.save(ziel, "JPEG", quality=80)
    return ziel


def _aus_pdf(lib: Library, item: Item, scan: int):
    pdf = next((d for d in item.dateien if d.lower().endswith(".pdf")), None)
    if not pdf:
        return None
    try:
        path = dateien.original(lib, item, pdf)
    except (FileNotFoundError, OSError):
        return None
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        if scan > len(doc):
            return None
        page = doc[scan - 1]
        breite_pt = page.get_width() or 600
        return page.render(scale=min(3.0, BREITE / breite_pt)).to_pil()
    finally:
        doc.close()


def _geteilt_aus_ocr(lib: Library, item: Item, scan: int):
    """Seitenbild aus dem OCR-Lauf (gedreht und am Bund getrennt), Zuordnung über den Dateinamen."""
    from . import teilung

    n = item.teile()
    seite, teil = teilung.herkunft(scan, n)
    lauf, quelle, index = ("spalten/", f"s{seite:05d}-{teil // 2}.png", teil % 2) if n == 4 else ("", f"s{seite:05d}.png", teil)
    try:
        report = json.loads(dateien.ocr_datei(lib, item, lauf + "report.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    eintrag = next((e for e in report.get("entries", []) if e.get("source") == quelle), None)
    if not eintrag or index >= len(eintrag.get("pages", [])):
        return None
    try:
        path = dateien.ocr_datei(lib, item, lauf + eintrag["pages"][index]["image"])
    except (FileNotFoundError, OSError):
        return None
    from PIL import Image

    return Image.open(path)


def _geteilt_aus_pdf(lib: Library, item: Item, scan: int):
    """PDF-Seite mit Textebene: an denselben Grenzen zuschneiden wie der Text."""
    from . import teilung

    pdf = next((d for d in item.dateien if d.lower().endswith(".pdf")), None)
    if not pdf:
        return None
    try:
        path = dateien.original(lib, item, pdf)
    except (FileNotFoundError, OSError):
        return None
    import pypdfium2 as pdfium

    seite, teil = teilung.herkunft(scan, item.teile())
    doc = pdfium.PdfDocument(str(path))
    try:
        if seite > len(doc):
            return None
        page = doc[seite - 1]
        bild = page.render(scale=min(3.0, 2 * BREITE / (page.get_width() or 600))).to_pil()
        return teilung.bild_teile(bild, page, item.teile())[teil]
    finally:
        doc.close()


def _aus_ocr(lib: Library, item: Item, scan: int):
    try:
        report = json.loads(dateien.ocr_datei(lib, item, "report.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    seiten = [p for e in report.get("entries", []) for p in e.get("pages", [])]
    if scan > len(seiten) or not seiten[scan - 1].get("image"):
        return None
    try:
        path = dateien.ocr_datei(lib, item, seiten[scan - 1]["image"])
    except (FileNotFoundError, OSError):
        return None
    from PIL import Image

    return Image.open(path)


def _aus_bilddatei(lib: Library, item: Item, scan: int):
    bilder = [d for d in item.dateien if Path(d).suffix.lower() in extract.IMAGE_SUFFIXES]
    if scan > len(bilder):
        return None
    try:
        path = dateien.original(lib, item, bilder[scan - 1])
    except (FileNotFoundError, OSError):
        return None
    from PIL import Image

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image.open(path)
