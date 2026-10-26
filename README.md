# Bibliothek

Zentrale Literaturverwaltung für Studienarbeiten und Bücher: eine Sammlung
für alle Projekte, mit eingebautem Buch-OCR, Volltext- und semantischer Suche
und Ausgaben, die für KI-Agenten gemacht sind (Seitenmarken, ehrliche
Seitenangaben, JSON).

Läuft auf macOS, Ubuntu und Windows als App (Fenster) und als Kommandozeile `bib`.

## Was sie kann

- **Formate:** PDF (mit und ohne Textebene), Word (.docx, .doc mit Zusatzwerkzeug),
  OpenDocument, RTF, EPUB, HTML, TXT/Markdown/TeX, PowerPoint, Bilder.
- **OCR:** Seiten ohne Text werden automatisch erkannt. Buchfotos mit Doppelseiten
  (auch als Immich-Album) trennt das eingebaute Buch-OCR in Einzelseiten.
  Apple Vision auf dem Mac, Tesseract auf Linux/Windows. Alles lokal.
- **Seitenzahlen:** Druckseiten werden aus Kopf-/Fußzeilen geschätzt, abschnittsweise
  (fehlende oder doppelte Scans werden erkannt und gemeldet). Geschätzte Seiten sind
  mit `?` markiert, bis sie bestätigt sind.
- **Ordnen:** Tags, Kategorien (hierarchisch, `AT/Propheten`), Projekte, Notizen.
- **Suche:** SQLite-Volltext (Präfixsuche, Phrasen, OR/NOT) plus semantische Suche
  (Qwen3-Embedding-0.6B, mehrsprachig), kombiniert per Reciprocal Rank Fusion.
- **BibTeX:** Felder bleiben wörtlich erhalten. `refs.bib` wird erzeugt; `bib bibtex --aus-tex`
  schreibt nur die Einträge, die eine Arbeit zitiert.
- **Import:** BibTeX-Dateien (mit Textordner), Zotero (nur lesend, inkl. Sammlungen und
  Markierungen), fertige Buch-OCR-Ausgaben.

## Daten

```
~/Documents/Bibliothek/          (änderbar: bib einstellung --bibliothek PFAD)
  CLAUDE.md                      Anleitung für KI-Agenten (automatisch)
  refs.bib                       alle Einträge (erzeugt)
  titel/<citekey>/
    meta.yaml                    Metadaten — die Wahrheit
    seiten.jsonl                 Text je Seite/Abschnitt — die Wahrheit
    text.md                      Lesefassung mit [[S. 45 (Scan 57)]]-Marken (erzeugt)
    notizen.md                   Notizen, Zotero-Markierungen
    <Originaldateien>
  ocr/<citekey>/                 OCR-Läufe (Scans, review.html)
```

Der Suchindex liegt **nicht** im Bibliotheksordner, sondern pro Rechner im
Programmordner (`~/Library/Application Support/Bibliothek/indizes`,
`~/.local/share/bibliothek/indizes`, `%LOCALAPPDATA%\Bibliothek\indizes`).
So kann der Bibliotheksordner in iCloud, Nextcloud oder Syncthing liegen. Jeder
Rechner baut seinen Index mit `bib index` selbst.

## Nextcloud als Ablage (optional)

Originale und OCR-Läufe können auf einer Nextcloud liegen statt auf dem Rechner.
Lokal bleiben nur Katalog und Suchindex (wenige MB), deshalb funktionieren Suche,
Lesen und KI auch ohne Verbindung. Originale werden beim Öffnen geholt und in
einem Zwischenspeicher (höchstens 2 GB) gehalten. Der Katalog gleicht sich über
die Nextcloud zwischen allen Rechnern ab.

**Eigener Benutzer.** Die Bibliothek nimmt kein Admin-Konto an. Lege in der
Nextcloud einen eigenen Benutzer an (z. B. `bibliothek`, nicht in der Gruppe
admin) — er sieht nichts von deinen anderen Dateien. Willst du den Eingang aus
deinem eigenen Konto befüllen, gibt dieser Benutzer seinen Ordner `Bibliothek`
für dich frei.

```sh
bib nas verbinden http://nas:9001 --ohne-browser   # Link in einem privaten Fenster öffnen, als dieser Benutzer anmelden
bib sync                                          # erster Abgleich = Umzug
bib nas                                           # Status, Ausstehendes, Zwischenspeicher
bib oeffne <citekey>                              # Original öffnen (holt es bei Bedarf)
bib nas trennen                                   # Zugang löschen, auch auf der Nextcloud
```

Auf der Nextcloud: `Bibliothek/titel/<citekey>/` (Katalog + Originale),
`Bibliothek/ocr/<citekey>/` (Scans), `Bibliothek/Eingang/` — was dort liegt
(Dateien oder ein Ordner mit Buchfotos), nimmt die App automatisch auf und
entfernt es danach aus dem Eingang. Die App gleicht beim Start, alle 5 Minuten
und nach Änderungen ab; ohne Verbindung bleibt alles vorgemerkt. Das App-Passwort
liegt im Schlüsselbund des Systems.

## Installation

Fertige Pakete entstehen per GitHub Actions (`.github/workflows/release.yml`) bei
jedem Tag `v*`:

| System | Paket | Hinweise |
|---|---|---|
| macOS (Apple Silicon) | `Bibliothek.dmg` | App nach Programme ziehen. Beim ersten Start Rechtsklick → Öffnen (nicht notarisiert). `bib` einrichten: in der App unter Einstellungen. |
| Ubuntu 22.04+ | `bibliothek_<version>_amd64.deb` | `sudo apt install ./bibliothek_*.deb` — zieht Tesseract mit; `bib` liegt in `/usr/bin`. |
| Windows 10/11 | `Bibliothek-Setup.exe` | Installiert ohne Adminrechte, Tesseract ist dabei, `bib` landet im PATH. |

Das Embedding-Modell (~1,2 GB) lädt beim ersten semantischen Suchen bzw. bei
`bib index` in den Hugging-Face-Cache. Danach geht alles offline.

Selbst bauen: `packaging/build.sh` (macOS/Linux) bzw. `packaging/build.ps1` (Windows).

### Entwicklung

```sh
uv sync --extra dev
uv run bib --help
uv run bibliothek            # App
uv run pytest
```

## Kurzreferenz

```sh
bib neu datei.pdf --tag Jona --projekt Hermeneutik
bib scans ~/Fotos/Buch --titel "Biblische Hermeneutik" --autor "Nachname, Vorname" --jahr 2019
bib suche "Satire im Jonabuch"
bib zeige hurlbert2023drowning --seiten 90-91
bib seiten feestuart2015effektives            # Seitenzählung prüfen
bib bearbeite feestuart2015effektives --tag +Hermeneutik --kategorie Methodik
bib bibtex --aus-tex lectures/Hermeneutik/arbeit/src -o lectures/Hermeneutik/arbeit/src/references.bib
bib status
```

## Herkunft

`src/immich_book_ocr` ist das Buch-OCR aus `~/Documents/OCR`, eingebaut mit zwei
Änderungen: Tesseract-Pfad über `BOOK_OCR_TESSERACT` (für das Windows-Paket) und
Modell-Cache über `BOOK_OCR_MODELS` statt im Arbeitsverzeichnis.
