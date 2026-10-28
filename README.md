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
  Apple Vision auf dem Mac, Tesseract auf Linux/Windows; umschaltbar auf nur Open Source
  (`bib einstellung --ocr-engine tesseract`). Alles lokal.
- **Seitenzahlen:** Druckseiten werden aus Kopf-/Fußzeilen geschätzt, abschnittsweise
  (fehlende oder doppelte Scans werden erkannt und gemeldet). Geschätzte Seiten sind
  mit `?` markiert, bis sie bestätigt sind.
- **Doppelseiten und Spalten:** Kopierte Bücher mit einer Doppelseite pro PDF-Seite werden
  erkannt und am Bund getrennt (auch gedreht), Lexika mit Spaltenzählung als „Sp.“
  (`bib teilen <citekey> doppelseiten|spalten|doppelseiten-spalten`).
- **Ordnen:** Tags, Kategorien (hierarchisch, `AT/Propheten`), Projekte, Notizen.
- **Suche:** SQLite-Volltext (Präfixsuche, Phrasen, OR/NOT) plus semantische Suche
  (Qwen3-Embedding-0.6B, mehrsprachig), kombiniert per Reciprocal Rank Fusion.
- **BibTeX:** Felder bleiben wörtlich erhalten. `refs.bib` wird erzeugt; `bib bibtex --aus-tex`
  schreibt nur die Einträge, die eine Arbeit zitiert.
- **Import:** BibTeX-Dateien (mit Textordner), Zotero (nur lesend, inkl. Sammlungen und
  Markierungen), fertige Buch-OCR-Ausgaben, per DOI/ISBN (Angaben aus Katalogen, frei
  zugängliches PDF über OpenAlex).
- **Kataloge:** Fehlende Angaben in DNB, Crossref, OpenAlex und Open Library nachschlagen.
  Jeder Vorschlag nennt Quelle und Belegstärke (sicher = ISBN/DOI und Titel stehen im
  Dokument). Übernommen wird nur, was du auswählst; der alte Zitierschlüssel bleibt als
  Alias (`ids`) gültig.
- **Zitate prüfen:** `bib beleg` findet ein wörtliches Zitat im Volltext (tolerant gegen
  Silbentrennung), nennt die echte Seite, ob sie zitierfähig ist, und Abweichungen Wort für Wort.
- **Prüfen:** eine Seite mit allem, was noch offen ist (fehlende Angaben, ungeprüfte
  Seitenzahlen), Scanbilder direkt neben der Seitenzählung.
- **Für KI-Agenten:** MCP-Server (`bib mcp`) mit Werkzeugen für Suche, Lesen, Zitatprüfung,
  BibTeX und Aufnahme; jede Seitenangabe sagt, ob sie zitierfähig ist.
- **Sicherung:** `bib sicherung <ordner>` schreibt eine vollständige, direkt nutzbare Kopie
  (auch der Originale aus der Ablage), inkrementell.

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

## Speicherort: dieser Rechner, Nextcloud oder WebDAV (optional)

Standard ist **nur dieser Rechner** — alles liegt im Bibliotheksordner. Wer will,
legt Originale und OCR-Läufe auf einen eigenen Server: eine Nextcloud oder einen
anderen WebDAV-Server (ownCloud Infinite Scale, OpenCloud, Seafile, Apache/nginx,
`rclone serve webdav`). Lokal bleiben nur Katalog und Suchindex (wenige MB), deshalb
funktionieren Suche, Lesen und KI auch ohne Verbindung. Originale werden beim Öffnen
geholt und in einem Zwischenspeicher (höchstens 2 GB) gehalten. Der Katalog gleicht
sich über den Server zwischen allen Rechnern ab. Einstellen in der App unter
Einstellungen → Speicherort oder per `bib nas`.

