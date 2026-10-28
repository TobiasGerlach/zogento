"""Titel aufnehmen: Datei, Scan-Ordner, Immich-Album oder vorhandene OCR-Ausgabe.

Grundsatz: Metadaten werden nie geraten. Was nicht aus der Datei oder vom
Aufrufer kommt, landet in `unvollstaendig` und wird so angezeigt.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import bibtex, dateien, extract, index, ocr, pagenums, teilung
from .store import (STATUS_AUTO, STATUS_KEINE, STATUS_PDF, STATUS_UNBEKANNT, Item, Library, Page)

Progress = Callable[[str], None]
PFLICHT = ("author", "title", "year")


class Duplicate(Exception):
    def __init__(self, item: Item):
        super().__init__(f"Schon vorhanden als {item.citekey} ({item.short}: {item.title})")
        self.item = item


def _prepare(lib: Library, felder: dict[str, str], citekey: str | None, *, allow_duplicate: bool) -> tuple[str, list[str]]:
    if not allow_duplicate:
        dup = lib.find_duplicate(felder)
        if dup:
            raise Duplicate(dup)
    missing = [f for f in PFLICHT if not felder.get(f) and not (f == "author" and felder.get("editor"))
               and not (f == "year" and felder.get("date"))]
    key = citekey or lib.new_citekey(felder)
    if lib.exists(key):
        raise ValueError(f"Schlüssel {key} ist schon vergeben")
    return key, missing


def _numbering(item: Item, pages: list[Page], paginiert: bool, has_labels: bool) -> None:
    if not paginiert:
        item.seitenzaehlung = {"status": STATUS_KEINE}
        return
    if has_labels:
        item.seitenzaehlung = {"status": STATUS_PDF}
        return
    guess = pagenums.detect(pages)
    if guess:
        item.seitenzaehlung = {"status": STATUS_AUTO, "segmente": guess["segmente"],
                               "schaetzung": f"{guess['treffer']} Seiten tragen die erwartete Zahl "
                                             f"({int(guess['quote'] * 100)} %)"}
        if guess["spruenge"]:
            item.seitenzaehlung["spruenge"] = guess["spruenge"]
    else:
        item.seitenzaehlung = {"status": STATUS_UNBEKANNT}


def _finish(lib: Library, item: Item, pages: list[Page], *, reindex: bool) -> Item:
    lib.write_pages(item, pages)   # speichert auch meta.yaml und text.md
    if reindex:
        index.update(lib, keys=[item.citekey])
    return item


def add_file(lib: Library, path: Path, *, felder: dict[str, str] | None = None, eintrag: str | None = None,
             citekey: str | None = None, tags: list[str] | None = None, kategorien: list[str] | None = None,
             projekte: list[str] | None = None, run_ocr: bool = True, allow_duplicate: bool = False,
             reindex: bool = True, herkunft: str = "", progress: Progress | None = None,
             teilung_art: str | None = None) -> Item:
    """teilung_art: siehe teilung.ARTEN; None = bei PDFs automatisch (Doppelseiten, wenn die Zählung um 2 springt)."""
    progress = progress or (lambda _: None)
    path = Path(path).expanduser().resolve()
    summe = pruefsumme(path)
    if not allow_duplicate:
        gleich = next((it for it in lib.items() if summe in (it.text.get("sha256") or [])), None)
        if gleich:
            raise Duplicate(gleich)
    progress(f"Lese {path.name}")
    ex = extract.extract(path)
    felder = dict(felder or {})
    hinweise = list(ex.hinweise)
    if not felder.get("title"):
        felder["title"], hinweis = guess_title(ex.meta.get("title"), path)
        hinweise.append(hinweis)
    if not felder.get("author") and ex.meta.get("author"):
        # Dateieigenschaften nennen meist den Benutzer am Scanner oder in Word, nicht den Autor
        hinweise.append(f"Dateieigenschaften nennen „{ex.meta['author'].strip()}“ als Autor — nicht übernommen.")
    key, missing = _prepare(lib, felder, citekey, allow_duplicate=allow_duplicate)

    item = Item(citekey=key, eintrag=eintrag or _guess_type(ex), felder=felder, tags=tags or [],
                kategorien=kategorien or [], projekte=projekte or [], unvollstaendig=missing,
                herkunft=herkunft or str(path))
    folder = lib.folder(key)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _safe_name(path.name)
    shutil.copy2(path, target)
    item.dateien = [target.name]
    item.text = {"quelle": ex.quelle, "sha256": [summe]}

    pages = ex.pages
    teile, einheit = teilung.ARTEN.get(teilung_art or "aus", (1, "S."))
    if ex.quelle == "pdf" and teile > 1:
        ex = extract.extract_pdf(target, teile)
        pages = ex.pages
    if ex.ocr_noetig and run_ocr:
        try:
            if ex.quelle == "bild":
                pages = _ocr_single_image(target, dateien.ocr_workdir(lib, key), progress)
                item.text["quelle"] = "ocr"
                item.text["ocr_ordner"] = str(dateien.ocr_workdir(lib, key))
            else:
                progress(f"OCR für {len(ex.ocr_noetig)} Seite(n) ohne Textebene")
                recognized = ocr.ocr_pdf_pages(target, ex.ocr_noetig, dateien.ocr_workdir(lib, key), progress=progress,
                                               teile=teile)
                for p in pages:
                    if p.scan in recognized:
                        p.text, p.konfidenz = recognized[p.scan].text, recognized[p.scan].konfidenz
                item.text["quelle"] = "ocr" if len(ex.ocr_noetig) == len(pages) else "pdf+ocr"
                item.text["ocr_ordner"] = str(dateien.ocr_workdir(lib, key))
            conf = [p.konfidenz for p in pages if p.konfidenz is not None]
            if conf:
                item.text["konfidenz"] = round(sum(conf) / len(conf), 1)
            hinweise.append("Text per OCR erkannt — Zitate am Original prüfen.")
        except Exception as exc:  # OCR-Probleme sollen die Aufnahme nicht verhindern
            hinweise.append(f"OCR fehlgeschlagen: {exc}")
    elif ex.ocr_noetig:
        hinweise.append(f"{len(ex.ocr_noetig)} Seite(n) ohne Text, OCR übersprungen.")
    item.text["hinweise"] = hinweise
    if teile > 1:
        item.text["teilung"] = {"teile": teile, "art": teilung_art}
    has_labels = any(p.label for p in pages)
    _numbering(item, pages, ex.paginiert, has_labels)
    if einheit != "S.":
        item.seitenzaehlung["einheit"] = einheit
    if teilung_art is None and ex.quelle == "pdf" and pagenums.schritt(pages) == 2:
        # Zählung springt pro Scan um 2: kopierte Doppelseiten — getrennt neu erfassen
        progress("Doppelseiten erkannt — jede Seite wird ein eigener Abschnitt")
        item = _finish(lib, item, pages, reindex=False)
        item = teilen(lib, item, "doppelseiten", run_ocr=run_ocr, reindex=reindex, progress=progress)
        item.text.setdefault("hinweise", []).append("Doppelseiten erkannt und getrennt (bib teilen, falls falsch).")
        lib.save(item)
        return item
    return _finish(lib, item, pages, reindex=reindex)


def add_ocr_output(lib: Library, ocr_dir: Path, *, felder: dict[str, str] | None = None, eintrag: str = "book",
                   citekey: str | None = None, tags: list[str] | None = None, kategorien: list[str] | None = None,
                   projekte: list[str] | None = None, allow_duplicate: bool = False, reindex: bool = True,
                   herkunft: str = "") -> Item:
    """Fertige Ausgabe des Buch-OCR übernehmen. Der OCR-Ordner bleibt, wo er ist (Scans sind groß)."""
    ocr_dir = Path(ocr_dir).expanduser().resolve()
    pages, summary = ocr.pages_from_report(ocr_dir)
    felder = dict(felder or {})
    hinweise = ["Text per OCR erkannt — Zitate am Scan prüfen (review.html im OCR-Ordner)."]
    if not felder.get("title"):
        felder["title"] = summary.get("titel") or ocr_dir.name
        hinweise.append("Titel aus dem OCR-Album — prüfen.")
    if not summary["vollstaendig"]:
        hinweise.append("OCR-Lauf unvollständig: einzelne Scans fehlen.")
    key, missing = _prepare(lib, felder, citekey, allow_duplicate=allow_duplicate)
    item = Item(citekey=key, eintrag=eintrag, felder=felder, tags=tags or [], kategorien=kategorien or [],
                projekte=projekte or [], unvollstaendig=missing, herkunft=herkunft or str(ocr_dir))
    item.text = {"quelle": "ocr", "ocr_ordner": str(ocr_dir), "konfidenz": summary["konfidenz"],
                 "ocr_warnungen": summary["warnungen"], "hinweise": hinweise}
    _numbering(item, pages, True, False)
    return _finish(lib, item, pages, reindex=reindex)


def add_scans(lib: Library, source: str, *, single_page: bool = False, order: str = "filename",
              progress: Progress | None = None, **kw) -> Item:
    """OCR starten (Ordner mit Fotos oder Immich-Link) und das Ergebnis aufnehmen."""
    progress = progress or (lambda _: None)
    work = dateien.ocr_workdir(lib, "lauf-" + bibtex.ascii_slug(Path(source).name if "://" not in source else source)[-24:])
    if "://" in source:
        ocr.ocr_album(source, work, progress=progress)
    else:
        ocr.ocr_folder(Path(source).expanduser(), work, single_page=single_page, order=order, progress=progress)
    item = add_ocr_output(lib, work, herkunft=source, **kw)
    final = dateien.ocr_workdir(lib, item.citekey)
    if not final.exists():
        work.rename(final)
        item.text["ocr_ordner"] = str(final)
        lib.save(item)
    return item


def _ocr_single_image(image: Path, out: Path, progress: Progress) -> list[Page]:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(image, Path(tmp) / image.name)
        ocr.ocr_folder(Path(tmp), out, single_page=True, progress=progress)
    pages, _ = ocr.pages_from_report(out)
    return pages


def reocr(lib: Library, item: Item, *, progress: Progress | None = None) -> Item:
    """Alle Seiten eines PDFs neu erkennen (z. B. schlechte eingebettete Textebene); Teilung bleibt."""
    art = (item.text.get("teilung") or {}).get("art") or "aus"
    return teilen(lib, item, art, alles_erkennen=True, progress=progress)


def teilen(lib: Library, item: Item, art: str, *, alles_erkennen: bool = False, run_ocr: bool = True,
           reindex: bool = True, progress: Progress | None = None) -> Item:
    """Text eines PDFs mit neuer Teilung erfassen (siehe teilung.ARTEN). Seitenzählung wird neu geschätzt.

    Seiten mit Textebene werden geteilt ausgelesen; Scan-Seiten neu erkannt (OCR), ebenso alle mit alles_erkennen.
    """
    if art not in teilung.ARTEN:
        raise ValueError(f"Teilung {art!r} unbekannt — {', '.join(teilung.ARTEN)}")
    pdfs = [f for f in item.dateien if f.lower().endswith(".pdf")]
    if not pdfs:
        raise ValueError("Teilen geht nur bei PDFs.")
    progress = progress or (lambda _: None)
    teile, einheit = teilung.ARTEN[art]
    pdf = dateien.original(lib, item, pdfs[0])   # mit NAS: vorher herunterladen
    shutil.rmtree(dateien.zwischenspeicher() / "vorschau" / item.citekey, ignore_errors=True)
    ex = extract.extract_pdf(pdf, teile)
    pages = ex.pages
    war_ocr = (item.text.get("quelle") or "") in ("ocr", "pdf+ocr")
    zu_erkennen = [p.scan for p in pages] if alles_erkennen or item.text.get("quelle") == "ocr" else ex.ocr_noetig
    alt_ocr = dateien.ocr_ort(item) or ""
    if zu_erkennen and run_ocr:
        out = dateien.ocr_workdir(lib, item.citekey)
        if out.exists():
            shutil.rmtree(out)
        progress(f"OCR für {len(zu_erkennen)} Abschnitt(e)")
        recognized = ocr.ocr_pdf_pages(pdf, zu_erkennen, out, progress=progress, teile=teile)
        for p in pages:
            if p.scan in recognized:
                p.text, p.konfidenz = recognized[p.scan].text, recognized[p.scan].konfidenz
        item.text["quelle"] = "ocr" if len(zu_erkennen) == len(pages) else "pdf+ocr"
        item.text["ocr_ordner"] = str(out)
        conf = [p.konfidenz for p in pages if p.konfidenz is not None]
        if conf:
            item.text["konfidenz"] = round(sum(conf) / len(conf), 1)
        if alt_ocr.startswith(dateien.NAS_PREFIX):
            lib._journal({"op": "ocr_loeschen", "key": item.citekey})   # alter Lauf passt nicht mehr
    elif war_ocr and not zu_erkennen:
        item.text["quelle"] = "pdf"
    if teile > 1:
        item.text["teilung"] = {"teile": teile, "art": art}
    else:
        item.text.pop("teilung", None)
    _numbering(item, pages, True, any(p.label for p in pages))
    if einheit != "S.":
        item.seitenzaehlung["einheit"] = einheit
    item.text.setdefault("hinweise", [])
    return _finish(lib, item, pages, reindex=reindex)


_JUNK_TITLE = re.compile(
    r"^(untitled|unbenannt|dokument\d*|document\d*|scan\w*|img[_\-]?\d+)$"   # Platzhalter
    r"|^[A-Z]{1,4}[_\-]?\d{2,}"                                          # Scanner/Kopierer (KM_364e-…)
    r"|\d{8,}",                                                          # Zeitstempel
    re.I)


def guess_title(meta_title: str | None, path: Path) -> tuple[str, str]:
    """Titel aus den Dateieigenschaften, wenn plausibel, sonst aus dem Dateinamen."""
    t = (meta_title or "").strip()
    t = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", t)
    t = re.sub(r"\.(docx?|pdf|pptx?|odt|rtf)$", "", t, flags=re.I).strip()
    if len(t) > 3 and not _JUNK_TITLE.search(t):
        return t, "Titel aus den Dateieigenschaften — prüfen."
    stem = re.sub(r"[_]+", " ", path.stem)
    stem = re.sub(r"[\-\s]+\d+$", "", stem)                       # Nummer am Ende (-1, -2)
    stem = re.sub(r"\s+(copy|kopie)(\s*\d+)?$", "", stem, flags=re.I)  # Finder-Kopien
    stem = re.sub(r"^\d+([\-–]\d+)?[\s.\-_]+", "", stem)          # Kursnummerierung vorne (03-04 …)
    stem = stem.strip() or path.stem
    return stem, "Titel aus dem Dateinamen — prüfen."


def pruefsumme(path: Path) -> str:
    """SHA-256 des Dateiinhalts — erkennt dieselbe Datei unter anderem Namen."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _guess_type(ex: extract.Extracted) -> str:
    if ex.quelle in ("html",):
        return "online"
    if ex.quelle == "pdf" and len(ex.pages) <= 60:
        return "article"
    return "book" if ex.paginiert else "misc"


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "datei"


