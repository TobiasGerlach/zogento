"""Nextcloud-Attrappe im Speicher, mit derselben Schnittstelle wie ablage.WebDAV.

Wie Nextcloud ändern sich Ordner-ETags, sobald sich etwas darin ändert.
"""
from __future__ import annotations

import itertools
import time
from pathlib import Path, PurePosixPath

from bibliothek.ablage import AblageFehler, Eintrag, NichtErreichbar

_zaehler = itertools.count(1)


class FakeDAV:
    def __init__(self):
        self.files: dict[str, tuple[bytes, str, float]] = {}   # rel → (inhalt, etag, zeit)
        self.dirs: dict[str, str] = {"": "d0"}                 # rel → etag
        self.online = True
        self.uploads = 0

    # Hilfen
    def _check(self):
        if not self.online:
            raise NichtErreichbar("offline")

    @staticmethod
    def _norm(rel: str) -> str:
        return str(PurePosixPath(rel)).strip("/") if rel not in ("", ".") else ""

    def _touch(self, rel: str):
        parts = PurePosixPath(rel).parts
        for i in range(len(parts)):
            d = "/".join(parts[:i])
            self.dirs[d] = f"d{next(_zaehler)}"

    def put_bytes(self, rel: str, data: bytes, alter: float = 0):
        """Für Tests: Datei „von einem anderen Rechner“ ablegen."""
        rel = self._norm(rel)
        self.mkdirs(str(PurePosixPath(rel).parent))
        self.files[rel] = (data, f"f{next(_zaehler)}", time.time() - alter)
        self._touch(rel)

    # Schnittstelle
    def erreichbar(self) -> bool:
        return self.online

    def liste(self, rel: str):
        self._check()
        rel = self._norm(rel)
        if rel not in self.dirs:
            return None
        prefix = rel + "/" if rel else ""
        out = []
        for d, etag in self.dirs.items():
            if d.startswith(prefix) and d != rel and "/" not in d[len(prefix):]:
                out.append(Eintrag(d[len(prefix):], True, 0, etag, time.time() - 1000))
        for f, (data, etag, t) in self.files.items():
            if f.startswith(prefix) and "/" not in f[len(prefix):]:
                out.append(Eintrag(f[len(prefix):], False, len(data), etag, t))
        return out

    def info(self, rel: str):
        parent, _, name = self._norm(rel).rpartition("/")
        return next((e for e in self.liste(parent) or [] if e.name == name), None)

    def holen(self, rel: str, ziel: Path):
        self._check()
        rel = self._norm(rel)
        if rel not in self.files:
            raise FileNotFoundError(rel)
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(self.files[rel][0])

    def mkdirs(self, rel: str):
        self._check()
        parts = [p for p in PurePosixPath(rel or ".").parts if p not in ("/", ".")]
        for i in range(len(parts)):
            d = "/".join(parts[:i + 1])
            if d not in self.dirs:
                self.dirs[d] = f"d{next(_zaehler)}"
                self._touch(d)

    def ablegen(self, quelle: Path, rel: str) -> str:
        self._check()
        rel = self._norm(rel)
        self.mkdirs(str(PurePosixPath(rel).parent))
        etag = f"f{next(_zaehler)}"
        self.files[rel] = (quelle.read_bytes(), etag, time.time())
        self._touch(rel)
        self.uploads += 1
        return etag

    def loeschen(self, rel: str):
        self._check()
        rel = self._norm(rel)
        self.files = {k: v for k, v in self.files.items() if k != rel and not k.startswith(rel + "/")}
        self.dirs = {k: v for k, v in self.dirs.items() if k != rel and not k.startswith(rel + "/")}
        self._touch(rel)

    def verschieben(self, von: str, nach: str, *, ueberschreiben: bool = False):
        self._check()
        von, nach = self._norm(von), self._norm(nach)
        if von not in self.files and von not in self.dirs:
            raise AblageFehler(f"MOVE {von}: 404")
        self.mkdirs(str(PurePosixPath(nach).parent))
        for k in [k for k in self.files if k == von or k.startswith(von + "/")]:
            self.files[nach + k[len(von):]] = self.files.pop(k)
        for k in [k for k in self.dirs if k == von or k.startswith(von + "/")]:
            self.dirs[nach + k[len(von):]] = self.dirs.pop(k)
        self._touch(von)
        self._touch(nach)

    def ordner_hochladen(self, lokal: Path, rel: str, *, fortschritt=None) -> int:
        n = 0
        for f in sorted(p for p in lokal.rglob("*") if p.is_file()):
            self.ablegen(f, f"{rel}/{f.relative_to(lokal).as_posix()}")
            n += 1
        return n
