"""Selbsttest für eingefrorene Builds: lassen sich alle schweren Bibliotheken laden?

Aufruf: BIBLIOTHEK_SMOKE=1 <bib oder Bibliothek>  — gibt "SMOKE OK" aus, sonst Exit 1.
"""
from __future__ import annotations

import importlib
import sys


def run() -> int:
    modules = [
        "numpy", "yaml", "flask", "requests", "PIL.Image", "pillow_heif", "cv2", "docx",
        "striprtf.striprtf", "pypdfium2", "torch", "transformers", "sentence_transformers",
        "transformers.models.qwen3.modeling_qwen3", "immich_book_ocr.pipeline",
        "bibliothek.store", "bibliothek.extract", "bibliothek.index", "bibliothek.ingest",
    ]
    if sys.platform != "linux":
        modules.append("webview")
    if sys.platform == "darwin":
        modules += ["Vision", "Quartz"]
    failed = []
    for name in modules:
        try:
            importlib.import_module(name)
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001 — jeder Fehler zählt als Fehlschlag
            failed.append(name)
            print(f"FEHLT {name}: {exc!r}")
    if not failed:
        import pypdfium2 as pdfium
        import torch

        doc = pdfium.PdfDocument.new()
        doc.new_page(200, 300)
        print("ok    pdfium seiten:", len(doc))
        print("ok    torch:", torch.__version__, float(torch.ones(3).sum()))
        from importlib.resources import files
        for pkg in ("bibliothek", "immich_book_ocr"):
            tmpl = files(pkg) / "templates"
            print(f"ok    {pkg}/templates:", sorted(p.name for p in tmpl.iterdir()) if tmpl.is_dir() else "FEHLT")
            if not tmpl.is_dir():
                failed.append(f"{pkg}/templates")
        anleitung = files("bibliothek") / "KI-ANLEITUNG.md"
        print("ok    KI-ANLEITUNG.md" if anleitung.is_file() else "FEHLT KI-ANLEITUNG.md")
        if not anleitung.is_file():
            failed.append("KI-ANLEITUNG.md")
        for name in ("bibliothek.cli", "bibliothek.app", "bibliothek.web"):
            try:
                importlib.import_module(name)
                print(f"ok    {name}")
            except Exception as exc:  # noqa: BLE001
                failed.append(name)
                print(f"FEHLT {name}: {exc!r}")
        from bibliothek import config
        print("ok    tesseract (gebündelt):", config.bundled_tesseract())
    print("SMOKE OK" if not failed else f"SMOKE FEHLGESCHLAGEN: {', '.join(failed)}")
    return 1 if failed else 0
