"""Oberfläche mit Nextcloud: Schnell-Upload, Originale vom NAS, Einstellungen, Abgleich, Anmeldung."""
import io
import time

import pytest

from bibliothek import ablage, index, web
from bibliothek.jobs import JobRunner
from bibliothek.store import Item, Library, Page
from fake_dav import FakeDAV

TOKEN = "test-token"
NAS = {"server": "http://nas.local:9001", "benutzer": "tobias", "ordner": "Bibliothek"}


@pytest.fixture
def dav():
    return FakeDAV()


@pytest.fixture
def verbunden(monkeypatch, dav, tmp_path):
    state = {"an": True}
    monkeypatch.setattr(ablage, "verbunden", lambda: state["an"])
    # Bindung an die Testbibliothek (sonst verweigert sync den Abgleich als „andere Bibliothek“)
    monkeypatch.setattr(ablage, "einstellungen",
                        lambda: dict(NAS, bibliothek=str(tmp_path / "bib")) if state["an"] else {})
    monkeypatch.setattr(ablage, "oeffnen", lambda: dav if state["an"] else None)
    monkeypatch.setattr(ablage, "trennen", lambda: state.update(an=False))
    return state


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "bib")
    item = Item(citekey="hurlbert2023drowning", eintrag="article",
                felder={"author": "Hurlbert, Brandon M.", "title": "Drowning Jonah in a Thousand Genres", "year": "2023"},
                dateien=["hurlbert.pdf"], text={"quelle": "pdf"})
    lib.save(item)
    lib.write_pages(item, [Page(1, "Jonah as satire " * 30)])
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


def test_hinzufuegen_knopf_auf_jeder_seite(client, monkeypatch):
    monkeypatch.setattr(ablage, "verbunden", lambda: False)
    for url in ("/", "/suche", "/einstellungen", "/titel/hurlbert2023drowning"):
        html = client.get(url).get_data(as_text=True)
        assert "Literatur hinzufügen" in html and "data-hinzufuegen-datei" in html
        assert 'accept="' in html and ".pdf" in html and ".docx" in html
        assert "kein NAS" in html


