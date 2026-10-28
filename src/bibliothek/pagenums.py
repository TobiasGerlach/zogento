"""Druckseiten aus dem Text schätzen.

Bücher und Zeitschriftenaufsätze tragen ihre Seitenzahl meist in der ersten
oder letzten Zeile. Daraus ergibt sich je Seite ein Versatz (Druckseite − Scan).
Bei abfotografierten Büchern springt dieser Versatz, wo Scans fehlen oder
Leerseiten übersprungen wurden. Die Zählung besteht deshalb aus Abschnitten
(`segmente`), jeder mit eigenem Versatz.

Das Ergebnis ist immer nur ein Vorschlag (Status „automatisch“), bis jemand ihn bestätigt.
"""
from __future__ import annotations

import re
from collections import Counter

from .store import Page

_ALONE = re.compile(r"^\D{0,3}(\d{1,4})\D{0,3}$")
_LEAD = re.compile(r"^(\d{1,4})\s+\S")
_TRAIL = re.compile(r"\S(?:\s+|\s*[/|]\s*)(\d{1,4})$")   # „… 45“, „Kolumnentitel/45“, „… | 45“
WINDOW = 8       # so viele Nachbarseiten stimmen über einen Versatz ab
MIN_VOTES = 3    # … und so viele müssen ihn stützen


def candidates(text: str) -> set[int]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found = set()
    for line in lines[:2] + lines[-2:]:
        for rx in (_ALONE, _LEAD, _TRAIL):
            m = rx.search(line)
            if m and 0 < int(m.group(1)) < 3000:
                found.add(int(m.group(1)))
    return found


def detect(pages: list[Page]) -> dict | None:
    """Abschnitte [{ab_scan, versatz}] plus Trefferquote, oder None ohne klares Muster."""
    offsets = {p.scan: {n - p.scan for n in candidates(p.text)} for p in pages if len(p.text) >= 200}
    scans = sorted(offsets)
    if len(scans) < MIN_VOTES:
        return None
    segments: list[dict] = []
    current = None
    for i, scan in enumerate(scans):
        votes = Counter(o for s in scans[i:i + WINDOW] for o in offsets[s])
        if not votes:
            continue
        best, hits = votes.most_common(1)[0]
        if hits < MIN_VOTES or best == current or best not in offsets[scan]:
            continue
        # Nur wechseln, wenn der bisherige Versatz in diesem Fenster kaum noch vorkommt
        if current is None or votes[current] < 2:
            start, j = scan, i - 1
            floor = segments[-1]["ab_scan"] if segments else 0
            # Grenze zurückziehen bis hinter den letzten Beleg des alten Versatzes
            while j >= 0 and scans[j] > floor and current not in offsets[scans[j]]:
                if best in offsets[scans[j]]:
                    start = scans[j]
                j -= 1
            segments.append({"ab_scan": start, "versatz": best})
            current = best
    if not segments:
        return None
    segments[0]["ab_scan"] = max(1, 1 - segments[0]["versatz"])
    support = sum(1 for s in scans if offset_for(segments, s) in offsets[s])
    jumps = []
    for prev, seg in zip(segments, segments[1:]):
        d = seg["versatz"] - prev["versatz"]
        what = f"{d} Druckseite(n) fehlen im Scan" if d > 0 else f"{-d} Seite(n) vermutlich doppelt gescannt"
        jumps.append(f"Bei Scan {seg['ab_scan']} springt die Zählung um {d:+d}: {what}.")
    return {"segmente": segments, "treffer": support, "quote": round(support / len(scans), 2), "spruenge": jumps}


def schritt(pages: list[Page]) -> int:
    """Um wie viel steigt die Zählung von Scan zu Scan? 2 heißt meist: Doppelseiten, 4: Doppelseiten mit Spalten.

    Gibt 1 zurück, wenn kein anderer Schritt klar überwiegt.
    """
    cands = [candidates(p.text) for p in pages if len(p.text) >= 200]
    votes = Counter()
    for a, b in zip(cands, cands[1:]):
        for x in a:
            for d in (1, 2, 4):
                if x + d in b:
                    votes[d] += 1
    if not votes:
        return 1
    best, n = votes.most_common(1)[0]
    return best if best > 1 and n >= max(3, 2 * votes[1]) else 1


def offset_for(segments: list[dict], scan: int) -> int | None:
    found = None
    for seg in sorted(segments, key=lambda s: s["ab_scan"]):
        if scan >= seg["ab_scan"]:
            found = seg["versatz"]
    return found


def set_segment(segments: list[dict], ab_scan: int, versatz: int) -> list[dict]:
    """Abschnitt ab `ab_scan` setzen; spätere Abschnitte bleiben, ein gleich beginnender wird ersetzt."""
    out = [s for s in segments if s["ab_scan"] != ab_scan] + [{"ab_scan": ab_scan, "versatz": versatz}]
    return sorted(out, key=lambda s: s["ab_scan"])
