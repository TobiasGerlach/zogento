"""Die Ablage: ein Ordner pro Titel unter titel/<citekey>/.

    meta.yaml     Metadaten (BibTeX-Felder wörtlich, dazu Tags, Kategorien, Projekte, Seitenzählung)
    seiten.jsonl  der extrahierte Text, eine Zeile pro Seite bzw. Abschnitt — die Textquelle
    text.md       daraus erzeugte Lesefassung mit Seitenmarken, gedacht für KI und grep
    notizen.md    eigene Notizen, Zotero-Anmerkungen (optional)
    <Originaldateien>

Die Dateien sind die einzige Wahrheit; der Suchindex wird daraus abgeleitet
und liegt lokal (config.index_path), nicht im Bibliotheksordner.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from . import bibtex, config

STATUS_BESTAETIGT = "bestätigt"   # von Tobias geprüft
STATUS_PDF = "pdf"                # Seitenlabels aus dem PDF selbst
STATUS_AUTO = "automatisch"       # aus Seitenzahlen im OCR-Text geschätzt, ungeprüft
STATUS_UNBEKANNT = "unbekannt"
STATUS_KEINE = "keine"            # Format ohne Seiten (Word, HTML, EPUB): Abschnitte


@dataclass
class Page:
    scan: int                     # laufende Nummer im Dokument, ab 1
    text: str
    label: str | None = None      # Seitenlabel aus dem PDF, falls vorhanden
    konfidenz: float | None = None


@dataclass
class Item:
    citekey: str
    eintrag: str = "book"                      # BibTeX-Eintragstyp
    felder: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    kategorien: list[str] = field(default_factory=list)
    projekte: list[str] = field(default_factory=list)
    dateien: list[str] = field(default_factory=list)
    text: dict = field(default_factory=dict)   # quelle, seiten, zeichen, ocr_ordner, extrahiert, hinweise
    seitenzaehlung: dict = field(default_factory=lambda: {"status": STATUS_UNBEKANNT})  # + segmente, ausnahmen
    notiz: str = ""
    kommentar: str = ""                        # BibTeX-Kommentar vor dem Eintrag (z. B. Prüfvermerke)
    unvollstaendig: list[str] = field(default_factory=list)  # fehlende Pflichtangaben, nicht erfinden
    herkunft: str = ""
    hinzugefuegt: str = field(default_factory=lambda: dt.date.today().isoformat())

    # -- Anzeige ------------------------------------------------------------
    @property
    def title(self) -> str:
        return bibtex.strip_tex(self.felder.get("title", "")) or self.citekey

    @property
    def authors_short(self) -> str:
        names = bibtex.split_names(self.felder.get("author") or self.felder.get("editor") or "")
        lasts = [bibtex.strip_tex(bibtex.last_name(n)) for n in names]
        if not lasts:
            return "o. A."
        if len(lasts) > 3:
            return lasts[0] + " u. a."
        return "/".join(lasts)

    @property
    def year(self) -> str:
        m = re.search(r"\d{4}", self.felder.get("year") or self.felder.get("date") or "")
        return m.group(0) if m else "o. J."

    @property
    def short(self) -> str:
        return f"{self.authors_short} {self.year}"

    @property
    def folder(self) -> Path:
        return config.ITEMS / self.citekey

    # -- Seitenzählung --------------------------------------------------------
    def segments(self) -> list[dict]:
        """Abschnitte der Seitenzählung [{ab_scan, versatz}]; älteres Format (ein Versatz) wird umgedeutet."""
        sz = self.seitenzaehlung or {}
        if sz.get("segmente"):
            return sorted(sz["segmente"], key=lambda s: s["ab_scan"])
        if sz.get("versatz") is not None:
            return [{"ab_scan": int(sz.get("ab_scan") or 1), "versatz": int(sz["versatz"])}]
        return []

    def _segment(self, scan: int) -> dict | None:
        found = None
        for seg in self.segments():
            if scan >= seg["ab_scan"]:
                found = seg
        return found

    def page_number(self, p: Page) -> str | None:
        """Druckseite als Text, oder None, wenn sie nicht belegt ist."""
        sz = self.seitenzaehlung or {}
        status = sz.get("status")
        ausnahmen = sz.get("ausnahmen") or {}
        if str(p.scan) in ausnahmen:
            return str(ausnahmen[str(p.scan)]) or None
        segs = self.segments()
        if status in (STATUS_BESTAETIGT, STATUS_AUTO) and segs:
            seg = self._segment(p.scan)
            if seg is None:
                return None
            n = p.scan + int(seg["versatz"])
            return str(n) if n > 0 else None
        if status in (STATUS_PDF, STATUS_BESTAETIGT) and p.label:
            return p.label
        return None

    def teile(self) -> int:
        """Wie viele Seiten/Spalten ein PDF-Scan enthält (Doppelseiten: 2), aufgeteilt in eigene Abschnitte."""
        return int((self.text.get("teilung") or {}).get("teile", 1))

    def scan_name(self, scan: int) -> str:
        """„Scan 57“ — bei geteilten Scans die Stelle im PDF: „PDF-S. 10 rechts“, „PDF-S. 3, Spalte 2/4“."""
        n = self.teile()
        if n == 1:
            return f"Scan {scan}"
        seite, teil = (scan - 1) // n + 1, (scan - 1) % n
        if n == 2:
            return f"PDF-S. {seite} {'links' if teil == 0 else 'rechts'}"
        return f"PDF-S. {seite}, Spalte {teil + 1}/{n}"

    def page_ref(self, p: Page) -> str:
        """Seitenangabe für Ausgaben. Unsichere Angaben sind als solche markiert."""
        sz = self.seitenzaehlung or {}
        status = sz.get("status")
        if status == STATUS_KEINE:
            return f"Abschn. {p.scan}"
        einheit = sz.get("einheit") or "S."
        num = self.page_number(p)
        wo = self.scan_name(p.scan)
        if num is None:
            return f"{wo} ({'Spalte' if einheit == 'Sp.' else 'Druckseite'} ungeklärt)"
        if status == STATUS_AUTO and not (self._segment(p.scan) or {}).get("geprueft"):
            return f"{einheit} {num}? ({wo}, {'Spaltenzahl' if einheit == 'Sp.' else 'Seitenzahl'} geschätzt)"
        if self.text.get("quelle") == "pdf" and str(num) == str(p.scan) and self.teile() == 1:
            return f"{einheit} {num}"
        return f"{einheit} {num} ({wo})"

    def citable(self, p: Page) -> bool:
        """Darf diese Seitenangabe so zitiert werden (geprüft oder aus dem PDF)?"""
        status = (self.seitenzaehlung or {}).get("status")
        if status == STATUS_KEINE or self.page_number(p) is None:
            return False
        return not (status == STATUS_AUTO and not (self._segment(p.scan) or {}).get("geprueft"))

    def to_entry(self) -> bibtex.Entry:
        return bibtex.Entry(self.eintrag, self.citekey, dict(self.felder), self.kommentar)


# --- YAML mit lesbaren Mehrzeilern ------------------------------------------

class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)


def _dump_yaml(data) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


# --- Bibliothek ----------------------------------------------------------------

class Library:
    def __init__(self, root: Path | None = None):
        self.root = root or config.ROOT
        self.items_dir = self.root / "titel"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self._write_guide()

    def _write_guide(self) -> None:
        """Anleitung für KI-Agenten in den Bibliotheksordner legen (CLAUDE.md), aktuell halten."""
        source = Path(__file__).with_name("KI-ANLEITUNG.md")
        target = self.root / "CLAUDE.md"
        try:
            text = source.read_text(encoding="utf-8")
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                target.write_text(text, encoding="utf-8")
        except OSError:
            pass

    @property
    def index_path(self) -> Path:
        return config.index_path(self.root)

    def folder(self, key: str) -> Path:
        return self.items_dir / key

    def keys(self) -> list[str]:
        return sorted(p.name for p in self.items_dir.iterdir() if (p / "meta.yaml").exists())

    def items(self) -> Iterator[Item]:
        for key in self.keys():
            yield self.get(key)

    def exists(self, key: str) -> bool:
        return (self.folder(key) / "meta.yaml").exists()

    def get(self, key: str) -> Item:
        path = self.folder(key) / "meta.yaml"
        if not path.exists():
            raise KeyError(f"Kein Titel mit Schlüssel {key!r}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = {f for f in Item.__dataclass_fields__}
        item = Item(**{k: v for k, v in data.items() if k in known})
        item.felder = {k: str(v) for k, v in (item.felder or {}).items()}
        return item

    def resolve(self, ref: str) -> Item:
        """Schlüssel exakt, sonst eindeutiger Präfix oder Titelteil."""
        if self.exists(ref):
            return self.get(ref)
        alias = self.by_alias(ref)
        if alias:
            return alias
        keys = self.keys()
        hits = [k for k in keys if k.startswith(ref.lower())]
        if not hits:
            needle = ref.lower()
            hits = [it.citekey for it in self.items() if needle in it.title.lower()]
        if len(hits) == 1:
            return self.get(hits[0])
        if not hits:
            raise KeyError(f"Kein Titel passt zu {ref!r}")
        raise KeyError(f"{ref!r} ist mehrdeutig: {', '.join(hits[:8])}")

    def by_alias(self, key: str) -> Item | None:
        """Titel, der `key` als früheren Schlüssel führt (biblatex-Feld ids, gesetzt beim Umbenennen)."""
        for it in self.items():
            if key in [x.strip() for x in it.felder.get("ids", "").split(",")]:
                return it
        return None

    def save(self, item: Item, *, render: bool = True) -> None:
        folder = self.folder(item.citekey)
        folder.mkdir(parents=True, exist_ok=True)
        data = asdict(item)
        (folder / "meta.yaml").write_text(_dump_yaml(data), encoding="utf-8")
        if render and (folder / "seiten.jsonl").exists():
            self.render_text(item)

    def delete(self, key: str) -> None:
        shutil.rmtree(self.folder(key))
        self._journal({"op": "loeschen", "key": key})

    def rename(self, item: Item, new_key: str) -> Item:
        if self.exists(new_key):
            raise ValueError(f"{new_key} existiert schon")
        old = item.citekey
        self.folder(old).rename(self.folder(new_key))
        item.citekey = new_key
        ocr = item.text.get("ocr_ordner") or ""
        if ocr == f"nas:ocr/{old}":
            item.text["ocr_ordner"] = f"nas:ocr/{new_key}"
        self.save(item)
        self._journal({"op": "umbenennen", "von": old, "nach": new_key})
        return item

    def _journal(self, entry: dict) -> None:
        """Umbenennen/Löschen für den Abgleich mit der Nextcloud vormerken."""
        if not (config.settings().get("nas") or {}).get("server"):
            return
        path = config.sync_dir(self.root) / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- Seiten ---------------------------------------------------------------
    def pages(self, key: str) -> list[Page]:
        path = self.folder(key) / "seiten.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                out.append(Page(d["scan"], d.get("text", ""), d.get("label"), d.get("konfidenz")))
        return out

    def write_pages(self, item: Item, pages: list[Page]) -> None:
        folder = self.folder(item.citekey)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "seiten.jsonl").open("w", encoding="utf-8") as fh:
            for p in pages:
                fh.write(json.dumps({k: v for k, v in asdict(p).items() if v is not None}, ensure_ascii=False) + "\n")
        item.text["seiten"] = len(pages)
        item.text["zeichen"] = sum(len(p.text) for p in pages)
        item.text["extrahiert"] = dt.date.today().isoformat()
        self.save(item)

    def render_text(self, item: Item) -> Path:
        """Lesefassung für KI: Kopf mit Zitierhinweis, dann Text mit Seitenmarken."""
        pages = self.pages(item.citekey)
        sz = item.seitenzaehlung or {}
        hinweis = {
            STATUS_BESTAETIGT: "Seitenzahlen geprüft.",
            STATUS_PDF: "Seitenzahlen aus dem PDF übernommen.",
            STATUS_AUTO: "Seitenzahlen nur geschätzt (mit ? markiert) — vor dem Zitieren am Scan prüfen.",
            STATUS_UNBEKANNT: "Druckseiten ungeklärt — nur Scan-Nummern, nicht als Seitenzahl zitieren.",
            STATUS_KEINE: "Format ohne feste Seiten — Abschnitte statt Seiten.",
        }.get(sz.get("status"), "")
        head = {
            "citekey": item.citekey,
            "kurz": item.short,
            "titel": item.title,
            "typ": item.eintrag,
            "quelle": item.text.get("quelle"),
            "seitenzaehlung": hinweis,
        }
        if sz.get("spruenge"):
            head["seitenspruenge"] = sz["spruenge"]
        if item.text.get("hinweise"):
            head["textqualitaet"] = item.text["hinweise"]
        if item.tags:
            head["tags"] = item.tags
        if item.unvollstaendig:
            head["metadaten_fehlen"] = item.unvollstaendig
        out = ["---", _dump_yaml(head).rstrip(), "---", ""]
        for p in pages:
            out.append(f"[[{item.page_ref(p)}]]")
            out.append(p.text.strip())
            out.append("")
        path = self.folder(item.citekey) / "text.md"
        path.write_text("\n".join(out), encoding="utf-8")
        return path

    # -- Schlüssel und Dubletten ---------------------------------------------------
    def new_citekey(self, felder: dict[str, str]) -> str:
        stem = bibtex.citekey_stem(felder)
        key, n = stem, 0
        while self.exists(key):
            n += 1
            key = stem + "abcdefghijklmnopqrstuvwxyz"[n - 1]
        return key

    def find_duplicate(self, felder: dict[str, str]) -> Item | None:
        doi = (felder.get("doi") or "").lower().strip()
        isbn = re.sub(r"[^0-9X]", "", (felder.get("isbn") or "").upper())
        title = _norm_title(felder.get("title", ""))
        year = re.search(r"\d{4}", felder.get("year") or felder.get("date") or "")
        for it in self.items():
            if doi and doi == (it.felder.get("doi") or "").lower().strip():
                return it
            if isbn and isbn == re.sub(r"[^0-9X]", "", (it.felder.get("isbn") or "").upper()):
                return it
            if title and len(title) > 8 and title == _norm_title(it.felder.get("title", "")):
                if not year or it.year in ("o. J.", year.group(0)):
                    return it
        return None

    # -- BibTeX-Gesamtdatei ----------------------------------------------------------
    def write_refs_bib(self, path: Path | None = None, keys: list[str] | None = None) -> Path:
        path = path or (self.root / "refs.bib")
        chosen = keys if keys is not None else self.keys()
        parts = [
            "% Erzeugt von der Bibliothek (bib bibtex). Nicht von Hand bearbeiten —",
            "% Änderungen in titel/<citekey>/meta.yaml oder in der Web-Oberfläche.",
            "",
        ]
        for key in chosen:
            parts.append(bibtex.format_entry(self.get(key).to_entry()))
            parts.append("")
        path.write_text("\n".join(parts), encoding="utf-8")
        return path


def _norm_title(t: str) -> str:
    t = bibtex.strip_tex(t).lower()
    t = re.split(r"[:.]", t, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9äöüß]", "", t)
