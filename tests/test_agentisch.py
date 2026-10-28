"""Kataloge, Zitatprüfung, Sicherung, MCP-Server, Scanbilder, Prüfliste."""
import json

import pytest

from bibliothek import bilder, belege, config, ingest, katalog, mcp, sicherung, sync, web
from bibliothek.jobs import JobRunner
from bibliothek.store import STATUS_AUTO, STATUS_BESTAETIGT, Item, Library, Page

from fake_dav import FakeDAV
from test_core import _pdf

TOKEN = "test-token"


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "bib")
    item = Item(citekey="anonymojbosch", eintrag="book", felder={"title": "Bosch Ganzheitliche Mission Teil I"},
                unvollstaendig=["author", "year"], text={"quelle": "pdf"},
                seitenzaehlung={"status": STATUS_AUTO, "segmente": [{"ab_scan": 1, "versatz": 10}]})
    lib.save(item)
    lib.write_pages(item, [
        Page(1, "David J. Bosch\nGanzheitliche Mission\nTheologische Perspektiven"),
        Page(2, "Bibliografische Information der DNB\nISBN 978-3-86827-244-4\nFrancke, Marburg 2011"),
        Page(3, "Mission ist die Teilnahme der Kirche an der Sendung Gottes. Sie ist nicht\n"
                "zuerst Tätigkeit der Kirche, sondern ein Attri-\nbut Gottes selbst."),
        Page(4, "Die Kirche existiert, indem sie sendet, wie das Feuer existiert, indem es brennt."),
    ])
    return lib


# --- Kataloge -----------------------------------------------------------------------------------

def test_isbn_pruefziffer_und_kennungen():
    assert katalog.isbn_gueltig("978-3-86827-244-4") and katalog.isbn_gueltig("376154667X")
    assert not katalog.isbn_gueltig("978-3-86827-244-5")
    ids = katalog.kennungen([Page(1, "Druck: 9783868272444"), Page(2, "ISBN 0-8028-0176-5"),
                             Page(3, "doi: 10.1093/oxfordhb/9780199367313.013.22.")])
    assert ids["isbn"] == {"9783868272444": 1, "0802801765": 2}     # 978… gilt auch ohne „ISBN“
    assert ids["doi"] == {"10.1093/oxfordhb/9780199367313.013.22": 3}


MARC = b"""<?xml version="1.0"?><searchRetrieveResponse><records><record><recordData>
<record xmlns="http://www.loc.gov/MARC21/slim"><controlfield tag="001">1012345678</controlfield>
<datafield tag="020"><subfield code="a">978-3-86827-244-4</subfield></datafield>
<datafield tag="100"><subfield code="a">Bosch, David Jacobus</subfield><subfield code="4">aut</subfield></datafield>
<datafield tag="245"><subfield code="a">Ganzheitliche Mission</subfield><subfield code="b">theologische Perspektiven</subfield></datafield>
<datafield tag="250"><subfield code="a">2. Aufl.</subfield></datafield>
<datafield tag="264"><subfield code="a">Marburg an der Lahn</subfield><subfield code="b">Francke</subfield><subfield code="c">2011</subfield></datafield>
<datafield tag="700"><subfield code="a">Bosch, David Jacobus</subfield><subfield code="4">aut</subfield></datafield>
</record></recordData></record></records></searchRetrieveResponse>"""


class Antwort:
    def __init__(self, status_code=200, content=b"", js=None):
        self.status_code, self.content, self._js = status_code, content, js

    def json(self):
        return self._js


@pytest.fixture
def kataloge(monkeypatch):
    """DNB kennt Bosch per ISBN; alle anderen Kataloge finden nichts."""
    aufrufe = []

    def get(url, params=None, **kw):
        aufrufe.append((url, params))
        if "dnb.de" in url and "num=9783868272444" in (params or {}).get("query", ""):
            return Antwort(200, MARC)
        if "dnb.de" in url:
            return Antwort(200, b"<searchRetrieveResponse/>")
        if "openlibrary" in url:
            return Antwort(200, js={})
        if url.endswith("/works"):   # Titelsuche bei Crossref/OpenAlex
            return Antwort(200, js={"message": {"items": []}, "results": []})
        return Antwort(404)

    monkeypatch.setattr(katalog.requests, "get", get)
    return aufrufe


