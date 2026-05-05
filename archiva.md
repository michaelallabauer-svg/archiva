# Archiva Projektmemory

## Stand 2026-05-05 09:35

### Wiederaufnahme nach Modellwechsel
- Archiva-Memory wurde geladen und Projektstand reaktiviert.
- Hauptinstanz auf `http://localhost:8000` lief zunächst nicht; neu gestartet mit:
  ```bash
  cd ~/.openclaw/workspace/archiva
  nohup venv/bin/python3.11 -m archiva.main > server.log 2>&1 &
  ```
- Health Check ist grün: `GET /api/v1/health` → `200 {"status":"healthy","service":"archiva"}`.
- OpenSearch läuft weiter auf `localhost:9200`.
- Workflow Designer auf `:8000` liefert HTTP 200.
- Runtime-Routen auf `:8000` verifiziert:
  - Workflow duplizieren funktioniert.
  - Neue Workflow-Version anlegen funktioniert.
  - Schritt mit ausgehender Transition wird beim Löschen korrekt blockiert.
  - Schritt ohne Transitionen wird korrekt gelöscht.
- Testsuche über Archiva API weiterhin grün:
  - `GET /api/search?q=Allabauer&page=1&page_size=5` findet `Lebenslauf`.
- FastAPI-Deprecation-Warnung für `@app.on_event` in `archiva/main.py` wurde behoben:
  - Queue-Worker-Start/Shutdown läuft jetzt über einen `lifespan`-Handler.
  - Danach: `py_compile` grün und Neustart ohne `on_event`-Deprecation-Warnung.

### Diff-Review / Bereinigung 2026-05-05
- Arbeitsbaum wurde reviewed und die halb verdrahtete Soft-Delete-Arbeit gebündelt:
  - Soft-Delete-Felder für `cabinets`, `registers`, `documents` in Models + DB-Bootstrap konsistent.
  - App blendet soft-gelöschte Dokumente/Cabinets/Register aus.
  - Admin-Papierkorb zeigt jetzt Dokumente, Cabinets und Register.
  - Restore-Routen für alle drei Typen ergänzt.
  - Search-Fallback und OpenSearch-Merge filtern soft-gelöschte Dokumente aus.
  - Alembic-Setup auf lokale Settings-DB-URL umgestellt; Soft-Delete-Migration umfasst alle drei Tabellen.
- Verifiziert:
  - `py_compile` für App-, Search- und Alembic-Dateien grün.
  - Health `:8000` grün.
  - Workflow Designer HTTP 200.
  - Admin-Papierkorb HTTP 200.
  - Runtime-Test Soft-Delete/Trash/Restore für synthetisches Cabinet, Register und Dokument grün; Testdaten bereinigt.
  - Runtime-Test Workflow Duplicate/Version/Step-Delete weiterhin grün; Testdaten bereinigt.
- Hinweis: `ruff` ist in `venv/bin` aktuell nicht installiert, daher kein Ruff-Gate gelaufen.
- Auffälligkeit beim Review: Das bestehende Testdokument `Lebenslauf.pdf` hat `deleted_at` gesetzt und liegt damit fachlich im Papierkorb; dadurch wird es von der bereinigten Suche nun korrekt ausgefiltert.

---

## Stand 2026-04-28 23:10

### Lokale Runtime
- Repo: `/Users/michaelallabauer/.openclaw/workspace/archiva`
- Lokale App: `http://localhost:8000`
- Admin UI: `http://localhost:8000/ui/admin`
- App UI: `http://localhost:8000/ui/app`
- Health: `http://localhost:8000/api/v1/health`
- Startbefehl Host:
  ```bash
  cd ~/.openclaw/workspace/archiva
  nohup venv/bin/python3.11 -m archiva.main > server.log 2>&1 &
  ```
- PostgreSQL läuft lokal mit DB `archiva`, User/Pass `postgres`/`postgres`.
- OpenSearch läuft lokal per Docker als Container `archiva-opensearch` auf `http://localhost:9200`.
- Docker Compose ist im Repo verankert: `docker-compose.yml`; Start:
  ```bash
  docker compose up -d opensearch
  ```

### Heute umgesetzt / korrigiert
- Admin UI:
  - Dokumenttyp-Icon-Auswahl von Freitext auf Dropdown umgestellt.
  - Icon `🧾 Rechnung` ergänzt.
  - Metadatenfeld-Anlage repariert: Dokumenttyp-Dropdown wird jetzt aus Definitionen befüllt und der aktuelle Dokumenttyp wird vorausgewählt.
