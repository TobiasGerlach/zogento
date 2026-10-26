import io

import pytest

from bibliothek import index, web
from bibliothek.jobs import JobRunner
from bibliothek.store import STATUS_AUTO, STATUS_BESTAETIGT, Item, Library, Page

TOKEN = "test-token"


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path)
    item = Item(citekey="fee2015effektives", eintrag="book",
                felder={"author": "Fee, Gordon D. and Stuart, Douglas", "title": "Effektives Bibelstudium",
                        "year": "2015"},
                tags=["Hermeneutik"], kategorien=["Methodik/Exegese"], projekte=["Gattung"],
                text={"quelle": "ocr"},
                seitenzaehlung={"status": STATUS_AUTO, "segmente": [{"ab_scan": 13, "versatz": -12}],
                                "spruenge": ["Bei Scan 214 springt die Zählung um -2: 2 Seite(n) vermutlich doppelt gescannt."]})
    lib.save(item)
    pages = [Page(n, f"Kopfzeile\nSeite {n}: " + ("Die Gattung der Gleichnisse verlangt Aufmerksamkeit. " * 6)
                  + f"\n{n - 12}") for n in range(1, 31)]
    lib.write_pages(item, pages)
    index.update(lib)
    return lib


@pytest.fixture
def runner():
    return JobRunner()


@pytest.fixture
def client(lib, runner, monkeypatch):
    monkeypatch.setattr(index, "embed_missing", lambda *a, **k: 0)
    app = web.create_app(lib, runner=runner)
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = TOKEN
    return c


def post(client, url, data=None, **kw):
    data = dict(data or {})
    data["csrf"] = TOKEN
    return client.post(url, data=data, **kw)


def test_overview_lists_items_and_filters(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Effektives Bibelstudium" in html and "Fee/Stuart 2015" in html
    assert "geschätzt" in html
    assert "Effektives" in client.get("/?tag=Hermeneutik").get_data(as_text=True)
    assert "Kein Titel passt" in client.get("/?tag=Unbekannt").get_data(as_text=True)
    assert "Effektives" in client.get("/?kategorie=Methodik").get_data(as_text=True)


def test_detail_and_text_pages(client):
    r = client.get("/titel/fee2015effektives")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Seitenzählung" in html and "data-seitenhelfer" in html
    t = client.get("/titel/fee2015effektives/text?scan=20").get_data(as_text=True)
    assert 'id="scan-20"' in t
    assert "S. 8? (Scan 20" in t
    assert client.get("/titel/gibtsnicht").status_code == 404


def test_post_requires_csrf_and_local_host(client):
    r = client.post("/titel/fee2015effektives/metadaten", data={"tags": "x"})
    assert r.status_code == 403
    r = client.get("/", headers={"Host": "evil.example"})
    assert r.status_code == 403
    r = post(client, "/titel/fee2015effektives/metadaten", {"tags": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_edit_metadata_recomputes_missing_and_writes_bib(client, lib):
    r = post(client, "/titel/fee2015effektives/metadaten", {
        "eintrag": "book",
        "feld_name": ["author", "title", "year", "publisher", "Ungültig Feld"],
        "feld_wert": ["Fee, Gordon D.", "Effektives Bibelstudium", "", "Brunnen", ""],
        "tags": "Hermeneutik, Methode", "kategorien": "", "projekte": "Gattung", "notiz": "Gut",
    })
    assert r.status_code == 302
    item = lib.get("fee2015effektives")
    assert item.felder == {"author": "Fee, Gordon D.", "title": "Effektives Bibelstudium", "publisher": "Brunnen"}
    assert item.unvollstaendig == ["year"]
    assert item.tags == ["Hermeneutik", "Methode"]
    assert "publisher" in (lib.root / "refs.bib").read_text(encoding="utf-8")


def test_invalid_field_name_is_rejected(client, lib):
    post(client, "/titel/fee2015effektives/metadaten", {"feld_name": ["Bad Name"], "feld_wert": ["x"]})
    assert lib.get("fee2015effektives").felder.get("title") == "Effektives Bibelstudium"


def test_page_numbering_section_shows_segments_and_jumps(client):
    html = client.get("/titel/fee2015effektives").get_data(as_text=True)
    assert "abschnitte-tabelle" in html and "13–30" in html and "Scan -12" in html
    assert "springt die Zählung um -2" in html
    assert "S. 1? (Scan 13" in html          # Gegenprobe am Abschnittsanfang, ungeprüft mit ?
    assert "Abschnitt bestätigen" in html and "Alles bestätigen" in html


def test_scan_shows_printed_page_confirms_its_segment(client, lib):
    r = post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "setzen", "scan": "20", "druckseite": "8"})
    assert r.status_code == 302 and r.headers["Location"].endswith("#seiten")
    item = lib.get("fee2015effektives")
    assert item.seitenzaehlung["segmente"] == [{"ab_scan": 13, "versatz": -12, "geprueft": True}]
    assert item.seitenzaehlung["status"] == STATUS_BESTAETIGT
    assert item.page_ref(Page(20, "")) == "S. 8 (Scan 20)"
    assert "[[S. 8 (Scan 20)]]" in (lib.folder(item.citekey) / "text.md").read_text(encoding="utf-8")


def test_new_segment_stays_estimated_until_all_confirmed(client, lib):
    post(client, "/titel/fee2015effektives/seitenzaehlung",
         {"aktion": "setzen", "scan": "25", "druckseite": "15", "ab_scan": "25"})
    item = lib.get("fee2015effektives")
    segs = item.seitenzaehlung["segmente"]
    assert segs == [{"ab_scan": 13, "versatz": -12}, {"ab_scan": 25, "versatz": -10, "geprueft": True}]
    assert item.seitenzaehlung["status"] == STATUS_AUTO
    assert item.page_ref(Page(20, "")).startswith("S. 8?")      # alter Abschnitt ungeprüft
    assert item.page_ref(Page(26, "")) == "S. 16 (Scan 26)"     # neuer Abschnitt geprüft
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "abschnitt_bestaetigen", "ab_scan": "13"})
    assert lib.get("fee2015effektives").seitenzaehlung["status"] == STATUS_BESTAETIGT
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "abschnitt_loeschen", "ab_scan": "25"})
    item = lib.get("fee2015effektives")
    assert item.seitenzaehlung["segmente"] == [{"ab_scan": 13, "versatz": -12, "geprueft": True}]
    assert item.page_ref(Page(26, "")) == "S. 14 (Scan 26)"


