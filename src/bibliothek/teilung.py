"""Scans mit mehreren Druckseiten oder Spalten in einzelne Abschnitte teilen.

Kopierte Bücher liegen oft als PDF mit einer aufgeschlagenen Doppelseite pro
Seite vor; Lexika zählen Spalten statt Seiten. Jede Seite bzw. Spalte wird ein
eigener Abschnitt (Page) mit genau einer Zahl — dann gilt die übliche
Seitenzählung. Die Teilung ist für ein Dokument einheitlich: PDF-Seite p, Teil j
(ab 0) ist Abschnitt (p − 1) · teile + j + 1.

    doppelseiten          2 Teile, Zählung „S.“
    spalten               2 Teile, Zählung „Sp.“
    doppelseiten-spalten  4 Teile, Zählung „Sp.“
"""
from __future__ import annotations

ARTEN = {"aus": (1, "S."), "doppelseiten": (2, "S."), "spalten": (2, "Sp."), "doppelseiten-spalten": (4, "Sp.")}
RAND = 0.08   # Kopf- und Fußzeilen laufen oft über die ganze Breite: für die Grenze nicht mitzählen


def art_von(teile: int, einheit: str) -> str:
    return next((a for a, (n, e) in ARTEN.items() if n == teile and e == einheit), "aus" if teile == 1 else "doppelseiten")


def herkunft(scan: int, teile: int) -> tuple[int, int]:
    """Abschnitt → (PDF-Seite ab 1, Teil ab 0)."""
    return (scan - 1) // teile + 1, (scan - 1) % teile


def _kanal(xs: list[tuple[float, float]], links: float, rechts: float) -> float:
    """Mitte des breitesten leeren Kanals im mittleren Bereich [links, rechts]; ohne Befund die Mitte."""
    from immich_book_ocr.imaging import _gutter_from_lines

    breite = rechts - links
    boxes = [((max(l, links) - links) / breite, 0.0, (min(r, rechts) - links) / breite, 0.0)
             for l, r in xs if r > links and l < rechts]
    found = _gutter_from_lines(boxes)
    pos = found[0] if found else 0.5
    return links + pos * breite


def pdf_grenzen(page, teile: int) -> list[tuple[float, float]]:
    """x-Bereiche (in PDF-Punkten) der Teile einer PDF-Seite mit Textebene."""
    breite, hoehe = page.get_width(), page.get_height()
    if teile == 1:
        return [(0.0, breite)]
    tp = page.get_textpage()
    xs = []
    for i in range(tp.count_chars()):
        l, b, r, t = tp.get_charbox(i)
        if r > l and hoehe * RAND < b and t < hoehe * (1 - RAND):
            xs.append((l, r))
    mitte = _kanal(xs, 0.0, breite)
    haelften = [(0.0, mitte), (mitte, breite)]
    if teile == 2:
        return haelften
    out = []
    for a, b in haelften:
        m = _kanal(xs, a, b)
        out += [(a, m), (m, b)]
    return out


def pdf_text_teile(page, teile: int) -> list[str]:
    tp = page.get_textpage()
    hoehe = page.get_height()
    return [tp.get_text_bounded(left=a, bottom=0, right=b, top=hoehe) for a, b in pdf_grenzen(page, teile)]


def bild_teile(bild, page, teile: int) -> list:
    """Vorschaubild einer PDF-Seite mit Textebene an denselben Grenzen zuschneiden."""
    w = page.get_width()
    return [bild.crop((round(a / w * bild.width), 0, round(b / w * bild.width), bild.height))
            for a, b in pdf_grenzen(page, teile)]
