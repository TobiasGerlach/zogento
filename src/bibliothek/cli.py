"""Kommandozeile `bib` — für Menschen und für KI-Agenten.

Suchen laufen über die geöffnete App, falls sie läuft (dort ist das
Embedding-Modell schon geladen); sonst lokal.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import bibtex, config, importers, index, ingest, pagenums
from .store import STATUS_AUTO, STATUS_BESTAETIGT, STATUS_PDF, Library, Page


def _lib() -> Library:
    return Library(config.library_root())


def _felder(args) -> dict[str, str]:
    f = {}
    for name, attr in (("title", "titel"), ("author", "autor"), ("year", "jahr")):
        if getattr(args, attr, None):
            f[name] = getattr(args, attr)
    for kv in getattr(args, "feld", None) or []:
        k, _, v = kv.partition("=")
        f[k.strip().lower()] = v.strip()
    return f


def _common_meta(p: argparse.ArgumentParser) -> None:
    p.add_argument("--titel")
    p.add_argument("--autor", help='BibTeX-Form: "Nachname, Vorname and Nachname, Vorname"')
    p.add_argument("--jahr")
    p.add_argument("--typ", help="BibTeX-Eintragstyp: book, article, incollection, online …")
    p.add_argument("--feld", action="append", metavar="NAME=WERT", help="weiteres BibTeX-Feld, mehrfach möglich")
    p.add_argument("--key", help="Zitierschlüssel vorgeben (sonst nachnameJAHRschlagwort)")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--kategorie", action="append", default=[])
    p.add_argument("--projekt", action="append", default=[])
    p.add_argument("--trotzdem", action="store_true", help="auch anlegen, wenn eine Dublette erkannt wird")


def _meta_kwargs(args) -> dict:
    return dict(felder=_felder(args), citekey=args.key, tags=args.tag, kategorien=args.kategorie,
                projekte=args.projekt, allow_duplicate=args.trotzdem)


def _print_item_line(item) -> None:
    flags = []
    if item.unvollstaendig:
        flags.append("Metadaten fehlen: " + ", ".join(item.unvollstaendig))
    status = (item.seitenzaehlung or {}).get("status")
    if item.text.get("seiten"):
        flags.append(f"{item.text['seiten']} S., Seitenzählung {status}")
    else:
        flags.append("kein Text")
    print(f"{item.citekey:32} {item.short:24} {item.title[:60]}")
    extra = []
    if item.tags:
        extra.append("#" + " #".join(item.tags))
    if item.kategorien:
        extra.append("[" + "; ".join(item.kategorien) + "]")
    if item.projekte:
        extra.append("Projekte: " + ", ".join(item.projekte))
    print(" " * 33 + " · ".join(extra + flags))


# --- Befehle ----------------------------------------------------------------------------

def cmd_neu(args) -> int:
    lib = _lib()
    rc = 0
    if not args.dateien and not (args.doi or args.isbn):
        print("Datei(en) angeben oder --doi / --isbn.", file=sys.stderr)
        return 2
    for art, kennung in [("doi", d) for d in args.doi] + [("isbn", i) for i in args.isbn]:
        from . import katalog
        try:
            item, hinweis = katalog.aufnehmen(lib, **{art: kennung}, tags=args.tag, projekte=args.projekt,
                                              kategorien=args.kategorie, pdf=not args.ohne_pdf,
                                              progress=lambda m: print("  " + m, file=sys.stderr))
            print(f"+ {item.citekey}  {item.short}, {item.title}" + (f"  ({item.text.get('seiten')} Seiten)" if item.text.get("seiten") else ""))
            if hinweis:
                print(f"  ! {hinweis}")
        except ingest.Duplicate as d:
            print(f"= {kennung}: {d}")
        except katalog.KatalogFehler as exc:
            print(f"! {kennung}: {exc}", file=sys.stderr)
            rc = 1
    for f in args.dateien:
        try:
            item = ingest.add_file(lib, Path(f), eintrag=args.typ, run_ocr=not args.ohne_ocr,
                                   progress=lambda m: print("  " + m, file=sys.stderr), **_meta_kwargs(args))
            print(f"+ {item.citekey}  ({item.text.get('seiten', 0)} Seiten, Quelle {item.text.get('quelle')})")
            for h in item.text.get("hinweise", []):
                print(f"  ! {h}")
            if item.unvollstaendig:
                print(f"  ! Fehlende Angaben: {', '.join(item.unvollstaendig)} — mit `bib bearbeite {item.citekey}` ergänzen")
        except ingest.Duplicate as d:
            print(f"= {f}: {d}  (--trotzdem legt einen zweiten Titel an)")
        except Exception as exc:
            print(f"! {f}: {exc}", file=sys.stderr)
            rc = 1
    lib.write_refs_bib()
    _embed_hint(lib)
    _nach_aenderung(lib)
    return rc


def cmd_scans(args) -> int:
    lib = _lib()
    item = ingest.add_scans(lib, args.quelle, single_page=args.einzelseiten, order=args.reihenfolge,
                            eintrag=args.typ or "book", progress=lambda m: print("  " + m, file=sys.stderr),
                            **_meta_kwargs(args))
    print(f"+ {item.citekey}  ({item.text.get('seiten')} Seiten, Ø Konfidenz {item.text.get('konfidenz')})")
    print(f"  Prüfansicht: {Path(item.text['ocr_ordner']) / 'review.html'}")
    _print_numbering(lib, item)
    lib.write_refs_bib()
    _embed_hint(lib)
    _nach_aenderung(lib)
    return 0


def cmd_ocr_uebernehmen(args) -> int:
    lib = _lib()
    for d in args.ordner:
        try:
            item = importers.import_ocr_dir(lib, Path(d), felder=_felder(args) or None)
            index.update(lib, keys=[item.citekey])
            _print_numbering(lib, item)
        except ingest.Duplicate as dup:
            print(f"= {d}: {dup}")
    lib.write_refs_bib()
    _embed_hint(lib)
    _nach_aenderung(lib)
    return 0


def cmd_import_bibtex(args) -> int:
    lib = _lib()
    stats = importers.import_bibtex(lib, Path(args.datei), texte=Path(args.texte) if args.texte else None,
                                    tags=args.tag)
    index.update(lib)
    lib.write_refs_bib()
    print(stats)
    _embed_hint(lib)
    _nach_aenderung(lib)
    return 0


def cmd_import_zotero(args) -> int:
    lib = _lib()
    stats = importers.import_zotero(lib, Path(args.ordner) if args.ordner else None)
    index.update(lib)
    lib.write_refs_bib()
    print(stats)
    _embed_hint(lib)
    _nach_aenderung(lib)
    return 0


def cmd_liste(args) -> int:
    lib = _lib()
    items = [it for it in lib.items() if _matches(it, args)]
    if args.json:
        print(json.dumps([_item_json(it) for it in items], ensure_ascii=False, indent=1))
        return 0
    for it in items:
        _print_item_line(it)
    print(f"\n{len(items)} Titel")
    return 0


def _matches(it, args) -> bool:
    if args.tag and not set(args.tag) <= set(it.tags):
        return False
    if args.kategorie and not any(k.startswith(c) for c in args.kategorie for k in it.kategorien):
        return False
    if args.projekt and not set(args.projekt) & set(it.projekte):
        return False
    if getattr(args, "unvollstaendig", False) and not it.unvollstaendig:
        return False
    return True


def _item_json(it) -> dict:
    return {"citekey": it.citekey, "kurz": it.short, "titel": it.title, "typ": it.eintrag, "felder": it.felder,
            "tags": it.tags, "kategorien": it.kategorien, "projekte": it.projekte,
            "seiten": it.text.get("seiten"), "textquelle": it.text.get("quelle"),
            "seitenzaehlung": (it.seitenzaehlung or {}).get("status"), "metadaten_fehlen": it.unvollstaendig,
            "ordner": str(it.folder)}


def cmd_suche(args) -> int:
    lib = _lib()
    hits = _remote_search(args) if not args.lokal else None
    if hits is None:
        keys = _filter_keys(lib, args)
        try:
            raw = index.search(lib, args.anfrage, mode=args.modus, limit=args.n, keys=keys)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        hits = []
        cache = {}
        for h in raw:
            it = cache.setdefault(h.citekey, lib.get(h.citekey))
            hits.append({"citekey": h.citekey, "kurz": it.short, "titel": it.title,
                         "seite": it.page_ref(Page(h.scan, "")), "scan": h.scan, "snippet": h.snippet,
                         "text": h.text, "wege": sorted(h.wege), "score": round(h.score, 4)})
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    if not hits:
        print("Keine Treffer.")
        return 1
    for i, h in enumerate(hits, 1):
        print(f"{i:2}  {h['citekey']} — {h['kurz']}, {h['titel'][:70]}")
        print(f"    {h['seite']} · {'+'.join(h['wege'])}")
        body = h["text"] if args.voll else (h["snippet"] or h["text"][:300])
        for line in _wrap(re.sub(r"\s+", " ", body), 100):
            print("    " + line)
        print()
    return 0


def _filter_keys(lib: Library, args) -> set[str] | None:
    if not (args.tag or args.kategorie or args.projekt or args.titel):
        return None
    keys = {it.citekey for it in lib.items() if _matches(it, args)}
    if args.titel:
        keys &= {lib.resolve(t).citekey for t in args.titel}
    return keys


def _remote_search(args) -> list | None:
    """Laufende App fragen; None, wenn keine läuft oder sie eine andere Bibliothek hat."""
    base = f"http://127.0.0.1:{config.web_port()}"
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=0.4) as r:
            status = json.load(r)
        if Path(status.get("bibliothek", "")) != config.library_root():
            return None
        params = {"q": args.anfrage, "modus": args.modus, "limit": args.n}
        for name in ("tag", "kategorie", "projekt", "titel"):
            for v in getattr(args, name) or []:
                params.setdefault(name, v)
        with urllib.request.urlopen(base + "/api/suche?" + urllib.parse.urlencode(params), timeout=120) as r:
            return json.load(r)["treffer"]
    except Exception:
        return None


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


def cmd_zeige(args) -> int:
    lib = _lib()
    item = lib.resolve(args.titel)
    if args.bibtex:
        print(bibtex.format_entry(item.to_entry()))
        return 0
    if not args.seiten:
        print((lib.folder(item.citekey) / "meta.yaml").read_text(encoding="utf-8"))
        notes = lib.folder(item.citekey) / "notizen.md"
        if notes.exists():
            print(notes.read_text(encoding="utf-8"))
        print(f"Ordner: {lib.folder(item.citekey)}")
        return 0
    pages = lib.pages(item.citekey)
    wanted = _parse_range(args.seiten)
    shown = 0
    for p in pages:
        num = item.page_number(p)
        key = p.scan if args.scan or num is None else (int(num) if str(num).isdigit() else None)
        if key in wanted:
            print(f"[[{item.page_ref(p)}]]")
            print(p.text.strip())
            print()
            shown += 1
    if not shown:
        print("Keine passenden Seiten. Druckseiten sind nur bekannt, wenn die Seitenzählung gesetzt ist; "
              "mit --scan nach Scan-Nummer wählen.", file=sys.stderr)
        return 1
    return 0


def _parse_range(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def cmd_bearbeite(args) -> int:
    lib = _lib()
    item = lib.resolve(args.titel)
    for kv in args.feld or []:
        k, _, v = kv.partition("=")
        k = k.strip().lower()
        if v.strip():
            item.felder[k] = v.strip()
        else:
            item.felder.pop(k, None)
    if args.typ:
        item.eintrag = args.typ
    for attr in ("tag", "kategorie", "projekt"):
        target = {"tag": item.tags, "kategorie": item.kategorien, "projekt": item.projekte}[attr]
        for v in getattr(args, attr) or []:
            if v.startswith("-"):
                if v[1:] in target:
                    target.remove(v[1:])
            elif v.lstrip("+") not in target:
                target.append(v.lstrip("+"))
    if args.notiz is not None:
        item.notiz = args.notiz
    item.unvollstaendig = [f for f in ingest.PFLICHT if not item.felder.get(f)
                           and not (f == "author" and item.felder.get("editor"))
                           and not (f == "year" and item.felder.get("date"))]
    lib.save(item)
    if args.neuer_key:
        item = lib.rename(item, args.neuer_key)
        index.update(lib)
    lib.write_refs_bib()
    _print_item_line(item)
    _nach_aenderung(lib)
    return 0


def cmd_metadaten(args) -> int:
    """Fehlende Angaben in offenen Katalogen nachschlagen und auf Wunsch übernehmen."""
    from . import katalog

    lib = _lib()
    if args.alle:
        items = [it for it in lib.items() if it.unvollstaendig or katalog.ist_automatischer_key(it.citekey)]
    elif args.titel:
        items = [lib.resolve(args.titel)]
    else:
        print("Titel angeben oder --alle.", file=sys.stderr)
        return 2
    ergebnis = []
    geaendert = False
    for item in items:
        if args.nehmen:
            vs = katalog.gemerkt(item) or katalog.vorschlaege(lib, item, titel=args.suchtitel, autor=args.autor)
        else:
            try:
                vs = katalog.vorschlaege(lib, item, titel=args.suchtitel, autor=args.autor)
            except katalog.KatalogFehler as exc:
                print(f"{item.citekey}: {exc}", file=sys.stderr)
                continue
            katalog.merken(item, vs)
        wahl = None
        if args.nehmen:
            if not 1 <= args.nehmen <= len(vs):
                print(f"{item.citekey}: Vorschlag {args.nehmen} gibt es nicht ({len(vs)} Vorschläge).", file=sys.stderr)
                return 1
            wahl = vs[args.nehmen - 1]
        elif args.sichere_uebernehmen and vs and vs[0].sicherheit == katalog.SICHER:
            wahl = vs[0]
        if wahl:
            alt = item.citekey
            item = katalog.uebernehmen(lib, item, wahl, nur_fehlende=args.nur_fehlende)
            geaendert = True
            if not args.json:
                print(f"✓ {alt}" + (f" → {item.citekey}" if item.citekey != alt else "") + f": {item.short}, {item.title}"
                      f"  [{wahl.quelle}, {wahl.sicherheit}]")
        ergebnis.append({"citekey": item.citekey, "uebernommen": wahl.to_dict() if wahl else None,
                         "vorschlaege": [v.to_dict() for v in vs]})
        if not args.json and not wahl:
            print(f"{item.citekey} — {item.short}, {item.title}")
            if not vs:
                print("    keine Treffer in DNB, Crossref, OpenAlex, Open Library")
            for i, v in enumerate(vs, 1):
                f = v.felder
                wer = f.get("author") or (f"{f['editor']} (Hrsg.)" if f.get("editor") else "?")
                titel = f.get("title", "") + (f". {f['subtitle']}" if f.get("subtitle") else "")
                wo = ", ".join(x for x in (f.get("journaltitle") or f.get("booktitle"), f.get("location"),
                                           f.get("publisher"), f.get("year")) if x)
                print(f"  {i}. [{v.sicherheit}] {wer}: {titel}. {wo}")
                print(f"     {v.quelle} {v.url} — {'; '.join(v.begruendung) or 'nur Titelsuche'}")
            if vs:
                print(f"     übernehmen: bib metadaten {item.citekey} --nehmen N")
    if geaendert:
        index.update(lib)
        lib.write_refs_bib()
        _nach_aenderung(lib)
    if args.json:
        print(json.dumps(ergebnis, ensure_ascii=False, indent=1))
    return 0


def cmd_seiten(args) -> int:
    lib = _lib()
    item = lib.resolve(args.titel)
    sz = dict(item.seitenzaehlung or {})
    segs = [dict(x) for x in item.segments()]
    if args.scan is not None and args.druckseite is not None:
        off = args.druckseite - args.scan
        if args.ab_scan:
            start = args.ab_scan
        elif segs:
            start = max([x["ab_scan"] for x in segs if x["ab_scan"] <= args.scan] or [args.scan])
        else:
            start = max(1, 1 - off)
        segs = pagenums.set_segment(segs, start, off)
        next(x for x in segs if x["ab_scan"] == start)["geprueft"] = True
        sz["segmente"] = segs
        sz.pop("versatz", None), sz.pop("ab_scan", None)
        sz["status"] = STATUS_BESTAETIGT if all(x.get("geprueft") for x in segs) else STATUS_AUTO
    elif args.bestaetigen:
        if not segs and sz.get("status") != STATUS_PDF:
            print("Nichts zu bestätigen — erst --scan N --druckseite S angeben.", file=sys.stderr)
            return 1
        for x in segs:
            x["geprueft"] = True
        if segs:
            sz["segmente"] = segs
        sz["status"] = STATUS_BESTAETIGT
    elif args.ausnahme:
        scan, _, label = args.ausnahme.partition("=")
        sz.setdefault("ausnahmen", {})[scan.strip()] = label.strip()
    if sz != item.seitenzaehlung:
        if sz.get("status") == STATUS_BESTAETIGT:
            sz.pop("schaetzung", None)
        item.seitenzaehlung = sz
        lib.save(item)
        index.update(lib, keys=[item.citekey])
    _print_numbering(lib, item)
    _nach_aenderung(lib)
    return 0


def _print_numbering(lib: Library, item) -> None:
    sz = item.seitenzaehlung or {}
    print(f"Seitenzählung {item.citekey}: {sz.get('status')}" + (f" — {sz['schaetzung']}" if sz.get("schaetzung") else ""))
    for jump in sz.get("spruenge") or []:
        print(f"  ! {jump}")
    pages = lib.pages(item.citekey)
    by_scan = {p.scan: p for p in pages}
    segs = item.segments()
    for i, seg in enumerate(segs):
        end = segs[i + 1]["ab_scan"] - 1 if i + 1 < len(segs) else (pages[-1].scan if pages else seg["ab_scan"])
        mark = "geprüft" if seg.get("geprueft") or sz.get("status") == STATUS_BESTAETIGT else "geschätzt"
        print(f"  Scan {seg['ab_scan']:>4}–{end:<4}  Druckseite = Scan {seg['versatz']:+d}   [{mark}]")
        for scan in (seg["ab_scan"], (seg["ab_scan"] + end) // 2, end):
            p = by_scan.get(scan)
            if p and p.text.strip():
                lines = [x.strip() for x in p.text.splitlines() if x.strip()]
                print(f"      Scan {scan:4} → {item.page_ref(p):38} oben: {lines[0][:40]!r}  unten: {lines[-1][:30]!r}")
    if sz.get("status") not in (STATUS_BESTAETIGT, STATUS_PDF, "keine"):
        print(f"  Prüfen: Stimmen oben/unten mit der Druckseite überein, dann  bib seiten {item.citekey} --bestaetigen")
        print(f"  Korrigieren: bib seiten {item.citekey} --scan N --druckseite S   (gilt für den Abschnitt, in dem N liegt)")


def cmd_bibtex(args) -> int:
    """Ohne Auswahl: alles. Sonst die Vereinigung aus zitierten Schlüsseln, Projekt und genannten Schlüsseln."""
    lib = _lib()
    keys: set[str] | None = None
    missing: list[str] = []
    if args.aus_tex or args.projekt or args.keys:
        keys = set()
    for tex in args.aus_tex or []:
        cited: set[str] = set()
        for path in [Path(tex)] if Path(tex).is_file() else Path(tex).rglob("*.tex"):
            text = path.read_text(encoding="utf-8", errors="replace")
            text = re.sub(r"(?<!\\)%.*", "", text)  # auskommentierte Zitate zählen nicht
            for m in re.finditer(r"\\\w*cite\w*\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}", text):
                cited.update(k.strip() for k in m.group(1).split(",") if k.strip() and k.strip() != "*")
        for k in sorted(cited):
            if lib.exists(k):
                keys.add(k)
            elif (alias := lib.by_alias(k)):
                keys.add(alias.citekey)   # alter Schlüssel, steht in ids
            else:
                missing.append(k)
    for projekt in args.projekt or []:
        keys |= {it.citekey for it in lib.items() if projekt in it.projekte}
    for k in args.keys or []:
        keys.add(lib.resolve(k).citekey)
    for k in missing:
        print(f"! {k} wird zitiert, ist aber nicht in der Bibliothek (bib neu … --key {k})", file=sys.stderr)
    out = Path(args.ausgabe) if args.ausgabe else None
    chosen = sorted(keys) if keys is not None else None
    path = lib.write_refs_bib(out, chosen)
    print(f"{path} ({len(chosen) if chosen is not None else len(lib.keys())} Einträge)")
    return 1 if missing and args.streng else 0


def cmd_index(args) -> int:
    lib = _lib()
    if args.neu:
        for it in lib.items():
            if (lib.folder(it.citekey) / "seiten.jsonl").exists():
                lib.render_text(it)
    print(index.update(lib, rebuild=args.neu, progress=lambda m: None))
    if not args.ohne_embeddings:
        n = index.embed_missing(lib, progress=lambda m: print("\r" + m, end="", file=sys.stderr))
        print(f"\n{n} neue Embeddings", file=sys.stderr)
    print(index.stats(lib))
    return 0


def cmd_status(args) -> int:
    lib = _lib()
    items = list(lib.items())
    s = index.stats(lib)
    print(f"Bibliothek:   {lib.root}")
    print(f"Titel:        {len(items)}  (mit Text: {sum(1 for i in items if i.text.get('seiten'))})")
    print(f"Index:        {s['abschnitte']} Abschnitte, {s['embeddings']} mit Embedding")
    todo = [i.citekey for i in items if i.unvollstaendig]
    if todo:
        print(f"Metadaten unvollständig: {', '.join(todo)}")
    unchecked = [i.citekey for i in items if (i.seitenzaehlung or {}).get("status") in ("automatisch", "unbekannt")
                 and i.text.get("seiten")]
    if unchecked:
        print(f"Seitenzählung ungeprüft: {', '.join(unchecked)}")
    if s["embeddings"] < s["abschnitte"]:
        print("Semantische Suche unvollständig — `bib index` berechnet die fehlenden Embeddings.")
    from . import sicherung
    sich = sicherung.status()
    if sich["zuletzt"]:
        print(f"Sicherung:    vor {sich['tage']:.0f} Tag(en) nach {sich['ziel']}"
              + ("  ← fällig: bib sicherung" if sich["faellig"] else ""))
    elif sich["extern"]:
        print("Sicherung:    übernimmt die Ablage (NAS)")
    elif items:
        print("Sicherung:    noch nie — bib sicherung <ordner auf externer Platte>")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp
    return mcp.main()


def cmd_beleg(args) -> int:
    from . import belege

    lib = _lib()
    item = lib.resolve(args.titel)
    r = belege.pruefen(lib, item, args.zitat, seite=args.seite)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["gefunden"] else 1
    print(f"{item.citekey}: {r.get('bewertung', 'nicht gefunden')}"
          + (f" — {r['seite']}" + (" (zitierfähig)" if r.get("zitierfaehig") else "") if r["gefunden"] else ""))
    if "angegebene_seite_stimmt" in r:
        print("  angegebene Seite " + ("stimmt" if r["angegebene_seite_stimmt"] else f"stimmt NICHT ({args.seite})"))
    for u in r.get("unterschiede", []):
        print(f"  Zitat „{u['zitat']}“ ↔ Text „{u['text']}“")
    if r.get("fundstelle"):
        print("  " + r["fundstelle"])
    if r.get("hinweis"):
        print("  ! " + r["hinweis"])
    return 0 if r["gefunden"] else 1


def cmd_sicherung(args) -> int:
    from . import ablage, sicherung

    lib = _lib()
    ziel = args.ziel or sicherung.status()["ziel"]
    if not ziel:
        print("Zielordner angeben, z. B.: bib sicherung /Volumes/Platte/Bibliothek", file=sys.stderr)
        return 2
    try:
        b = sicherung.sichern(lib, Path(ziel), fortschritt=lambda m: print("  " + m, file=sys.stderr))
    except (ValueError, ablage.AblageFehler) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Gesichert nach {b['ziel']}: {b['kopiert']} lokal kopiert, {b['geholt']} von der Ablage geholt "
          f"({b['bytes'] / 1e6:.0f} MB), {b['unveraendert']} unverändert.")
    for f in b["fehler"]:
        print(f"  ! {f}", file=sys.stderr)
    print(f"Wiederherstellen/Öffnen: bib einstellung --bibliothek \"{b['ziel']}\"")
    return 1 if b["fehler"] else 0


def cmd_zusammenfuehren(args) -> int:
    lib = _lib()
    q, z = lib.resolve(args.quelle), lib.resolve(args.ziel)
    if not args.ja:
        print(f"Würde {q.citekey} ({q.short}, {q.title}) in {z.citekey} ({z.short}, {z.title}) aufgehen lassen.\n"
              f"Angaben von {z.citekey} haben Vorrang; {q.citekey} bleibt als Alias zitierbar. Mit --ja bestätigen.")
        return 1
    try:
        item = ingest.zusammenfuehren(lib, q.citekey, z.citekey)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    index.update(lib)
    lib.write_refs_bib()
    _print_item_line(item)
    _nach_aenderung(lib)
    return 0


def cmd_loeschen(args) -> int:
    lib = _lib()
    item = lib.resolve(args.titel)
    if not args.ja:
        print(f"Würde {item.citekey} ({item.title}) samt Ordner löschen. Mit --ja bestätigen.")
        return 1
    lib.delete(item.citekey)
    index.update(lib)
    lib.write_refs_bib()
    print(f"- {item.citekey}")
    _nach_aenderung(lib)
    return 0


def cmd_reocr(args) -> int:
    lib = _lib()
    item = ingest.reocr(lib, lib.resolve(args.titel), progress=lambda m: print("  " + m, file=sys.stderr))
    print(f"~ {item.citekey} neu erkannt (Ø Konfidenz {item.text.get('konfidenz')})")
    _embed_hint(lib)
    _nach_aenderung(lib)
    return 0


def cmd_web(args) -> int:
    from . import app
    return app.main() or 0


def cmd_einstellung(args) -> int:
    if args.bibliothek:
        config.save_settings({"bibliothek": str(Path(args.bibliothek).expanduser().resolve())})
    if args.ocr_sprache:
        config.save_settings({"ocr_sprache": args.ocr_sprache})
    if args.ocr_engine:
        config.save_settings({"ocr_engine": args.ocr_engine})
    if args.sicherung_extern:
        s = dict(config.settings().get("sicherung") or {})
        s["extern"] = args.sicherung_extern == "ja"
        config.save_settings({"sicherung": s})
    print(f"Einstellungen: {config.SETTINGS_FILE}")
    print(json.dumps(config.settings(), ensure_ascii=False, indent=2))
    return 0


def cmd_nas(args) -> int:
    from . import ablage, dateien, sync

    if args.aktion == "verbinden":
        if not args.server:
            print("Server angeben, z. B.: bib nas verbinden http://192.168.0.70:9001\n"
                  "  oder für einen anderen WebDAV-Server: bib nas verbinden https://server/dav --webdav --benutzer NAME",
                  file=sys.stderr)
            return 2
        try:
            if args.webdav:
                if not args.benutzer:
                    print("Mit --webdav auch --benutzer angeben.", file=sys.stderr)
                    return 2
                import getpass
                passwort = getpass.getpass(f"Passwort für {args.benutzer}: ")
                nas = ablage.verbinden_webdav(args.server, args.benutzer, passwort, ordner=args.ordner)
            else:
                nas = ablage.verbinden(args.server, ordner=args.ordner, browser=not args.ohne_browser)
        except ablage.AblageFehler as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Verbunden als {nas['benutzer']} @ {nas['server']}, Ordner „{nas['ordner']}“.")
        print("Erster Abgleich: bib sync   (lädt Originale und OCR-Läufe hoch, kann dauern)")
        return 0
    if args.aktion == "trennen":
        ablage.trennen()
        print("Getrennt. Dateien in der Ablage bleiben, wo sie sind.")
        return 0
    lib = _lib()
    st = sync.status(lib)
    if not st["nas"]:
        print("Kein NAS eingerichtet — alles liegt lokal. Einrichten: bib nas verbinden <server>")
        return 0
    import datetime as _dt
    print(f"{ablage.anzeigename(st['nas']) + ':':<14}{st['nas']['benutzer']} @ {st['nas']['server']}, Ordner {st['nas'].get('ordner')}")
    print("Letzter Abgleich: " + (_dt.datetime.fromtimestamp(st["zuletzt"]).strftime("%d.%m.%Y %H:%M") if st["zuletzt"] else "noch nie"))
    a = st["ausstehend"]
    print(f"Ausstehend:   {a['originale']} Originale ({a['bytes'] / 1e6:.0f} MB), OCR-Läufe: {', '.join(a['ocr']) or '–'}")
    print(f"Zwischenspeicher: {st['zwischenspeicher'] / 1e6:.0f} MB ({dateien.zwischenspeicher()})")
    return 0


def cmd_sync(args) -> int:
    from . import ablage, sync

    lib = _lib()
    try:
        b = sync.abgleichen(lib, eingang=not args.ohne_eingang, fortschritt=lambda m: print("  " + m, file=sys.stderr))
    except sync.Gesperrt as exc:
        print(exc, file=sys.stderr)
        return 1
    except ablage.NichtErreichbar:
        print("Verbindung zur Ablage unterbrochen. Bisheriges ist gesichert — `bib sync` macht dort weiter.",
              file=sys.stderr)
        return 1
    if b["status"] != "ok":
        print({"kein NAS": "Kein NAS eingerichtet (bib nas verbinden <server>).",
               "offline": "Ablage nicht erreichbar — Änderungen bleiben vorgemerkt."}[b["status"]])
        return 1
    print(f"Abgleich: {b['hoch']} hochgeladen, {b['runter']} geholt"
          + (f", aktualisiert: {', '.join(b['geholt'])}" if b["geholt"] else ""))
    for line in b["eingang"]:
        print(f"  Eingang: {line}")
    for k in b["konflikte"]:
        print(f"  ! Konflikt — andere Fassung liegt in der Ablage als {k}")
    for f in b["fehler"]:
        print(f"  ! {f}")
    if (b["geholt"] or b["eingang"]) and not args.ohne_embeddings:
        n = index.embed_missing(lib, progress=lambda m: print("\r  " + m, end="", file=sys.stderr))
        if n:
            print(f"\n  {n} neue Embeddings", file=sys.stderr)
    return 0


def cmd_oeffne(args) -> int:
    import os
    import subprocess

    from . import dateien

    lib = _lib()
    item = lib.resolve(args.titel)
    if not item.dateien:
        print(f"{item.citekey} hat keine Originaldatei.", file=sys.stderr)
        return 1
    name = args.datei or item.dateien[0]
    path = dateien.original(lib, item, name)
    print(path)
    if not args.nur_pfad:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)])
    return 0


def _nach_aenderung(lib: Library) -> None:
    """Mit NAS: Änderungen gleich abgleichen (still, wenn offline)."""
    from . import sync

    msg = sync.versuchen(lib)
    if msg:
        print("  " + msg, file=sys.stderr)


def _embed_hint(lib: Library) -> None:
    s = index.stats(lib)
    if s["embeddings"] < s["abschnitte"]:
        print(f"  Semantische Suche: {s['abschnitte'] - s['embeddings']} Abschnitte ohne Embedding — "
              f"`bib index` ausführen (oder die App läuft gerade und erledigt das).", file=sys.stderr)


# --- Parser ------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bib", description="Zentrale Literaturverwaltung mit OCR und Suche.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="BEFEHL")

    s = sub.add_parser("suche", help="Volltext + semantisch suchen, mit Seitenangabe")
    s.add_argument("anfrage")
    s.add_argument("--modus", choices=["hybrid", "text", "semantisch"], default="hybrid")
    s.add_argument("-n", type=int, default=10, help="Anzahl Treffer (Standard 10)")
    s.add_argument("--tag", action="append")
    s.add_argument("--kategorie", action="append")
    s.add_argument("--projekt", action="append")
    s.add_argument("--titel", action="append", help="nur in diesem Titel (Schlüssel oder Titelteil)")
    s.add_argument("--voll", action="store_true", help="ganzen Abschnitt statt Ausschnitt zeigen")
    s.add_argument("--json", action="store_true")
    s.add_argument("--lokal", action="store_true", help="nicht über die laufende App suchen")
    s.set_defaults(func=cmd_suche)

    s = sub.add_parser("zeige", help="Metadaten oder Seiten eines Titels")
    s.add_argument("titel")
    s.add_argument("--seiten", help="z. B. 45 oder 40-45,50 (Druckseiten; mit --scan Scan-Nummern)")
    s.add_argument("--scan", action="store_true")
    s.add_argument("--bibtex", action="store_true")
    s.set_defaults(func=cmd_zeige)

    s = sub.add_parser("liste", help="Titel auflisten und filtern")
    s.add_argument("--tag", action="append")
    s.add_argument("--kategorie", action="append")
    s.add_argument("--projekt", action="append")
    s.add_argument("--unvollstaendig", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_liste)

    s = sub.add_parser("neu", help="Datei(en) aufnehmen (PDF, Word, ODT, RTF, EPUB, HTML, TXT, MD, PPTX, Bilder) oder per --doi/--isbn")
    s.add_argument("dateien", nargs="*")
    _common_meta(s)
    s.add_argument("--ohne-ocr", action="store_true")
    s.add_argument("--doi", action="append", default=[], help="per DOI aufnehmen (Angaben aus Crossref/OpenAlex, PDF falls frei)")
    s.add_argument("--isbn", action="append", default=[], help="per ISBN aufnehmen (Angaben aus DNB/Open Library)")
    s.add_argument("--ohne-pdf", action="store_true", help="mit --doi: kein frei zugängliches PDF laden")
    s.set_defaults(func=cmd_neu)

    s = sub.add_parser("scans", help="Buchfotos (Ordner) oder Immich-Link per OCR erfassen")
    s.add_argument("quelle", help="Ordner mit Bildern oder Immich-Freigabelink")
    s.add_argument("--einzelseiten", action="store_true", help="jedes Bild ist eine Seite (keine Doppelseiten)")
    s.add_argument("--reihenfolge", choices=["filename", "date"], default="filename")
    _common_meta(s)
    s.set_defaults(func=cmd_scans)

    s = sub.add_parser("ocr-uebernehmen", help="fertigen OCR-Ausgabeordner (report.json) übernehmen")
    s.add_argument("ordner", nargs="+")
    s.add_argument("--titel")
    s.add_argument("--autor")
    s.add_argument("--jahr")
    s.add_argument("--feld", action="append")
    s.set_defaults(func=cmd_ocr_uebernehmen)

    s = sub.add_parser("bearbeite", help="Felder, Tags, Kategorien, Projekte ändern")
    s.add_argument("titel")
    s.add_argument("--feld", action="append", metavar="NAME=WERT", help="leerer Wert löscht das Feld")
    s.add_argument("--typ")
    s.add_argument("--tag", action="append", help="+name fügt hinzu, -name entfernt")
    s.add_argument("--kategorie", action="append")
    s.add_argument("--projekt", action="append")
    s.add_argument("--notiz")
    s.add_argument("--neuer-key")
    s.set_defaults(func=cmd_bearbeite)

    s = sub.add_parser("metadaten", help="fehlende Angaben in DNB, Crossref, OpenAlex, Open Library nachschlagen")
    s.add_argument("titel", nargs="?")
    s.add_argument("--alle", action="store_true", help="alle unvollständigen Titel")
    s.add_argument("--nehmen", type=int, metavar="N", help="Vorschlag N übernehmen (wie zuletzt angezeigt)")
    s.add_argument("--sichere-uebernehmen", action="store_true",
                   help="nur Vorschläge „sicher“ (ISBN/DOI und Titel stehen im Dokument) automatisch übernehmen")
    s.add_argument("--nur-fehlende", action="store_true", help="vorhandene Felder nicht überschreiben")
    s.add_argument("--suchtitel", help="mit diesem Titel suchen statt dem gespeicherten")
    s.add_argument("--autor", help="mit diesem Autor suchen")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_metadaten)

    s = sub.add_parser("seiten", help="Seitenzählung anzeigen, bestätigen oder setzen")
    s.add_argument("titel")
    s.add_argument("--scan", type=int, help="Scan-Nummer …")
    s.add_argument("--druckseite", type=int, help="… und die darauf gedruckte Seitenzahl")
    s.add_argument("--ab-scan", type=int, help="neuen Abschnitt ab dieser Scan-Nummer beginnen")
    s.add_argument("--ausnahme", metavar="SCAN=LABEL", help="Einzelseite abweichend, z. B. 5=XII")
    s.add_argument("--bestaetigen", action="store_true")
    s.set_defaults(func=cmd_seiten)

    s = sub.add_parser("bibtex", help="refs.bib schreiben (alles, Projekt, oder was eine .tex zitiert)")
    s.add_argument("keys", nargs="*")
    s.add_argument("--aus-tex", action="append", help=".tex-Datei oder Ordner; nur zitierte Einträge")
    s.add_argument("--projekt", action="append", help="alle Titel dieses Projekts (zusätzlich)")
    s.add_argument("--ausgabe", "-o")
    s.add_argument("--streng", action="store_true", help="Fehlercode, wenn Zitiertes fehlt")
    s.set_defaults(func=cmd_bibtex)

    s = sub.add_parser("import-bibtex", help="Einträge aus einer .bib übernehmen")
    s.add_argument("datei")
    s.add_argument("--texte", help="Ordner mit <citekey>.pdf/.txt …")
    s.add_argument("--tag", action="append", default=[])
    s.set_defaults(func=cmd_import_bibtex)

    s = sub.add_parser("import-zotero", help="Zotero-Bibliothek übernehmen (nur lesend)")
    s.add_argument("--ordner", help="Zotero-Datenordner (Standard ~/Zotero)")
    s.set_defaults(func=cmd_import_zotero)

    s = sub.add_parser("index", help="Suchindex aktualisieren und Embeddings berechnen")
    s.add_argument("--neu", action="store_true", help="komplett neu aufbauen")
    s.add_argument("--ohne-embeddings", action="store_true")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("reocr", help="PDF komplett neu per OCR erkennen")
    s.add_argument("titel")
    s.set_defaults(func=cmd_reocr)

    s = sub.add_parser("zusammenfuehren", help="Doppelten Eintrag in einen anderen aufgehen lassen (Schlüssel bleibt als Alias)")
    s.add_argument("quelle", help="der aufgehende Eintrag (z. B. das PDF ohne Angaben)")
    s.add_argument("ziel", help="der bleibende Eintrag (sein Schlüssel und seine Angaben gelten)")
    s.add_argument("--ja", action="store_true")
    s.set_defaults(func=cmd_zusammenfuehren)

    s = sub.add_parser("loeschen", help="Titel samt Dateien löschen")
    s.add_argument("titel")
    s.add_argument("--ja", action="store_true")
    s.set_defaults(func=cmd_loeschen)

    s = sub.add_parser("status", help="Überblick und offene Prüfpunkte")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("beleg", help="wörtliches Zitat gegen den Volltext prüfen (Seite, Abweichungen)")
    s.add_argument("titel")
    s.add_argument("zitat")
    s.add_argument("--seite", help="angegebene Seite, z. B. 45 oder 45f.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_beleg)

    s = sub.add_parser("mcp", help="MCP-Server für KI-Agenten (stdin/stdout): claude mcp add bibliothek -- bib mcp")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("sicherung", help="alles (inkl. Originale von der Ablage) in einen Ordner sichern")
    s.add_argument("ziel", nargs="?", help="Zielordner, z. B. auf einer externen Platte (Standard: zuletzt benutzter)")
    s.set_defaults(func=cmd_sicherung)

    s = sub.add_parser("einstellung", help="Bibliotheksordner, OCR-Sprache")
    s.add_argument("--bibliothek")
    s.add_argument("--ocr-sprache", help="Tesseract-Codes, z. B. eng+deu, deu_latf für Fraktur")
    s.add_argument("--ocr-engine", choices=config.OCR_ENGINES,
                   help="auto = Apple Vision auf dem Mac, sonst Tesseract; tesseract = nur Open Source")
    s.add_argument("--sicherung-extern", choices=["ja", "nein"],
                   help="ja: die Ablage wird anderweitig gesichert (z. B. vom NAS), keine Erinnerung")
    s.set_defaults(func=cmd_einstellung)

    s = sub.add_parser("nas", help="Nextcloud oder WebDAV-Server als Ablage: verbinden, status, trennen")
    s.add_argument("aktion", choices=["verbinden", "status", "trennen"], nargs="?", default="status")
    s.add_argument("server", nargs="?", help="Nextcloud-Adresse, z. B. http://192.168.0.70:9001, oder WebDAV-Adresse")
    s.add_argument("--webdav", action="store_true", help="anderer WebDAV-Server statt Nextcloud (fragt das Passwort)")
    s.add_argument("--benutzer", help="mit --webdav: Benutzername")
    s.add_argument("--ordner", default="Bibliothek", help="Ordner in der Ablage (Standard: Bibliothek)")
    s.add_argument("--ohne-browser", action="store_true", help="Login-Link nur anzeigen")
    s.set_defaults(func=cmd_nas)

    s = sub.add_parser("sync", help="mit der Ablage abgleichen (inkl. Eingangsordner)")
    s.add_argument("--ohne-eingang", action="store_true")
    s.add_argument("--ohne-embeddings", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("oeffne", help="Originaldatei öffnen (holt sie bei Bedarf vom NAS)")
    s.add_argument("titel")
    s.add_argument("datei", nargs="?")
    s.add_argument("--nur-pfad", action="store_true")
    s.set_defaults(func=cmd_oeffne)

    s = sub.add_parser("web", help="Oberfläche öffnen")
    s.set_defaults(func=cmd_web)
    return p


EPILOG = """\
Beispiele:
  bib suche "Gattung als Lesekonvention"            hybrid (Wörter + Bedeutung)
  bib suche '"historischer Präzedenzfall"' --modus text
  bib zeige feestuart2015effektives --seiten 45-46   Druckseiten mit Seitenmarken
  bib neu ~/Downloads/aufsatz.pdf --tag Jona --projekt Hermeneutik
  bib scans ~/Bilder/Buchfotos --titel "…" --autor "Nachname, Vorname" --jahr 2019
  bib seiten feestuart2015effektives --scan 23 --druckseite 11
  bib bibtex --aus-tex lectures/Hermeneutik/arbeit/src -o lectures/Hermeneutik/arbeit/src/references.bib

Seitenangaben: „S. 45“ ist geprüft oder stammt aus dem PDF; „S. 45?“ ist geschätzt;
„Scan 57“ heißt: Druckseite unbekannt — so nicht zitieren.
"""


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyError as exc:
        print(exc.args[0] if exc.args else exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
