import pytest


@pytest.fixture(autouse=True)
def _lokale_programmdaten(tmp_path, monkeypatch):
    """Tests dürfen nie echte Programmdaten, Einstellungen, Schlüsselbund oder Nextcloud berühren."""
    import keyring

    from bibliothek import ablage, config

    monkeypatch.setenv("BIBLIOTHEK_DATEN", str(tmp_path / "programmdaten"))
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "einstellungen-test.json")

    def _kein_echtes_nas(*a, **k):
        raise AssertionError("Test wollte die echte Nextcloud öffnen — ablage.oeffnen monkeypatchen")

    monkeypatch.setattr(ablage, "oeffnen", _kein_echtes_nas)
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    monkeypatch.setattr(keyring, "set_password", lambda *a, **k: None)
