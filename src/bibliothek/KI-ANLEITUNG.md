# Bibliothek — Anleitung für KI-Agenten

Diese Datei legt die Bibliothek automatisch an. Sie erklärt Agenten (Claude Code
u. a.), wie die zentrale Literatur benutzt wird. Nicht von Hand bearbeiten — sie
wird bei Programm-Updates erneuert.

## Werkzeuge (MCP) — der bequemste Weg

`bib mcp` stellt die Bibliothek als Werkzeuge bereit. Einbinden in Claude Code:
`claude mcp add bibliothek -- bib mcp` (oder `.mcp.json` im Projekt).

| Werkzeug | Zweck |
|---|---|
| `suche` | Volltext + semantisch, mit Seitenangabe und `zitierfaehig` |
| `lies` | Seiten lesen (Druckseiten oder Scans), mit Seitenmarken |
| `zitat_pruefen` | steht das Zitat wörtlich da, auf welcher Seite, stimmt die angegebene Seite? |
| `titel`, `liste`, `bibtex`, `status` | Metadaten, Übersicht, BibTeX-Einträge |
| `metadaten_vorschlaege` | Katalogtreffer für fehlende Angaben (nur Vorschläge) |
| `aufnehmen` | neue Datei oder per DOI/ISBN aufnehmen |

## Suchen statt Dateien kopieren

Literatur wird nicht in Projekte kopiert. Ohne MCP laufen Suchen und Lesen über `bib`:

```sh
bib suche "Gattung als Lesekonvention"          # hybrid: Wörter + Bedeutung
bib suche '"historischer Präzedenzfall"' --modus text   # exakte Phrase
bib suche "Jona Satire" --tag Jona -n 20 --json # gefiltert, maschinenlesbar
bib zeige <citekey> --seiten 45-47              # Druckseiten mit Seitenmarken
bib zeige <citekey> --scan --seiten 57          # nach Scan-Nummer
bib zeige <citekey>                             # Metadaten + Notizen
bib liste --projekt Hermeneutik                 # was gehört zu einem Projekt
bib bibtex --aus-tex <pfad/zur/arbeit> -o <pfad>/references.bib
bib beleg <citekey> "wörtliches Zitat" --seite 45   # Zitat gegen den Volltext prüfen
```

Vor dem Abgeben jedes wörtliche Zitat mit `zitat_pruefen` bzw. `bib beleg` prüfen:
„wörtlich“ + `zitierfaehig` heißt belegt; „abweichend“ nennt die Unterschiede Wort
für Wort (bei OCR-Text am Scan klären).

Direkt lesen geht auch: `titel/<citekey>/text.md` (Seitenmarken `[[…]]`),
`titel/<citekey>/meta.yaml`, `titel/<citekey>/notizen.md`.

Originaldateien können auf der Nextcloud liegen statt im Ordner:
`bib oeffne <citekey> --nur-pfad` holt sie und nennt den lokalen Pfad.
Für Suchen und Lesen braucht es das nicht — Text und Seiten sind immer lokal.

## Seitenangaben — verbindlich

| Ausgabe | Bedeutung | Zitierbar? |
|---|---|---|
| `S. 45` / `S. 45 (Scan 57)` | geprüft oder aus dem PDF | ja |
| `S. 45? (Scan 57, Seitenzahl geschätzt)` | automatisch geschätzt | **nein** — erst prüfen lassen |
| `Scan 57 (Druckseite ungeklärt)` | Druckseite unbekannt | **nein** |
| `Abschn. 3` | Format ohne Seiten (Word, HTML, EPUB) | Seitenzahl aus anderer Quelle |

Keine Seitenzahl erfinden oder aus Scan-Nummern ableiten. Unsichere Stellen als
offen markieren und den Menschen bitten: `bib seiten <citekey>` zeigt die
Abschnitte; bestätigen mit `bib seiten <citekey> --bestaetigen`.

OCR-Text kann Erkennungsfehler enthalten. Wörtliche Zitate am Scan prüfen
lassen (`review.html` im OCR-Ordner, Pfad in `meta.yaml` unter `text.ocr_ordner`).

## Metadaten

`metadaten_fehlen` in `text.md` bzw. `unvollstaendig` in `meta.yaml` heißt:
Angaben fehlen. Nicht raten. `bib metadaten <citekey>` (bzw. `metadaten_vorschlaege`)
sucht in DNB, Crossref, OpenAlex und Open Library und nennt, wie gut jeder Treffer
belegt ist: **sicher** (ISBN/DOI und Titel stehen im Dokument), **wahrscheinlich**,
**möglich**. Übernehmen entscheidet der Mensch (`--nehmen N` oder App → Prüfen).
Von Hand: `bib bearbeite <citekey> --feld year=2015 --feld publisher=Brunnen`.

Umbenannte Titel behalten ihren alten Schlüssel im Feld `ids` — alte `\cite`-Befehle
funktionieren weiter, `bib bibtex --aus-tex` löst sie auf.

## Neue Literatur aufnehmen

```sh
bib neu <datei>... --tag X --projekt Y          # PDF, Word, ODT, RTF, EPUB, HTML, TXT, MD, PPTX, Bilder
bib scans <ordner-mit-fotos> --titel "…" --autor "Nachname, Vorname" --jahr 2019
bib scans <immich-link>
bib neu --doi 10.1234/abcd --projekt Y            # Angaben aus Crossref, PDF falls frei zugänglich
bib neu --isbn 978-3-7655-9425-1                  # Angaben aus DNB/Open Library
```

Seiten ohne Textebene werden automatisch per OCR erkannt.
