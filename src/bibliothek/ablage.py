"""Entfernte Ablage auf einer Nextcloud (WebDAV).

Aufbau dort, unter <WebDAV-Wurzel>/<ordner>/:
    titel/<citekey>/    meta.yaml, seiten.jsonl, notizen.md und die Originaldateien
    ocr/<citekey>/      OCR-Läufe (Buchfotos, Seitenbilder, review.html)
    Eingang/            Ablage zum Aufnehmen (Dateien oder Ordner mit Buchfotos)

Die Anmeldung läuft über den Nextcloud-Login im Browser (Login Flow v2); das
App-Passwort liegt im Schlüsselbund des Systems (keyring), nie in einer Datei.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Callable
from xml.etree import ElementTree as ET

import requests

from . import config

KEYRING_SERVICE = "Bibliothek-Nextcloud"
DAV = "{DAV:}"
TIMEOUT = (3, 120)   # Verbindungsaufbau kurz (offline schnell erkennen), Übertragung lang


class AblageFehler(RuntimeError):
    pass


class NichtErreichbar(AblageFehler):
    pass


@dataclass
class Eintrag:
    name: str
    ist_ordner: bool
    groesse: int
    etag: str
    geaendert: float   # Unix-Zeit


class WebDAV:
    def __init__(self, server: str, benutzer: str, passwort: str, ordner: str = "Bibliothek"):
        self.server = server.rstrip("/")
        self.benutzer = benutzer
        self.base = f"{self.server}/remote.php/dav/files/{urllib.parse.quote(benutzer)}/{ordner.strip('/')}"
        self.s = requests.Session()
        self.s.auth = (benutzer, passwort)
        self.s.headers["User-Agent"] = "Bibliothek"
        self._ordner_da: set[str] = set()

    # -- Grundlagen ---------------------------------------------------------------
    def _url(self, rel: str) -> str:
        rel = str(PurePosixPath(rel)).lstrip("/") if rel not in ("", ".") else ""
        return self.base + ("/" + urllib.parse.quote(rel) if rel else "")

    def _req(self, method: str, rel: str, *, versuche: int = 4, **kw) -> requests.Response:
        """Anfrage mit Wiederholung bei Verbindungsabbruch (ruhende Verbindung geschlossen, WLAN-Aussetzer).

        Ein Datei-Körper (offene Datei) lässt sich nicht wiederholen — das übernimmt `ablegen`.
        """
        timeout = kw.pop("timeout", TIMEOUT)
        if hasattr(kw.get("data"), "read"):
            versuche = 1
        for versuch in range(versuche):
            try:
                r = self.s.request(method, self._url(rel), timeout=timeout, **kw)
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                if versuch == versuche - 1:
                    raise NichtErreichbar(f"Nextcloud nicht erreichbar ({self.server})") from exc
                time.sleep(2 ** versuch)
        if r.status_code == 401:
            raise AblageFehler("Nextcloud lehnt die Anmeldung ab — neu verbinden (bib nas verbinden).")
        return r

    def erreichbar(self) -> bool:
        try:
            r = self._req("PROPFIND", "", headers={"Depth": "0"}, timeout=(2, 10))
            if r.status_code == 404:
                self.mkdirs("")
                return True
            return r.status_code in (207, 200)
        except NichtErreichbar:
            return False

    # -- Lesen --------------------------------------------------------------------
    def liste(self, rel: str) -> list[Eintrag] | None:
        """Inhalt eines Ordners (Tiefe 1), None wenn es ihn nicht gibt."""
        r = self._req("PROPFIND", rel, headers={"Depth": "1"}, data=_PROPFIND_BODY)
        if r.status_code == 404:
            return None
        if r.status_code != 207:
            raise AblageFehler(f"PROPFIND {rel}: HTTP {r.status_code}")
        own = urllib.parse.urlparse(self._url(rel)).path.rstrip("/")
        out = []
        for resp in ET.fromstring(r.content).iter(DAV + "response"):
            href = urllib.parse.unquote(urllib.parse.urlparse(resp.findtext(DAV + "href")).path).rstrip("/")
            if href == urllib.parse.unquote(own):
                continue
            prop = resp.find(f"{DAV}propstat/{DAV}prop")
            is_dir = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
            mod = prop.findtext(DAV + "getlastmodified")
            out.append(Eintrag(
                name=href.rsplit("/", 1)[-1], ist_ordner=is_dir,
                groesse=int(prop.findtext(DAV + "getcontentlength") or 0),
                etag=(prop.findtext(DAV + "getetag") or "").strip('"'),
                geaendert=parsedate_to_datetime(mod).timestamp() if mod else 0.0))
        return out

    def info(self, rel: str) -> Eintrag | None:
        parent, _, name = rel.rstrip("/").rpartition("/")
        for e in self.liste(parent) or []:
            if e.name == name:
                return e
        return None

    def holen(self, rel: str, ziel: Path) -> None:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_name(ziel.name + ".teil")
        with self._req("GET", rel, stream=True) as r:
            if r.status_code == 404:
                raise FileNotFoundError(rel)
            if r.status_code != 200:
                raise AblageFehler(f"GET {rel}: HTTP {r.status_code}")
            with tmp.open("wb") as fh:
                for block in r.iter_content(1 << 20):
                    fh.write(block)
        tmp.replace(ziel)

    def text(self, rel: str) -> bytes:
        r = self._req("GET", rel)
        if r.status_code != 200:
            raise AblageFehler(f"GET {rel}: HTTP {r.status_code}")
        return r.content

    # -- Schreiben ----------------------------------------------------------------
    def mkdirs(self, rel: str) -> None:
        parts = [p for p in PurePosixPath(rel or ".").parts if p not in ("/", ".")]
        for cur in [""] + ["/".join(parts[:i + 1]) for i in range(len(parts))]:
            if cur in self._ordner_da:
                continue
            r = self._req("MKCOL", cur)
            if r.status_code not in (201, 405):   # 405 = gibt es schon
                raise AblageFehler(f"MKCOL {cur or '/'}: HTTP {r.status_code}")
            self._ordner_da.add(cur)

    def ablegen(self, quelle: Path, rel: str) -> str:
        """Datei hochladen, gibt das neue ETag zurück."""
        self.mkdirs(str(PurePosixPath(rel).parent))
        for versuch in range(4):
            try:
                with quelle.open("rb") as fh:   # bei jedem Versuch neu öffnen
                    r = self._req("PUT", rel, data=fh, headers={"Content-Type": "application/octet-stream"})
                break
            except NichtErreichbar:
                if versuch == 3:
                    raise
                time.sleep(2 ** versuch)
        if r.status_code not in (200, 201, 204):
            raise AblageFehler(f"PUT {rel}: HTTP {r.status_code}")
        etag = (r.headers.get("OC-ETag") or r.headers.get("ETag") or "").strip('"')
        if not etag:
            e = self.info(rel)
            etag = e.etag if e else ""
        return etag

    def loeschen(self, rel: str) -> None:
        r = self._req("DELETE", rel)
        if r.status_code not in (200, 204, 404):
            raise AblageFehler(f"DELETE {rel}: HTTP {r.status_code}")

    def verschieben(self, von: str, nach: str, *, ueberschreiben: bool = False) -> None:
        self.mkdirs(str(PurePosixPath(nach).parent))
        r = self._req("MOVE", von, headers={"Destination": self._url(nach), "Overwrite": "T" if ueberschreiben else "F"})
        if r.status_code not in (201, 204):
            raise AblageFehler(f"MOVE {von} → {nach}: HTTP {r.status_code}")

    def ordner_hochladen(self, lokal: Path, rel: str, *, fortschritt: Callable[[str], None] | None = None) -> int:
        """Ordner rekursiv hochladen; gleich große Dateien, die schon da sind, werden übersprungen."""
        n = 0
        for d in sorted({lokal, *[p for p in lokal.rglob("*") if p.is_dir()]}):
            sub = (rel + "/" + d.relative_to(lokal).as_posix()).rstrip("/.") if d != lokal else rel
            self.mkdirs(sub)
            vorhanden = {e.name: e.groesse for e in (self.liste(sub) or []) if not e.ist_ordner}
            for f in sorted(p for p in d.iterdir() if p.is_file() and p.name != ".DS_Store"):
                if vorhanden.get(f.name) == f.stat().st_size:
                    continue
                self.ablegen(f, f"{sub}/{f.name}")
                n += 1
                if fortschritt and n % 25 == 0:
                    fortschritt(f"{n} Dateien hochgeladen ({sub})")
        return n


_PROPFIND_BODY = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                  b"<d:resourcetype/><d:getcontentlength/><d:getetag/><d:getlastmodified/>"
                  b"</d:prop></d:propfind>")


# --- Einrichtung -------------------------------------------------------------------------

def einstellungen() -> dict:
    return config.settings().get("nas") or {}


def verbunden() -> bool:
    return bool(einstellungen().get("server"))


def oeffnen() -> WebDAV | None:
    """WebDAV-Zugang aus Einstellungen + Schlüsselbund, oder None ohne NAS."""
    nas = einstellungen()
    if not nas.get("server"):
        return None
    import keyring

    passwort = keyring.get_password(KEYRING_SERVICE, f"{nas['benutzer']}@{nas['server']}")
    if not passwort:
        raise AblageFehler("Kein Nextcloud-Passwort im Schlüsselbund — neu verbinden (bib nas verbinden).")
    return WebDAV(nas["server"], nas["benutzer"], passwort, nas.get("ordner", "Bibliothek"))


def login_starten(server: str) -> dict:
    """Login Flow v2: liefert {'login': URL zum Öffnen, 'poll': {...}}."""
    server = server.rstrip("/")
    try:
        r = requests.post(f"{server}/index.php/login/v2", timeout=(3, 15), headers={"User-Agent": "Bibliothek"})
    except requests.RequestException as exc:
        raise NichtErreichbar(f"{server} nicht erreichbar") from exc
    if r.status_code != 200:
        raise AblageFehler(f"{server} ist keine Nextcloud oder der Login ist gesperrt (HTTP {r.status_code}).")
    return r.json()


def login_abwarten(flow: dict, *, ordner: str = "Bibliothek", timeout: float = 600,
                   abbruch: Callable[[], bool] | None = None) -> dict:
    """Wartet, bis im Browser bestätigt ist, und speichert den Zugang."""
    import keyring

    ende = time.time() + timeout
    while time.time() < ende:
        if abbruch and abbruch():
            raise AblageFehler("Abgebrochen")
        try:
            r = requests.post(flow["poll"]["endpoint"], data={"token": flow["poll"]["token"]}, timeout=(3, 15))
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            d = r.json()
            server = d["server"].rstrip("/")
            if ist_admin(server, d["loginName"], d["appPassword"]):
                _widerrufen(server, d["loginName"], d["appPassword"])
                raise AblageFehler(
                    f"„{d['loginName']}“ ist Administrator und sieht alle Daten der Nextcloud. Bitte einen eigenen "
                    "Benutzer nur für die Bibliothek anlegen (z. B. „bibliothek“) und mit dem verbinden. "
                    "Das eben erzeugte App-Passwort wurde wieder gelöscht.")
            keyring.set_password(KEYRING_SERVICE, f"{d['loginName']}@{server}", d["appPassword"])
            nas = {"server": server, "benutzer": d["loginName"], "ordner": ordner,
                   "bibliothek": str(config.library_root().resolve())}
            config.save_settings({"nas": nas})
            return nas
        time.sleep(2)
    raise AblageFehler("Zeit abgelaufen — Anmeldung im Browser nicht bestätigt.")


def ist_admin(server: str, benutzer: str, passwort: str) -> bool:
    """Gehört der Benutzer zur Gruppe admin? Lässt sich das nicht klären, wird abgelehnt."""
    try:
        r = requests.get(f"{server}/ocs/v2.php/cloud/users/{urllib.parse.quote(benutzer)}/groups",
                         auth=(benutzer, passwort), headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                         timeout=(3, 15))
        groups = r.json()["ocs"]["data"]["groups"] if r.status_code == 200 else None
    except (requests.RequestException, KeyError, ValueError):
        groups = None
    if groups is None:
        _widerrufen(server, benutzer, passwort)
        raise AblageFehler("Die Rechte des Nextcloud-Benutzers ließen sich nicht prüfen — Verbindung abgelehnt.")
    return "admin" in groups


def _widerrufen(server: str, benutzer: str, passwort: str) -> None:
    """App-Passwort auf der Nextcloud löschen (nicht nur lokal)."""
    try:
        requests.delete(f"{server}/ocs/v2.php/core/apppassword", auth=(benutzer, passwort),
                        headers={"OCS-APIRequest": "true"}, timeout=(3, 15))
    except requests.RequestException:
        pass


def verbinden(server: str, *, ordner: str = "Bibliothek", browser: bool = True,
              ausgabe: Callable[[str], None] = lambda m: print(m, flush=True)) -> dict:
    flow = login_starten(server)
    ausgabe(f"Im Browser anmelden und „Zugriff gewähren“ klicken:\n  {flow['login']}")
    if browser:
        webbrowser.open(flow["login"])
    return login_abwarten(flow, ordner=ordner)


def trennen() -> None:
    import keyring

    nas = einstellungen()
    if nas.get("server"):
        key = f"{nas['benutzer']}@{nas['server']}"
        passwort = keyring.get_password(KEYRING_SERVICE, key)
        if passwort:
            _widerrufen(nas["server"], nas["benutzer"], passwort)   # auch auf der Nextcloud ungültig machen
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
    s = config.settings()
    s.pop("nas", None)
    config.SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
