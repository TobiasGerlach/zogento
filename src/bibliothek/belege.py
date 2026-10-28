"""Zitate gegen den Volltext prüfen: Steht das so da, und auf welcher Seite?

Toleriert Silbentrennung am Zeilenende, typografische Anführungszeichen und
kleinere OCR-Fehler; meldet die tatsächliche Fundstelle mit ehrlicher
Seitenangabe (zitierfähig oder geschätzt).
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .store import Item, Library, Page

FAST = 0.85   # darunter: nicht gefunden


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)          # Trennung am Zeilenende
    text = re.sub(r"[„“”«»‚‘’‹›\"']", "", text)
    text = re.sub(r"[–—]", "-", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def pruefen(lib: Library, item: Item, zitat: str, *, seite: str | None = None) -> dict:
    pages = lib.pages(item.citekey)
    if not pages:
        return {"gefunden": False, "hinweis": "Kein Volltext zu diesem Titel — Zitat lässt sich nicht prüfen."}
    q = _norm(zitat)
    if len(q) < 8:
        return {"gefunden": False, "hinweis": "Zitat zu kurz zum Prüfen."}
    texte = [_norm(p.text) for p in pages]
    best = (0.0, 0, 0, "")   # (Ähnlichkeit, Seitenindex, Startposition, Fundtext)
    # 1. wörtlich — auch über eine Seitengrenze hinweg
    for i, t in enumerate(texte):
        nxt = texte[i + 1][:len(q) + 50] if i + 1 < len(texte) else ""
        pos = (t + " " + nxt).find(q)
        if pos >= 0 and pos < len(t):
            best = (1.0, i, pos, (t + " " + nxt)[pos:pos + len(q)])
            break
    # 2. ungefähr: Seiten mit den meisten Ankerwörtern, darin das beste Fenster
    if best[0] < 1.0:
        woerter = [w for w in q.split() if len(w) > 3]
        kandidaten = sorted(range(len(texte)), key=lambda i: -sum(1 for w in woerter if w in texte[i]))[:6]
        for i in kandidaten:
            t = texte[i] + (" " + texte[i + 1][:len(q) + 50] if i + 1 < len(texte) else "")
            tw = t.split(" ")
            n = len(q.split())
            pos = 0
            for j in range(0, max(1, len(tw) - n + 1)):
                fenster = " ".join(tw[j:j + n])
                r = SequenceMatcher(None, q, fenster, autojunk=False).ratio()
                if r > best[0]:
                    best = (r, i, pos, fenster)
                pos += len(tw[j]) + 1
    r, i, pos, fund = best
    p = pages[i]
    ueber_grenze = pos + len(fund) > len(texte[i])
    ref = item.page_ref(p) + (" f." if ueber_grenze and item.page_number(p) else "")
    ergebnis = {
        "gefunden": r >= FAST, "aehnlichkeit": round(r, 3),
        "bewertung": "wörtlich" if r == 1.0 else ("abweichend — Unterschiede am Scan prüfen" if r >= FAST
                                                 else "nicht gefunden"),
        "seite": ref, "scan": p.scan, "zitierfaehig": item.citable(p) and r == 1.0,
        "fundstelle": _kontext(pages[i].text + ("\n" + pages[i + 1].text if ueber_grenze else ""), fund) if r >= FAST else "",
    }
    if FAST <= r < 1.0:
        qw, fw = q.split(), fund.split()
        ops = SequenceMatcher(None, qw, fw, autojunk=False).get_opcodes()
        ergebnis["unterschiede"] = [{"zitat": " ".join(qw[a:b]), "text": " ".join(fw[c:d])}
                                    for tag, a, b, c, d in ops if tag != "equal"][:8]
    if seite and r >= FAST:
        num = item.page_number(p)
        angegeben = re.sub(r"[^\d\-–f.]", "", seite).replace("–", "-")
        start = re.match(r"\d+", angegeben)
        ergebnis["angegebene_seite_stimmt"] = bool(num and start and (
            start.group(0) == str(num) or (ueber_grenze and str(int(start.group(0)) - 1) == str(num))))
    if r >= FAST and not item.citable(p):
        ergebnis["hinweis"] = "Seitenzahl dieser Stelle ist nicht geprüft — vor dem Zitieren bestätigen lassen."
    if FAST <= r < 1.0 and (item.text.get("quelle") or "").startswith(("ocr", "pdf+ocr")):
        ergebnis["hinweis"] = (ergebnis.get("hinweis", "") + " Text ist OCR — Abweichung kann ein Erkennungsfehler sein.").strip()
    return ergebnis


def _kontext(original: str, fund_norm: str, rand: int = 120) -> str:
    """Fundstelle mit etwas Umgebung aus dem Originaltext (nicht normalisiert)."""
    flach = re.sub(r"\s+", " ", re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", original.replace("\u00ad", "")))
    woerter = re.findall(r"\w+", fund_norm)
    m = None
    for n in (6, 4, 2):   # mit weniger Wörtern suchen, falls OCR-Fehler im Anfang stecken
        if len(woerter) >= n:
            m = re.search(r"\W+".join(map(re.escape, woerter[:n])), flach, re.I)
            if m:
                break
    if not m:
        return flach[:2 * rand + len(fund_norm)]
    a, b = max(0, m.start() - rand), min(len(flach), m.start() + len(fund_norm) + rand)
    return ("…" if a else "") + flach[a:b] + ("…" if b < len(flach) else "")