def test_confirm_all_and_delete_last_segment(client, lib):
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "alles"})
    item = lib.get("fee2015effektives")
    assert item.seitenzaehlung["status"] == STATUS_BESTAETIGT
    assert all(x["geprueft"] for x in item.seitenzaehlung["segmente"])
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "abschnitt_loeschen", "ab_scan": "13"})
    item = lib.get("fee2015effektives")
    assert "segmente" not in item.seitenzaehlung and item.seitenzaehlung["status"] == "unbekannt"
    assert item.page_ref(Page(20, "")) == "Scan 20 (Druckseite ungeklärt)"
    r = post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "alles"})
    assert r.status_code == 302 and lib.get("fee2015effektives").seitenzaehlung["status"] == "unbekannt"


def test_page_numbering_rejects_bad_input(client, lib):
    before = lib.get("fee2015effektives").seitenzaehlung
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "setzen", "scan": "abc", "druckseite": "3"})
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "setzen", "scan": "0", "druckseite": "3"})
    assert lib.get("fee2015effektives").seitenzaehlung == before
    assert post(client, "/titel/fee2015effektives/seitenzaehlung",
                {"aktion": "abschnitt_loeschen", "ab_scan": "99"}).status_code == 400
    assert post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "quatsch"}).status_code == 400


def test_old_single_offset_format_is_read(client, lib):
    item = lib.get("fee2015effektives")
    item.seitenzaehlung = {"status": STATUS_AUTO, "versatz": -12, "ab_scan": 13}
    lib.save(item)
    assert "13–30" in client.get("/titel/fee2015effektives").get_data(as_text=True)
    post(client, "/titel/fee2015effektives/seitenzaehlung", {"aktion": "alles"})
    sz = lib.get("fee2015effektives").seitenzaehlung
    assert "versatz" not in sz and sz["segmente"] == [{"ab_scan": 13, "versatz": -12, "geprueft": True}]


def test_index_is_not_in_library_folder(client, lib):
    client.get("/api/suche?q=Gattung&modus=text")
    assert not (lib.root / "index.sqlite").exists()
    assert lib.index_path.exists()


