"""Abgleich mit der Nextcloud.

Ein Durchlauf:
  1. vorgemerktes Umbenennen/Löschen auf der Nextcloud nachziehen
  2. je Titel: neue OCR-Läufe und Originale hochladen (lokal danach nur noch im
     Zwischenspeicher), geänderte Katalogdateien hochladen
  3. Änderungen anderer Rechner herunterladen (nur Katalogdateien; Originale
     erst beim Öffnen)
  4. Eingangsordner verarbeiten

Konflikte (dieselbe Datei hier und anderswo geändert) entscheidet die lokale
Fassung; die andere bleibt auf der Nextcloud als <datei>.konflikt-<zeit> liegen.
Ordner-ETags der Nextcloud ändern sich mit jedem Inhalt darin, deshalb werden
nur geänderte Titel einzeln abgefragt.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import ablage, config, dateien, extract, index, ingest
from .store import Library

Fortschritt = Callable[[str], None]
EINGANG = "Eingang"
EINGANG_RUHE = 120   # Sekunden ohne Änderung, bevor eine Datei im Eingang als fertig gilt


class Gesperrt(RuntimeError):
    pass


# --- Zustand und Sperre -----------------------------------------------------------------------

class _Zustand:
    def __init__(self, lib: Library):
        self.dir = config.sync_dir(lib.root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "zustand.json"
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.ordner: dict[str, str] = d.get("ordner", {})       # key → Ordner-ETag
        self.dateien: dict[str, dict] = d.get("dateien", {})     # titel/key/datei → {etag, mtime, size}
        self.zuletzt: float = d.get("zuletzt", 0.0)

    def speichern(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ordner": self.ordner, "dateien": self.dateien, "zuletzt": self.zuletzt},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def lokal_geaendert(self, rel: str, path: Path) -> bool:
        st = path.stat()
        known = self.dateien.get(rel)
        return not known or known.get("mtime") != st.st_mtime_ns or known.get("size") != st.st_size

    def merken(self, rel: str, path: Path, etag: str) -> None:
        st = path.stat()
        self.dateien[rel] = {"etag": etag, "mtime": st.st_mtime_ns, "size": st.st_size}

    def vergessen(self, key: str) -> None:
        self.ordner.pop(key, None)
        for rel in [r for r in self.dateien if r.startswith(f"titel/{key}/")]:
            del self.dateien[rel]

    def umbenennen(self, alt: str, neu: str) -> None:
        if alt in self.ordner:
            self.ordner[neu] = self.ordner.pop(alt)
        for rel in [r for r in self.dateien if r.startswith(f"titel/{alt}/")]:
            self.dateien[f"titel/{neu}/" + rel.split("/", 2)[2]] = self.dateien.pop(rel)


class _Sperre:
    """Nur ein Abgleich gleichzeitig (App und Kommandozeile teilen sich den Zustand)."""

    def __init__(self, lib: Library):
        self.path = config.sync_dir(lib.root) / "sperre"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - self.path.stat().st_mtime < 3 * 3600:
                raise Gesperrt("Ein anderer Abgleich läuft gerade.")
            self.path.unlink(missing_ok=True)   # verwaist
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


# --- Abgleich ---------------------------------------------------------------------------------

def abgleichen(lib: Library, *, dav=None, eingang: bool = True, fortschritt: Fortschritt | None = None) -> dict:
    fortschritt = fortschritt or (lambda _: None)
    bericht = {"status": "ok", "hoch": 0, "runter": 0, "geholt": [], "konflikte": [], "eingang": [], "fehler": []}
    if dav is None:
        if not ablage.verbunden():
            return {**bericht, "status": "kein NAS"}
        gebunden = ablage.einstellungen().get("bibliothek")
        if not gebunden or Path(gebunden).resolve() != lib.root.resolve():
            # Nur die beim Verbinden gewählte Bibliothek darf mit der Nextcloud abgleichen
            return {**bericht, "status": "andere Bibliothek"}
        dav = ablage.oeffnen()
    if not dav.erreichbar():
        return {**bericht, "status": "offline", "ausstehend": dateien.ausstehend(lib)}
    with _Sperre(lib):
        z = _Zustand(lib)
        _journal_anwenden(lib, dav, z, bericht)
        remote = {e.name: e for e in (dav.liste("titel") or []) if e.ist_ordner}
        if not remote:
            dav.mkdirs("titel")
        for key in lib.keys():
            try:
                _hochladen(lib, dav, z, key, remote.get(key), bericht, fortschritt)
            except ablage.NichtErreichbar:
                raise
            except Exception as exc:   # ein kaputter Titel soll den Rest nicht aufhalten
                bericht["fehler"].append(f"{key}: {exc}")
        _herunterladen(lib, dav, z, bericht, fortschritt)
        z.zuletzt = time.time()
        z.speichern()
    if bericht["geholt"]:
        for key in bericht["geholt"]:
            if lib.exists(key) and (lib.folder(key) / "seiten.jsonl").exists():
                lib.render_text(lib.get(key))
        index.update(lib)
        lib.write_refs_bib()
    if eingang:
        bericht["eingang"] = eingang_verarbeiten(lib, dav, fortschritt=fortschritt)
        if bericht["eingang"]:
            abgleichen(lib, dav=dav, eingang=False, fortschritt=fortschritt)   # Neues gleich hochladen
    return bericht


def _journal_anwenden(lib: Library, dav, z: _Zustand, bericht: dict) -> None:
    path = config.sync_dir(lib.root) / "journal.jsonl"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e["op"] == "loeschen":
            dav.loeschen(f"titel/{e['key']}")
            dav.loeschen(f"ocr/{e['key']}")
            z.vergessen(e["key"])
        elif e["op"] == "umbenennen":
            for base in ("titel", "ocr"):
                if dav.info(f"{base}/{e['von']}"):
                    dav.verschieben(f"{base}/{e['von']}", f"{base}/{e['nach']}")
            z.umbenennen(e["von"], e["nach"])
    path.unlink()


def _hochladen(lib: Library, dav, z: _Zustand, key: str, remote_dir, bericht: dict, fortschritt: Fortschritt) -> None:
    folder = lib.folder(key)
    item = lib.get(key)
    # Titel, der anderswo gelöscht wurde: hier ebenfalls entfernen, sofern hier nichts Neues dazukam
    if remote_dir is None and key in z.ordner:
        if not any(z.lokal_geaendert(f"titel/{key}/{n}", folder / n) for n in dateien.KATALOG if (folder / n).exists()):
            shutil.rmtree(folder)
            z.vergessen(key)
            bericht["geholt"].append(key)
            return
    # 1. OCR-Lauf, der noch lokal liegt
    ort = dateien.ocr_ort(item)
    if ort and not ort.startswith(dateien.NAS_PREFIX) and Path(ort).is_dir():
        fortschritt(f"{key}: OCR-Lauf hochladen …")
        n = dav.ordner_hochladen(Path(ort), f"ocr/{key}", fortschritt=fortschritt)
        item.text["ocr_ordner"] = f"{dateien.NAS_PREFIX}ocr/{key}"
        lib.save(item)
        shutil.rmtree(ort)
        bericht["hoch"] += n
    # 2. Originale
    for p in sorted(folder.iterdir()):
        if dateien.ist_original(p):
            rel = f"titel/{key}/{p.name}"
            fortschritt(f"{key}: {p.name} hochladen …")
            size = p.stat().st_size
            dav.ablegen(p, rel)
            info = dav.info(rel)
            if not info or info.groesse != size:
                raise ablage.AblageFehler(f"{p.name}: Größe auf der Nextcloud stimmt nicht, lokal behalten")
            dateien.in_zwischenspeicher(p, rel)
            bericht["hoch"] += 1
    # 3. Katalog
    remote_files = None
    if remote_dir is not None and remote_dir.etag != z.ordner.get(key):
        remote_files = {e.name: e for e in dav.liste(f"titel/{key}") or []}
    for name in dateien.KATALOG:
        p = folder / name
        rel = f"titel/{key}/{name}"
        if not p.exists() or not z.lokal_geaendert(rel, p):
            continue
        known = z.dateien.get(rel, {}).get("etag")
        if remote_files and name in remote_files and remote_files[name].etag != known:
            konflikt = f"{rel}.konflikt-{dateien.jetzt()}"
            dav.verschieben(rel, konflikt)
            bericht["konflikte"].append(konflikt)
        z.merken(rel, p, dav.ablegen(p, rel))
        bericht["hoch"] += 1


def _herunterladen(lib: Library, dav, z: _Zustand, bericht: dict, fortschritt: Fortschritt) -> None:
    remote = {e.name: e for e in (dav.liste("titel") or []) if e.ist_ordner}
    for key, entry in remote.items():
        if entry.etag == z.ordner.get(key):
            continue
        files = {e.name: e for e in dav.liste(f"titel/{key}") or [] if not e.ist_ordner}
        folder = lib.folder(key)
        changed = False
        for name in dateien.KATALOG:
            e = files.get(name)
            rel = f"titel/{key}/{name}"
            if not e or e.etag == z.dateien.get(rel, {}).get("etag"):
                continue
            fortschritt(f"{key}: {name} holen")
            dav.holen(rel, folder / name)
            z.merken(rel, folder / name, e.etag)
            bericht["runter"] += 1
            changed = True
        z.ordner[key] = entry.etag
        if changed:
            bericht["geholt"].append(key)


# --- Eingang ----------------------------------------------------------------------------------

def eingang_verarbeiten(lib: Library, dav, *, fortschritt: Fortschritt | None = None) -> list[str]:
    """Dateien und Fotoordner aus Bibliothek/Eingang aufnehmen; danach dort entfernen."""
    fortschritt = fortschritt or (lambda _: None)
    entries = dav.liste(EINGANG)
    if entries is None:
        dav.mkdirs(EINGANG)
        return []
    done: list[str] = []
    jetzt = time.time()
    for e in entries:
        if e.name in ("doppelt", "fehler") or e.name.startswith("."):
            continue
        if jetzt - e.geaendert < EINGANG_RUHE:
            continue   # wird vermutlich noch hochgeladen
        rel = f"{EINGANG}/{e.name}"
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / e.name
            try:
                if e.ist_ordner:
                    for f in dav.liste(rel) or []:
                        if not f.ist_ordner and Path(f.name).suffix.lower() in extract.IMAGE_SUFFIXES:
                            dav.holen(f"{rel}/{f.name}", local / f.name)
                    fortschritt(f"Eingang: Buchfotos „{e.name}“ per OCR erkennen …")
                    item = ingest.add_scans(lib, str(local), herkunft=f"Eingang/{e.name}", progress=fortschritt)
                else:
                    if Path(e.name).suffix.lower() not in extract.SUPPORTED:
                        continue
                    dav.holen(rel, local)
                    fortschritt(f"Eingang: {e.name} aufnehmen …")
                    item = ingest.add_file(lib, local, herkunft=f"Eingang/{e.name}", progress=fortschritt)
                dav.loeschen(rel)
                done.append(f"{e.name} → {item.citekey}")
            except ingest.Duplicate as d:
                dav.verschieben(rel, f"{EINGANG}/doppelt/{e.name}", ueberschreiben=True)
                done.append(f"{e.name}: schon vorhanden als {d.item.citekey} (→ Eingang/doppelt)")
            except ablage.NichtErreichbar:
                raise
            except Exception as exc:
                dav.verschieben(rel, f"{EINGANG}/fehler/{e.name}", ueberschreiben=True)
                note = Path(tmp) / "grund.txt"
                note.write_text(f"{e.name}: {exc}\n", encoding="utf-8")
                dav.ablegen(note, f"{EINGANG}/fehler/{e.name}.fehler.txt")
                done.append(f"{e.name}: Fehler ({exc}) → Eingang/fehler")
    if done:
        index.update(lib)
        lib.write_refs_bib()
    return done


def status(lib: Library) -> dict:
    """Kurzer Überblick ohne Netzzugriff."""
    z = _Zustand(lib) if dateien.nas_aktiv() else None
    return {
        "nas": ablage.einstellungen() or None,
        "zuletzt": z.zuletzt if z else None,
        "ausstehend": dateien.ausstehend(lib) if z else None,
        "zwischenspeicher": dateien.zwischenspeicher_groesse(),
    }


def versuchen(lib: Library, fortschritt: Fortschritt | None = None) -> str | None:
    """Abgleich nach einer Änderung (Kommandozeile); still, wenn kein NAS oder offline."""
    if not dateien.nas_aktiv():
        return None
    try:
        b = abgleichen(lib, eingang=False, fortschritt=fortschritt)
    except Gesperrt:
        return "Abgleich läuft schon (App) — Änderungen werden dort übernommen."
    except ablage.AblageFehler as exc:
        return f"Abgleich: {exc}"
    if b["status"] == "andere Bibliothek":
        return None
    if b["status"] == "offline":
        return "Nextcloud nicht erreichbar — Änderungen bleiben vorgemerkt."
    return f"Abgleich: {b['hoch']} hoch, {b['runter']} runter" + (f", Konflikte: {b['konflikte']}" if b["konflikte"] else "")
