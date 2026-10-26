"""Programmstart per Doppelklick: lokaler Server plus Fenster.

macOS und Windows bekommen ein eigenes Fenster (pywebview: WKWebView bzw.
WebView2). Fehlt pywebview (Linux), öffnet sich der Standardbrowser und das
Programm läuft, bis es über „Beenden“ oder Strg+C gestoppt wird.

Läuft auf dem Standardport schon eine Bibliothek, wird nur ein Fenster dafür
geöffnet — die CLI erwartet den Server auf diesem Port.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.request
import webbrowser

from werkzeug.serving import make_server

from . import config
from .store import Library


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # wie werkzeug: ein Port im TIME_WAIT nach einem Neustart gilt als frei
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def running_instance(port: int) -> dict | None:
    """Antwortet auf dem Port schon eine Bibliothek? Dann ihr Status."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if "bibliothek" in data else None
    except Exception:
        return None


def _pick_port(start: int) -> int:
    for port in range(start, start + 50):
        if _port_free(port):
            return port
    raise RuntimeError("Kein freier Port gefunden.")


class Server:
    def __init__(self, lib: Library, port: int):
        from .web import create_app

        self.app = create_app(lib)
        self.app.config["SHUTDOWN"] = self.stop
        self.httpd = make_server("127.0.0.1", port, self.app, threaded=True)
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="bibliothek-server", daemon=True)
        self.on_stop = []

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        # nicht im Request-Thread blockieren (shutdown wartet auf serve_forever) und
        # kurz warten, damit die Seite „beendet“ noch ausgeliefert wird
        def run():
            self.httpd.shutdown()
            for cb in self.on_stop:
                try:
                    cb()
                except Exception:
                    pass
        timer = threading.Timer(0.6, run)
        timer.daemon = True
        timer.start()


def _window(url: str, server: Server | None) -> bool:
    """Natives Fenster öffnen. False, wenn pywebview nicht verfügbar ist."""
    if sys.platform.startswith("linux"):
        return False
    try:
        import webview
    except Exception:
        return False
    window = webview.create_window("Bibliothek", url, width=1280, height=860, min_size=(720, 520))
    if server is not None:
        server.on_stop.append(window.destroy)
    try:
        webview.start()
    except Exception:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    browser_only = "--browser" in argv
    no_open = "--ohne-fenster" in argv
    port = config.web_port()

    existing = running_instance(port)
    if existing:
        url = f"http://127.0.0.1:{port}/"
        print(f"Bibliothek läuft bereits: {url} ({existing['bibliothek']})")
        if not no_open and (browser_only or not _window(url, None)):
            webbrowser.open(url)
        return 0

    lib = Library(config.library_root())
    server = Server(lib, port if _port_free(port) else _pick_port(port + 1))
    server.start()
    print(f"Bibliothek: {lib.root}\nOberfläche: {server.url}  (Strg+C beendet)", flush=True)

    if not no_open and not browser_only and _window(server.url, server):
        server.stop()   # Fenster geschlossen → Programm beenden
        return 0
    if not no_open:
        webbrowser.open(server.url)
    try:
        while server.thread.is_alive():   # join mit Timeout bleibt für Strg+C unterbrechbar
            server.thread.join(0.5)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
