"""Suchindex: SQLite-FTS5 für Wörter, Embeddings für Bedeutung.

Der Index ist abgeleitet. Er wird aus titel/*/seiten.jsonl aufgebaut und kann
jederzeit gelöscht werden. Einheit ist der Abschnitt (chunk) einer Seite; jede
Fundstelle kennt ihren Titel und ihre Scan-Seite.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import config
from .store import Library

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(citekey TEXT PRIMARY KEY, signatur TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, citekey TEXT, scan INTEGER, pos INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_key ON chunks(citekey);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize="unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS emb(chunk_id INTEGER PRIMARY KEY, vec BLOB);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Hit:
    citekey: str
    scan: int
    text: str
    snippet: str = ""
    score: float = 0.0
    wege: set[str] = field(default_factory=set)   # "text", "semantisch"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


# --- Aufbau ------------------------------------------------------------------------

def chunk_text(text: str, size: int = config.CHUNK_CHARS, overlap: int = config.CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if len(para) <= size:
            pieces.append(para)
        else:
            pieces.extend(s for s in re.split(r"(?<=[.!?;:])\s+", para) if s)
    chunks, cur = [], ""
    for piece in pieces:
        while len(piece) > size:  # Notfall: harter Schnitt
            chunks.append((cur + " " + piece[:size]).strip())
            cur, piece = "", piece[size - overlap:]
        if cur and len(cur) + len(piece) + 1 > size:
            chunks.append(cur.strip())
            cur = cur[-overlap:].split(" ", 1)[-1] + " "
        cur += piece + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _signature(lib: Library, key: str) -> str:
    path = lib.folder(key) / "seiten.jsonl"
    st = path.stat() if path.exists() else None
    return f"{st.st_mtime_ns}:{st.st_size}" if st else "leer"


def update(lib: Library, *, keys: Iterable[str] | None = None, rebuild: bool = False,
           progress: Callable[[str], None] | None = None) -> dict:
    progress = progress or (lambda _: None)
    changed = removed = 0
    with closing(connect(lib.index_path)) as con:
        if rebuild:
            con.executescript("DELETE FROM chunks; DELETE FROM docs; DELETE FROM emb;"
                              "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild');")
        known = dict(con.execute("SELECT citekey, signatur FROM docs"))
        present = set(lib.keys())
        neu = {k: _signature(lib, k) for k in present - set(known)}
        vorrat: dict[str, bytes] = {}   # Text → Embedding: bei unverändertem Text nicht neu rechnen
        for key in set(known) - present:
            ziel = next((k for k, sig in neu.items() if sig == known[key] and sig != "leer"), None)
            if ziel:   # umbenannt: Abschnitte samt Embeddings behalten
                con.execute("UPDATE chunks SET citekey=? WHERE citekey=?", (ziel, key))
                con.execute("UPDATE docs SET citekey=? WHERE citekey=?", (ziel, key))
                known[ziel] = known.pop(key)
                del neu[ziel]
                continue
            vorrat.update(_drop(con, key))
            removed += 1
        for key in (keys or sorted(present)):
            sig = _signature(lib, key)
            if known.get(key) == sig:
                continue
            vorrat.update(_drop(con, key))
            for page in lib.pages(key):
                for pos, chunk in enumerate(chunk_text(page.text)):
                    cur = con.execute("INSERT INTO chunks(citekey, scan, pos, text) VALUES(?,?,?,?)",
                                      (key, page.scan, pos, chunk))
                    con.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, chunk))
                    if chunk in vorrat:
                        con.execute("INSERT INTO emb VALUES(?,?)", (cur.lastrowid, vorrat[chunk]))
            con.execute("INSERT OR REPLACE INTO docs VALUES(?,?)", (key, sig))
            changed += 1
            progress(f"Index: {key}")
        con.commit()
    _matrix_cache.clear()
    return {"aktualisiert": changed, "entfernt": removed}


def _drop(con: sqlite3.Connection, key: str) -> dict[str, bytes]:
    """Abschnitte eines Titels entfernen; gibt Text → Embedding der entfernten Abschnitte zurück."""
    vecs = dict(con.execute("SELECT c.text, e.vec FROM chunks c JOIN emb e ON e.chunk_id = c.id WHERE c.citekey=?",
                            (key,)))
    rows = con.execute("SELECT id, text FROM chunks WHERE citekey=?", (key,)).fetchall()
    for cid, text in rows:
        con.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", (cid, text))
        con.execute("DELETE FROM emb WHERE chunk_id=?", (cid,))
    con.execute("DELETE FROM chunks WHERE citekey=?", (key,))
    con.execute("DELETE FROM docs WHERE citekey=?", (key,))
    return vecs


# --- Embeddings ------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()


def model():
    global _model
    with _model_lock:
        if _model is None:
            import logging
            import os

            # Ladebalken und Torch-Hinweise gehören nicht in Suchausgaben
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            logging.getLogger("torch.distributed.elastic.multiprocessing.redirects").setLevel(logging.ERROR)
            import torch
            from sentence_transformers import SentenceTransformer

            device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
            try:  # liegt das Modell schon im Cache, ohne Netz laden
                _model = SentenceTransformer(config.EMBED_MODEL, device=device, local_files_only=True)
            except (OSError, ValueError):  # erster Gebrauch: einmal herunterladen (~1,2 GB)
                _model = SentenceTransformer(config.EMBED_MODEL, device=device)
        return _model


def embed_missing(lib: Library, *, batch: int = 16, progress: Callable[[str], None] | None = None) -> int:
    progress = progress or (lambda _: None)
    with closing(connect(lib.index_path)) as con:
        stored = con.execute("SELECT value FROM meta WHERE key='modell'").fetchone()
        if stored and stored[0] != config.EMBED_MODEL:
            con.execute("DELETE FROM emb")
        con.execute("INSERT OR REPLACE INTO meta VALUES('modell', ?)", (config.EMBED_MODEL,))
        con.commit()
        todo = con.execute("SELECT c.id, c.text FROM chunks c LEFT JOIN emb e ON e.chunk_id=c.id "
                           "WHERE e.chunk_id IS NULL ORDER BY c.id").fetchall()
        if not todo:
            return 0
        m = model()
        for start in range(0, len(todo), batch):
            part = todo[start:start + batch]
            vecs = m.encode([t for _, t in part], normalize_embeddings=True, batch_size=batch)
            con.executemany("INSERT OR REPLACE INTO emb VALUES(?,?)",
                            [(cid, np.asarray(v, dtype=np.float16).tobytes()) for (cid, _), v in zip(part, vecs)])
            con.commit()
            progress(f"Embeddings: {min(start + batch, len(todo))}/{len(todo)}")
    _matrix_cache.clear()
    return len(todo)


_matrix_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
_matrix_lock = threading.Lock()


def _matrix(con: sqlite3.Connection, db: str) -> tuple[np.ndarray, np.ndarray]:
    with _matrix_lock:
        if db not in _matrix_cache:
            rows = con.execute("SELECT chunk_id, vec FROM emb ORDER BY chunk_id").fetchall()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            mat = (np.vstack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(np.float32)
                   if rows else np.zeros((0, 1), dtype=np.float32))
            _matrix_cache[db] = (ids, mat)
        return _matrix_cache[db]


# --- Suche ------------------------------------------------------------------------------

def fts_query(q: str) -> str:
    """Benutzereingabe in FTS5-Syntax: Wörter als Präfix (Gattung → Gattungen), Phrasen in "…"."""
    parts = re.findall(r'"[^"]+"|\S+', q)
    out = []
    for p in parts:
        if p.startswith('"'):
            out.append(p)
        elif p in ("OR", "NOT", "AND"):
            out.append(p)
        else:
            word = re.sub(r"[^\w]", "", p)
            if word:
                out.append(f'"{word}"*' if len(word) >= 4 else f'"{word}"')
    return " ".join(out)


def search_text(lib: Library, q: str, *, limit: int = 50, keys: set[str] | None = None, raw: bool = False) -> list[Hit]:
    with closing(connect(lib.index_path)) as con:
        try:
            rows = con.execute(
                "SELECT c.citekey, c.scan, c.text, snippet(chunks_fts, 0, '«', '»', ' … ', 30), bm25(chunks_fts) "
                "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (q if raw else fts_query(q), limit * 4 if keys is not None else limit)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Suchanfrage nicht verstanden: {exc}") from exc
    hits = [Hit(k, s, t, sn, -score, {"text"}) for k, s, t, sn, score in rows if keys is None or k in keys]
    return hits[:limit]


def search_semantic(lib: Library, q: str, *, limit: int = 50, keys: set[str] | None = None) -> list[Hit]:
    db = str(lib.index_path)
    with closing(connect(Path(db))) as con:
        ids, mat = _matrix(con, db)
        if not len(ids):
            return []
        qv = model().encode([config.EMBED_QUERY_PROMPT + q], normalize_embeddings=True)[0].astype(np.float32)
        scores = mat @ qv
        order = np.argsort(-scores)
        hits: list[Hit] = []
        for i in order[: limit * 8]:
            row = con.execute("SELECT citekey, scan, text FROM chunks WHERE id=?", (int(ids[i]),)).fetchone()
            if row and (keys is None or row[0] in keys):
                hits.append(Hit(row[0], row[1], row[2], _excerpt(row[2]), float(scores[i]), {"semantisch"}))
            if len(hits) >= limit:
                break
    return hits


def search(lib: Library, q: str, *, mode: str = "hybrid", limit: int = 10, keys: set[str] | None = None) -> list[Hit]:
    """mode: hybrid (Standard), text, semantisch. Ergebnis: eine Fundstelle pro Seite."""
    if mode == "text":
        ranked = [search_text(lib, q, limit=limit * 3, keys=keys)]
    elif mode == "semantisch":
        ranked = [search_semantic(lib, q, limit=limit * 3, keys=keys)]
    else:
        ranked = [search_text(lib, q, limit=60, keys=keys), search_semantic(lib, q, limit=60, keys=keys)]
    fused: dict[tuple[str, int], Hit] = {}
    for hits in ranked:
        for rank, h in enumerate(hits):
            k = (h.citekey, h.scan)
            score = 1.0 / (60 + rank)
            if k in fused:
                fused[k].score += score
                fused[k].wege |= h.wege
                if "text" in h.wege:
                    fused[k].snippet = h.snippet
            else:
                h.score = score
                fused[k] = h
    return sorted(fused.values(), key=lambda h: -h.score)[:limit]


def _excerpt(text: str, n: int = 320) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def stats(lib: Library) -> dict:
    with closing(connect(lib.index_path)) as con:
        return {
            "titel": con.execute("SELECT count(*) FROM docs").fetchone()[0],
            "abschnitte": con.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "embeddings": con.execute("SELECT count(*) FROM emb").fetchone()[0],
        }
