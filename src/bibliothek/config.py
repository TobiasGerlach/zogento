"""Pfade und Einstellungen, plattformunabhängig.

Reihenfolge: Umgebungsvariable > einstellungen.json > Vorgabe.
einstellungen.json liegt im üblichen Konfigurationsordner des Systems:
  macOS    ~/Library/Application Support/Bibliothek/
  Linux    ~/.config/bibliothek/
  Windows  %APPDATA%\\Bibliothek\\
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = "Bibliothek"


def config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP.lower()


def data_dir() -> Path:
    """Programmdaten (OCR-Modelle, Suchindex) — nicht die Bibliothek selbst."""
    if os.environ.get("BIBLIOTHEK_DATEN"):
        return Path(os.environ["BIBLIOTHEK_DATEN"]).expanduser()
    if sys.platform == "darwin":
        return config_dir()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP.lower()


SETTINGS_FILE = config_dir() / "einstellungen.json"


def settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = settings() | values
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def _get(env: str, key: str, default):
    return os.environ.get(env) or settings().get(key) or default


def library_root() -> Path:
    return Path(_get("BIBLIOTHEK", "bibliothek", Path.home() / "Documents" / "Bibliothek")).expanduser()


def ocr_language() -> str:
    return _get("BIBLIOTHEK_OCR_SPRACHE", "ocr_sprache", "eng+deu")


OCR_ENGINES = ("auto", "vision", "tesseract")


def ocr_engine() -> str:
    """auto = Apple Vision auf dem Mac (beste Erkennung), sonst Tesseract; tesseract = nur Open Source."""
    wert = _get("BIBLIOTHEK_OCR_ENGINE", "ocr_engine", "auto")
    return wert if wert in OCR_ENGINES else "auto"


def web_port() -> int:
    return int(_get("BIBLIOTHEK_PORT", "port", 8777))


def bundled_tesseract() -> Path | None:
    """Tesseract, das im App-Paket mitgeliefert wird (Windows)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        exe = base / "tesseract" / ("tesseract.exe" if sys.platform == "win32" else "tesseract")
        if exe.exists():
            return exe
    return None


def index_path(root: Path) -> Path:
    """Suchindex pro Bibliothek und Rechner, außerhalb des Bibliotheksordners.

    Der Index ist abgeleitet und wird lokal gehalten: SQLite verträgt keine
    Cloud-Synchronisation (iCloud, Nextcloud …), und jeder Rechner baut ihn
    aus den synchronisierten Titel-Dateien selbst auf.
    """
    return data_dir() / "indizes" / f"{_tag(root)}.sqlite"


def sync_dir(root: Path) -> Path:
    """Abgleichsstand mit der Nextcloud (pro Bibliothek und Rechner)."""
    return data_dir() / "abgleich" / _tag(root)


def _tag(root: Path) -> str:
    import hashlib

    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]
    return f"{root.name}-{digest}"


ROOT = library_root()
ITEMS = ROOT / "titel"            # ein Ordner pro Titel
OCR_DIR = ROOT / "ocr"            # OCR-Läufe, die aus der Bibliothek gestartet wurden

EMBED_MODEL = os.environ.get("BIBLIOTHEK_MODELL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_QUERY_PROMPT = (
    "Instruct: Given a search query, retrieve passages from scholarly and theological "
    "literature that answer or discuss it\nQuery: "
)

# Seiten mit weniger Zeichen gelten in PDFs als Scan ohne Textebene
MIN_TEXT_CHARS = 25
CHUNK_CHARS = 1100
CHUNK_OVERLAP = 150