- App UI:
  - Archiva-Logo/Favicon als SVG unter `assets/` ergänzt.
  - Header-Layout: links Suche/Branding, rechts Auswahlbox ca. 1/5 Breite.
  - Objektbereich wieder 3-spaltig: Archivbaum, Ergebnisfenster, Inhaltsvorschau/Metadaten rechts.
  - Block „Schnell anlegen“ entfernt.
- Upload / Queues:
  - Internal Server Error beim Speichern behoben: PostgreSQL-Enum `previewjobstatus` erwartet lowercase (`pending`), SQLAlchemy schrieb vorher Enum-Namen (`PENDING`). Fix in `archiva/models.py` via `values_callable`.
  - App startet jetzt einen internen Queue-Worker für Preview- und Index-Jobs (`archiva/main.py`). Jobs bleiben dadurch nicht mehr dauerhaft `pending`.
  - Index-Worker nutzt jetzt den zentralen Extractor (`archiva/indexer/extractor.py`) statt der UI/API-Hilfsfunktion.
  - Wenn OpenSearch nicht erreichbar ist, bleibt Archiva lokal nutzbar und markiert Jobs über PostgreSQL-Fallback als abgeschlossen statt sie hängen zu lassen.
- OpenSearch:
  - Docker Desktop gestartet.
  - Container `archiva-opensearch` mit `opensearchproject/opensearch:2.19.1` eingerichtet.
  - Security Plugin im Dev-Setup deaktiviert (`DISABLE_SECURITY_PLUGIN=true`); Initialpasswort ist nur für Image-Startup gesetzt.
  - Bestehende Dokumente neu indexiert; OpenSearch-Index `archiva-documents-v1` enthält Dokumente.
- OCR / PDF-Text:
  - Systemtools installiert: `poppler`, `tesseract`, `tesseract-lang`, `ocrmypdf`.
  - Python-Abhängigkeit `pypdf>=6.10.0` in `pyproject.toml` verankert.
  - `README.md`, `config.example.yaml`, `archiva/OCR_SETUP.md` und `docker-compose.yml` aktualisiert/ergänzt.
  - `Lebenslauf.pdf` erfolgreich neu indexiert: Engine `opensearch/pypdf`, Textlänge ca. 1670 Zeichen, Suche nach `Allabauer` findet das Dokument.

### Aktueller geprüfter Stand
- `python3 -m py_compile` bzw. venv-Compile für geänderte Module war grün.
- Archiva Health Check: OK.
- Preview Jobs: `completed`.
- Index Jobs: `completed`.
- OpenSearch erreichbar auf `localhost:9200`.
- Testsuche über Archiva API:
  - `GET /api/search?q=Allabauer&page=1&page_size=5` findet `Lebenslauf.pdf`.

### Wichtige Dateien aus diesem Stand
- `pyproject.toml` — Python-Abhängigkeit `pypdf` ergänzt.
- `docker-compose.yml` — lokaler OpenSearch-Single-Node.
- `config.example.yaml` — OpenSearch-Konfiguration dokumentiert.
- `README.md` — Setup, OpenSearch und OCR-Abhängigkeiten dokumentiert.
- `archiva/OCR_SETUP.md` — konkrete OCR/PDF-Extraktionsvoraussetzungen.
- `archiva/main.py` — interner Queue Worker.
- `archiva/indexer/worker.py` — Extractor + OpenSearch/Postgres-Fallback.
- `archiva/models.py` — PreviewJobStatus Enum-Fix.
- `archiva/ui.py` — Admin/App-Layout und UI-Fixes.
- `assets/archiva-logo-flow.svg`, `assets/archiva-favicon.svg` — Logo/Favicon.

