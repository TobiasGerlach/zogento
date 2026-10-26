"""Wo liegen Originale und OCR-Läufe — lokal oder auf der Nextcloud?

Ohne NAS bleibt alles im Bibliotheksordner (titel/<citekey>/, ocr/<citekey>/).
Mit NAS ist die Nextcloud die Ablage: Neue Dateien liegen nur so lange lokal,
bis der Abgleich sie hochgeladen hat; geöffnete Dateien kommen in einen
Zwischenspeicher, der sich auf eine Höchstgröße selbst aufräumt.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import ablage, config
from .store import Item, Library

KATALOG = ("meta.yaml", "seiten.jsonl", "notizen.md")   # wird abgeglichen
NUR_LOKAL = ("text.md",)                                # wird lokal erzeugt
NAS_PREFIX = "nas:"
ZWISCHENSPEICHER_MAX = int(float(os.environ.get("BIBLIOTHEK_ZWISCHENSPEICHER_GB", "2")) * 1e9)


def nas_aktiv() -> bool:
    return ablage.verbunden()


def zwischenspeicher() -> Path:
    return config.data_dir() / "zwischenspeicher"


def arbeitsordner() -> Path:
    """Hier entstehen OCR-Läufe, bevor sie auf die Nextcloud wandern."""
    return config.data_dir() / "ocr-arbeit"


def ist_original(path: Path) -> bool:
    return path.is_file() and path.name not in KATALOG + NUR_LOKAL and not path.name.startswith(".") \
        and not path.name.endswith(".teil")


# --- OCR-Läufe ------------------------------------------------------------------------------

def ocr_workdir(lib: Library, key: str) -> Path:
    return arbeitsordner() / key if nas_aktiv() else lib.root / "ocr" / key


def ocr_ort(item: Item) -> str | None:
    return item.text.get("ocr_ordner") or None


def ocr_datei(lib: Library, item: Item, rel: str) -> Path:
    """Datei aus dem OCR-Lauf eines Titels (z. B. review.html, previews/…)."""
    ort = ocr_ort(item)
    if not ort:
        raise FileNotFoundError("Kein OCR-Lauf zu diesem Titel")
    if ".." in Path(rel).parts:
        raise FileNotFoundError(rel)
    if not ort.startswith(NAS_PREFIX):
        path = Path(ort) / rel
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path
    remote = ort[len(NAS_PREFIX):].rstrip("/") + "/" + rel
    return _aus_nas(remote)


# --- Originale ------------------------------------------------------------------------------

def original(lib: Library, item: Item, name: str) -> Path:
    """Pfad zu einer Originaldatei; holt sie bei Bedarf von der Nextcloud."""
    if name not in item.dateien or "/" in name or "\\" in name:
        raise FileNotFoundError(name)
    local = lib.folder(item.citekey) / name
    if local.is_file():
        return local
    if not nas_aktiv():
        raise FileNotFoundError(name)
    return _aus_nas(f"titel/{item.citekey}/{name}")


def _aus_nas(remote: str) -> Path:
    target = zwischenspeicher() / remote
    if target.is_file():
        os.utime(target)   # zuletzt benutzt
        return target
    dav = ablage.oeffnen()
    dav.holen(remote, target)
    aufraeumen()
    return target


def in_zwischenspeicher(path: Path, remote: str) -> None:
    """Nach dem Hochladen: lokale Datei nicht löschen, sondern als zuletzt benutzt behalten."""
    target = zwischenspeicher() / remote
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target)
    aufraeumen()


def aufraeumen(limit: int = ZWISCHENSPEICHER_MAX) -> int:
    """Älteste Dateien löschen, bis der Zwischenspeicher unter `limit` Bytes liegt."""
    root = zwischenspeicher()
    if not root.exists():
        return 0
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.rglob("*") if p.is_file()]
    total = sum(s for _, s, _ in files)
    freed = 0
    for _, size, p in sorted(files):
        if total <= limit:
            break
        p.unlink(missing_ok=True)
        total -= size
        freed += size
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    return freed


def zwischenspeicher_groesse() -> int:
    root = zwischenspeicher()
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def ausstehend(lib: Library) -> dict:
    """Was noch hochgeladen werden muss (nur mit NAS sinnvoll)."""
    originale = [p for key in lib.keys() for p in lib.folder(key).iterdir() if ist_original(p)]
    ocr = [it.citekey for it in lib.items() if (ocr_ort(it) or "").strip() and not ocr_ort(it).startswith(NAS_PREFIX)]
    return {"originale": len(originale), "bytes": sum(p.stat().st_size for p in originale), "ocr": ocr}


def jetzt() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