def test_dnb_ueber_isbn_im_dokument_ist_sicher(lib, kataloge):
    vs = katalog.vorschlaege(lib, lib.get("anonymojbosch"))
    v = vs[0]
    assert v.quelle == "DNB" and v.sicherheit == katalog.SICHER
    assert v.felder == {"title": "Ganzheitliche Mission", "subtitle": "Theologische Perspektiven",
                        "author": "Bosch, David Jacobus", "location": "Marburg an der Lahn", "publisher": "Francke",
                        "year": "2011", "edition": "2", "isbn": "9783868272444"}
    assert "ISBN 9783868272444 auf Scan 2" in v.begruendung
    assert not any("crossref" in u for u, _ in kataloge)   # sicherer Treffer: keine Titelsuche nötig


def test_uebernehmen_benennt_um_und_alter_schluessel_bleibt_gueltig(lib, kataloge):
    v = katalog.vorschlaege(lib, lib.get("anonymojbosch"))[0]
    item = katalog.uebernehmen(lib, lib.get("anonymojbosch"), v)
    assert item.citekey == "bosch2011ganzheitliche" and not item.unvollstaendig
    assert item.felder["ids"] == "anonymojbosch"
    assert lib.resolve("anonymojbosch").citekey == "bosch2011ganzheitliche"
    assert "Angaben aus DNB" in item.kommentar and item.text["metadaten"]["sicherheit"] == "sicher"
    lib.write_refs_bib()
    assert "% Angaben aus DNB" in (lib.root / "refs.bib").read_text()


def test_bibtex_aus_tex_loest_alte_schluessel_auf(lib, kataloge, tmp_path, monkeypatch, capsys):
    from bibliothek import cli
    v = katalog.vorschlaege(lib, lib.get("anonymojbosch"))[0]
    katalog.uebernehmen(lib, lib.get("anonymojbosch"), v)
    tex = tmp_path / "arbeit.tex"
    tex.write_text(r"Wie \autocite[12]{anonymojbosch} zeigt …")
    monkeypatch.setattr(config, "library_root", lambda: lib.root)
    out = tmp_path / "references.bib"
    assert cli.main(["bibtex", "--aus-tex", str(tex), "-o", str(out), "--streng"]) == 0
    assert "@book{bosch2011ganzheitliche," in out.read_text()


def test_mehrere_werke_im_dokument_sind_nicht_sicher(lib):
    a = katalog.Vorschlag("DNB", "u1", "book", {"title": "Den Griechen eine Torheit"})
    b = katalog.Vorschlag("Open Library", "u2", "book", {"title": "Foolishness to the Greeks"})
    vorn = "Den Griechen eine Torheit … Originaltitel: Foolishness to the Greeks"
    item = Item(citekey="x")
    assert katalog._bewerten(a, "ISBN 1", vorn, item)[0] == katalog.SICHER
    assert katalog._bewerten(katalog.Vorschlag("C", "u", "misc", {"title": "Theology"}), "", "Theology of music",
                             item)[0] == katalog.MOEGLICH   # ein Wort beweist nichts


def test_name_aus_anzeige():
    assert katalog._name_aus_anzeige("David J. Bosch") == "Bosch, David J."
    assert katalog._name_aus_anzeige("Gerhard von Rad") == "von Rad, Gerhard"
    assert katalog._sauber('"Den Griechen eine Torheit"') == r"\enquote{Den Griechen eine Torheit}"


