"""Doppelseiten und Spalten: mehrere Druckseiten pro PDF-Seite als eigene Abschnitte."""
import json
from pathlib import Path

import pytest

from bibliothek import extract, ingest, ocr, pagenums, teilung, web
from bibliothek.jobs import JobRunner
from bibliothek.store import Item, Library, Page

TOKEN = "test-token"
SATZ = "Der Prophet Jona flieht vor dem Auftrag nach Tarschisch "


def _doppel_pdf(path: Path, n: int, erste: int = 2) -> Path:
    """Querformat-PDF: links und rechts je ein Textblock mit Seitenzahl darunter (wie kopierte Bücher)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for i in range(n):
        links, rechts = erste + 2 * i, erste + 2 * i + 1
        teile = []
        for x, nr, seite in ((60, links, "links"), (440, rechts, "rechts")):
            for z in range(8):
                teile.append(f"BT /F1 9 Tf {x} {520 - z * 14} Td ({seite} {nr} Zeile {z} {SATZ[:30]}) Tj ET")
            teile.append(f"BT /F1 9 Tf {x + 150} 40 Td ({nr}) Tj ET")
        stream = "\n".join(teile)
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] /Contents {content} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))
    return path


def test_pdf_mit_textebene_wird_am_bund_geteilt(tmp_path):
    pdf = _doppel_pdf(tmp_path / "d.pdf", 2)
    ex = extract.extract_pdf(pdf, 2)
    assert [p.scan for p in ex.pages] == [1, 2, 3, 4]
    assert "links 2 Zeile 0" in ex.pages[0].text and "rechts" not in ex.pages[0].text
    assert "rechts 3 Zeile 7" in ex.pages[1].text and ex.pages[1].text.strip().endswith("3")
    assert "links 4" in ex.pages[2].text


def test_doppelseiten_werden_beim_aufnehmen_erkannt(tmp_path):
    lib = Library(tmp_path / "bib")
    item = ingest.add_file(lib, _doppel_pdf(tmp_path / "buch.pdf", 6),
                           felder={"title": "Jona", "author": "Muster, Max", "year": "2020"}, run_ocr=False)
    assert item.teile() == 2 and item.text["seiten"] == 12
    assert any("Doppelseiten erkannt" in h for h in item.text["hinweise"])
    pages = lib.pages(item.citekey)
    assert item.page_number(pages[0]) == "2" and item.page_number(pages[3]) == "5"
    assert item.page_ref(pages[3]) == "S. 5? (PDF-S. 2 rechts, Seitenzahl geschätzt)"
    assert pagenums.schritt(pages) == 1
    # zurück: eine Seite pro PDF-Seite
    item = ingest.teilen(lib, item, "aus", run_ocr=False)
    assert item.teile() == 1 and item.text["seiten"] == 6 and "teilung" not in item.text


def test_spalten_zaehlen_als_sp(tmp_path):
    lib = Library(tmp_path / "bib")
    item = ingest.add_file(lib, _doppel_pdf(tmp_path / "lexikon.pdf", 4, erste=245),
                           felder={"title": "Lexikon", "author": "Muster, Max", "year": "2020"}, run_ocr=False,
                           teilung_art="spalten")
    p = lib.pages(item.citekey)[1]
    assert item.seitenzaehlung["einheit"] == "Sp."
    assert item.page_ref(p) == "Sp. 246? (PDF-S. 1 rechts, Spaltenzahl geschätzt)"


def test_scan_namen():
    it = Item(citekey="x", text={"teilung": {"teile": 4, "art": "doppelseiten-spalten"}})
    assert it.scan_name(7) == "PDF-S. 2, Spalte 3/4"
    assert teilung.herkunft(7, 4) == (2, 2)
    assert Item(citekey="y").scan_name(7) == "Scan 7"


@pytest.mark.parametrize("teile", [2, 4])
def test_ocr_ergebnisse_werden_den_teilen_zugeordnet(tmp_path, monkeypatch, teile):
    pdf = _doppel_pdf(tmp_path / "d.pdf", 3)
    laeufe = []

    def ocr_folder(images, output, *, single_page, order, progress):
        """Attrappe der Pipeline: jedes Bild ergibt zwei Seiten (links/rechts)."""
        laeufe.append((Path(images).name, single_page))
        output.mkdir(parents=True, exist_ok=True)
        (output / "text").mkdir(exist_ok=True)
        (output / "pages").mkdir(exist_ok=True)
        entries = []
        for img in sorted(Path(images).glob("*.png")):
            pages = []
            for j in range(2):
                (output / "text" / f"{img.stem}-{j}.txt").write_text(f"{img.stem} Teil {j}")
                (output / "pages" / f"{img.stem}-{j}.png").write_bytes(img.read_bytes())
                pages.append({"text_file": f"text/{img.stem}-{j}.txt", "image": f"pages/{img.stem}-{j}.png",
                              "confidence": 90})
            entries.append({"source": img.name, "pages": pages})
        (output / "report.json").write_text(json.dumps({"entries": entries}))

    monkeypatch.setattr(ocr, "ocr_folder", ocr_folder)
    scans = list(range(1, 3 * teile + 1))
    r = ocr.ocr_pdf_pages(pdf, scans, tmp_path / "lauf", teile=teile)
    assert sorted(r) == scans
    if teile == 2:
        assert r[3].text == "s00002 Teil 0" and r[4].text == "s00002 Teil 1"
        assert laeufe == [("pdfseiten", False)]
    else:
        assert r[5].text == "s00002-0 Teil 0" and r[8].text == "s00002-1 Teil 1"
        assert laeufe == [("pdfseiten", False), ("haelften", False)]


def test_pruefliste_schlaegt_trennen_vor(tmp_path, monkeypatch):
    from bibliothek import ablage, index
    monkeypatch.setattr(ablage, "verbunden", lambda: False)
    monkeypatch.setattr(index, "embed_missing", lambda *a, **k: 0)
    lib = Library(tmp_path / "bib")
    ingest.add_file(lib, _doppel_pdf(tmp_path / "buch.pdf", 6), felder={"title": "Jona", "author": "M, M", "year": "2020"},
                    run_ocr=False, teilung_art="aus")
    runner = JobRunner()
    app = web.create_app(lib, runner=runner)
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = TOKEN
    html = c.get("/pruefen").get_data(as_text=True)
    assert "Mehrere Seiten pro Scan" in html and "Doppelseiten trennen" in html
    r = c.post("/titel/m2020jona/teilen", data={"csrf": TOKEN, "art": "doppelseiten"})
    assert r.status_code == 302
    runner.wait(30)
    assert lib.get("m2020jona").teile() == 2
    assert "Mehrere Seiten pro Scan" not in c.get("/pruefen").get_data(as_text=True)
    assert "PDF-S. 1 links" in c.get("/titel/m2020jona").get_data(as_text=True)