**Nextcloud: eigener Benutzer.** Die Bibliothek nimmt kein Admin-Konto an. Lege in der
Nextcloud einen eigenen Benutzer an (z. B. `bibliothek`, nicht in der Gruppe
admin) — er sieht nichts von deinen anderen Dateien. Willst du den Eingang aus
deinem eigenen Konto befüllen, gibt dieser Benutzer seinen Ordner `Bibliothek`
für dich frei. Eine Nextcloud lässt sich nur über diesen Weg verbinden, nicht als
„anderer WebDAV-Server“, damit die Admin-Prüfung nicht umgangen wird.

**Anderer WebDAV-Server.** Adresse, Benutzer und Passwort; am besten ein Zugang,
der nur diesen einen Ordner sieht. Unverschlüsseltes `http://` geht nur im Heimnetz.
Weil einfache Server Ordner-ETags nicht mitändern, sieht der Abgleich dort jeden
Titelordner an (etwas langsamer, sonst gleich).

```sh
bib nas verbinden http://nas:9001 --ohne-browser   # Nextcloud: Link in einem privaten Fenster öffnen, als dieser Benutzer anmelden
bib nas verbinden https://server/dav --webdav --benutzer bib   # anderer WebDAV-Server, fragt das Passwort
bib sync                                          # erster Abgleich = Umzug
bib nas                                           # Status, Ausstehendes, Zwischenspeicher
bib oeffne <citekey>                              # Original öffnen (holt es bei Bedarf)
bib nas trennen                                   # Zugang löschen (bei Nextcloud auch auf dem Server)
```

Auf dem Server: `Bibliothek/titel/<citekey>/` (Katalog + Originale),
`Bibliothek/ocr/<citekey>/` (Scans), `Bibliothek/Eingang/` — was dort liegt
(Dateien oder ein Ordner mit Buchfotos), nimmt die App automatisch auf und
entfernt es danach aus dem Eingang. Die App gleicht beim Start, alle 5 Minuten
und nach Änderungen ab; ohne Verbindung bleibt alles vorgemerkt. Passwörter
liegen im Schlüsselbund des Systems.

## KI-Agenten (MCP)

```sh
claude mcp add bibliothek -- bib mcp        # Claude Code, im Projektordner
```

Oder als `.mcp.json` im Projekt: `{"mcpServers": {"bibliothek": {"command": "bib", "args": ["mcp"]}}}`.
Läuft die App, nutzt die Suche deren schon geladenes Sprachmodell. Werkzeuge: `suche`, `lies`,
`zitat_pruefen`, `titel`, `liste`, `bibtex`, `status`, `metadaten_vorschlaege`, `aufnehmen`.

## Sicherung

```sh
bib sicherung /Volumes/Platte/Bibliothek    # danach reicht: bib sicherung
```

Die Sicherung ist selbst eine vollständige Bibliothek (öffnen mit `bib einstellung --bibliothek …`).
Mit Ablage werden Originale und Scans von dort geholt. Gelöscht wird in der Sicherung nie etwas.
`bib status` und die App erinnern nach zwei Wochen — außer die Ablage wird schon anderweitig gesichert
(`bib einstellung --sicherung-extern ja`, z. B. wenn das NAS selbst sichert).

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
bib neu --doi 10.1234/abcd --projekt Hermeneutik
bib suche "Satire im Jonabuch"
bib beleg hurlbert2023drowning "these attempts to classify Jonah are, as I will argue, misguided" --seite 88
bib metadaten --alle                         # Katalogvorschläge für alle unvollständigen Titel
bib zeige hurlbert2023drowning --seiten 90-91
bib seiten feestuart2015effektives            # Seitenzählung prüfen
bib bearbeite feestuart2015effektives --tag +Hermeneutik --kategorie Methodik
bib bibtex --aus-tex lectures/Hermeneutik/arbeit/src -o lectures/Hermeneutik/arbeit/src/references.bib
bib status
bib sicherung /Volumes/Platte/Bibliothek
```

## Herkunft

`src/immich_book_ocr` ist das Buch-OCR aus `~/Documents/OCR`, eingebaut mit zwei
Änderungen: Tesseract-Pfad über `BOOK_OCR_TESSERACT` (für das Windows-Paket) und
Modell-Cache über `BOOK_OCR_MODELS` statt im Arbeitsverzeichnis.