def zusammenfuehren(lib: Library, quelle: str, ziel: str) -> Item:
    """Zwei Einträge desselben Werks vereinen. Der Schlüssel `ziel` bleibt, `quelle` wird Alias (ids).

    Angaben von `ziel` haben Vorrang, fehlende kommen aus `quelle`. Höchstens einer der beiden darf
    Dateien oder Text haben; der wird umbenannt (auf der Ablage nur verschoben, nicht neu hochgeladen).
    """
    q, z = lib.get(quelle), lib.get(ziel)
    if q.citekey == z.citekey:
        raise ValueError("Zweimal derselbe Titel.")

    def hat_inhalt(it: Item) -> bool:
        return bool(it.dateien) or (lib.folder(it.citekey) / "seiten.jsonl").exists() or bool(it.text.get("ocr_ordner"))

    if hat_inhalt(q) and hat_inhalt(z):
        raise ValueError(f"{quelle} und {ziel} haben beide Dateien oder Text — einen davon vorher löschen "
                         f"(bib loeschen), dann zusammenführen.")
    felder = {**{k: v for k, v in q.felder.items() if k != "ids"}, **z.felder}
    ids = [x.strip() for x in (z.felder.get("ids", "") + "," + q.felder.get("ids", "")).split(",") if x.strip()]
    felder["ids"] = ", ".join(dict.fromkeys(ids + [quelle]))
    notizen = [t for t in ((lib.folder(k) / "notizen.md") for k in (ziel, quelle)) if t.exists()]
    notiztext = "\n\n".join(t.read_text(encoding="utf-8").strip() for t in notizen)
    traeger = q if hat_inhalt(q) else z
    traeger.felder = felder
    traeger.eintrag = z.eintrag
    for attr in ("tags", "kategorien", "projekte"):
        setattr(traeger, attr, list(dict.fromkeys(getattr(z, attr) + getattr(q, attr))))
    traeger.notiz = "\n\n".join(x for x in (z.notiz, q.notiz) if x)
    traeger.kommentar = "\n".join(x for x in (z.kommentar, q.kommentar) if x)
    traeger.unvollstaendig = [f for f in PFLICHT if not felder.get(f) and not (f == "author" and felder.get("editor"))
                              and not (f == "year" and felder.get("date"))]
    if "metadaten" in z.text:
        traeger.text["metadaten"] = z.text["metadaten"]
    if traeger is q:
        lib.save(q)
        lib.delete(ziel)
        traeger = lib.rename(q, ziel)
    else:
        lib.save(z)
        lib.delete(quelle)
    if notiztext:
        (lib.folder(ziel) / "notizen.md").write_text(notiztext + "\n", encoding="utf-8")
    return lib.get(ziel)
