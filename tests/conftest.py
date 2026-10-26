import pytest


@pytest.fixture(autouse=True)
def _lokale_programmdaten(tmp_path, monkeypatch):
    """Index und Modelle der Tests nie in den echten Programmordner schreiben."""
    monkeypatch.setenv("BIBLIOTHEK_DATEN", str(tmp_path / "programmdaten"))
