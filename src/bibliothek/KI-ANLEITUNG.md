# Bibliothek — Anleitung für KI-Agenten

Diese Datei legt die Bibliothek automatisch an. Sie erklärt Agenten (Claude Code
u. a.), wie die zentrale Literatur benutzt wird. Nicht von Hand bearbeiten — sie
wird bei Programm-Updates erneuert.

## Suchen statt Dateien kopieren

Literatur wird nicht in Projekte kopiert. Suchen und Lesen laufen über `bib`:

```sh
bib suche "Gattung als Lesekonvention"          # hybrid: Wörter + Bedeutung
bib suche '"historischer Präzedenzfall"' --modus text   # exakte Phrase
bib suche "Jona Satire" --tag Jona -n 20 --json # gefiltert, maschinenlesbar
bib zeige <citekey> --seiten 45-47              # Druckseiten mit Seitenmarken
bib zeige <citekey> --scan --seiten 57          # nach Scan-Nummer
bib zeige <citekey>                             # Metadaten + Notizen
bib liste --projekt Hermeneutik                 # was gehört zu einem Projekt
bib bibtex --aus-tex <pfad/zur/arbeit> -o <pfad>/references.bib
```

Direkt lesen geht auch: `titel/<citekey>/text.md` (Seitenmarken `[[…]]`),
`titel/<citekey>/meta.yaml`, `titel/<citekey>/notizen.md`.

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
Angaben fehlen. Nicht raten, sondern nachfragen oder an verlässlicher Quelle
(Verlag, DNB, DOI) prüfen und dann eintragen:
`bib bearbeite <citekey> --feld year=2015 --feld publisher=Brunnen`.

## Neue Literatur aufnehmen

```sh
bib neu <datei>... --tag X --projekt Y          # PDF, Word, ODT, RTF, EPUB, HTML, TXT, MD, PPTX, Bilder
bib scans <ordner-mit-fotos> --titel "…" --autor "Nachname, Vorname" --jahr 2019
bib scans <immich-link>
```

Seiten ohne Textebene werden automatisch per OCR erkannt.
