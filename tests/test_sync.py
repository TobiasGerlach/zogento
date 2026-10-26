import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from bibliothek import ablage, config, dateien, index, ingest, sync
from bibliothek.store import Library

from fake_dav import FakeDAV


@pytest.fixture
def nas(tmp_path, monkeypatch):
    """Nextcloud-Attrappe, eingetragen wie nach `bib nas verbinden`."""
    settings = tmp_path / "einstellungen.json"
    settings.write_text(json.dumps({"nas": {"server": "http://nas", "benutzer": "u", "ordner": "Bibliothek"}}))
    monkeypatch.setattr(config, "SETTINGS_FILE", settings)
    dav = FakeDAV()
    monkeypatch.setattr(ablage, "oeffnen", lambda: dav)
    return dav


@pytest.fixture
def rechner(tmp_path, monkeypatch):
    """Zwei Rechner: eigene Bibliothek und eigene Programmdaten, dieselbe Nextcloud."""
    @contextmanager
    def an(name):
        monkeypatch.setenv("BIBLIOTHEK_DATEN", str(tmp_path / f"daten-{name}"))
        yield Library(tmp_path / f"bib-{name}")
    return an


def _neu(lib, tmp_path, name="jona.txt", text="Jona flieht nach Tarschisch.\fDer Fisch."):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return ingest.add_file(lib, f, felder={"title": "Jona lesen", "author": "Muster, Max", "year": "2020"},
                           run_ocr=False)