def test_aufnehmen_per_doi_ohne_freies_pdf(lib, monkeypatch):
    def get(url, params=None, **kw):
        if "crossref" in url:
            return Antwort(200, js={"message": {"type": "journal-article", "title": ["Drowning Jonah"],
                                                "author": [{"family": "Hurlbert", "given": "Brandon M."}],
                                                "issued": {"date-parts": [[2023]]}, "container-title": ["JSBV"],
                                                "volume": "2", "page": "84-103", "DOI": "10.1/ABC"}})
        if "openalex" in url:
            return Antwort(200, js={"title": "Drowning Jonah", "best_oa_location": None})
        return Antwort(404)

    monkeypatch.setattr(katalog.requests, "get", get)
    item, hinweis = katalog.aufnehmen(lib, doi="https://doi.org/10.1/abc", projekte=["Hermeneutik/x"])
    assert item.citekey == "hurlbert2023drowning" and item.eintrag == "article"
    assert item.felder["pages"] == "84--103" and item.felder["journaltitle"] == "JSBV"
    assert "Kein frei zugängliches PDF" in hinweis and item.projekte == ["Hermeneutik/x"]
    with pytest.raises(ingest.Duplicate):
        katalog.aufnehmen(lib, doi="10.1/abc")


# --- Zitate prüfen --------------------------------------------------------------------------------

def test_zitat_woertlich_trotz_silbentrennung(lib):
    r = belege.pruefen(lib, lib.get("anonymojbosch"), "„nicht zuerst Tätigkeit der Kirche, sondern ein Attribut Gottes selbst“",
                       seite="13")
    assert r["bewertung"] == "wörtlich" and r["scan"] == 3 and r["angegebene_seite_stimmt"]
    assert not r["zitierfaehig"] and "nicht geprüft" in r["hinweis"]   # Seitenzählung nur geschätzt
    assert r["seite"].startswith("S. 13?")


def test_zitat_abweichung_wird_benannt_und_zitierfaehig_nach_pruefung(lib):
    item = lib.get("anonymojbosch")
    item.seitenzaehlung = {"status": STATUS_BESTAETIGT, "segmente": [{"ab_scan": 1, "versatz": 10, "geprueft": True}]}
    lib.save(item)
    r = belege.pruefen(lib, item, "Die Kirche existiert, indem sie sendet, wie das Feuer existiert, indem es leuchtet.",
                       seite="15")
    assert r["bewertung"].startswith("abweichend") and r["unterschiede"] == [{"zitat": "leuchtet.", "text": "brennt."}]
    assert not r["zitierfaehig"] and not r["angegebene_seite_stimmt"]   # steht auf S. 14
    r = belege.pruefen(lib, item, "wie das Feuer existiert, indem es brennt")
    assert r["zitierfaehig"] and r["seite"] == "S. 14 (Scan 4)"
    assert not belege.pruefen(lib, item, "Ganz anderer Satz über Schafe in Kansas, der nirgends steht")["gefunden"]


# --- Sicherung --------------------------------------------------------------------------------------

def test_sicherung_mit_ablage_ist_eigenstaendig_und_inkrementell(tmp_path, monkeypatch):
    config.save_settings({"nas": {"art": "webdav", "server": "https://dav", "benutzer": "u",
                                  "bibliothek": str(tmp_path / "bib")}})
    dav = FakeDAV()
    monkeypatch.setattr(sync.ablage, "oeffnen", lambda: dav)
    lib = Library(tmp_path / "bib")
    f = tmp_path / "jona.txt"
    f.write_text("Jona flieht.", encoding="utf-8")
    item = ingest.add_file(lib, f, felder={"title": "Jona lesen", "author": "Muster, Max", "year": "2020"}, run_ocr=False)
    run = tmp_path / "ocrlauf"
    (run / "pages").mkdir(parents=True)
    (run / "pages" / "1.png").write_bytes(b"png")
    item.text["ocr_ordner"] = str(run)
    lib.save(item)
    sync.abgleichen(lib, dav=dav)
    assert not (lib.folder(item.citekey) / "jona.txt").exists()          # Original nur noch auf der Ablage

    ziel = tmp_path / "platte"
    b = sicherung.sichern(lib, ziel)
    assert b["geholt"] == 2 and (ziel / "titel/muster2020jona/jona.txt").read_text() == "Jona flieht."
    assert (ziel / "ocr/muster2020jona/pages/1.png").exists()
    kopie = Library(ziel).get("muster2020jona")
    assert kopie.text["ocr_ordner"] == str(ziel / "ocr/muster2020jona")   # zeigt nicht mehr auf die Ablage
    b2 = sicherung.sichern(lib, ziel)
    assert b2["geholt"] == 0 and b2["kopiert"] == 0
    assert sicherung.status()["faellig"] is False
    with pytest.raises(ValueError):
        sicherung.sichern(lib, lib.root / "sicherung")


