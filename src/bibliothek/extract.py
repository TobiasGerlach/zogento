"""Text aus Dateien holen, ohne systemspezifische Werkzeuge.

Jede Funktion liefert Seiten (PDF, Folien, OCR) oder Abschnitte (Word, EPUB,
HTML). Seiten ohne Textebene meldet sie in `ocr_noetig`; die OCR übernimmt
`ocr.py`, damit Extraktion und Erkennung getrennt testbar bleiben.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from . import config
from .store import Page

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic", ".heif", ".avif"}
SUPPORTED = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".epub", ".html", ".htm", ".xhtml",
    ".txt", ".md", ".markdown", ".pptx", ".tex",
} | IMAGE_SUFFIXES


@dataclass
class Extracted:
    pages: list[Page]
    quelle: str                       # pdf, docx, epub, html, text, pptx, bild …
    paginiert: bool                   # echte Seiten (True) oder Abschnitte (False)
    meta: dict = field(default_factory=dict)       # Titel/Autor aus den Dateieigenschaften
    hinweise: list[str] = field(default_factory=list)
    ocr_noetig: list[int] = field(default_factory=list)  # Seitennummern (scan) ohne Textebene


class ExtractError(RuntimeError):
    pass


def extract(path: Path) -> Extracted:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".doc":
        return extract_doc(path)
    if suffix == ".odt":
        return extract_odt(path)
    if suffix == ".rtf":
        from striprtf.striprtf import rtf_to_text
        return _sections(rtf_to_text(path.read_text(encoding="utf-8", errors="replace")), "rtf")
    if suffix == ".epub":
        return extract_epub(path)
    if suffix in (".html", ".htm", ".xhtml"):
        text, title = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
        ex = _sections(text, "html")
        if title:
            ex.meta["title"] = title
        return ex
    if suffix == ".pptx":
        return extract_pptx(path)
    if suffix in (".txt", ".md", ".markdown", ".tex"):
        return extract_plain(path)
    if suffix in IMAGE_SUFFIXES:
        return Extracted([Page(1, "")], "bild", True, ocr_noetig=[1])
    raise ExtractError(f"Format {suffix} wird nicht unterstützt.")


# --- PDF -------------------------------------------------------------------------

def extract_pdf(path: Path) -> Extracted:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise ExtractError(f"PDF lässt sich nicht öffnen: {exc}") from exc
    pages, missing, labels = [], [], 0
    for i in range(len(doc)):
        page = doc[i]
        text = page.get_textpage().get_text_bounded()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        label = doc.get_page_label(i) or None
        if label and label != str(i + 1):
            labels += 1
        if len(re.sub(r"\s", "", text)) < config.MIN_TEXT_CHARS:
            missing.append(i + 1)
        pages.append(Page(i + 1, _clean(text), label))
    meta = {k.lower(): v for k, v in doc.get_metadata_dict().items() if v and k in ("Title", "Author")}
    doc.close()
    ex = Extracted(pages, "pdf", True, meta)
    if labels == 0:
        for p in pages:
            p.label = None
    else:
        ex.hinweise.append("Seitenlabels aus dem PDF übernommen.")
    # Nur OCR anstoßen, wenn das PDF überwiegend ein Scan ist oder einzelne Seiten fehlen
    if missing:
        ex.ocr_noetig = missing
    return ex


# --- Word, OpenDocument, RTF ----------------------------------------------------

def extract_docx(path: Path) -> Extracted:
    import docx

    d = docx.Document(str(path))
    lines: list[str] = []
    for para in d.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        m = re.match(r"(heading|überschrift)\s*(\d)", style)
        lines.append(("#" * int(m.group(2)) + " " + text) if m else text)
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    ex = _sections("\n\n".join(lines), "docx", headings=True)
    props = d.core_properties
    if props.title:
        ex.meta["title"] = props.title
    if props.author:
        ex.meta["author"] = props.author
    return ex


def extract_doc(path: Path) -> Extracted:
    """Altes Word-Format: braucht ein externes Werkzeug."""
    if sys.platform == "darwin" and shutil.which("textutil"):
        out = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True)
        if out.returncode == 0:
            return _sections(out.stdout, "doc")
    if shutil.which("antiword"):
        out = subprocess.run(["antiword", str(path)], capture_output=True, text=True)
        if out.returncode == 0:
            return _sections(out.stdout, "doc")
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if office:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([office, "--headless", "--convert-to", "docx", "--outdir", tmp, str(path)],
                           capture_output=True, timeout=180)
            converted = Path(tmp) / (path.stem + ".docx")
            if converted.exists():
                ex = extract_docx(converted)
                ex.quelle = "doc"
                return ex
    raise ExtractError("Altes .doc-Format: bitte in Word als .docx speichern (oder LibreOffice installieren).")


def extract_odt(path: Path) -> Extracted:
    ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    lines = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("h", "p"):
            text = "".join(el.itertext()).strip()
            if not text:
                continue
            if tag == "h":
                level = el.get(f"{{{ns['text']}}}outline-level") or "1"
                text = "#" * int(level) + " " + text
            lines.append(text)
    return _sections("\n\n".join(lines), "odt", headings=True)


# --- EPUB, HTML, Folien ---------------------------------------------------------------

def extract_epub(path: Path) -> Extracted:
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(opf_path))
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        manifest = {el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("item")}
        spine = [el.get("idref") for el in opf.iter() if el.tag.endswith("itemref")]
        meta = {}
        for el in opf.iter():
            if el.tag.endswith("}title") and el.text and "title" not in meta:
                meta["title"] = el.text.strip()
            if el.tag.endswith("}creator") and el.text and "author" not in meta:
                meta["author"] = el.text.strip()
        pages = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            try:
                raw = z.read(base + href).decode("utf-8", errors="replace")
            except KeyError:
                continue
            text, _ = html_to_text(raw)
            if text.strip():
                pages.append(Page(len(pages) + 1, text))
    return Extracted(pages, "epub", False, meta)


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")
        if re.fullmatch(r"h[1-6]", tag) and not self.skip:
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    parser = _HTMLText()
    parser.feed(raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return html.unescape(text).strip(), parser.title.strip()


def extract_pptx(path: Path) -> Extracted:
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        pages = []
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            paras = []
            for p in root.iter():
                if p.tag.endswith("}p"):
                    t = "".join(x.text or "" for x in p.iter() if x.tag.endswith("}t")).strip()
                    if t:
                        paras.append(t)
            pages.append(Page(i, "\n".join(paras)))
    return Extracted(pages, "pptx", True)


# --- Klartext ---------------------------------------------------------------------------

def extract_plain(path: Path) -> Extracted:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if "\f" in text:  # Formfeed = Seitenumbruch (pdftotext, book-ocr)
        parts = text.split("\f")
        if parts and not parts[-1].strip():
            parts.pop()
        return Extracted([Page(i, _clean(t)) for i, t in enumerate(parts, 1)], "text", True)
    return _sections(text, "text", headings=path.suffix.lower() in (".md", ".markdown"))


def _sections(text: str, quelle: str, *, headings: bool = False, target: int = 6000) -> Extracted:
    """Unpaginierten Text in Abschnitte teilen: an Überschriften, sonst nach Länge."""
    text = text.replace("\r\n", "\n").strip()
    blocks = re.split(r"\n\s*\n", text)
    sections: list[str] = []
    cur: list[str] = []
    size = 0
    for b in blocks:
        is_heading = headings and re.match(r"#{1,2} ", b)
        if cur and (is_heading or size + len(b) > target):
            sections.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(b.strip())
        size += len(b)
    if cur:
        sections.append("\n\n".join(cur))
    pages = [Page(i, s) for i, s in enumerate(sections, 1) if s.strip()]
    return Extracted(pages, quelle, False)


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("­", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
