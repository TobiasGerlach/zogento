"""MCP-Server (Model Context Protocol) über stdin/stdout: die Bibliothek als Werkzeuge für KI-Agenten.

Start: `bib mcp`. Einbinden in Claude Code (im Projektordner):

    claude mcp add bibliothek -- bib mcp

Bewusst ohne MCP-Bibliothek: JSON-RPC 2.0, eine Nachricht pro Zeile. Auf stdout
steht nur das Protokoll; alles andere (Fortschritt, Modell-Logs) geht nach stderr.
"""
from __future__ import annotations

import contextlib
import json
import sys
import traceback

from . import belege, bibtex, config, index, ingest, katalog
from .store import Library, Page

PROTOKOLLE = ("2025-06-18", "2025-03-26", "2024-11-05")

ANLEITUNG = """Zentrale Literaturbibliothek. Regeln für Seitenangaben:
- „S. 45“ / „S. 45 (Scan 57)“ und zitierfaehig=true: zitierbar.
- „S. 45? (…geschätzt)“ oder „Druckseite ungeklärt“: NICHT zitieren — den Menschen bitten, die Seitenzählung
  zu prüfen (App: Prüfen; oder `bib seiten <citekey>`).
- „Abschn. 3“: Format ohne Seiten; Seitenzahl aus anderer Quelle nötig.
Keine Seitenzahl erfinden oder aus Scan-Nummern ableiten. Wörtliche Zitate mit zitat_pruefen kontrollieren.
OCR-Text kann Erkennungsfehler enthalten. Fehlende Metadaten nicht raten: metadaten_vorschlaege zeigt
Katalogtreffer; übernehmen entscheidet der Mensch."""


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_S = {"type": "string"}
_FILTER = {"tag": _S, "projekt": _S, "kategorie": _S}