def test_sicherung_in_vollen_ordner_legt_unterordner_an(lib, tmp_path):
    voll = tmp_path / "platte"
    voll.mkdir()
    (voll / "fotos").mkdir()
    b = sicherung.sichern(lib, voll)
    assert b["ziel"] == str((voll / "Bibliothek-Sicherung").resolve())
    assert sicherung.sichern(lib, voll / "Bibliothek-Sicherung")["kopiert"] == 0


# --- MCP ------------------------------------------------------------------------------------------

def test_mcp_protokoll(lib, monkeypatch):
    monkeypatch.setattr(mcp, "_app_suche", lambda *a: None)
    from bibliothek import index
    index.update(lib)
    init = mcp.behandeln(lib, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26" and "S. 45?" in init["result"]["instructions"]
    assert mcp.behandeln(lib, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    namen = {t["name"] for t in mcp.behandeln(lib, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"suche", "lies", "zitat_pruefen", "bibtex", "aufnehmen"} <= namen

    def call(name, **args):
        r = mcp.behandeln(lib, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})["result"]
        return r["isError"], json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"]

    fehler, treffer = call("suche", anfrage="Feuer", modus="text")
    assert not fehler and treffer[0]["citekey"] == "anonymojbosch" and treffer[0]["zitierfaehig"] is False
    fehler, text = call("lies", citekey="anonymojbosch", seiten="14")
    assert "[[S. 14? (Scan 4, Seitenzahl geschätzt)]]" in text["text"]
    fehler, r = call("zitat_pruefen", citekey="anonymojbosch", zitat="indem es brennt")
    assert r["bewertung"] == "wörtlich"
    fehler, text = call("titel", citekey="gibtsnicht")
    assert fehler and "gibtsnicht" in text
    assert call("suche", anfrage="Feuer", modus="text", projekt="keins")[1] == []
    assert mcp.behandeln(lib, {"jsonrpc": "2.0", "id": 9, "method": "gibts/nicht"})["error"]["code"] == -32601


def test_mcp_main_schreibt_nur_protokoll_auf_stdout(lib, monkeypatch, capsys):
    import io
    monkeypatch.setattr(config, "library_root", lambda: lib.root)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nkaputt\n'))
    monkeypatch.setattr(mcp, "_status", lambda lib, a: print("Lärm") or {})
    mcp.main()
    zeilen = capsys.readouterr().out.strip().splitlines()
    assert json.loads(zeilen[0]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert json.loads(zeilen[1])["error"]["code"] == -32700


# --- Scanbilder und Oberfläche -----------------------------------------------------------------------

def test_scanbild_aus_pdf(tmp_path):
    lib = Library(tmp_path / "bib")
    item = ingest.add_file(lib, _pdf(tmp_path / "a.pdf", ["Erste Seite mit Text", "Zweite Seite mit Text"]),
                           felder={"title": "A", "author": "B, C", "year": "2000"}, run_ocr=False)
    bild = bilder.scan_bild(lib, item, 2)
    assert bild and bild.read_bytes()[:2] == b"\xff\xd8"
    assert bilder.scan_bild(lib, item, 3) is None


@pytest.fixture
def client(lib, monkeypatch):
    from bibliothek import ablage, index
    monkeypatch.setattr(ablage, "verbunden", lambda: False)
    monkeypatch.setattr(index, "embed_missing", lambda *a, **k: 0)
    app = web.create_app(lib, runner=JobRunner())
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = TOKEN
    return c


def test_pruefliste_und_katalogseite(client, lib, kataloge):
    html = client.get("/pruefen").get_data(as_text=True)
    assert "Angaben fehlen" in html and "anonymojbosch" in html and "Seitenzahlen ungeprüft" in html
    assert "Prüfen <span class=\"zahl\">2</span>" in client.get("/").get_data(as_text=True)
    html = client.get("/titel/anonymojbosch/katalog").get_data(as_text=True)
    assert "sicher" in html and "Ganzheitliche Mission" in html and "Theologische Perspektiven" in html
    r = client.post("/titel/anonymojbosch/katalog", data={"csrf": TOKEN, "nr": "1"}, follow_redirects=True)
    assert "neuer Schlüssel bosch2011ganzheitliche" in r.get_data(as_text=True)
    assert lib.get("bosch2011ganzheitliche").felder["publisher"] == "Francke"


def test_sicherung_aus_den_einstellungen(client, tmp_path):
    html = client.get("/einstellungen").get_data(as_text=True)
    assert "Noch keine Sicherung" in html
    r = client.post("/sicherung", data={"csrf": TOKEN, "ziel": str(tmp_path / "platte")})
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]


def test_umbenennen_behaelt_embeddings(lib):
    import sqlite3
    from bibliothek import index
    index.update(lib)
    with sqlite3.connect(lib.index_path) as con:
        con.executemany("INSERT INTO emb VALUES(?, ?)", [(cid, b"v") for (cid,) in con.execute("SELECT id FROM chunks")])
        vorher = con.execute("SELECT count(*) FROM emb").fetchone()[0]
    lib.rename(lib.get("anonymojbosch"), "bosch2011ganzheitlichea")
    index.update(lib)
    with sqlite3.connect(lib.index_path) as con:
        assert con.execute("SELECT count(*) FROM emb").fetchone()[0] == vorher
        assert {k for (k,) in con.execute("SELECT DISTINCT citekey FROM chunks")} == {"bosch2011ganzheitlichea"}
    # Text geändert: gleiche Abschnitte behalten ihr Embedding, nur neue fehlen
    pages = lib.pages("bosch2011ganzheitlichea") + [Page(5, "Ganz neuer Abschnitt")]
    lib.write_pages(lib.get("bosch2011ganzheitlichea"), pages)
    index.update(lib)
    with sqlite3.connect(lib.index_path) as con:
        assert con.execute("SELECT count(*) FROM emb").fetchone()[0] == vorher
        assert con.execute("SELECT count(*) FROM chunks").fetchone()[0] == vorher + 1


def test_zusammenfuehren_angaben_und_datei(lib, kataloge):
    zotero = Item(citekey="bosch2011ganzheitliche", eintrag="book", projekte=["Mission"],
                  felder={"author": "Bosch, David J.", "title": "Ganzheitliche Mission", "year": "2011"})
    lib.save(zotero)
    (lib.folder("bosch2011ganzheitliche") / "notizen.md").write_text("Zotero-Markierung")
    vs = katalog.vorschlaege(lib, lib.get("anonymojbosch"))
    assert vs[0].sicherheit == katalog.MOEGLICH and "schon als bosch2011ganzheitliche in der Bibliothek" in vs[0].begruendung
    item = ingest.zusammenfuehren(lib, "anonymojbosch", "bosch2011ganzheitliche")
    assert item.citekey == "bosch2011ganzheitliche" and item.felder["author"] == "Bosch, David J."
    assert item.felder["ids"] == "anonymojbosch" and item.projekte == ["Mission"] and not item.unvollstaendig
    assert len(lib.pages("bosch2011ganzheitliche")) == 4 and not lib.exists("anonymojbosch")
    assert lib.resolve("anonymojbosch").citekey == "bosch2011ganzheitliche"
    assert (lib.folder("bosch2011ganzheitliche") / "notizen.md").read_text().startswith("Zotero-Markierung")
    andere = Item(citekey="x2000y", felder={"title": "X"}, dateien=["a.pdf"])
    lib.save(andere)
    with pytest.raises(ValueError, match="beide"):
        ingest.zusammenfuehren(lib, "bosch2011ganzheitliche", "x2000y")
