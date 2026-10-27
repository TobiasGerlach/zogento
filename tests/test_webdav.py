"""Allgemeiner WebDAV-Server statt Nextcloud: Verbinden, Schutzregeln, Abgleich ohne Ordner-ETags."""
import json

import pytest

from bibliothek import ablage, config, ingest, sync
from bibliothek.store import Library

from fake_dav import FakeDAV


class Antwort:
    def __init__(self, status_code, content=b"", js=None):
        self.status_code, self.content, self._js, self.headers = status_code, content, js, {}

    def json(self):
        if self._js is None:
            raise ValueError("kein JSON")
        return self._js


@pytest.fixture
def server(monkeypatch):
    """Ein WebDAV-Server im Heimnetz, der kein Nextcloud ist."""
    st = {"propfind": 207, "nextcloud": False, "mkcol": [], "keyring": {}}

    def request(self, method, url, **kw):
        if method == "PROPFIND":
            return Antwort(st["propfind"])
        if method == "MKCOL":
            st["mkcol"].append(url)
            return Antwort(201)
        raise AssertionError(method)

    def get(url, **kw):
        if url.endswith("/status.php") and st["nextcloud"]:
            return Antwort(200, js={"installed": True, "productname": "Nextcloud"})
        return Antwort(404)

    monkeypatch.setattr(ablage.requests.Session, "request", request)
    monkeypatch.setattr(ablage.requests, "get", get)
    monkeypatch.setattr(ablage, "_im_heimnetz", lambda host: host.startswith("192.168."))
    import keyring
    monkeypatch.setattr(keyring, "set_password", lambda svc, k, pw: st["keyring"].__setitem__(k, pw))
    return st


def test_verbinden_speichert_art_und_passwort_im_schluesselbund(server):
    nas = ablage.verbinden_webdav("http://192.168.0.5/dav/", "bib", "geheim")
    assert nas["art"] == "webdav" and nas["server"] == "http://192.168.0.5/dav"
    assert server["keyring"] == {"bib@http://192.168.0.5/dav": "geheim"}
    assert server["mkcol"][-1] == "http://192.168.0.5/dav/Bibliothek"
    gespeichert = json.loads(config.SETTINGS_FILE.read_text())["nas"]
    assert "geheim" not in json.dumps(gespeichert) and ablage.anzeigename(gespeichert) == "WebDAV-Server"


def test_nextcloud_nur_ueber_eigenen_login(server):
    # Sonst ließe sich die Admin-Sperre mit einem normalen Passwort umgehen
    with pytest.raises(ablage.AblageFehler, match="Nextcloud"):
        ablage.verbinden_webdav("http://192.168.0.70:9001/remote.php/dav/files/admin", "admin", "pw")
    server["nextcloud"] = True
    with pytest.raises(ablage.AblageFehler, match="Nextcloud"):
        ablage.verbinden_webdav("http://192.168.0.70:9001/dav", "admin", "pw")
    assert not server["keyring"]


def test_unverschluesselt_nur_im_heimnetz(server):
    with pytest.raises(ablage.AblageFehler, match="https"):
        ablage.verbinden_webdav("http://dav.example.org/", "bib", "pw")
    assert not server["keyring"]


def test_falsches_passwort(server):
    server["propfind"] = 401
    with pytest.raises(ablage.AblageFehler, match="Passwort falsch"):
        ablage.verbinden_webdav("https://dav.example.org/", "bib", "falsch")
    assert not server["keyring"] and not config.SETTINGS_FILE.exists()


def test_trennen_widerruft_nur_bei_nextcloud(monkeypatch):
    config.save_settings({"nas": {"art": "webdav", "server": "https://dav.example.org", "benutzer": "bib"}})
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a: "pw")
    monkeypatch.setattr(keyring, "delete_password", lambda *a: None)
    monkeypatch.setattr(ablage, "_widerrufen", lambda *a: pytest.fail("WebDAV hat keinen Widerruf"))
    ablage.trennen()
    assert "nas" not in config.settings()


def test_liste_ohne_etags_vom_server(monkeypatch):
    dav = ablage.WebDAV("https://dav.example.org", "bib", "pw", art="webdav")
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/Bibliothek/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
      <d:response><d:href>/Bibliothek/a.txt</d:href><d:propstat><d:prop><d:resourcetype/>
        <d:getcontentlength>5</d:getcontentlength><d:getlastmodified>Mon, 26 Oct 2026 10:00:00 GMT</d:getlastmodified>
      </d:prop></d:propstat></d:response></d:multistatus>'''
    monkeypatch.setattr(dav.s, "request", lambda *a, **k: Antwort(207, xml))
    (e,) = dav.liste("")
    assert e.name == "a.txt" and e.etag.startswith("5-")


def test_abgleich_ohne_ordner_etags(tmp_path, monkeypatch):
    """Einfache Server ändern Ordner-ETags nicht — Änderungen anderer Rechner müssen trotzdem ankommen."""
    config.save_settings({"nas": {"art": "webdav", "server": "https://dav", "benutzer": "u"}})
    dav = FakeDAV(ordner_etags=False)

    def rechner(name):
        monkeypatch.setenv("BIBLIOTHEK_DATEN", str(tmp_path / f"daten-{name}"))
        return Library(tmp_path / f"bib-{name}")

    mac = rechner("mac")
    f = tmp_path / "jona.txt"
    f.write_text("Jona flieht.", encoding="utf-8")
    ingest.add_file(mac, f, felder={"title": "Jona lesen", "author": "Muster, Max", "year": "2020"}, run_ocr=False)
    sync.abgleichen(mac, dav=dav)

    win = rechner("win")
    sync.abgleichen(win, dav=dav)
    it = win.get("muster2020jona")
    it.tags = ["von-windows"]
    win.save(it)
    sync.abgleichen(win, dav=dav)

    mac = rechner("mac")
    assert sync.abgleichen(mac, dav=dav)["geholt"] == ["muster2020jona"]
    assert mac.get("muster2020jona").tags == ["von-windows"]
    assert sync.abgleichen(mac, dav=dav)["runter"] == 0      # nichts doppelt holen
