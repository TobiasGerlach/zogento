"""Kleiner verlustarmer BibTeX-Parser.

Feldwerte bleiben wörtlich erhalten (Klammern, Makros wie \\enquote), damit ein
Import aus refs.bib und ein späterer Export dasselbe ergeben. %-Kommentare vor
einem Eintrag werden mitgenommen.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Entry:
    entrytype: str
    key: str
    fields: dict[str, str]
    comment: str = ""


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    pos = 0
    pending_comment: list[str] = []
    n = len(text)
    while pos < n:
        at = text.find("@", pos)
        chunk = text[pos:] if at < 0 else text[pos:at]
        for line in chunk.splitlines():
            s = line.strip()
            if s.startswith("%"):
                pending_comment.append(s.lstrip("%").rstrip()[1:] if s.startswith("% ") else s.lstrip("%"))
            elif not s and pending_comment and pending_comment[-1] != "":
                pending_comment.append("")
        if at < 0:
            break
        m = re.match(r"@\s*(\w+)\s*([{(])", text[at:])
        if not m:
            pos = at + 1
            continue
        etype = m.group(1).lower()
        pos = at + m.end()
        if etype in ("comment", "preamble", "string"):
            pos = _skip_balanced(text, pos - 1)
            continue
        comma = text.find(",", pos)
        key = text[pos:comma].strip()
        pos = comma + 1
        fields: dict[str, str] = {}
        while pos < n:
            pos = _skip_ws(text, pos)
            if pos >= n or text[pos] in "})":
                pos += 1
                break
            fm = re.match(r"([\w\-:.]+)\s*=\s*", text[pos:])
            if not fm:
                pos += 1
                continue
            name = fm.group(1).lower()
            pos += fm.end()
            value, pos = _read_value(text, pos)
            fields[name] = value
            pos = _skip_ws(text, pos)
            if pos < n and text[pos] == ",":
                pos += 1
        comment = "\n".join(pending_comment).strip("\n")
        entries.append(Entry(etype, key, fields, comment))
        pending_comment = []
    return entries


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _skip_balanced(text: str, pos: int) -> int:
    depth = 0
    while pos < len(text):
        c = text[pos]
        if c in "{(":
            depth += 1
        elif c in "})":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return pos


def _read_value(text: str, pos: int) -> tuple[str, int]:
    parts: list[str] = []
    while pos < len(text):
        c = text[pos]
        if c == "{":
            end = _skip_balanced(text, pos)
            parts.append(text[pos + 1:end - 1])
            pos = end
        elif c == '"':
            end = pos + 1
            depth = 0
            while end < len(text) and not (text[end] == '"' and depth == 0):
                depth += {"{": 1, "}": -1}.get(text[end], 0)
                end += 1
            parts.append(text[pos + 1:end])
            pos = end + 1
        else:
            m = re.match(r"[^,}\s#]+", text[pos:])
            parts.append(m.group(0) if m else "")
            pos += m.end() if m else 1
        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == "#":
            pos = _skip_ws(text, pos + 1)
            continue
        break
    return "".join(parts), pos


FIELD_ORDER = [
    "author", "editor", "title", "subtitle", "booktitle", "journaltitle", "journal",
    "maintitle", "series", "number", "volume", "issue", "edition", "pages", "year",
    "date", "publisher", "location", "address", "institution", "organization",
    "isbn", "issn", "doi", "url", "urldate", "langid", "note",
]


def format_entry(e: Entry) -> str:
    out = []
    if e.comment:
        out.extend(("% " + line).rstrip() for line in e.comment.splitlines())
    names = sorted(e.fields, key=lambda f: (FIELD_ORDER.index(f) if f in FIELD_ORDER else len(FIELD_ORDER), f))
    width = max((len(n) for n in names), default=0)
    out.append(f"@{e.entrytype}{{{e.key},")
    for name in names:
        value = e.fields[name]
        if "\n" in value:
            pad = " " * (width + 6)
            value = ("\n" + pad).join(line.strip() for line in value.splitlines())
        out.append(f"  {name.ljust(width)} = {{{value}}},")
    out.append("}")
    return "\n".join(out)


# --- Namen und Schlüssel ---------------------------------------------------

def split_names(value: str) -> list[str]:
    """Teilt ein BibTeX-Namensfeld an ' and ' auf oberster Klammerebene."""
    names, depth, cur = [], 0, []
    tokens = re.split(r"(\{|\}|\s+and\s+)", value or "")
    for t in tokens:
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
        if re.fullmatch(r"\s+and\s+", t or "") and depth == 0:
            names.append("".join(cur).strip())
            cur = []
        else:
            cur.append(t)
    if "".join(cur).strip():
        names.append("".join(cur).strip())
    return names


def last_name(name: str) -> str:
    name = name.strip()
    if name.startswith("{") and name.endswith("}"):
        return name[1:-1]
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else name


def strip_tex(s: str) -> str:
    s = re.sub(r"\\enquote\{([^}]*)\}", r"„\1“", s or "")
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)
    return s.replace("{", "").replace("}", "").replace("~", " ").strip()


_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
STOPWORDS = {
    "der", "die", "das", "des", "dem", "den", "ein", "eine", "einer", "eines", "und", "zur", "zum",
    "im", "in", "von", "vom", "mit", "für", "fuer", "über", "ueber", "auf", "aus", "wie",
    "the", "a", "an", "of", "on", "and", "in", "to", "for", "with", "at", "by", "is", "toward", "towards",
    "le", "la", "les", "de", "du",
}


def ascii_slug(s: str) -> str:
    s = strip_tex(s).translate(_UMLAUT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def citekey_stem(fields: dict[str, str]) -> str:
    """Schema aus refs.bib: nachnameJAHRschlagwort (zwei Autoren: beide Namen)."""
    names = split_names(fields.get("author") or fields.get("editor") or "")
    if len(names) == 2:
        who = "".join(ascii_slug(last_name(n)) for n in names)
    elif names:
        who = ascii_slug(last_name(names[0]))
    else:
        who = ""
    year = re.search(r"\d{4}", fields.get("year") or fields.get("date") or "")
    word = ""
    for w in re.split(r"[\s\-–:/,.;!?„“\"'()]+", strip_tex(fields.get("title", ""))):
        if w and w.lower() not in STOPWORDS and ascii_slug(w):
            word = ascii_slug(w)
            break
    return (who or "anonym") + (year.group(0) if year else "oj") + word[:20]