TOOLS = [
    {"name": "suche", "description": "Volltext + semantisch über alle Texte suchen. Liefert Fundstellen mit ehrlicher "
     "Seitenangabe (seite, zitierfaehig) und Textausschnitt. Phrasen in \"…\", OR/NOT im Textmodus.",
     "inputSchema": _schema({"anfrage": _S, "n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                             "modus": {"type": "string", "enum": ["hybrid", "text", "semantisch"], "default": "hybrid"},
                             "titel": {"type": "array", "items": _S, "description": "nur in diesen Citekeys"},
                             **_FILTER}, ["anfrage"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "lies", "description": "Seiten eines Titels lesen, mit Seitenmarken [[S. 45 (Scan 57)]]. seiten = "
     "Druckseiten (\"45-47\", \"12,15\"); scans = Scan-Nummern, falls die Druckseite unbekannt ist.",
     "inputSchema": _schema({"citekey": _S, "seiten": _S, "scans": _S,
                             "max_zeichen": {"type": "integer", "default": 40000}}, ["citekey"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "titel", "description": "Metadaten, Notizen, Seitenzählung, Dateien und BibTeX-Eintrag eines Titels.",
     "inputSchema": _schema({"citekey": _S}, ["citekey"]), "annotations": {"readOnlyHint": True}},
    {"name": "liste", "description": "Titel auflisten, gefiltert nach Projekt, Tag, Kategorie oder offenen Prüfpunkten.",
     "inputSchema": _schema({**_FILTER, "nur_offene": {"type": "boolean", "description":
                             "nur Titel mit fehlenden Angaben oder ungeprüften Seitenzahlen"}}),
     "annotations": {"readOnlyHint": True}},
    {"name": "bibtex", "description": "BibTeX-Einträge (biblatex) für Citekeys; alte Schlüssel (ids) werden aufgelöst.",
     "inputSchema": _schema({"citekeys": {"type": "array", "items": _S}}, ["citekeys"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "zitat_pruefen", "description": "Prüft ein wörtliches Zitat gegen den Volltext: steht es so da, auf "
     "welcher Seite, ist die Seite zitierfähig, stimmt die angegebene Seite? Nennt Abweichungen Wort für Wort.",
     "inputSchema": _schema({"citekey": _S, "zitat": _S, "seite": {"type": "string", "description":
                             "angegebene Seite, z. B. \"45\" oder \"45f.\""}}, ["citekey", "zitat"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "metadaten_vorschlaege", "description": "Fehlende Angaben in DNB, Crossref, OpenAlex, Open Library "
     "nachschlagen (Netz). Nur Vorschläge mit Quelle und Belegstärke — übernehmen entscheidet der Mensch "
     "(App: Prüfen, oder `bib metadaten <citekey> --nehmen N`).",
     "inputSchema": _schema({"citekey": _S, "suchtitel": _S, "autor": _S}, ["citekey"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
    {"name": "status", "description": "Überblick: Anzahl Titel, offene Prüfpunkte, Sicherung, Index.",
     "inputSchema": _schema({}), "annotations": {"readOnlyHint": True}},
    {"name": "aufnehmen", "description": "Neue Literatur aufnehmen: lokale Datei (pfad) oder per doi/isbn "
     "(Angaben aus Katalogen, frei zugängliches PDF falls vorhanden). Metadaten werden nie geraten.",
     "inputSchema": _schema({"pfad": _S, "doi": _S, "isbn": _S, "projekt": _S,
                             "tags": {"type": "array", "items": _S}}),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}},
]


# --- Werkzeuge ----------------------------------------------------------------------------------

def _treffer(lib: Library, a: dict) -> list[dict]:
    keys = None
    if a.get("tag") or a.get("projekt") or a.get("kategorie") or a.get("titel"):
        keys = {it.citekey for it in lib.items()
                if (not a.get("tag") or a["tag"] in it.tags) and (not a.get("projekt") or a["projekt"] in it.projekte)
                and (not a.get("kategorie") or any(k == a["kategorie"] or k.startswith(a["kategorie"] + "/")
                                                    for k in it.kategorien))}
        if a.get("titel"):
            keys &= {lib.resolve(t).citekey for t in a["titel"]}
    if keys is not None and not keys:
        return []   # Filter passt auf keinen Titel (eine leere Menge hieße für index.search „alle“)
    modus = a.get("modus", "hybrid")
    entfernt = None if a.get("titel") else _app_suche(lib, a, modus)   # die App filtert nicht nach Titeln
    if entfernt is not None:
        return entfernt
    hits = index.search(lib, a["anfrage"], mode=modus, limit=int(a.get("n", 10)), keys=keys)
    out, cache = [], {}
    for h in hits:
        it = cache.setdefault(h.citekey, lib.get(h.citekey))
        p = Page(h.scan, "")
        out.append({"citekey": h.citekey, "kurz": it.short, "titel": it.title, "seite": it.page_ref(p),
                    "zitierfaehig": it.citable(p), "scan": h.scan, "text": h.snippet or h.text[:400],
                    "wege": sorted(h.wege)})
    return out


def _app_suche(lib: Library, a: dict, modus: str) -> list[dict] | None:
    """Laufende App fragen — sie hat das Embedding-Modell schon geladen. None, wenn keine läuft."""
    import urllib.parse
    import urllib.request
    from pathlib import Path

    base = f"http://127.0.0.1:{config.web_port()}"
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=0.4) as r:
            if Path(json.load(r).get("bibliothek", "")) != lib.root:
                return None
        params = [("q", a["anfrage"]), ("modus", modus), ("limit", int(a.get("n", 10)))]
        params += [(k, a[k]) for k in ("tag", "projekt", "kategorie") if a.get(k)]
        with urllib.request.urlopen(base + "/api/suche?" + urllib.parse.urlencode(params), timeout=120) as r:
            treffer = json.load(r)["treffer"]
    except Exception:
        return None
    out = []
    for h in treffer:
        it = lib.get(h["citekey"])
        p = Page(h["scan"], "")
        out.append({"citekey": h["citekey"], "kurz": h["kurz"], "titel": h["titel"], "seite": it.page_ref(p),
                    "zitierfaehig": it.citable(p), "scan": h["scan"], "text": h.get("snippet") or h["text"][:400],
                    "wege": h.get("wege", [])})
    return out


def _bereich(spec: str) -> set[int]:
    out: set[int] = set()
    for teil in spec.replace(" ", "").split(","):
        if not teil:
            continue
        a, _, b = teil.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def _lies(lib: Library, a: dict) -> dict:
    item = lib.resolve(a["citekey"])
    pages = lib.pages(item.citekey) if (lib.folder(item.citekey) / "seiten.jsonl").exists() else []
    if not pages:
        return {"citekey": item.citekey, "text": "", "hinweis": "Kein Volltext vorhanden (nur Literaturangabe)."}
    if a.get("scans"):
        wanted, by_scan = _bereich(a["scans"]), True
    elif a.get("seiten"):
        wanted, by_scan = _bereich(a["seiten"]), False
    else:
        wanted, by_scan = {p.scan for p in pages[:3]}, True
    teile, limit, n = [], int(a.get("max_zeichen", 40000)), 0
    for p in pages:
        num = item.page_number(p)
        key = p.scan if by_scan else (int(num) if num and str(num).isdigit() else None)
        if key in wanted:
            block = f"[[{item.page_ref(p)}]]\n{p.text.strip()}\n"
            if n + len(block) > limit:
                if not teile:
                    teile.append(block[:limit])
                teile.append("[… gekürzt — kleineren Bereich oder größeres max_zeichen anfordern]")
                break
            teile.append(block)
            n += len(block)
    hinweis = "" if teile else ("Keine passenden Seiten. Druckseiten sind nur bekannt, wenn die Seitenzählung gesetzt "
                                "ist — mit scans nach Scan-Nummer wählen.")
    return {"citekey": item.citekey, "seitenzaehlung": (item.seitenzaehlung or {}).get("status"),
            "seiten_gesamt": len(pages), "text": "\n".join(teile), "hinweis": hinweis}


def _titel(lib: Library, a: dict) -> dict:
    item = lib.resolve(a["citekey"])
    notizen = lib.folder(item.citekey) / "notizen.md"
    sz = item.seitenzaehlung or {}
    return {"citekey": item.citekey, "kurz": item.short, "eintrag": item.eintrag, "felder": item.felder,
            "tags": item.tags, "kategorien": item.kategorien, "projekte": item.projekte, "dateien": item.dateien,
            "unvollstaendig": item.unvollstaendig, "notiz": item.notiz,
            "notizen": notizen.read_text(encoding="utf-8") if notizen.exists() else "",
            "seitenzaehlung": {"status": sz.get("status"), "spruenge": sz.get("spruenge", [])},
            "text": {k: item.text.get(k) for k in ("quelle", "seiten", "konfidenz", "metadaten") if item.text.get(k)},
            "bibtex": bibtex.format_entry(item.to_entry()),
            "textdatei": str(lib.folder(item.citekey) / "text.md")}


def _liste(lib: Library, a: dict) -> list[dict]:
    out = []
    for it in lib.items():
        if a.get("tag") and a["tag"] not in it.tags:
            continue
        if a.get("projekt") and a["projekt"] not in it.projekte:
            continue
        if a.get("kategorie") and not any(k == a["kategorie"] or k.startswith(a["kategorie"] + "/") for k in it.kategorien):
            continue
        st = (it.seitenzaehlung or {}).get("status")
        offen = bool(it.unvollstaendig) or (st in ("automatisch", "unbekannt") and it.text.get("seiten"))
        if a.get("nur_offene") and not offen:
            continue
        out.append({"citekey": it.citekey, "kurz": it.short, "titel": it.title, "eintrag": it.eintrag,
                    "seitenzaehlung": st, "volltext": bool(it.text.get("seiten")),
                    "fehlt": it.unvollstaendig, "projekte": it.projekte, "tags": it.tags})
    return out


def _bibtex(lib: Library, a: dict) -> dict:
    eintraege, fehlt = [], []
    for k in a["citekeys"]:
        it = lib.get(k) if lib.exists(k) else lib.by_alias(k)
        if it:
            eintraege.append(bibtex.format_entry(it.to_entry()))
        else:
            fehlt.append(k)
    return {"bibtex": "\n\n".join(eintraege), "nicht_gefunden": fehlt}


def _status(lib: Library, a: dict) -> dict:
    from . import sicherung

    items = list(lib.items())
    s = index.stats(lib)
    return {"bibliothek": str(lib.root), "titel": len(items),
            "mit_volltext": sum(1 for i in items if i.text.get("seiten")),
            "angaben_fehlen": [i.citekey for i in items if i.unvollstaendig],
            "seiten_ungeprueft": [i.citekey for i in items if (i.seitenzaehlung or {}).get("status")
                                  in ("automatisch", "unbekannt") and i.text.get("seiten")],
            "index": s, "sicherung": sicherung.status()}


def _aufnehmen(lib: Library, a: dict) -> dict:
    from pathlib import Path

    projekte = [a["projekt"]] if a.get("projekt") else []
    tags = a.get("tags") or []
    try:
        if a.get("pfad"):
            item = ingest.add_file(lib, Path(a["pfad"]).expanduser(), projekte=projekte, tags=tags,
                                   progress=lambda m: print(m, file=sys.stderr))
            hinweis = "; ".join(item.text.get("hinweise", []))
        elif a.get("doi") or a.get("isbn"):
            item, hinweis = katalog.aufnehmen(lib, doi=a.get("doi", ""), isbn=a.get("isbn", ""), projekte=projekte,
                                              tags=tags, progress=lambda m: print(m, file=sys.stderr))
        else:
            raise ValueError("pfad, doi oder isbn angeben")
    except ingest.Duplicate as d:
        return {"neu": False, "vorhanden": d.item.citekey, "hinweis": str(d)}
    lib.write_refs_bib()
    from . import sync
    sync.versuchen(lib)
    return {"neu": True, "citekey": item.citekey, "kurz": item.short, "titel": item.title,
            "fehlt": item.unvollstaendig, "hinweis": hinweis}


def _metadaten(lib: Library, a: dict) -> dict:
    item = lib.resolve(a["citekey"])
    vs = katalog.vorschlaege(lib, item, titel=a.get("suchtitel"), autor=a.get("autor"))
    katalog.merken(item, vs)
    return {"citekey": item.citekey, "jetzt": item.felder, "fehlt": item.unvollstaendig,
            "vorschlaege": [dict(nr=i, **v.to_dict()) for i, v in enumerate(vs, 1)],
            "uebernehmen": f"Nach Rückfrage beim Menschen: bib metadaten {item.citekey} --nehmen N"}


AUSFUEHREN = {"suche": _treffer, "lies": _lies, "titel": _titel, "liste": _liste, "bibtex": _bibtex,
              "zitat_pruefen": lambda lib, a: belege.pruefen(lib, lib.resolve(a["citekey"]), a["zitat"],
                                                            seite=a.get("seite")),
              "metadaten_vorschlaege": _metadaten, "status": _status, "aufnehmen": _aufnehmen}


# --- Protokoll ----------------------------------------------------------------------------------

def _version() -> str:
    try:
        from importlib.metadata import version
        return version("bibliothek")
    except Exception:
        return "0"


def behandeln(lib: Library, msg: dict) -> dict | None:
    """Eine JSON-RPC-Nachricht beantworten (None bei Benachrichtigungen)."""
    mid, methode, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if methode == "initialize":
            gewuenscht = params.get("protocolVersion")
            ergebnis = {"protocolVersion": gewuenscht if gewuenscht in PROTOKOLLE else PROTOKOLLE[0],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "bibliothek", "version": _version()}, "instructions": ANLEITUNG}
        elif methode == "ping":
            ergebnis = {}
        elif methode == "tools/list":
            ergebnis = {"tools": TOOLS}
        elif methode == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in AUSFUEHREN:
                return _fehler(mid, -32602, f"Unbekanntes Werkzeug: {name}")
            try:
                daten = AUSFUEHREN[name](lib, args)
                text = json.dumps(daten, ensure_ascii=False, indent=1)
                ergebnis = {"content": [{"type": "text", "text": text}], "isError": False}
                if isinstance(daten, dict):
                    ergebnis["structuredContent"] = daten
            except (KeyError, ValueError, katalog.KatalogFehler) as exc:
                text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
                ergebnis = {"content": [{"type": "text", "text": str(text)}], "isError": True}
        else:
            return _fehler(mid, -32601, f"Methode nicht unterstützt: {methode}")
    except Exception as exc:   # nie den Server sterben lassen
        traceback.print_exc(file=sys.stderr)
        return _fehler(mid, -32603, f"{type(exc).__name__}: {exc}")
    return {"jsonrpc": "2.0", "id": mid, "result": ergebnis}


def _fehler(mid, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}


def main() -> int:
    protokoll = sys.stdout
    lib = Library(config.library_root())
    for zeile in sys.stdin:
        if not zeile.strip():
            continue
        try:
            msg = json.loads(zeile)
        except ValueError:
            protokoll.write(json.dumps(_fehler(None, -32700, "Ungültiges JSON")) + "\n")
            protokoll.flush()
            continue
        nachrichten = msg if isinstance(msg, list) else [msg]
        with contextlib.redirect_stdout(sys.stderr):   # Ausgaben der Bibliothek nicht ins Protokoll
            antworten = [a for a in (behandeln(lib, m) for m in nachrichten) if a]
        if antworten:
            protokoll.write(json.dumps(antworten if isinstance(msg, list) else antworten[0], ensure_ascii=False) + "\n")
            protokoll.flush()
    return 0
