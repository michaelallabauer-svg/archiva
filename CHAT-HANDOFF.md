# Chat Handoff

Nutze diese Datei als stabilen Übergabestand vor einem neuen Chat.

## Aktuelles Projekt
- Name: Archiva
- Ziel: Ein leichtgewichtiges ECM mit klar getrennter Admin-, App-, Workflow- und Preview-Architektur weiterentwickeln, inklusive Dokumentaufnahme, Metadatenmodell und asynchroner Dokumentenvorschau.
- Status: FastAPI-Anwendung läuft lokal über `python -m archiva.main`. Eine queue-basierte Dokumentenvorschau mit separatem Worker-Dienst ist angelegt. Uploads erzeugen Preview-Jobs, fertige Vorschauen werden als Artefakte ausgeliefert.
- GitHub: https://github.com/michaelallabauer-svg/archiva/
- Lokaler Start:
  1. `cd /Users/clawdia/.openclaw/workspace`
  2. `source venv/bin/activate`
  3. Entweder einzeln starten:
     - App: `python -m archiva.main`
     - Preview-Worker: `python -m archiva.preview_worker`
  4. Oder gemeinsam: `bash dev/start_archiva_dev.sh`

## Entscheidungen
- Archiva ist als FastAPI-App aufgebaut.
- Das Domänenmodell wurde korrigiert: `ERB` ist kein konkretes Cabinet, sondern ein `Cabinettyp`. Konkrete Cabinets sind z. B. `2025`, `2026` und hängen unter einem Cabinettyp.
- Zielhierarchie: Cabinettyp → Cabinet → Register → Dokumenttyp → Dokument.
- Die UI ist serverseitig gerendert und in drei Flächen getrennt:
  - `/ui/admin` für Struktur, Cabinets, Registers, Dokumenttypen und Metadatenmodell
  - `/ui/app` für tägliche Nutzung, Intake, Objektübersicht und Dokumentdetails
  - `/ui/workflows` als vorgesehene dritte Fläche für Workflows
- In der ECM-App gibt es bereits eine direkte Objektübersicht mit Suche und Schnellfiltern statt eines tief verschachtelten Klickpfads.
- Die Dokumentdetailseite bleibt der zentrale Ort für die Vorschau.
- Die Vorschau läuft jetzt als separater Dienstpfad mit Queue-Modell:
  - Upload erzeugt einen Preview-Job
  - ein separater Worker verarbeitet Jobs
  - Vorschau-Artefakte werden gespeichert und von der UI ausgeliefert
- Es gibt jetzt einen Preview-Status-Endpunkt und Auto-Refresh in der Warteansicht.
- Text-Extraktion für PDF, DOCX und Bilder ist in `archiva/api_documents.py` noch als TODO offen.

## Offene Punkte
- Echte Konvertierung für DOCX/XLSX/PPTX im Preview-Worker anschließen.
- OCR/Text-Extraktion und Volltextindex anbinden.
- Prüfen, wie Preview-Artefakte versioniert und invalidiert werden sollen.
- Optional: Thumbnail-Generierung und Mehrseiten-Navigation für PDFs.
- Flow-Archiv Branding und visuelles Redesign schrittweise in die UI einziehen.

## Nächste Schritte
1. Kontextmenü in der Baumübersicht ist weiter ausgebaut: Aktionen öffnen jetzt gezielt die Schnellanlage bzw. den Intake-Bereich, mit Kontext für Cabinettyp und Cabinet. Quick-Create schreibt Kontextwerte jetzt konsequenter in Default-Auswahl und Formularzustand zurück, visuell ruhiger mit Kontext-Note und gemutetem Sekundärformular.
2. Danach Flow-Archiv Branding aus `ARCHIVA-BRANDING.md` weiter in die Hauptseite und App-Komponenten übersetzen.
3. Danach Office-Konvertierung oder OCR/Indexing priorisieren.
4. Service-Karten und Objektkarten gestalterisch vereinheitlichen.

## Wichtige Dateien
- `archiva/main.py` - App-Start, Router-Montage, Storage-Initialisierung
- `archiva/ui.py` - Servergerenderte Admin-, App- und Detailoberflächen, Preview-Einbindung, Status-Endpunkt, Warteansicht mit Polling, jetzt auch mit Schnellanlage für Cabinet/Register in `/ui/app`
- `archiva/api.py` - Admin-API für Cabinets, Registers, Dokumenttypen und Metadatenfelder
- `archiva/api_documents.py` - Dokument-API, Upload, Listing, Detail, Suche, Capture-Definition, TODO für Text-Extraktion
- `archiva/preview_queue.py` - Queue-Logik, Render-Pipeline, Artefakt-Erzeugung
- `archiva/preview_worker.py` - separater Preview-Worker-Dienst
- `dev/start_archiva_dev.sh` - gemeinsamer Dev-Start für App plus Preview-Worker
- `ARCHIVA-BRANDING.md` - Logo-, Farb- und UX-Konzept im Flow-Archiv-Stil

## Startprompt für neuen Chat
"Lies CHAT-HANDOFF.md und mach beim Archiva-Projekt weiter. Fokus gerade: ECM, Dokumentdetailseite und Dokumentenvorschau." 
