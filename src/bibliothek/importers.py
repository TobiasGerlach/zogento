"""Bestehende Bestände übernehmen: BibTeX-Dateien, Zotero, fertige OCR-Ausgaben.

Dubletten werden zusammengeführt statt doppelt angelegt: Hat ein vorhandener
Titel noch keinen Text, bekommt er den Text der neuen Quelle.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable

from . import bibtex, dateien, extract, index, ingest, ocr
from .store import Item, Library

Progress = Callable[[str], None]


def _has_text(lib: Library, key: str) -> bool:
    return (lib.folder(key) / "seiten.jsonl").exists()


def _attach_file(lib: Library, item: Item, path: Path, progress: Progress) -> None:
    """Datei an einen vorhandenen Titel hängen; ohne Text wird sie zur Textquelle."""
    folder = lib.folder(item.citekey)
    target = folder / ingest._safe_name(path.name)
    if target.exists() and target.stat().st_size == path.stat().st_size:
        return
    n = 2
    while target.exists():
        target = folder / f"{path.stem}-{n}{path.suffix}"
        n += 1
    shutil.copy2(path, target)
    item.dateien.append(target.name)
    item.text.setdefault("sha256", [])
    if ingest.pruefsumme(target) not in item.text["sha256"]:
        item.text["sha256"].append(ingest.pruefsumme(target))
    lib.save(item, render=False)
    if not _has_text(lib, item.citekey):
        _text_from_file(lib, item, target, progress)


def _text_from_file(lib: Library, item: Item, path: Path, progress: Progress) -> None:
    try:
        ex = extract.extract(path)
    except extract.ExtractError as exc:
        progress(f"  kein Text aus {path.name}: {exc}")
        return
    pages = ex.pages
    hinweise = list(ex.hinweise)
    item.text = {"quelle": ex.quelle, "sha256": item.text.get("sha256", [])}
    if ex.ocr_noetig:
        try:
            out = dateien.ocr_workdir(lib, item.citekey)
            if ex.quelle == "bild":
                pages = ingest._ocr_single_image(path, out, progress)
            else:
                progress(f"  OCR für {len(ex.ocr_noetig)} Seite(n)")
                rec = ocr.ocr_pdf_pages(path, ex.ocr_noetig, out, progress=lambda _: None)
                for p in pages:
                    if p.scan in rec:
                        p.text, p.konfidenz = rec[p.scan].text, rec[p.scan].konfidenz
                item.text["ocr_ordner"] = str(out)
            item.text["quelle"] = "ocr" if len(ex.ocr_noetig) == len(pages) else f"{ex.quelle}+ocr"
            hinweise.append("Text per OCR erkannt — Zitate am Original prüfen.")
        except Exception as exc:
            hinweise.append(f"OCR fehlgeschlagen: {exc}")
    item.text["hinweise"] = hinweise
    ingest._numbering(item, pages, ex.paginiert, any(p.label for p in pages))
    lib.write_pages(item, pages)


# --- BibTeX ----------------------------------------------------------------------------

def import_bibtex(lib: Library, path: Path, *, texte: Path | None = None, tags: list[str] | None = None,
                  progress: Progress | None = None) -> dict:
    """Einträge übernehmen; liegen in `texte` Dateien namens <citekey>.*, werden sie angehängt."""
    progress = progress or print
    entries = bibtex.parse(Path(path).read_text(encoding="utf-8"))
    stats = {"neu": 0, "vorhanden": 0, "texte": 0}
    section = ""
    for e in entries:
        # Abschnittskommentare (mit ---- eingerahmt) gelten für alle folgenden Einträge,
        # ein Kommentarblock direkt vor dem Eintrag nur für diesen.
        blocks = [b for b in re.split(r"\n\s*\n", e.comment) if b.strip()]
        is_section = [bool(re.search(r"^-{10,}", b, re.M)) for b in blocks]
        if any(is_section):
            section = blocks[max(i for i, s in enumerate(is_section) if s)]
        own = blocks[-1] if blocks and not is_section[-1] else ""
        existing = lib.get(e.key) if lib.exists(e.key) else lib.find_duplicate(e.fields)
        if existing:
            stats["vorhanden"] += 1
            item = existing
            progress(f"= {e.key} (schon da als {item.citekey})")
        else:
            kommentar = "\n\n".join(x for x in (section.strip("-\n "), own.strip()) if x)
            item = Item(citekey=e.key, eintrag=e.entrytype, felder=e.fields, tags=list(tags or []),
                        kommentar=_clean_comment(kommentar), herkunft=str(path),
                        unvollstaendig=[f for f in ingest.PFLICHT if not e.fields.get(f)
                                        and not (f == "author" and e.fields.get("editor"))
                                        and not (f == "year" and e.fields.get("date"))])
            item.seitenzaehlung = {"status": "unbekannt"}
            lib.save(item)
            stats["neu"] += 1
            progress(f"+ {e.key}")
        if texte:
            for f in sorted(Path(texte).glob(f"{e.key}.*"), key=lambda p: p.suffix != ".pdf"):
                if f.suffix.lower() in extract.SUPPORTED:
                    _attach_file(lib, item, f, progress)
                    stats["texte"] += 1
                    break
    return stats


def _clean_comment(text: str) -> str:
    lines = [l for l in text.splitlines() if not re.fullmatch(r"-{5,}", l.strip())]
    return "\n".join(lines).strip()


# --- Fertige OCR-Ausgaben ------------------------------------------------------------------

def import_ocr_dir(lib: Library, ocr_dir: Path, *, felder: dict[str, str] | None = None,
                   progress: Progress | None = None) -> Item:
    """OCR-Ausgabe übernehmen; passt sie zu einem Titel ohne Text, wird sie dort eingehängt."""
    progress = progress or print
    pages, summary = ocr.pages_from_report(Path(ocr_dir))
    probe = dict(felder or {}) or {"title": summary.get("titel") or Path(ocr_dir).name}
    dup = lib.find_duplicate(probe)
    if dup and not _has_text(lib, dup.citekey):
        dup.text = {"quelle": "ocr", "ocr_ordner": str(Path(ocr_dir).resolve()), "konfidenz": summary["konfidenz"],
                    "ocr_warnungen": summary["warnungen"],
                    "hinweise": ["Text per OCR erkannt — Zitate am Scan prüfen (review.html im OCR-Ordner)."]}
        ingest._numbering(dup, pages, True, False)
        lib.write_pages(dup, pages)
        progress(f"~ OCR-Text an {dup.citekey} gehängt")
        return dup
    if dup:
        raise ingest.Duplicate(dup)
    item = ingest.add_ocr_output(lib, Path(ocr_dir), felder=felder, reindex=False)
    progress(f"+ {item.citekey} (aus OCR)")
    return item


# --- Zotero ------------------------------------------------------------------------------------

ZOTERO_TYPES = {
    "journalArticle": "article", "magazineArticle": "article", "newspaperArticle": "article",
    "book": "book", "bookSection": "incollection", "thesis": "thesis", "report": "report",
    "webpage": "online", "blogPost": "online", "conferencePaper": "inproceedings",
    "document": "misc", "presentation": "misc", "encyclopediaArticle": "inreference",
}
ZOTERO_FIELDS = {
    "title": "title", "publisher": "publisher", "place": "location", "volume": "volume",
    "issue": "number", "pages": "pages", "DOI": "doi", "ISBN": "isbn", "ISSN": "issn",
    "url": "url", "edition": "edition", "series": "series", "seriesNumber": "number",
    "websiteTitle": "organization", "blogTitle": "organization",
}


def default_zotero_dir() -> Path:
    return Path.home() / "Zotero"


def import_zotero(lib: Library, zotero_dir: Path | None = None, *, progress: Progress | None = None) -> dict:
    progress = progress or print
    zdir = Path(zotero_dir or default_zotero_dir())
    db = zdir / "zotero.sqlite"
    if not db.exists():
        raise FileNotFoundError(f"Keine Zotero-Datenbank unter {db}")
    # immutable=1: liest auch, während Zotero läuft, und schreibt garantiert nichts
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    stats = {"neu": 0, "zusammengefuehrt": 0, "dateien": 0, "ohne_eltern": 0}
    with closing(con):
        deleted = {r[0] for r in con.execute("SELECT itemID FROM deletedItems")}
        types = dict(con.execute("SELECT itemTypeID, typeName FROM itemTypes"))
        items = [(i, types[t], k) for i, t, k in con.execute("SELECT itemID, itemTypeID, key FROM items")
                 if i not in deleted]
        fields = {}
        for iid, name, value in con.execute(
                "SELECT d.itemID, f.fieldName, v.value FROM itemData d JOIN fields f USING(fieldID) "
                "JOIN itemDataValues v USING(valueID)"):
            fields.setdefault(iid, {})[name] = value
        creators = {}
        for iid, first, last, mode, ctype in con.execute(
                "SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode, ct.creatorType FROM itemCreators ic "
                "JOIN creators c USING(creatorID) JOIN creatorTypes ct USING(creatorTypeID) ORDER BY ic.orderIndex"):
            name = "{" + last + "}" if mode == 1 or not first else f"{last}, {first}"
            creators.setdefault(iid, {}).setdefault(ctype, []).append(name)
        tags = {}
        for iid, name in con.execute("SELECT it.itemID, t.name FROM itemTags it JOIN tags t USING(tagID)"):
            tags.setdefault(iid, []).append(name)
        colls = {cid: (name, parent) for cid, name, parent in
                 con.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")}

        def coll_path(cid):
            parts = []
            while cid:
                name, cid = colls[cid]
                parts.insert(0, name)
            return "/".join(parts)

        cats = {}
        for cid, iid in con.execute("SELECT collectionID, itemID FROM collectionItems"):
            cats.setdefault(iid, []).append(coll_path(cid))
        attachments = {}
        for aid, parent, mode, ctype, path in con.execute(
                "SELECT itemID, parentItemID, linkMode, contentType, path FROM itemAttachments"):
            if aid in deleted:
                continue
            key = next((k for i, _, k in items if i == aid), None)
            file = None
            if path and path.startswith("storage:") and key:
                file = zdir / "storage" / key / path[len("storage:"):]
            elif path and mode == 2:
                file = Path(path)
            attachments.setdefault(parent, []).append((aid, ctype, file))
        notes = {}
        for parent, text, comment, label in con.execute(
                "SELECT parentItemID, text, comment, pageLabel FROM itemAnnotations ORDER BY parentItemID, sortIndex"):
            notes.setdefault(parent, []).append((text, comment, label))

    def files_of(iid):
        out = []
        for aid, ctype, file in attachments.get(iid, []):
            if file and file.exists():
                out.append((aid, file))
        # PDFs zuerst, HTML-Schnappschüsse nur als Ersatz
        return sorted(out, key=lambda x: (x[1].suffix.lower() != ".pdf", x[1].name))

    regular = [(i, t) for i, t, _ in items if t not in ("attachment", "annotation", "note")]
    for iid, ztype in regular:
        f = fields.get(iid, {})
        felder = {ZOTERO_FIELDS[k]: v for k, v in f.items() if k in ZOTERO_FIELDS}
        entry_type = ZOTERO_TYPES.get(ztype, "misc")
        if f.get("publicationTitle"):
            felder["booktitle" if entry_type == "incollection" else "journaltitle"] = f["publicationTitle"]
        year = re.search(r"\d{4}", f.get("date", ""))
        if year:
            felder["year"] = year.group(0)
        if f.get("accessDate"):
            felder["urldate"] = f["accessDate"][:10]
        cr = creators.get(iid, {})
        if cr.get("author"):
            felder["author"] = " and ".join(cr["author"])
        if cr.get("editor"):
            felder["editor"] = " and ".join(cr["editor"])
        if cr.get("translator"):
            felder["translator"] = " and ".join(cr["translator"])
        if ztype == "blogPost" or ztype == "webpage":
            entry_type = "online"
        files = files_of(iid)
        dup = lib.find_duplicate(felder)
        if dup:
            item = dup
            stats["zusammengefuehrt"] += 1
            progress(f"= {item.citekey} ← Zotero „{felder.get('title', '')[:50]}“")
        else:
            key = lib.new_citekey(felder)
            item = Item(citekey=key, eintrag=entry_type, felder=felder, herkunft=f"Zotero {iid}",
                        unvollstaendig=[x for x in ingest.PFLICHT if not felder.get(x)
                                        and not (x == "author" and felder.get("editor"))])
            stats["neu"] += 1
            progress(f"+ {key}")
        item.tags = sorted(set(item.tags) | set(tags.get(iid, [])))
        item.kategorien = sorted(set(item.kategorien) | set(cats.get(iid, [])))
        if f.get("abstractNote") and "Zusammenfassung" not in item.notiz:
            item.notiz = (item.notiz + "\n\n" if item.notiz else "") + "Zusammenfassung (Zotero):\n" + f["abstractNote"]
        lib.save(item, render=False)
        for aid, file in files:
            _attach_file(lib, item, file, progress)
            stats["dateien"] += 1
            _write_annotations(lib, item, file.name, notes.get(aid, []))

    # Lose Anhänge ohne Elterneintrag: als eigene Titel mit unvollständigen Metadaten
    for aid, ctype, file in attachments.get(None, []):
        if not file or not file.exists():
            continue
        try:
            item = ingest.add_file(lib, file, reindex=False, herkunft=f"Zotero-Anhang {aid}", progress=lambda _: None)
            stats["ohne_eltern"] += 1
            progress(f"+ {item.citekey} (loser Anhang {file.name}, Metadaten prüfen)")
            _write_annotations(lib, item, file.name, notes.get(aid, []))
        except ingest.Duplicate as d:
            progress(f"= {file.name} schon da als {d.item.citekey}")
        except extract.ExtractError as exc:
            progress(f"! {file.name}: {exc}")
    return stats


def _write_annotations(lib: Library, item: Item, filename: str, notes: list) -> None:
    if not notes:
        return
    path = lib.folder(item.citekey) / "notizen.md"
    lines = [path.read_text(encoding="utf-8").rstrip(), ""] if path.exists() else [f"# Notizen zu {item.citekey}", ""]
    lines.append(f"## Markierungen aus Zotero ({filename})")
    lines.append("")
    for text, comment, label in notes:
        where = f"S. {label} (laut PDF)" if label else "ohne Seite"
        entry = f"- {where}: „{(text or '').strip()}“" if text else f"- {where}:"
        if comment:
            entry += f" — {comment.strip()}"
        lines.append(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reindex_all(lib: Library, progress: Progress | None = None) -> dict:
    for item in lib.items():
        if _has_text(lib, item.citekey):
            lib.render_text(item)
    return index.update(lib, progress=progress)
