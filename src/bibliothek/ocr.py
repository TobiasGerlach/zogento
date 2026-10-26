"""Brücke zum eingebauten Buch-OCR (immich_book_ocr).

Drei Wege hinein:
  * Fotos/Scans eines Buches in einem Ordner (Doppelseiten werden getrennt)
  * ein Immich-Freigabelink
  * Seiten eines PDFs ohne Textebene (werden gerendert und einzeln erkannt)

Ergebnis ist immer der OCR-Ordner des Programms (report.json, review.html, text/ …);
`pages_from_report` macht daraus Seiten für die Bibliothek.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from . import config
from .store import Page

Progress = Callable[[str], None]


def _env() -> None:
    bundled = config.bundled_tesseract()
    if bundled:
        # Windows-Paket: Tesseract und tessdata_best-Modelle (eng, deu) liegen bei
        os.environ.setdefault("BOOK_OCR_TESSERACT", str(bundled))
        os.environ.setdefault("TESSDATA_PREFIX", str(bundled.parent / "tessdata"))
        if (bundled.parent / "tessdata_best").is_dir():
            os.environ.setdefault("BOOK_OCR_MODELS", str(bundled.parent / "tessdata_best"))
    os.environ.setdefault("BOOK_OCR_MODELS", str(config.data_dir() / "tessdata"))


def _options(**kw):
    from immich_book_ocr.pipeline import PipelineOptions

    return PipelineOptions(language=config.ocr_language(), **kw)


def ocr_folder(images: Path, output: Path, *, single_page: bool = False, order: str = "filename",
               progress: Progress | None = None) -> dict:
    """Ordner mit Buchfotos oder Scans erkennen. order: filename oder date."""
    from immich_book_ocr.pipeline import run_local

    _env()
    return run_local(images, output, options=_options(single_page=single_page, order=order), progress=progress)


def ocr_album(url: str, output: Path, *, progress: Progress | None = None,
              api_key: str | None = None, password: str | None = None) -> dict:
    from immich_book_ocr.pipeline import run_album

    _env()
    return run_album(url, output, options=_options(), api_key=api_key, password=password, progress=progress)


def ocr_pdf_pages(pdf: Path, scans: list[int], output: Path, *, progress: Progress | None = None,
                  dpi: int = 300) -> dict[int, Page]:
    """Die genannten PDF-Seiten rendern und einzeln erkennen."""
    import pypdfium2 as pdfium

    _env()
    progress = progress or (lambda _: None)
    images = output / "pdfseiten"
    images.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf))
    for n in scans:
        target = images / f"s{n:05d}.png"
        if not target.exists():
            doc[n - 1].render(scale=dpi / 72, grayscale=True).to_pil().save(target)
        progress(f"Seite {n} gerendert")
    doc.close()
    ocr_folder(images, output, single_page=True, order="filename", progress=progress)
    result: dict[int, Page] = {}
    for source, page in _report_pages(output):
        n = int(Path(source).stem[1:])
        result[n] = Page(n, page.text, None, page.konfidenz)
    return result


def _report_pages(output: Path) -> list[tuple[str, Page]]:
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    out = []
    for entry in report.get("entries", []):
        for p in entry.get("pages", []):
            text_file = output / p["text_file"] if p.get("text_file") else None
            text = text_file.read_text(encoding="utf-8") if text_file and text_file.exists() else ""
            out.append((entry.get("source", ""), Page(len(out) + 1, text.strip(), None, p.get("confidence"))))
    return out


def pages_from_report(output: Path) -> tuple[list[Page], dict]:
    """Seiten in Lesereihenfolge plus Zusammenfassung (Titel, Vollständigkeit, Warnungen)."""
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    pages = [p for _, p in _report_pages(output)]
    conf = [p.konfidenz for p in pages if p.konfidenz is not None]
    summary = {
        "titel": report.get("title"),
        "vollstaendig": report.get("complete", False),
        "konfidenz": round(sum(conf) / len(conf), 1) if conf else None,
        "warnungen": sum(len(e.get("warnings", [])) for e in report.get("entries", [])),
        "engine": (report.get("options") or {}).get("engine"),
    }
    return pages, summary
