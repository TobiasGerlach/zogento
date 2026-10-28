"""Metadaten in offenen Katalogen nachschlagen: DNB, Crossref, OpenAlex, Open Library.

Grundsatz wie überall: nichts raten. Jeder Vorschlag nennt seine Quelle und wie
gut er belegt ist:

    sicher          ISBN/DOI steht im Dokument selbst und der Katalogtitel auch
    wahrscheinlich  Titel und ein Autorname stehen im Dokument
    möglich         nur über die Titelsuche gefunden — von Hand prüfen

Übernommen wird nur, was jemand auswählt; automatisch höchstens „sicher“.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from xml.etree import ElementTree as ET

import requests

from .store import Item, Library, Page

UA = {"User-Agent": "Bibliothek/0.1 (Literaturverwaltung; https://github.com/TobiasGerlach/zogento)"}
TIMEOUT = (4, 20)
SICHER, WAHRSCHEINLICH, MOEGLICH = "sicher", "wahrscheinlich", "möglich"
_RANG = {SICHER: 0, WAHRSCHEINLICH: 1, MOEGLICH: 2}


@dataclass
class Vorschlag:
    quelle: str                   # DNB, Crossref, OpenAlex, Open Library
    url: str                      # Katalogeintrag zum Nachsehen
    eintrag: str                  # BibTeX-Typ
    felder: dict[str, str]
    sicherheit: str = MOEGLICH
    begruendung: list[str] = field(default_factory=list)
    oa_pdf: str = ""              # frei zugängliches PDF (OpenAlex), falls bekannt

    def to_dict(self) -> dict:
        return asdict(self)


class KatalogFehler(RuntimeError):
    pass


# --- Kennungen im Dokument ------------------------------------------------------------------

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}]+)", re.I)
_ISBN = re.compile(r"(?:ISBN(?:-1[03])?:?\s*)?((?:97[89][\s-]?)?(?:\d[\s-]?){9}[\dXx])\b")


def isbn_gueltig(isbn: str) -> bool:
    s = re.sub(r"[^0-9X]", "", isbn.upper())
    if len(s) == 10:
        return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s)) % 11 == 0
    if len(s) == 13 and s.isdigit():
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s)) % 10 == 0
    return False


def kennungen(pages: list[Page]) -> dict[str, dict[str, int]]:
    """DOIs und ISBNs aus Titelei und Schluss (erste 8, letzte 2 Seiten), mit Scan-Nummer."""
    ausgewaehlt = pages[:8] + [p for p in pages[-2:] if p.scan > 8]
    dois: dict[str, int] = {}
    isbns: dict[str, int] = {}
    for p in ausgewaehlt:
        text = p.text.replace("­", "")
        doi_spannen = []
        for m in _DOI.finditer(text):
            doi = m.group(1).rstrip(".,;)]").lower()
            dois.setdefault(doi, p.scan)
            doi_spannen.append(m.span())
        for m in _ISBN.finditer(text):
            roh = m.group(1)
            if any(a <= m.start(1) < b for a, b in doi_spannen):
                continue   # ISBN als Teil einer DOI (Verlags-DOIs für Buchkapitel)
            # Ohne „ISBN“ davor nur 13-stellige mit 978/979 — sonst trifft man Telefonnummern
            if "isbn" not in text[max(0, m.start() - 6):m.start() + 5].lower() and not re.sub(r"\D", "", roh).startswith(("978", "979")):
                continue
            if isbn_gueltig(roh):
                isbns.setdefault(re.sub(r"[^0-9X]", "", roh.upper()), p.scan)
    return {"doi": dois, "isbn": isbns}


# --- Abfragen -------------------------------------------------------------------------------

def _get(url: str, **params) -> requests.Response:
    try:
        r = requests.get(url, params=params or None, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise KatalogFehler(f"{urllib.parse.urlparse(url).netloc} nicht erreichbar") from exc
    return r


def _crossref_felder(m: dict) -> tuple[str, dict[str, str]]:
    typ = {"journal-article": "article", "book-chapter": "incollection", "book": "book", "monograph": "book",
           "edited-book": "collection", "proceedings-article": "inproceedings", "reference-entry": "inreference",
           "dissertation": "thesis"}.get(m.get("type", ""), "misc")
    f: dict[str, str] = {}
    if m.get("title"):
        f["title"] = _sauber(m["title"][0])
    if m.get("subtitle"):
        f["subtitle"] = _sauber(m["subtitle"][0])
    for rolle in ("author", "editor"):
        namen = [_name(a.get("family", ""), a.get("given", "")) for a in m.get(rolle, []) if a.get("family")]
        if namen:
            f[rolle] = " and ".join(namen)
    for k in ("published-print", "published-online", "issued"):
        teile = (m.get(k) or {}).get("date-parts") or [[None]]
        if teile[0] and teile[0][0]:
            f["year"] = str(teile[0][0])
            break
    if m.get("container-title"):
        f["journaltitle" if typ == "article" else "booktitle"] = _sauber(m["container-title"][0])
    for src, dst in (("volume", "volume"), ("issue", "number"), ("publisher", "publisher")):
        if m.get(src):
            f[dst] = str(m[src])
    if m.get("page"):
        f["pages"] = m["page"].replace("-", "--")
    if m.get("ISBN"):
        f["isbn"] = m["ISBN"][0]
    if m.get("DOI"):
        f["doi"] = m["DOI"].lower()
    if typ == "article":
        f.pop("publisher", None)
    return typ, f


def crossref_doi(doi: str) -> Vorschlag | None:
    r = _get(f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}")
    if r.status_code != 200:
        return None
    typ, f = _crossref_felder(r.json()["message"])
    return Vorschlag("Crossref", f"https://doi.org/{doi}", typ, f)


def crossref_suche(titel: str, autor: str = "") -> list[Vorschlag]:
    r = _get("https://api.crossref.org/works", **{"query.bibliographic": f"{titel} {autor}".strip(), "rows": 4})
    if r.status_code != 200:
        return []
    out = []
    for m in r.json()["message"].get("items", []):
        typ, f = _crossref_felder(m)
        if f.get("title"):
            out.append(Vorschlag("Crossref", f"https://doi.org/{m.get('DOI', '')}", typ, f))
    return out


def _openalex_vorschlag(w: dict) -> Vorschlag:
    typ = {"article": "article", "book": "book", "book-chapter": "incollection", "dissertation": "thesis",
           "review": "article"}.get(w.get("type", ""), "misc")
    f: dict[str, str] = {}
    if w.get("title"):
        f["title"] = _sauber(w["title"])
    namen = [_name_aus_anzeige(a["author"]["display_name"]) for a in w.get("authorships", [])
             if (a.get("author") or {}).get("display_name")]
    if namen:
        f["author"] = " and ".join(namen)
    if w.get("publication_year"):
        f["year"] = str(w["publication_year"])
    quelle = ((w.get("primary_location") or {}).get("source") or {})
    if quelle.get("display_name") and typ in ("article", "incollection"):
        f["journaltitle" if typ == "article" else "booktitle"] = quelle["display_name"]
    b = w.get("biblio") or {}
    if b.get("volume"):
        f["volume"] = b["volume"]
    if b.get("issue"):
        f["number"] = b["issue"]
    if b.get("first_page"):
        f["pages"] = b["first_page"] + (f"--{b['last_page']}" if b.get("last_page") and b["last_page"] != b["first_page"] else "")
    if w.get("doi"):
        f["doi"] = w["doi"].removeprefix("https://doi.org/").lower()
    oa = (w.get("best_oa_location") or {}).get("pdf_url") or ""
    return Vorschlag("OpenAlex", w.get("id", ""), typ, f, oa_pdf=oa)


def openalex_doi(doi: str) -> Vorschlag | None:
    r = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}")
    return _openalex_vorschlag(r.json()) if r.status_code == 200 else None


def openalex_suche(titel: str, autor: str = "") -> list[Vorschlag]:
    r = _get("https://api.openalex.org/works", search=f"{titel} {autor}".strip(), **{"per-page": 4})
    if r.status_code != 200:
        return []
    return [_openalex_vorschlag(w) for w in r.json().get("results", []) if w.get("title")]


_MARC = "{http://www.loc.gov/MARC21/slim}"


def _dnb(query: str, n: int = 5) -> list[Vorschlag]:
    r = _get("https://services.dnb.de/sru/dnb", version="1.1", operation="searchRetrieve", query=query,
             recordSchema="MARC21-xml", maximumRecords=str(n))
    if r.status_code != 200:
        return []
    out = []
    for rec in ET.fromstring(r.content).iter(_MARC + "record"):
        felder: dict[str, list[dict[str, str]]] = {}
        idn = ""
        for df in rec:
            if df.tag == _MARC + "controlfield" and df.get("tag") == "001":
                idn = (df.text or "").strip()
            if df.tag != _MARC + "datafield":
                continue
            sub: dict[str, str] = {}
            for sf in df:
                sub.setdefault(sf.get("code"), (sf.text or "").strip())
            felder.setdefault(df.get("tag"), []).append(sub)

        def erst(tag, code):
            return next((s[code] for s in felder.get(tag, []) if s.get(code)), "")

        f: dict[str, str] = {}
        titel = erst("245", "a")
        if not titel:
            continue
        f["title"] = _sauber(titel.rstrip(" /:;"))
        if erst("245", "b"):
            sub = _sauber(erst("245", "b").rstrip(" /:;"))
            f["subtitle"] = sub[:1].upper() + sub[1:]   # DNB schreibt Untertitel klein
        autoren = [s["a"] for s in felder.get("100", []) if s.get("a")]
        autoren += [s["a"] for s in felder.get("700", []) if s.get("a") and s.get("4", "aut") == "aut"]
        hrsg = [s["a"] for s in felder.get("700", []) if s.get("a") and s.get("4") == "edt"]
        if autoren:
            f["author"] = " and ".join(dict.fromkeys(_sauber(a.rstrip(",")) for a in autoren))
        if hrsg:
            f["editor"] = " and ".join(dict.fromkeys(_sauber(a.rstrip(",")) for a in hrsg))
        ort, verlag, jahr = (erst("264", "a") or erst("260", "a"), erst("264", "b") or erst("260", "b"),
                             erst("264", "c") or erst("260", "c"))
        if ort:
            f["location"] = _sauber(ort.strip("[] :;"))
        if verlag:
            f["publisher"] = _sauber(verlag.strip("[] ,;"))
        m = re.search(r"\d{4}", jahr)
        if m:
            f["year"] = m.group(0)
        aufl = re.match(r"\s*(\d+)\.", erst("250", "a"))
        if aufl and aufl.group(1) != "1":
            f["edition"] = aufl.group(1)
        reihe = erst("490", "a") or erst("830", "a")
        if reihe:
            f["series"] = _sauber(reihe)
            if erst("490", "v"):
                f["number"] = _sauber(erst("490", "v"))
        isbn = next((re.sub(r"[^0-9X]", "", s["a"].upper()) for s in felder.get("020", []) if s.get("a")), "")
        if isbn:
            f["isbn"] = isbn
        out.append(Vorschlag("DNB", f"https://d-nb.info/{idn}" if idn else "https://portal.dnb.de",
                             "collection" if hrsg and not autoren else "book", f))
    return out


def dnb_isbn(isbn: str) -> list[Vorschlag]:
    return _dnb(f"num={isbn}", 3)


def dnb_suche(titel: str, autor: str = "") -> list[Vorschlag]:
    woerter = " ".join(_woerter(titel)[:6])
    if not woerter:
        return []
    q = f'tit="{woerter}"' + (f' and per="{autor}"' if autor else "")
    return _dnb(q, 4)


def openlibrary_isbn(isbn: str) -> Vorschlag | None:
    r = _get("https://openlibrary.org/api/books", bibkeys=f"ISBN:{isbn}", format="json", jscmd="data")
    if r.status_code != 200:
        return None
    d = r.json().get(f"ISBN:{isbn}")
    if not d:
        return None
    f = {"title": _sauber(d.get("title", ""))}
    if d.get("subtitle"):
        f["subtitle"] = _sauber(d["subtitle"])
    namen = [_name_aus_anzeige(a["name"]) for a in d.get("authors", []) if a.get("name")]
    if namen:
        f["author"] = " and ".join(namen)
    if d.get("publishers"):
        f["publisher"] = d["publishers"][0]["name"]
    if d.get("publish_places"):
        f["location"] = d["publish_places"][0]["name"]
    m = re.search(r"\d{4}", d.get("publish_date", ""))
    if m:
        f["year"] = m.group(0)
    f["isbn"] = isbn
    return Vorschlag("Open Library", d.get("url", "https://openlibrary.org"), "book", f)


# --- Zusammenführen ---------------------------------------------------------------------------

def vorschlaege(lib: Library, item: Item, *, titel: str | None = None, autor: str | None = None) -> list[Vorschlag]:
    """Kandidaten aus allen Katalogen, nach Belegstärke sortiert."""
    pages = lib.pages(item.citekey) if (lib.folder(item.citekey) / "seiten.jsonl").exists() else []
    vorn = "\n".join(p.text for p in pages[:8])
    kopf = "\n".join(p.text for p in pages[:2])[:3000]
    ids = kennungen(pages)
    for k, scan in (("doi", 0), ("isbn", 0)):
        if item.felder.get(k):
            ids[k].setdefault(re.sub(r"[^0-9X]", "", item.felder[k].upper()) if k == "isbn" else item.felder[k].lower(), scan)
    gefunden: list[tuple[Vorschlag, str]] = []   # (Vorschlag, Beleg)
    fehler = []

    def versuch(fn, *a):
        try:
            return fn(*a)
        except KatalogFehler as exc:
            fehler.append(str(exc))
            return None

    for doi, scan in list(ids["doi"].items())[:3]:
        for fn in (crossref_doi, openalex_doi):
            v = versuch(fn, doi)
            if v:
                gefunden.append((v, f"DOI {doi} " + (f"auf Scan {scan}" if scan else "aus den Metadaten")))
    for isbn, scan in list(ids["isbn"].items())[:3]:
        treffer = versuch(dnb_isbn, isbn) or []
        ol = versuch(openlibrary_isbn, isbn)
        for v in treffer + ([ol] if ol else []):
            gefunden.append((v, f"ISBN {isbn} " + (f"auf Scan {scan}" if scan else "aus den Metadaten")))

    such_titel = titel or _brauchbarer_titel(item, pages)
    such_autor = autor if autor is not None else _erster_nachname(item.felder.get("author", ""))
    if such_titel and not any(_RANG[_bewerten(v, b, vorn, item, kopf)[0]] == 0 for v, b in gefunden):
        for fn in (dnb_suche, crossref_suche, openalex_suche):
            for v in versuch(fn, such_titel, such_autor) or []:
                gefunden.append((v, ""))

    out: list[Vorschlag] = []
    gesehen = set()
    for v, beleg in gefunden:
        v.sicherheit, v.begruendung = _bewerten(v, beleg, vorn, item, kopf)
        schon = lib.find_duplicate(v.felder)
        if schon and schon.citekey != item.citekey:
            # Das Dokument zitiert vermutlich diesen Titel — er ist es nicht selbst
            v.sicherheit = MOEGLICH
            v.begruendung.append(f"schon als {schon.citekey} in der Bibliothek")
        schluessel = (v.quelle, _norm(v.felder.get("title", "")), v.felder.get("year", ""), v.felder.get("isbn", ""))
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        if _RANG[v.sicherheit] < 2 or _aehnlich(v.felder.get("title", ""), such_titel or "") > 0.45:
            out.append(v)
    # Mehrere Kennungen für verschiedene Werke (z. B. Übersetzung + Original im Impressum): Ausgabe prüfen
    sichere = {_norm(v.felder.get("title", "")) for v in out if v.sicherheit == SICHER}
    if len(sichere) > 1:
        for v in out:
            if v.sicherheit == SICHER:
                v.sicherheit = WAHRSCHEINLICH
                v.begruendung.append("Dokument nennt mehrere Werke (Übersetzung/Original?) — Ausgabe prüfen")
    out.sort(key=lambda v: (_RANG[v.sicherheit], -_aehnlich(v.felder.get("title", ""), such_titel or "")))
    if not out and fehler:
        raise KatalogFehler("; ".join(sorted(set(fehler))))
    return out[:8]


def merken(item: Item, vs: list[Vorschlag]) -> None:
    import json

    from . import config
    path = config.data_dir() / "vorschlaege" / f"{item.citekey}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([v.to_dict() for v in vs], ensure_ascii=False), encoding="utf-8")


def gemerkt(item: Item) -> list[Vorschlag]:
    import json

    from . import config
    path = config.data_dir() / "vorschlaege" / f"{item.citekey}.json"
    if not path.exists():
        return []
    return [Vorschlag(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def _bewerten(v: Vorschlag, beleg: str, vorn: str, item: Item, kopf: str | None = None) -> tuple[str, list[str]]:
    """vorn: Titelei (ISBN steht oft im Impressum); kopf: erste Seiten — dort muss stehen, wer und was es ist."""
    kopf = vorn if kopf is None else kopf
    gruende = []
    titel_da = _anteil_im_text(v.felder.get("title", ""), vorn)
    titel_kopf = _anteil_im_text(v.felder.get("title", ""), kopf)
    namen = [_erster_nachname(a) for a in (v.felder.get("author") or v.felder.get("editor") or "").split(" and ")]
    autor_da = any(n and len(n) > 1 and f" {_norm(n)} " in f" {_norm(kopf)} " for n in namen)
    if beleg:
        gruende.append(beleg)
    if titel_da >= 0.75:
        gruende.append("Titel steht im Dokument")
    if autor_da:
        gruende.append("Autorname steht am Anfang des Dokuments")
    if not vorn:
        gruende.append("kein Text zum Gegenprüfen")
    if beleg and titel_da >= 0.75:
        return SICHER, gruende
    if titel_kopf >= 0.75 and autor_da:
        return WAHRSCHEINLICH, gruende
    return MOEGLICH, gruende


def uebernehmen(lib: Library, item: Item, v: Vorschlag, *, nur_fehlende: bool = False,
                neuer_key: bool = True) -> Item:
    """Vorschlag eintragen; Herkunft als Kommentar über dem BibTeX-Eintrag. Gibt den (ggf. umbenannten) Titel zurück."""
    from . import ingest

    for k, wert in v.felder.items():
        if wert and (not nur_fehlende or not item.felder.get(k)):
            item.felder[k] = wert
    if not nur_fehlende:
        item.eintrag = v.eintrag
    belege = "; ".join(v.begruendung) or "Titelsuche"
    vermerk = f"Angaben aus {v.quelle} ({v.url}), {v.sicherheit}: {belege}. Übernommen {dt.date.today().isoformat()}."
    item.kommentar = (item.kommentar + "\n" if item.kommentar else "") + vermerk
    item.text["metadaten"] = {"quelle": v.quelle, "url": v.url, "sicherheit": v.sicherheit, "belege": v.begruendung}
    item.unvollstaendig = [f for f in ingest.PFLICHT if not item.felder.get(f)
                           and not (f == "author" and item.felder.get("editor"))
                           and not (f == "year" and item.felder.get("date"))]
    lib.save(item)
    if neuer_key and ist_automatischer_key(item.citekey):
        neu = lib.new_citekey(item.felder)
        if neu != item.citekey:
            alt = item.citekey
            item = lib.rename(item, neu)
            ids = [x.strip() for x in item.felder.get("ids", "").split(",") if x.strip()]
            item.felder["ids"] = ", ".join(ids + [alt])   # biblatex: alter Schlüssel zitiert weiter
            lib.save(item)
    return item


def ist_automatischer_key(key: str) -> bool:
    """Schlüssel aus fehlenden Angaben („anonym…“, „…oj…“) — die dürfen sich ändern."""
    return key.startswith("anonym") or ("oj" in key and not re.search(r"\d{4}", key))


# --- Hilfen -------------------------------------------------------------------------------------

def _sauber(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)            # Crossref-Titel mit <i>…</i>
    s = s.replace("\u0098", "").replace("\u009c", "")   # DNB-Nichtsortierzeichen
    s = re.sub(r"\s+", " ", s).strip()
    # Gerade Anführungszeichen vertragen sich nicht mit babel (ngerman); biblatex setzt \enquote passend
    return re.sub(r'["„“”]([^"„“”]+)["“”]', r"\\enquote{\1}", s)


_PARTIKEL = {"van", "von", "de", "der", "den", "du", "la", "le", "ten", "ter", "di", "da", "del", "dos", "zu"}


def _name(family: str, given: str) -> str:
    return f"{family.strip()}, {given.strip()}" if given.strip() else family.strip()


def _name_aus_anzeige(anzeige: str) -> str:
    """„David J. Bosch“ → „Bosch, David J.“, „Gerhard von Rad“ → „von Rad, Gerhard“."""
    teile = anzeige.split()
    if len(teile) < 2:
        return anzeige.strip()
    i = len(teile) - 1
    while i > 1 and teile[i - 1].lower() in _PARTIKEL:
        i -= 1
    return _name(" ".join(teile[i:]), " ".join(teile[:i]))


def _erster_nachname(autoren: str) -> str:
    erster = autoren.split(" and ")[0].strip()
    return erster.split(",")[0].strip() if "," in erster else (erster.split()[-1] if erster else "")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("\\enquote", "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _woerter(s: str) -> list[str]:
    return [w for w in _norm(s).split() if len(w) > 2]


def _anteil_im_text(titel: str, text: str) -> float:
    w = _woerter(titel)
    if len(w) < 2 or not text:   # ein einzelnes Wort („Theology“) beweist nichts
        return 0.0
    t = " " + _norm(text) + " "
    return sum(1 for x in w if f" {x} " in t) / len(w)


def _aehnlich(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio() if a and b else 0.0


def _brauchbarer_titel(item: Item, pages: list[Page]) -> str:
    """Titel aus den Metadaten, wenn er nicht nach Dateiname aussieht; sonst die längste Zeile der ersten Seite."""
    t = item.felder.get("title", "")
    if t and len(_woerter(t)) >= 2 and not re.search(r"\d{2,}|_|\.pdf$", t):
        return t
    for p in pages[:3]:
        zeilen = [z.strip() for z in p.text.splitlines() if 12 <= len(z.strip()) <= 140 and not re.search(r"\d{3,}", z)]
        if zeilen:
            return max(zeilen[:8], key=len)
    return t


# --- Aufnehmen per DOI/ISBN -------------------------------------------------------------------

MAX_PDF = 200 * 1024 * 1024


def nachschlagen(*, doi: str = "", isbn: str = "") -> Vorschlag | None:
    """Genau ein Datensatz zur Kennung; bei DOI mit Hinweis auf ein frei zugängliches PDF."""
    if doi:
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip(), flags=re.I).lower()
        v = crossref_doi(doi)
        oa = openalex_doi(doi)
        if v and oa:
            v.oa_pdf = oa.oa_pdf
        v = v or oa
        if v:
            v.begruendung = [f"DOI {doi} angegeben"]
        return v
    if isbn:
        isbn = re.sub(r"[^0-9X]", "", isbn.upper())
        if not isbn_gueltig(isbn):
            raise KatalogFehler(f"ISBN {isbn} ist ungültig (Prüfziffer).")
        v = (dnb_isbn(isbn) or [None])[0] or openlibrary_isbn(isbn)
        if v:
            v.begruendung = [f"ISBN {isbn} angegeben"]
            v.felder.setdefault("isbn", isbn)
        return v
    return None


def aufnehmen(lib: Library, *, doi: str = "", isbn: str = "", tags: list[str] | None = None,
              projekte: list[str] | None = None, kategorien: list[str] | None = None, pdf: bool = True,
              progress=None) -> tuple[Item, str]:
    """Titel per Kennung anlegen. Gibt (Titel, Hinweis) zurück."""
    import tempfile
    from pathlib import Path

    from . import ingest

    progress = progress or (lambda _: None)
    progress("Katalog abfragen …")
    v = nachschlagen(doi=doi, isbn=isbn)
    if not v or not v.felder.get("title"):
        raise KatalogFehler(f"Zu {'DOI ' + doi if doi else 'ISBN ' + isbn} nichts gefunden.")
    dup = lib.find_duplicate(v.felder)
    if dup:
        raise ingest.Duplicate(dup)
    vermerk = f"Angaben aus {v.quelle} ({v.url}) zur {v.begruendung[0]}, übernommen {dt.date.today().isoformat()}."
    hinweis = ""
    if pdf and v.oa_pdf:
        progress(f"Frei zugängliches PDF laden: {v.oa_pdf}")
        with tempfile.TemporaryDirectory() as tmp:
            datei = Path(tmp) / (re.sub(r"[^A-Za-z0-9]+", "_", v.felder.get("title", "dokument"))[:60] + ".pdf")
            if _pdf_laden(v.oa_pdf, datei):
                item = ingest.add_file(lib, datei, felder=dict(v.felder), eintrag=v.eintrag, tags=tags,
                                       projekte=projekte, kategorien=kategorien, herkunft=v.oa_pdf, progress=progress)
                item.kommentar = vermerk + f" PDF (Open Access): {v.oa_pdf}"
                item.text["metadaten"] = {"quelle": v.quelle, "url": v.url, "sicherheit": SICHER, "belege": v.begruendung}
                lib.save(item)
                return item, "mit frei zugänglichem PDF"
            hinweis = "Frei zugängliches PDF ließ sich nicht laden (Verlagsseite statt PDF?) — nur Angaben angelegt."
    elif pdf:
        hinweis = "Kein frei zugängliches PDF bekannt — nur Angaben angelegt. Datei später mit bib neu ergänzen."
    key, missing = ingest._prepare(lib, v.felder, None, allow_duplicate=False)
    item = Item(citekey=key, eintrag=v.eintrag, felder=dict(v.felder), tags=tags or [], projekte=projekte or [],
                kategorien=kategorien or [], unvollstaendig=missing, herkunft=v.url, kommentar=vermerk,
                text={"metadaten": {"quelle": v.quelle, "url": v.url, "sicherheit": SICHER, "belege": v.begruendung},
                      "hinweise": [hinweis] if hinweis else []})
    lib.save(item)
    return item, hinweis


def _pdf_laden(url: str, ziel) -> bool:
    try:
        with requests.get(url, headers=UA, timeout=(5, 60), stream=True, allow_redirects=True) as r:
            if r.status_code != 200:
                return False
            n = 0
            with open(ziel, "wb") as fh:
                for block in r.iter_content(1 << 16):
                    n += len(block)
                    if n > MAX_PDF:
                        return False
                    fh.write(block)
    except requests.RequestException:
        return False
    with open(ziel, "rb") as fh:
        return fh.read(5) == b"%PDF-"
