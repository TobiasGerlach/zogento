import json

from bibliothek import cli, ingest
from bibliothek.store import Library, Page

from test_core import _book, _pdf


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_neu_suche_zeige_bibtex(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BIBLIOTHEK", str(tmp_path / "bib"))
    pdf = _pdf(tmp_path / "x.pdf", ["Der Prophet Jona flieht nach Tarschisch", "Ninive tut Busse und Gott bereut"])
    rc, out, _ = _run(capsys, "neu", str(pdf), "--titel", "Jona lesen", "--autor", "Muster, Max", "--jahr", "2020",
                      "--tag", "Jona", "--projekt", "Hermeneutik", "--ohne-ocr")
    assert rc == 0 and "+ muster2020jona" in out

    rc, out, _ = _run(capsys, "suche", "Ninive", "--modus", "text", "--json", "--lokal")
    hits = json.loads(out)
    assert hits[0]["citekey"] == "muster2020jona" and hits[0]["scan"] == 2
    assert hits[0]["seite"] == "Scan 2 (Druckseite ungeklärt)"

    rc, out, _ = _run(capsys, "suche", "Ninive", "--modus", "text", "--tag", "Anderes", "--lokal")
    assert rc == 1

    rc, out, _ = _run(capsys, "zeige", "muster", "--scan", "--seiten", "2")
    assert "[[Scan 2" in out and "Ninive" in out

    tex = tmp_path / "arbeit.tex"
    tex.write_text(r"Siehe \autocite[12]{muster2020jona} und \cite{fehlt2000}.", encoding="utf-8")
    rc, out, err = _run(capsys, "bibtex", "--aus-tex", str(tex), "-o", str(tmp_path / "refs.bib"))
    assert "fehlt2000" in err
    assert "@article{muster2020jona," in (tmp_path / "refs.bib").read_text(encoding="utf-8")


def test_seiten_bestaetigen_und_korrigieren(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BIBLIOTHEK", str(tmp_path / "bib"))
    lib = Library(tmp_path / "bib")
    src = tmp_path / "buch.txt"
    src.write_text("\f".join(p.text for p in _book(offset=5, n=30)), encoding="utf-8")
    item = ingest.add_file(lib, src, felder={"title": "Buch", "author": "A, B", "year": "2000"}, run_ocr=False)
    assert item.seitenzaehlung["status"] == "automatisch"

    rc, out, _ = _run(capsys, "seiten", item.citekey)
    assert "geschätzt" in out
    rc, out, _ = _run(capsys, "seiten", item.citekey, "--scan", "20", "--druckseite", "26", "--ab-scan", "18")
    item = lib.get(item.citekey)
    assert [s["versatz"] for s in item.segments()] == [5, 6]
    assert item.page_ref(Page(19, "")) == "S. 25 (Scan 19)"          # korrigierter Abschnitt: geprüft
    assert item.page_ref(Page(10, "")).startswith("S. 15?")           # erster Abschnitt: noch geschätzt
    rc, out, _ = _run(capsys, "seiten", item.citekey, "--bestaetigen")
    assert lib.get(item.citekey).seitenzaehlung["status"] == "bestätigt"