def test_schnell_upload_legt_titel_an(client, runner, lib, monkeypatch):
    monkeypatch.setattr(ablage, "verbunden", lambda: False)
    r = client.post("/aufnehmen/schnell", headers={"X-CSRF-Token": TOKEN},
                    data={"dateien": (io.BytesIO("# Gleichnisse\n\nText über Gleichnisse.".encode()), "gleichnisse.md")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    antwort = r.get_json()
    assert antwort["anzahl"] == 1 and antwort["api"].startswith("/api/jobs/")
    runner.wait(10)
    job = client.get(antwort["api"]).get_json()
    assert job["status"] == "fertig" and len(job["ergebnis"]["neu"]) == 1
    assert lib.exists(job["ergebnis"]["neu"][0])

    # dieselbe Datei noch einmal: freundlich als Dublette gemeldet
    r = client.post("/aufnehmen/schnell", headers={"X-CSRF-Token": TOKEN},
                    data={"dateien": (io.BytesIO("# Gleichnisse\n\nText über Gleichnisse.".encode()), "gleichnisse.md")},
                    content_type="multipart/form-data")
    runner.wait(10)
    job = client.get(r.get_json()["api"]).get_json()
    assert job["ergebnis"]["doppelt"] and not job["ergebnis"]["neu"]


def test_schnell_upload_braucht_csrf(client):
    r = client.post("/aufnehmen/schnell", data={"dateien": (io.BytesIO(b"x"), "a.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 403


def test_original_kommt_vom_nas(client, verbunden, dav):
    dav.put_bytes("titel/hurlbert2023drowning/hurlbert.pdf", b"%PDF-1.4 vom NAS")
    r = client.get("/titel/hurlbert2023drowning/datei/hurlbert.pdf")
    assert r.status_code == 200 and r.data == b"%PDF-1.4 vom NAS"
    html = client.get("/titel/hurlbert2023drowning").get_data(as_text=True)
    assert "auf der Nextcloud" in html


def test_original_offline_freundliche_seite(client, verbunden, dav):
    dav.online = False
    r = client.get("/titel/hurlbert2023drowning/datei/hurlbert.pdf")
    assert r.status_code == 503
    html = r.get_data(as_text=True)
    assert "Nextcloud nicht erreichbar" in html and "Traceback" not in html


def test_ocr_pruefansicht_vom_nas(client, verbunden, dav, lib):
    item = lib.get("hurlbert2023drowning")
    item.text["ocr_ordner"] = "nas:ocr/hurlbert2023drowning"
    lib.save(item)
    dav.put_bytes("ocr/hurlbert2023drowning/review.html", b"<html><body>Pruefung</body></html>")
    assert "OCR-Prüfansicht" in client.get("/titel/hurlbert2023drowning").get_data(as_text=True)
    r = client.get("/titel/hurlbert2023drowning/ocr/review.html")
    assert r.status_code == 200 and b"Pruefung" in r.data
    assert client.get("/titel/hurlbert2023drowning/ocr/../meta.yaml").status_code == 404


def test_einstellungen_verbunden_und_getrennt(client, verbunden):
    html = client.get("/einstellungen").get_data(as_text=True)
    assert "Verbunden" in html and "tobias" in html and "Eingang" in html and "Jetzt abgleichen" in html
    r = post(client, "/nas/trennen", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Nextcloud getrennt" in html and "Mit Nextcloud verbinden" in html


def test_abgleich_job_laedt_hoch(client, runner, verbunden, dav):
    r = post(client, "/nas/abgleichen")
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    runner.wait(10)
    job = runner.all()[0]
    assert job.art == "abgleich" and job.status == "fertig", job.log
    assert "titel/hurlbert2023drowning/meta.yaml" in dav.files
    status = client.get("/api/status").get_json()
    assert status["nas"]["benutzer"] == "tobias"
    assert status["abgleich"]["online"] is True and status["abgleich"]["zuletzt"]
    assert status["abgleich"]["anzeige"]["text"].startswith("abgeglichen")
    assert "abgeglichen" in client.get("/").get_data(as_text=True)


def test_abgleich_offline_zeigt_status(client, runner, verbunden, dav):
    dav.online = False
    post(client, "/nas/abgleichen")
    runner.wait(10)
    s = client.get("/api/status").get_json()["abgleich"]
    assert s["online"] is False and s["anzeige"]["klasse"] == "offline"


def test_abgleich_ohne_nas(client, monkeypatch):
    monkeypatch.setattr(ablage, "verbunden", lambda: False)
    r = post(client, "/nas/abgleichen", follow_redirects=True)
    assert "Keine Nextcloud verbunden" in r.get_data(as_text=True)


def test_anmeldung_ueber_browser(client, runner, monkeypatch, dav, tmp_path):
    state = {"an": False}
    monkeypatch.setattr(ablage, "verbunden", lambda: state["an"])
    # Bindung an die Testbibliothek (sonst verweigert sync den Abgleich als „andere Bibliothek“)
    monkeypatch.setattr(ablage, "einstellungen",
                        lambda: dict(NAS, bibliothek=str(tmp_path / "bib")) if state["an"] else {})
    monkeypatch.setattr(ablage, "oeffnen", lambda: dav)
    geoeffnet = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: geoeffnet.append(url))
    monkeypatch.setattr(ablage, "login_starten", lambda server: {"login": f"{server}/login/v2/flow/abc", "poll": {}})

    def abwarten(flow, abbruch=None, **kw):
        state["an"] = True
        return dict(NAS)
    monkeypatch.setattr(ablage, "login_abwarten", abwarten)

    assert post(client, "/nas/verbinden", {"server": "kein-url"}, follow_redirects=True).status_code == 200
    assert not geoeffnet
    r = post(client, "/nas/verbinden", {"server": "http://nas.local:9001/"})
    assert r.status_code == 302 and geoeffnet == ["http://nas.local:9001/login/v2/flow/abc"]
    for _ in range(50):
        if client.get("/api/nas/login").get_json()["status"] != "wartet":
            break
        time.sleep(0.05)
    assert client.get("/api/nas/login").get_json()["status"] == "verbunden"
    runner.wait(10)   # anschließender erster Abgleich
    assert "Verbunden" in client.get("/einstellungen").get_data(as_text=True)


def test_anmeldung_server_nicht_erreichbar(client, monkeypatch):
    monkeypatch.setattr(ablage, "verbunden", lambda: False)

    def kaputt(server):
        raise ablage.NichtErreichbar(f"{server} nicht erreichbar")
    monkeypatch.setattr(ablage, "login_starten", kaputt)
    r = post(client, "/nas/verbinden", {"server": "http://10.0.0.1:9001"}, follow_redirects=True)
    assert "nicht erreichbar" in r.get_data(as_text=True)