def test_search_page_and_api(client):
    r = client.get("/api/suche?q=Gattung&modus=text&limit=5")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["treffer"]) == 5
    t = data["treffer"][0]
    assert t["citekey"] == "fee2015effektives" and "«" in t["snippet"] and t["wege"] == ["text"]
    assert t["seite"].startswith(("S. ", "Scan "))
    html = client.get("/suche?q=Gattung&modus=text").get_data(as_text=True)
    assert "<mark>" in html
    assert client.get("/api/suche?q=Gattung&modus=quatsch").status_code == 400
    assert client.get("/api/suche").status_code == 400
    assert client.get("/api/suche?q=Gattung&modus=text&tag=Fremd").get_json()["treffer"] == []


def test_hybrid_falls_back_to_text_without_model(client, monkeypatch):
    def broken():
        raise RuntimeError("kein Modell")
    monkeypatch.setattr(index, "model", broken)
    index._matrix_cache.clear()
    data = client.get("/api/suche?q=Gleichnisse").get_json()
    assert data["treffer"]
    # ohne Embeddings liefert die semantische Suche nichts; Text trägt das Ergebnis
    assert all("text" in t["wege"] for t in data["treffer"])


def test_api_status(client, lib):
    data = client.get("/api/status").get_json()
    assert data["bibliothek"] == str(lib.root)


def test_rename_and_delete(client, lib):
    r = post(client, "/titel/fee2015effektives/umbenennen", {"citekey": "feestuart2015effektives"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/titel/feestuart2015effektives")
    assert lib.exists("feestuart2015effektives") and not lib.exists("fee2015effektives")
    post(client, "/titel/feestuart2015effektives/loeschen", {"bestaetigung": "falsch"})
    assert lib.exists("feestuart2015effektives")
    post(client, "/titel/feestuart2015effektives/loeschen", {"bestaetigung": "feestuart2015effektives"})
    assert not lib.exists("feestuart2015effektives")
    assert index.stats(lib)["titel"] == 0


def test_upload_text_file_creates_item(client, lib, runner):
    data = {
        "dateien": [(io.BytesIO("# Jona\n\nDie Ironie im Buch Jona ist deutlich.\n".encode()), "jona-notiz.md")],
        "author": "Muster, Max", "title": "Ironie im Jonabuch", "year": "2020", "tags": "Jona",
    }
    r = post(client, "/aufnehmen/dateien", data, content_type="multipart/form-data")
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    runner.wait(30)
    job = next(j for j in runner.all() if j.art == "aufnahme")
    assert job.status == "fertig", job.fehler or list(job.log)
    key = job.ergebnis["neu"][0]
    item = lib.get(key)
    assert item.title == "Ironie im Jonabuch" and item.tags == ["Jona"]
    assert item.dateien == ["jona-notiz.md"]
    assert client.get(f"/titel/{key}/datei/jona-notiz.md").status_code == 200
    assert client.get(f"/titel/{key}/datei/fremd.pdf").status_code == 404
    hits = client.get("/api/suche?q=Ironie&modus=text").get_json()["treffer"]
    assert hits and hits[0]["citekey"] == key and hits[0]["seite"] == "Abschn. 1"
    job_json = client.get(f"/api/jobs/{job.id}").get_json()
    assert job_json["status"] == "fertig"
    # gleiche Datei noch einmal: als Dublette gemeldet
    data["dateien"] = [(io.BytesIO(b"# Jona\n\nnochmal"), "jona-notiz.md")]
    post(client, "/aufnehmen/dateien", data, content_type="multipart/form-data")
    runner.wait(30)
    latest = next(j for j in runner.all() if j.art == "aufnahme")
    assert latest.ergebnis["doppelt"][0]["citekey"] == key


def test_scan_form_validates_folder(client, tmp_path):
    r = post(client, "/aufnehmen/scans", {"quelle": str(tmp_path / "fehlt")})
    assert r.status_code == 302 and r.headers["Location"].endswith("/aufnehmen")
    r = post(client, "/aufnehmen/ocr-ordner", {"ordner": str(tmp_path)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/aufnehmen")


def test_settings_and_static_pages(client, monkeypatch, tmp_path):
    from bibliothek import config
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "einstellungen.json")
    assert client.get("/einstellungen").status_code == 200
    post(client, "/einstellungen", {"bibliothek": "", "ocr_sprache": "eng+deu+grc"})
    assert config.settings()["ocr_sprache"] == "eng+deu+grc"
    post(client, "/einstellungen", {"ocr_sprache": "rm -rf"})
    assert config.settings()["ocr_sprache"] == "eng+deu+grc"
    for url in ("/aufnehmen", "/jobs", "/suche", "/static/app.css", "/static/app.js"):
        assert client.get(url).status_code == 200, url
