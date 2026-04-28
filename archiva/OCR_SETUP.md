# Archiva OCR Setup

## Ziel

Für die Volltextindizierung sollen auch gescannte PDFs und Bilddateien per OCR erschlossen werden.
Dafür verwendet Archiva freie Tools.

## Verwendete freie Tools

- `tesseract`
- `ocrmypdf`
- optional `pypdf` für PDF-Textlayer
- optional `pdftotext` für PDF-Extraktion

## Empfohlene Installation auf macOS mit Homebrew

```bash
brew install poppler tesseract tesseract-lang ocrmypdf
pip install -e .
```

### Warum diese Pakete

- `tesseract` = OCR für Bilder
- `ocrmypdf` = OCR für PDFs
- `poppler` = liefert u. a. `pdftotext`
- `pypdf` = Python-basierte PDF-Textlayer-Extraktion

## Zusätzliche Sprachpakete für Tesseract

Wenn Deutsch wichtig ist:

```bash
brew install tesseract-lang
```

Danach stehen meist zusätzliche Sprachmodelle wie `deu` zur Verfügung.

## Prüfen, ob alles vorhanden ist

```bash
tesseract --version
ocrmypdf --version
pdftotext -v
python -c "import pypdf; print(pypdf.__version__)"
```

## Verhalten in Archiva aktuell

Der Extractor versucht derzeit in dieser Reihenfolge:

### PDF
1. `pypdf`
2. `pdftotext`
3. `ocrmypdf`

### Bilddateien
1. `tesseract`

## Wenn Tools fehlen

Dann funktioniert Archiva weiterhin, aber:

- reine Textdateien werden besser indexiert als Scans
- gescannte PDFs liefern ohne OCR wenig oder keinen Volltext
- Bilddateien liefern ohne Tesseract keinen OCR-Text

## Empfehlung

Für lokale Entwicklung und produktiven Einsatz sollten mindestens installiert sein:

```bash
brew install poppler tesseract tesseract-lang ocrmypdf
```

`pypdf` ist inzwischen als reguläre Python-Abhängigkeit in `pyproject.toml` verankert und kommt über:

```bash
source venv/bin/activate
pip install -e .
```