### Bekannte Hinweise / nächste Schritte
1. Die Queue-Worker-Integration nutzt aktuell `@app.on_event`, FastAPI gibt dafür eine Deprecation-Warnung aus. Später sauber auf Lifespan-Handler umbauen.
2. `config.yaml` ist lokal und sollte nicht ins Repo; `config.example.yaml` ist die Vorlage.
3. Für neue Maschinen: erst Systemtools installieren:
   ```bash
   brew install poppler tesseract tesseract-lang ocrmypdf
   ```
   dann Python-Setup:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -e ".[dev]"
   ```
   dann OpenSearch:
   ```bash
   docker compose up -d opensearch
   ```
4. Falls Docker nicht läuft, Archiva funktioniert weiter mit PostgreSQL-Fallback, aber OpenSearch-Suche/Ranking ist nicht aktiv.
5. Der aktuell hochgeladene Lebenslauf ist testweise als Dokumenttyp „Rechnung“ klassifiziert; fachlich später ggf. löschen oder neu klassifizieren.

---


## Stand 2026-04-23

### Workflow Designer
- Der Renderer in `archiva/ui.py` ist als zusammenhängender Block für `_render_workflow_designer_page(...)` vorhanden und im Dateistand konsistent.
- Der Workflow Designer enthält aktuell:
  - Workflow-Liste
  - grafische Schrittkette mit Drag-and-Drop-Reihenfolge
  - Schritt anlegen
  - Schritt bearbeiten
  - Transitionen anzeigen
  - Transitionen anlegen

### Heute umgesetzt
- Transitionen im Workflow Designer wurden funktional ausgebaut:
  - Transitionen können jetzt bearbeitet werden
  - Transitionen können jetzt gelöscht werden
  - bestehende Create-Route nutzt jetzt gemeinsame Validierungslogik
- In `archiva/ui.py` ergänzt:
  - Redirect-Helfer für Workflow-Designer-Weiterleitungen
  - zentrale Transition-Validierung
- Neue Routen:
  - `POST /workflow-designer/transitions/{transition_id}`
  - `POST /workflow-designer/transitions/{transition_id}/delete`

### Aktuelle Validierungsregeln für Transitionen
- kein Übergang auf denselben Schritt
- kein leeres Label
- keine doppelte Transition mit gleichem Label und gleichem Ziel vom selben Schritt
- pro Schritt maximal eine Transition mit `is_default=True`

### Verifizierter Runtime-Stand
- `archiva.ui` importiert in der venv sauber
- `_render_workflow_designer_page(...)` rendert erfolgreich HTML
- `/ui/workflow-designer` liefert erfolgreich HTTP 200
- Transitionen wurden end-to-end erfolgreich verifiziert für:
  - create
  - update
  - delete
  - Default-Validierung
- Konkret geprüft:
  - zweiter Default-Übergang vom selben Schritt wird korrekt blockiert
  - die Meldung lautet: `Es ist nur ein Standardübergang pro Schritt erlaubt`
  - nach dem Validierungsversuch blieb genau eine Default-Transition bestehen

### Zusätzlicher Fix aus dem Runtime-Check
- Beim echten Runtime-Check wurde noch ein Syntaxproblem im Workflow-Designer-CSS gefunden und behoben:
  - in `archiva/ui.py` fehlten schließende `}}` bei
    - `.workflow-step-summary`
    - `.workflow-transition-hint`
- Danach liefen `py_compile`, Import, Renderer-Smoke-Test und Route-Check erfolgreich.

### Neuer Ausbau danach
- Schritt-Löschen im Workflow Designer wurde begonnen und im Dateistand ergänzt.
- Neue Route in `archiva/ui.py`:
  - `POST /workflow-designer/steps/{step_id}/delete`
- Aktuelle Schutzregel:
  - ein Schritt darf nicht gelöscht werden, solange eingehende oder ausgehende Transitionen auf diesen Schritt existieren
- UI ergänzt:
  - Schrittkarten enthalten jetzt einen Button `Schritt löschen`
  - zusätzlich Hinweistext, ob Löschen aktuell blockiert ist oder möglich wäre
- Für diesen neuen Ausbau ist der Syntax-Gate bereits grün:
  - `python3 -m py_compile archiva/ui.py` erfolgreich
- Aber wichtig:
  - der Runtime-Test für Schritt-Löschen wurde noch nicht abgeschlossen, weil die Session-Exec-Policy weiter pro Command nach Freigaben gefragt hat

### OpenClaw-/Session-Kontext
- OpenClaw wurde erfolgreich repariert und aktualisiert; `openclaw status` lief wieder sauber.
- Relevanter produktiver State liegt unter `~/.openclaw/`
- Besonders wichtig:
  - Cronjobs unter `~/.openclaw/cron`
  - Workspace unter `~/.openclaw/workspace`
  - Config unter `~/.openclaw/openclaw.json`
- Das globale Paket liegt separat unter `/opt/homebrew/lib/node_modules/openclaw`
- Exec funktioniert wieder grundsätzlich, aber die laufende Session hing zuletzt noch an einer restriktiven Approval-Policy trotz gewünschter Lockerung. Deshalb sind Gateway-Neustart und neue Session vorgesehen.

### Verifizierter Runtime-Stand für Schritt-Löschen
- Exec nach OpenClaw-/Exec-Neustart kurz geprüft: ok
- Live-Check gegen die bestehende Instanz auf Port 8000 zeigte zuerst einen wichtigen Hinweis:
  - `POST /ui/workflow-designer/steps/{step_id}/delete` antwortete dort noch mit `404`
  - gleichzeitig war `POST /ui/workflow-designer/transitions/{transition_id}/delete` bereits vorhanden
  - das spricht dafür, dass auf `:8000` noch eine ältere geladene App-Version lief
- Deshalb wurde der Runtime-Test gegen eine frisch gestartete Instanz mit aktuellem Workspace-Code auf Port `8010` ausgeführt
- Ergebnis dort end-to-end erfolgreich:
  - Schritt mit **ausgehender** Transition wird korrekt blockiert
  - Schritt mit **eingehender** Transition wird korrekt blockiert
  - Schritt **ohne** Transitionen wird korrekt gelöscht
  - nach erfolgreichem Löschen werden die verbleibenden Schritt-Reihenfolgen sauber neu nummeriert (`10`, `20`, ...)
- Verifizierte Redirect-Meldungen:
  - `Schritt Schritt A blockiert (outgoing) kann nicht gelöscht werden, solange 1 ausgehende Transitionen existieren`
  - `Schritt Schritt B blockiert (incoming) kann nicht gelöscht werden, solange 1 eingehende Transitionen existieren`
  - `Schritt Schritt C löschbar gelöscht`
- Testdaten wurden danach wieder vollständig bereinigt

### Empfohlene nächsten Schritte
1. Archiva-Hauptprozess auf Port `8000` einmal sauber mit aktuellem Code neu starten, damit die neue Step-Delete-Route auch in der produktiven lokalen Instanz aktiv ist.
2. Danach kurzer Gegencheck direkt auf `:8000`, ob die Route jetzt denselben grünen Runtime-Stand hat.
3. Danach umgesetzt:
   - Rücksprünge/Schleifen werden im Workflow Designer jetzt sichtbar und verständlich markiert
   - Workflow kann jetzt dupliziert werden
   - neue Workflow-Version kann jetzt aus bestehendem Workflow erzeugt werden
4. Verifizierter Stand dieses Ausbaus:
   - frische Runtime-Instanz zeigte UI-Hinweise für Rücksprünge/Schleifen sowie Buttons für Duplizieren/Versionierung
   - `POST /workflow-designer/workflows/{workflow_id}/duplicate` funktioniert und kopiert Schritte + Transitionen
   - `POST /workflow-designer/workflows/{workflow_id}/version` funktioniert und erzeugt eine neue Version mit kopierten Schritten + Transitionen
   - geprüft mit Testworkflow inklusive Rücksprung `B -> A`; Ergebnis: Original + Kopie + `v2` jeweils mit 2 Schritten und 2 Transitionen
5. Offener Hinweis:
   - falls die Hauptinstanz auf `:8000` noch ohne Neustart läuft, braucht sie einen Archiva-Neustart, damit die neuen Duplicate-/Version-Routen und UI-Markierungen dort live sind

## Workflow Runtime Designentscheidung 2026-05-05 10:29
- Neymo entschieden: Archiva soll parallele Workflows erlauben.
- Für den MVP reicht Workflow Runtime auf Dokumenten, aber das Datenmodell soll später auch Cabinet und Register als Workflow-Träger unterstützen können.
- Workflow-History muss Ereignisse für Start, Transition/Schrittwechsel, Ende und Abbruch erzeugen.
- UI-Idee: Wenn auf einem Dokument ein aktiver Workflow läuft, zeigt die App einen prominenten Workflow-Hero/Button am Dokument.
- Klick auf den Workflow-Hero öffnet die Workflow-Maske im mittleren Bereich der App.
- Konsequenz fürs Datenmodell: Workflow-Instanz sollte polymorph auf ein Zielobjekt zeigen, z.B. `subject_kind` + `subject_id`, statt hart nur `document_id`; MVP validiert/nutzt zunächst `subject_kind=document`.

## Workflow Runtime MVP umgesetzt 2026-05-05 10:45
- Commit `ef6b7cd Add workflow runtime MVP` ergänzt erste echte Workflow Runtime.
- Neue Runtime-Tabellen/Modelle:
  - `workflow_instances` mit polymorphem `subject_kind`/`subject_id` für spätere Cabinet/Register-Workflows; MVP nutzt `document`.
  - `workflow_tasks` als aktueller Zuständigkeits-/Schritt-Snapshot.
  - `workflow_history_events` für `started`, `transitioned`, `completed`, `cancelled`.
- Neuer Service `archiva/workflow_runtime.py`:
  - Workflow auf Dokument starten.
  - Transition ausführen.
  - Workflow abschließen.
  - Workflow abbrechen.
  - Parallel aktive Workflows pro Dokument erlaubt.
- App-UI minimal verdrahtet:
  - Prominenter Workflow-Hero/Button erscheint bei aktiven Workflows am Dokument.
  - `workflow_panel=1` öffnet die Workflow-Maske im mittleren App-Bereich.
  - Maske zeigt aktive Workflows, aktuellen Schritt, verfügbare Transitionen, Startformular, Abschluss/Abbruch und Historie.
- Verifiziert:
  - `py_compile` grün für Models, DB, Runtime-Service, UI und Migration.
  - App/Health und Workflow Designer auf `:8000` HTTP 200.
  - Runtime-Smoke-Test: zwei parallele Workflow-Instanzen auf einem synthetischen Dokument gestartet; eine transitioned+completed, eine cancelled; History enthielt `started`, `transitioned`, `completed`, `cancelled`; Testdaten bereinigt.

## Eingangsrechnungs-MVP Seed umgesetzt 2026-05-05 10:55
- Admin-Route `POST /ui/admin/setup/invoice-mvp` legt die fachliche MVP-Struktur idempotent an:
  - Cabinet Type `Eingangsrechnungsbuch`
  - Register Type `Eingangsrechnungen`
  - Jahres-Cabinet für das aktuelle Jahr
  - Register `Eingangsrechnungen`
  - Document Type `Rechnung`
  - Standard-Metadatenfelder für Rechnungen
  - Rollen/Zuweisungsziele `Rechnungsprüfung`, `Rechnungsfreigabe`, `Buchhaltung`
  - Workflow-Vorlage `Eingangsrechnung` mit 6 Schritten und 7 Transitionen
- Admin-UI hat im Hero einen Button `Eingangsrechnungs-MVP einrichten`.
- Seed wurde auf lokaler DB ausgeführt; Workflow Designer zeigt `Eingangsrechnung`.
- Verifiziert:
  - `py_compile archiva/ui.py` grün
  - Health/Admin/Workflow Designer HTTP 200
  - Seed idempotent: zweiter Lauf erzeugt 0 neue Elemente
  - DB-Counts: 11 Felder, 6 Schritte, 7 Transitionen, 3 Rollen
  - App-Smoke: synthetische Rechnung konnte Workflow `Eingangsrechnung` starten; Workflow-Hero/-Maske und Start-History funktionieren; Testdokument bereinigt.

## Eingangsrechnungs-Seed korrigiert 2026-05-05 11:15
- Neymo fragte zurecht, warum ein Registertyp `Eingangsrechnungen` angelegt wurde. Entscheidung: Für MVP kein Registertyp/Register `Eingangsrechnungen`; `Rechnung` hängt direkt am Jahres-Cabinet. Workflowstatus bleibt führend.
- Seed `POST /ui/admin/setup/invoice-mvp` wurde korrigiert:
  - erstellt künftig keinen Registertyp und kein Register `Eingangsrechnungen`
  - migriert bestehende `Rechnung` vom Legacy-Register direkt ans Jahres-Cabinet
  - löscht leeres Legacy-Register und leeren Legacy-Registertyp
- Dokument-Kontextmenü ergänzt: `Eingangsrechnungs-WF starten` startet Workflow `Eingangsrechnung` direkt und öffnet die Workflow-Maske.
- Verifiziert:
  - `py_compile archiva/ui.py` grün
  - Health/Admin HTTP 200
  - Seed entfernt bestehendes Legacy-Register + Registertyp
  - Kontextmenü-HTML enthält `Eingangsrechnungs-WF starten`
  - POST `/ui/app/documents/{id}/workflows/start-invoice` startet Workflow und redirectet zu `workflow_panel=1`; Testdaten bereinigt.