def test_hochladen_dann_auf_zweitem_rechner_holen(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        item = _neu(mac, tmp_path)
        b = sync.abgleichen(mac, dav=nas)
        assert b["status"] == "ok"
        assert "titel/muster2020jona/jona.txt" in nas.files
        assert "titel/muster2020jona/meta.yaml" in nas.files
        assert "titel/muster2020jona/text.md" not in nas.files            # wird lokal erzeugt
        assert not (mac.folder(item.citekey) / "jona.txt").exists()        # lokal nur noch Zwischenspeicher
        assert dateien.original(mac, item, "jona.txt").read_text(encoding="utf-8").startswith("Jona")
        assert sync.abgleichen(mac, dav=nas)["hoch"] == 0                  # nichts doppelt

    with rechner("win") as win:
        b = sync.abgleichen(win, dav=nas)
        assert b["geholt"] == ["muster2020jona"]
        it = win.get("muster2020jona")
        assert (win.folder(it.citekey) / "text.md").exists()
        assert index.search(win, "Fisch", mode="text")[0].scan == 2
        assert not (win.folder(it.citekey) / "jona.txt").exists()          # Original erst bei Bedarf
        assert dateien.original(win, it, "jona.txt").exists()


def test_aenderung_und_konflikt(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        _neu(mac, tmp_path)
        sync.abgleichen(mac, dav=nas)
    with rechner("win") as win:
        sync.abgleichen(win, dav=nas)
        it = win.get("muster2020jona")
        it.tags = ["von-windows"]
        win.save(it)
        sync.abgleichen(win, dav=nas)
    with rechner("mac") as mac:
        b = sync.abgleichen(mac, dav=nas)
        assert mac.get("muster2020jona").tags == ["von-windows"] and b["runter"] >= 1
        # Konflikt: beide ändern, Windows gleicht zuerst ab
        it = mac.get("muster2020jona")
        it.tags = ["vom-mac"]
        mac.save(it)
    with rechner("win") as win:
        it = win.get("muster2020jona")
        it.tags = ["wieder-windows"]
        win.save(it)
        sync.abgleichen(win, dav=nas)
    with rechner("mac") as mac:
        b = sync.abgleichen(mac, dav=nas)
        assert b["konflikte"] and any(k.startswith("titel/muster2020jona/meta.yaml.konflikt-") for k in nas.files)
        assert mac.get("muster2020jona").tags == ["vom-mac"]


def test_umbenennen_und_loeschen_wandern_mit(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        item = _neu(mac, tmp_path)
        sync.abgleichen(mac, dav=nas)
    with rechner("win") as win:
        sync.abgleichen(win, dav=nas)
    with rechner("mac") as mac:
        mac.rename(mac.get("muster2020jona"), "muster2020jonaneu")
        sync.abgleichen(mac, dav=nas)
        assert "titel/muster2020jonaneu/jona.txt" in nas.files
        assert not any(k.startswith("titel/muster2020jona/") for k in nas.files)
    with rechner("win") as win:
        sync.abgleichen(win, dav=nas)
        assert win.keys() == ["muster2020jonaneu"]
    with rechner("mac") as mac:
        mac.delete("muster2020jonaneu")
        sync.abgleichen(mac, dav=nas)
    with rechner("win") as win:
        sync.abgleichen(win, dav=nas)
        assert win.keys() == []


def test_offline_bleibt_vorgemerkt(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        item = _neu(mac, tmp_path)
        nas.online = False
        b = sync.abgleichen(mac, dav=nas)
        assert b["status"] == "offline" and b["ausstehend"]["originale"] == 1
        assert (mac.folder(item.citekey) / "jona.txt").exists()
        nas.online = True
        sync.abgleichen(mac, dav=nas)
        assert dateien.ausstehend(mac)["originale"] == 0


def test_ocr_lauf_wandert_auf_die_nextcloud(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        item = _neu(mac, tmp_path)
        run = dateien.ocr_workdir(mac, item.citekey)
        (run / "previews").mkdir(parents=True)
        (run / "review.html").write_text("<html>Prüfansicht</html>", encoding="utf-8")
        (run / "previews" / "1.jpg").write_bytes(b"jpg")
        item.text["ocr_ordner"] = str(run)
        mac.save(item)
        sync.abgleichen(mac, dav=nas)
        item = mac.get(item.citekey)
        assert item.text["ocr_ordner"] == "nas:ocr/muster2020jona"
        assert not run.exists() and "ocr/muster2020jona/previews/1.jpg" in nas.files
        assert "Prüfansicht" in dateien.ocr_datei(mac, item, "review.html").read_text(encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            dateien.ocr_datei(mac, item, "../meta.yaml")


def test_eingang(nas, rechner, tmp_path):
    with rechner("mac") as mac:
        nas.put_bytes("Eingang/aufsatz.md", "# Gattung\n\nText über Gattungen.".encode(), alter=600)
        nas.put_bytes("Eingang/frisch.md", b"# Noch im Upload", alter=0)
        b = sync.abgleichen(mac, dav=nas)
        assert len(b["eingang"]) == 1 and "aufsatz" in b["eingang"][0]
        key = b["eingang"][0].split("→ ")[1]
        assert f"titel/{key}/aufsatz.md" in nas.files                     # gleich hochgeladen
        assert "Eingang/aufsatz.md" not in nas.files and "Eingang/frisch.md" in nas.files
        assert mac.get(key).unvollstaendig                                 # Metadaten nicht erfunden
        nas.put_bytes("Eingang/aufsatz.md", "# Gattung\n\nText über Gattungen.".encode(), alter=600)
        b = sync.abgleichen(mac, dav=nas)
        assert "Eingang/doppelt/aufsatz.md" in nas.files


def test_ohne_nas_bleibt_alles_lokal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "leer.json")
    lib = Library(tmp_path / "bib")
    item = _neu(lib, tmp_path)
    assert sync.abgleichen(lib)["status"] == "kein NAS"
    assert dateien.original(lib, item, "jona.txt") == lib.folder(item.citekey) / "jona.txt"
    assert dateien.ocr_workdir(lib, "x") == lib.root / "ocr" / "x"


def test_verbindungsabbruch_wird_wiederholt(monkeypatch, tmp_path):
    import requests
    from bibliothek.ablage import WebDAV

    dav = WebDAV("http://nas", "u", "pw")
    calls = []

    class Antwort:
        status_code = 201
        headers = {"OC-ETag": '"e1"'}

    def request(method, url, **kw):
        calls.append(method)
        if method == "PUT" and calls.count("PUT") < 3:
            raise requests.ConnectionError("Socket is not connected")
        return Antwort()

    monkeypatch.setattr(dav.s, "request", request)
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert dav.ablegen(f, "titel/k/a.txt") == "e1"
    assert calls.count("PUT") == 3
