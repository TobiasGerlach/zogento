"""Sicherung auf eine externe Platte oder einen anderen Ordner.

Das Ziel ist selbst eine vollständige Bibliothek (Katalog, Texte, Originale,
OCR-Läufe) und lässt sich direkt öffnen: `bib einstellung --bibliothek <ziel>`.
Was auf der Ablage (Nextcloud/WebDAV) liegt, wird von dort geholt. Jeder Lauf
überträgt nur Geändertes; gelöscht wird in der Sicherung nie etwas.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from . import ablage, config, dateien
from .store import Library, _dump_yaml

MARKER = ".bibliothek-sicherung.json"
ERINNERN_NACH_TAGEN = 14
Fortschritt = Callable[[str], None]


def zielordner(ziel: Path) -> Path:
    """Leerer oder bekannter Ordner wird direkt benutzt, sonst ein Unterordner darin."""
    ziel = ziel.expanduser().resolve()
    if (ziel / MARKER).exists() or not ziel.exists() or not any(ziel.iterdir()):
        return ziel
    return ziel / "Bibliothek-Sicherung"


def sichern(lib: Library, ziel: Path, *, fortschritt: Fortschritt | None = None) -> dict:
    fortschritt = fortschritt or (lambda _: None)
    ziel = zielordner(Path(ziel))
    if ziel.resolve() == lib.root.resolve() or lib.root.resolve() in ziel.resolve().parents:
        raise ValueError("Die Sicherung darf nicht im Bibliotheksordner selbst liegen.")
    ziel.mkdir(parents=True, exist_ok=True)
    marker = ziel / MARKER
    stand = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {"dateien": {}}
    bekannt: dict[str, dict] = stand.get("dateien", {})
    bericht = {"ziel": str(ziel), "kopiert": 0, "geholt": 0, "bytes": 0, "unveraendert": 0, "fehler": []}

    # 1. Alles, was lokal im Bibliotheksordner liegt
    for p in sorted(lib.root.rglob("*")):
        if not p.is_file() or p.name in (".DS_Store",) or p.name.endswith((".teil", ".lock")):
            continue
        rel = p.relative_to(lib.root).as_posix()
        st = p.stat()
        merkmal = {"size": st.st_size, "mtime": st.st_mtime_ns}
        if bekannt.get(rel) == merkmal and (ziel / rel).is_file():
            bericht["unveraendert"] += 1
            continue
        (ziel / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, ziel / rel)
        bekannt[rel] = merkmal
        bericht["kopiert"] += 1
        bericht["bytes"] += st.st_size

    # 2. Was nur auf der Ablage liegt: Originale und OCR-Läufe
    if dateien.nas_aktiv():
        dav = ablage.oeffnen()
        if not dav.erreichbar():
            raise ablage.NichtErreichbar("Ablage nicht erreichbar — Originale und Scans liegen dort. "
                                         "Im Heimnetz erneut sichern.")
        for item in lib.items():
            ordner = [f"titel/{item.citekey}"]
            ort = dateien.ocr_ort(item) or ""
            if ort.startswith(dateien.NAS_PREFIX):
                ordner.append(ort[len(dateien.NAS_PREFIX):].rstrip("/"))
            for basis in ordner:
                fortschritt(f"{item.citekey}: {basis} …")
                try:
                    for rel, e in _alle_dateien(dav, basis):
                        if basis.startswith("titel/") and rel.rsplit("/", 1)[-1] in dateien.KATALOG:
                            continue   # Katalog kommt lokal (Schritt 1)
                        merkmal = {"size": e.groesse, "etag": e.etag}
                        if bekannt.get(rel) == merkmal and (ziel / rel).is_file():
                            bericht["unveraendert"] += 1
                            continue
                        dav.holen(rel, ziel / rel)
                        bekannt[rel] = merkmal
                        bericht["geholt"] += 1
                        bericht["bytes"] += e.groesse
                        if bericht["geholt"] % 200 == 0:
                            _speichern(marker, lib, bekannt)
                            fortschritt(f"{bericht['geholt']} Dateien von der Ablage geholt")
                except ablage.NichtErreichbar:
                    _speichern(marker, lib, bekannt)
                    raise
                except ablage.AblageFehler as exc:
                    bericht["fehler"].append(f"{basis}: {exc}")

    _speichern(marker, lib, bekannt)
    _eigenstaendig_machen(lib, ziel)
    config.save_settings({"sicherung": {"ziel": str(Path(ziel)), "zuletzt": time.time()}})
    return bericht


def _alle_dateien(dav, basis: str):
    eintraege = dav.liste(basis)
    for e in eintraege or []:
        rel = f"{basis}/{e.name}"
        if e.ist_ordner:
            yield from _alle_dateien(dav, rel)
        else:
            yield rel, e


def _speichern(marker: Path, lib: Library, bekannt: dict) -> None:
    tmp = marker.with_name(marker.name + ".neu")
    tmp.write_text(json.dumps({"quelle": str(lib.root), "zuletzt": time.time(), "dateien": bekannt},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, marker)


def _eigenstaendig_machen(lib: Library, ziel: Path) -> None:
    """In der Sicherung zeigen OCR-Ordner auf die mitgesicherten Scans, nicht auf die Ablage."""
    import yaml

    for meta in (ziel / "titel").glob("*/meta.yaml"):
        data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
        ort = (data.get("text") or {}).get("ocr_ordner") or ""
        neu = None
        if ort.startswith(dateien.NAS_PREFIX):
            neu = str(ziel / ort[len(dateien.NAS_PREFIX):])
        elif ort and Path(ort).is_relative_to(lib.root):
            neu = str(ziel / Path(ort).relative_to(lib.root))
        if neu and neu != ort:
            data["text"]["ocr_ordner"] = neu
            meta.write_text(_dump_yaml(data), encoding="utf-8")


def status() -> dict:
    s = config.settings().get("sicherung") or {}
    zuletzt = s.get("zuletzt")
    tage = (time.time() - zuletzt) / 86400 if zuletzt else None
    return {"ziel": s.get("ziel"), "zuletzt": zuletzt, "tage": tage,
            "faellig": tage is None or tage > ERINNERN_NACH_TAGEN}
