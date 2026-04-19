# Archiva Volltextarchitektur

## Zielbild

Archiva soll den gesamten Dokumenteninhalt und die Metadaten performant durchsuchbar machen.
Die Suche soll fachlich brauchbar sein, gut filtern, später Hervorhebungen in der Dokumentenvorschau unterstützen und mit freien Tools umsetzbar bleiben.

## Leitentscheidungen

- **Nur freie Tools**
- **Postgres bleibt System of Record**
- **Eigener Indexdienst** für Extraktion und Indexpflege
- **Workerqueue** für asynchrone Verarbeitung
- **Spezialisierte Suchengine statt Graph-Datenbank**
- **Metadaten und Volltext gemeinsam durchsuchbar**
- **Spätere Treffer-Hervorhebung in der Vorschau vorbereiten**

## Warum keine Graph-Datenbank

Graph-Datenbanken sind stark bei Beziehungen, Traversals und Wissensgraphen.
Der Hauptbedarf in Archiva ist aber:

- OCR und Textextraktion
- Volltextindexierung
- Relevanzranking
- Filterung nach Metadaten
- schnelle Suche über viele Dokumente
- spätere Hervorhebung von Treffern in der Vorschau

Dafür ist eine Suchengine deutlich passender als eine Graph-Datenbank.

## Empfohlene Architektur

```text
Dokumentenablage
    -> Postgres speichert Dokument, Metadaten, Struktur, Status
    -> Indexierungsjob in Queue
    -> Index-Worker verarbeitet Dokument
        -> Text extrahieren
        -> OCR falls nötig
        -> Metadaten normalisieren
        -> Suchindex aktualisieren
        -> optional Seiten-/Trefferdaten vorbereiten
    -> UI-Suche fragt Suchindex ab
    -> UI lädt Dokument/Metadaten weiterhin aus Postgres
```

## Komponenten

### 1. Postgres

Bleibt führend für:

- Dokumentstammdaten
- Strukturmodell
- Metadaten
- Speicherpfade
- Status der Indexierung
- Revisions- und Änderungsstände
- Queue-Metadaten, falls die Queue ebenfalls über Postgres läuft

Zusätzliche sinnvolle Felder:

- `index_status` (`pending`, `running`, `done`, `error`)
- `index_revision`
- `indexed_at`
- `content_hash`
- `index_error`

## 2. Indexdienst

Eigener Dienst, getrennt von App und Admin.

Aufgaben:

- Jobs aus Queue holen
- Dokumentinhalt extrahieren
- OCR anstoßen, wenn nötig
- Metadaten und Text in Suchdokument überführen
- Suchindex aktualisieren
- Fehler und Status zurückschreiben

Der Dienst sollte idempotent arbeiten.
Ein Dokument darf jederzeit erneut indexiert werden.

## 3. Workerqueue

Für freie Tools und einfachen Betrieb empfehlenswert:

### Variante A, einfach und robust
- **Postgres-basierte Jobqueue**
- z. B. eigene Tabelle oder leichtgewichtige Queue-Implementierung

Vorteile:
- wenig Infrastruktur
- einfach zu deployen
- alles in derselben Betriebswelt

### Variante B, wenn mehr Last erwartet wird
- **Redis + Worker**

Vorteile:
- gute Entkopplung
- hohe Verarbeitungsgeschwindigkeit
- bequem für Retry-Mechanismen

Für Archiva würde ich **zuerst mit Postgres-Queue starten**.

## 4. Suchengine

### Empfehlung
- **OpenSearch** als Zielarchitektur

Warum:
- freie Software
- starke Volltextsuche
- Filter + Ranking + Highlighting
- spätere Erweiterbarkeit
- gute Basis für Snippets und Trefferhervorhebung

### Alternative für schnelleren Einstieg
- **Meilisearch**

Warum:
- einfacher aufzusetzen
- gute Developer Experience
- schnell gute Suchergebnisse

Aber:
- für später komplexeres Highlighting, tiefere Suchlogik und feinere Kontrolle ist OpenSearch meist die stabilere Langfristoption

## Empfehlung für Archiva

### Wenn schnell starten wichtiger ist
- Postgres + Indexdienst + Postgres-Queue + Meilisearch

### Wenn gleich zukunftsfest gebaut werden soll
- Postgres + Indexdienst + Postgres-Queue + OpenSearch

Meine Empfehlung ist:

- **kurzfristig:** OpenSearch direkt prüfen und bevorzugen
- **nur wenn Setup-Aufwand zu hoch wird:** Meilisearch als Startlösung

## Indexierungs-Pipeline

### Bei Dokumentablage

1. Dokument wird gespeichert
2. Metadaten werden gespeichert
3. Queue-Job `index_document` wird erzeugt
4. Worker verarbeitet den Job asynchron

### Verarbeitung im Worker

1. Dokument laden
2. Dateityp erkennen
3. Text extrahieren
   - PDF mit Textlayer direkt lesen
   - Bild/PDF ohne Textlayer per OCR
   - Office/Text-Dateien über geeignete freie Extraktoren lesen
4. Metadaten in Suchformat überführen
5. Suchdokument erzeugen
6. In Suchengine schreiben
7. Indexstatus in Postgres aktualisieren

## Zu indexierende Inhalte

### Volltext

- kompletter extrahierter Dokumenttext
- optional Text pro Seite
- optional Textsegmente oder Blöcke

### Metadaten

- Dokumenttitel
- Dateiname
- Dokumenttyp
- Cabinettyp
- Cabinet
- Register
- alle fachlichen Metadatenfelder
- Tags
- Autor
- Erstell-/Änderungszeitpunkte

## Suchdokument, Beispiel

```json
{
  "document_id": "uuid",
  "title": "Rechnung 4711",
  "filename": "rechnung-april.pdf",
  "document_type": "Rechnung",
  "cabinet_type": "ERB",
  "cabinet": "2026",
  "register": null,
  "metadata": {
    "rechnungsnummer": "4711",
    "lieferant": "Muster GmbH",
    "betrag": "1250.00"
  },
  "fulltext": "... gesamter Dokumenttext ...",
  "pages": [
    {
      "page": 1,
      "text": "... Text Seite 1 ..."
    }
  ],
  "created_at": "2026-04-19T18:00:00Z",
  "updated_at": "2026-04-19T18:03:00Z"
}
```

## Suche in der UI

Die UI sollte später kombinieren können:

- Volltextsuche
- Filter nach Dokumenttyp
- Filter nach Cabinettyp, Cabinet, Register
- Filter nach Metadatenwerten
- Sortierung nach Relevanz
- Sortierung nach Datum

## Trefferhervorhebung in der Vorschau

Für den späteren Endausbau wichtig:

- Suchengine soll Highlight-Snippets liefern
- Indexdienst sollte möglichst seitenbezogene Informationen vorbereiten
- OCR oder PDF-Extraktion sollte, wenn möglich, Layout- oder Positionsdaten liefern

### Zielbild

Bei Suche nach einem Begriff:

- Trefferliste zeigt Snippets
- Klick auf Dokument öffnet Vorschau
- Vorschau springt möglichst zur relevanten Seite
- Treffer werden farblich markiert

### Dafür vorbereiten

- Text pro Seite speichern oder ableitbar halten
- später optional Bounding Boxes speichern
- Highlighting nicht nur als UI-Trick, sondern datengetrieben vorbereiten

## Performance-Grundsätze

- Indexierung immer asynchron
- Re-Indexierung über `content_hash` oder `index_revision` steuerbar
- Batch-Verarbeitung für Massenimporte
- Suchindex nur aus dem Worker aktualisieren, nicht aus der UI direkt
- App liest für Suche aus Suchengine, nicht aus Postgres-Freitextabfragen

## Fehlerbehandlung

- fehlgeschlagene Indexjobs mit Retry
- Status sichtbar in Postgres
- Fehlertext pro Dokument speicherbar
- manuelle Re-Indexierung einzelner Dokumente ermöglichen
- Voll-Reindex als Wartungsfunktion vorsehen

## Offene Technologieentscheidungen

### Textextraktion
Zu klären ist noch, welche freien Tools für welche Formate genutzt werden:

- PDFs mit Textlayer
- OCR für Scans/Bilder
- Office-Dokumente
- Plain Text / E-Mail / HTML

### Queue
Zu entscheiden:

- Start mit Postgres-Queue
- später optional Redis, wenn Last oder Parallelisierung steigt

### Suchengine
Zu entscheiden:

- OpenSearch als bevorzugte Zielarchitektur
- Meilisearch als vereinfachter Einstieg

## Empfohlene nächste Schritte

1. Suchanforderungen fachlich konkretisieren
   - Ranking
   - Filter
   - gewünschte Trefferdarstellung
2. Entscheidung OpenSearch vs Meilisearch treffen
3. Index-Jobmodell in Postgres definieren
4. Indexdienst als eigenes Modul/Dienst anlegen
5. Extraktionspipeline pro Dateityp entwerfen
6. API zwischen App und Suchdienst definieren
7. Re-Indexierungsstrategie festlegen

## Klare Empfehlung

Für Archiva mit freiem Tooling und ernsthafter Zukunftsperspektive:

- **Postgres** für die Fachdaten
- **eigener Indexdienst**
- **Postgres-Queue** als Start
- **OpenSearch** als Suchindex

Keine Graph-Datenbank als Primärlösung für die Volltextsuche.
Sie löst hier nicht das eigentliche Problem besser als eine Suchengine.

---

# Version 2, konkreter Architekturvorschlag

## Systemkomponenten

```text
+-------------------+       +-------------------+       +-------------------+
| Archiva UI / API  | ----> |   Postgres        | <---- |  Admin / App      |
|                   |       |   System of Record|       |  Dokumentablage   |
+-------------------+       +-------------------+       +-------------------+
          |                           |
          |                           |
          v                           v
+-------------------+       +-------------------+
| Index Job Queue   | <---- | Index Dispatcher  |
| (Postgres)        |       | bei Dokumentablage|
+-------------------+       +-------------------+
          |
          v
+-------------------+
| Index Worker      |
| Extraktion / OCR  |
| Normalisierung    |
| Retry / Status    |
+-------------------+
          |
          v
+-------------------+
| OpenSearch        |
| Volltextindex     |
| Highlighting      |
| Filter / Ranking  |
+-------------------+
          |
          v
+-------------------+
| Search API        |
| Query / Facets    |
| Trefferlisten     |
+-------------------+
```

## Laufzeitfluss

### A. Dokumentablage

```text
Dokument speichern
  -> Dokument und Metadaten in Postgres
  -> Indexierungsjob in Postgres-Queue anlegen
  -> UI bekommt sofort Erfolg zurück
  -> Worker indexiert asynchron
```

### B. Indexierung

```text
Worker holt Job
  -> Dokument aus Storage laden
  -> Dateityp bestimmen
  -> Text extrahieren
  -> OCR falls nötig
  -> Suchdokument aufbauen
  -> OpenSearch upsert
  -> Status in Postgres auf done/error
```

### C. Suche

```text
UI stellt Suchanfrage
  -> Search API fragt OpenSearch
  -> Trefferliste + Highlights + Filterwerte zurück
  -> UI lädt bei Bedarf Detaildaten weiter aus Postgres
```

## Datenhaltung pro Schicht

### Postgres

Bleibt führend für:

- `documents`
- `document_types`
- `metadata`
- Cabinet/Register/CabinetType-Zuordnung
- Speicherpfade
- Indexstatus
- Queue-Jobs

### OpenSearch

Hält nur das, was für Suche und Trefferdarstellung gebraucht wird:

- Volltext
- Metadatenkopie für Suchzwecke
- fachliche Strukturfelder
- Snippets / Seiteninfos, soweit erzeugbar
- Ranking-relevante Felder

## Vorschlag: Queue-Tabellen in Postgres

### `index_jobs`

```sql
CREATE TABLE index_jobs (
  id UUID PRIMARY KEY,
  document_id UUID NOT NULL,
  job_type TEXT NOT NULL DEFAULT 'index_document',
  status TEXT NOT NULL DEFAULT 'pending',
  priority INT NOT NULL DEFAULT 100,
  attempts INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 5,
  scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ NULL,
  finished_at TIMESTAMPTZ NULL,
  worker_id TEXT NULL,
  error_message TEXT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Indizes

```sql
CREATE INDEX idx_index_jobs_status_scheduled_at
  ON index_jobs (status, scheduled_at);

CREATE INDEX idx_index_jobs_document_id
  ON index_jobs (document_id);
```

## Vorschlag: Zusatzfelder in `documents`

```sql
ALTER TABLE documents
  ADD COLUMN index_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN index_revision INT NOT NULL DEFAULT 0,
  ADD COLUMN indexed_at TIMESTAMPTZ NULL,
  ADD COLUMN content_hash TEXT NULL,
  ADD COLUMN index_error TEXT NULL;
```

## API-Schnittstellen, Vorschlag

## Interne Index-API

### Job anlegen

```http
POST /internal/index/jobs
```

Payload:

```json
{
  "document_id": "uuid",
  "reason": "document_created"
}
```

### Einzelnes Dokument reindizieren

```http
POST /internal/index/documents/{document_id}/reindex
```

### Voll-Reindex starten

```http
POST /internal/index/rebuild
```

## Search API

### Suche

```http
GET /search?q=rechnung+4711&cabinet_type=ERB&document_type=Rechnung&page=1
```

Antwort, Beispiel:

```json
{
  "hits": [
    {
      "document_id": "uuid",
      "title": "Rechnung April",
      "document_type": "Rechnung",
      "cabinet": "2026",
      "score": 12.7,
      "highlights": {
        "fulltext": [
          "... <mark>Rechnung</mark> 4711 ..."
        ],
        "metadata.lieferant": [
          "<mark>Muster GmbH</mark>"
        ]
      }
    }
  ],
  "facets": {
    "document_type": [
      {"value": "Rechnung", "count": 17}
    ],
    "cabinet_type": [
      {"value": "ERB", "count": 42}
    ]
  },
  "total": 17,
  "page": 1,
  "page_size": 20
}
```

## OpenSearch Indexschema, Vorschlag

Indexname:

```text
archiva-documents-v1
```

Dokumentstruktur:

```json
{
  "document_id": "uuid",
  "title": "Rechnung 4711",
  "filename": "rechnung-april.pdf",
  "document_type": "Rechnung",
  "document_type_id": "uuid",
  "cabinet_type": "ERB",
  "cabinet_type_id": "uuid",
  "cabinet": "2026",
  "cabinet_id": "uuid",
  "register": null,
  "register_id": null,
  "metadata": {
    "rechnungsnummer": "4711",
    "lieferant": "Muster GmbH",
    "betrag": "1250.00"
  },
  "metadata_labels": {
    "rechnungsnummer": "Rechnungsnummer",
    "lieferant": "Lieferant",
    "betrag": "Betrag"
  },
  "fulltext": "... gesamter extrahierter Text ...",
  "pages": [
    {
      "page": 1,
      "text": "... Text von Seite 1 ..."
    }
  ],
  "created_at": "2026-04-19T18:00:00Z",
  "updated_at": "2026-04-19T18:05:00Z"
}
```

## Extraktionsstrategie pro Dateityp

### PDF

- zuerst Textlayer lesen
- wenn leer oder zu wenig Text, OCR ausführen
- optional Seitenstruktur mitschreiben

### Bilddateien

- OCR direkt
- wenn möglich Layoutdaten mit erfassen

### Office-Dokumente

- freien Konverter / Extraktor verwenden
- normalisierten Text erzeugen

### Text / HTML / Markdown

- direkt extrahieren
- Markup bereinigen

## OCR- und Extraktions-Design

Der Worker sollte intern mehrere Stufen haben:

```text
load_document
  -> detect_format
  -> extract_text
  -> maybe_run_ocr
  -> normalize_text
  -> split_into_pages_or_segments
  -> build_search_document
  -> upsert_index
```

## Ranking-Strategie, Startversion

Treffer sollten höher gewichtet werden, wenn sie in folgenden Feldern vorkommen:

1. Titel
2. wichtige Metadatenfelder
3. Dateiname
4. Volltext

Zusätzlich sinnvoll:

- jüngere Dokumente leicht bevorzugen
- exakte Treffer in Metadaten höher werten als reine Volltexttreffer

## Suchqualität, Zielbild

Die Suche soll unterstützen:

- freie Volltextsuche
- exakte Filter
- kombinierte Suche in Volltext + Metadaten
- Facetten
- Highlighting in Trefferliste
- später Hervorhebung in Dokumentvorschau

## Vorbereitung auf Hervorhebung in der Vorschau

Für echten Endausbau sollten wir mittelfristig vorbereiten:

- seitenweise Textspeicherung
- Treffer je Seite
- optional Bounding Boxes aus OCR/Layoutanalyse
- Sprung zu Trefferseite in UI

## Betriebsmodell

### Dienste

- `archiva-api`
- `archiva-indexer`
- `opensearch`
- optional später `ocr-worker` getrennt, falls Last steigt

### Start klein

Zunächst reichen:

- 1 API-Prozess
- 1 Index-Worker
- 1 OpenSearch-Node
- 1 Postgres-Instanz

### Später skalierbar

- mehrere Worker
- Prioritätsklassen für Jobs
- separates OCR-Worker-Pool
- OpenSearch mit Replikation/Shards je nach Datenmenge

## Fehler- und Retry-Strategie

- jeder Job hat `attempts` und `max_attempts`
- transienter Fehler -> Retry mit Backoff
- permanenter Fehler -> `error`
- UI/Admin soll Re-Indexierung anstoßen können

## Sicherheit und Robustheit

- Indexdienst bekommt keine Schreibrechte auf Fachobjekte außer Statusfeldern
- Suchindex ist ableitbar und darf rebuildbar sein
- OpenSearch nie als einzige Wahrheitsquelle verwenden

## Migrationsstrategie

### Phase 1

- Queue-Tabelle anlegen
- Indexstatus in `documents`
- Indexdienst-Skelett erstellen
- OpenSearch lokal hochziehen
- erstes `document -> search_document` Mapping implementieren

### Phase 2

- PDF/Text-Extraktion
- Metadaten mit indexieren
- UI-Suche an Search API hängen

### Phase 3

- OCR für Scans/Bilder
- Highlights in Trefferliste
- Re-Index-Funktion im Admin

### Phase 4

- Seiten-/Trefferbezug
- Hervorhebung in Dokumentvorschau
- Ranking verfeinern

## Konkrete Empfehlung für den nächsten technischen Schritt

1. `index_jobs` in Postgres modellieren
2. `documents.index_status` und verwandte Felder ergänzen
3. Search-Document-Schema definieren
4. OpenSearch lokal anbinden
5. ersten Worker bauen, der Dummy-/Textdokumente indexiert
6. erst danach OCR und komplexe Extraktion ergänzen

---

# Version 3, konkreter Implementierungszuschnitt für Archiva

## Ziel von Version 3

Diese Version übersetzt die Architektur in einen konkret umsetzbaren Zuschnitt für das bestehende Archiva-Projekt.
Sie beschreibt:

- mögliche Python-Modulstruktur
- SQLAlchemy-Modelle
- Queue-Handling
- Indexdienst-Aufbau
- Search-API-Zuschnitt
- Docker-Compose-Grundlage für freie lokale Infrastruktur

## Vorgeschlagene Modulstruktur

```text
archiva/
  main.py
  ui.py
  db.py
  models.py

  search/
    __init__.py
    schemas.py
    service.py
    mapping.py
    query_builder.py
    highlights.py

  indexer/
    __init__.py
    dispatcher.py
    worker.py
    jobs.py
    extractor.py
    ocr.py
    normalizer.py
    opensearch_client.py
    status.py

  api/
    __init__.py
    search_api.py
    internal_index_api.py

  migrations/
    ...

deploy/
  docker-compose.search.yml
```

## Verantwortlichkeiten der Module

### `search/schemas.py`

Enthält:

- Python-Modelle für Search Requests
- Search Responses
- Trefferobjekte
- Facettenobjekte

### `search/mapping.py`

Übersetzt Archiva-Datenbankobjekte in Suchdokumente.

Beispielaufgaben:

- `Document -> SearchDocument`
- Metadatenlabel-Mapping
- Cabinet/Register-Pfad aufbauen

### `search/query_builder.py`

Erzeugt OpenSearch-Queries aus UI-Filtern.

Beispiel:

- Volltext
- Dokumenttypfilter
- Cabinettypfilter
- Datumsfilter
- Highlighting-Konfiguration

### `indexer/dispatcher.py`

Wird bei Dokumentablage oder Dokumentänderung aufgerufen.

Aufgaben:

- Indexjob anlegen
- Revisionsstand hochzählen
- Doppeljobs vermeiden, wenn sinnvoll

### `indexer/worker.py`

Laufender Workerprozess.

Aufgaben:

- nächsten Job holen
- Dokument laden
- Extraktion starten
- Suchdokument bauen
- OpenSearch aktualisieren
- Status zurückschreiben

### `indexer/extractor.py`

Dateiformatabhängige Textextraktion.

Beispielmethoden:

- `extract_pdf_text(...)`
- `extract_image_text(...)`
- `extract_office_text(...)`
- `extract_plain_text(...)`

### `indexer/ocr.py`

OCR-spezifische Logik.

Später trennbar, wenn OCR eigene Worker bekommen soll.

### `indexer/opensearch_client.py`

Kapselt OpenSearch-Zugriffe.

Methoden z. B.:

- `index_document(...)`
- `delete_document(...)`
- `search(...)`
- `ensure_index(...)`

### `api/search_api.py`

Öffentliche Suchschnittstelle für die UI.

### `api/internal_index_api.py`

Interne Admin-/Reindex-Endpunkte.

## SQLAlchemy-Modell, Vorschlag für Indexjobs

```python
class IndexJob(Base):
    __tablename__ = "index_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True)
    job_type = Column(String, nullable=False, default="index_document")
    status = Column(String, nullable=False, default="pending", index=True)
    priority = Column(Integer, nullable=False, default=100)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    scheduled_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    worker_id = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
```

## SQLAlchemy-Erweiterung für `Document`

```python
class Document(Base):
    # bestehende Felder ...

    index_status = Column(String, nullable=False, default="pending")
    index_revision = Column(Integer, nullable=False, default=0)
    indexed_at = Column(DateTime(timezone=True), nullable=True)
    content_hash = Column(String, nullable=True)
    index_error = Column(Text, nullable=True)
```

## Queue-Logik, Startvorschlag

### Job anlegen

Bei:

- Dokument neu gespeichert
- Metadaten geändert
- Datei ersetzt
- manuelle Re-Indexierung

Pseudoablauf:

```python
def enqueue_document_index(db: Session, document: Document, reason: str) -> IndexJob:
    document.index_status = "pending"
    document.index_revision += 1

    job = IndexJob(
        document_id=document.id,
        job_type="index_document",
        status="pending",
        payload_json=json.dumps({
            "reason": reason,
            "index_revision": document.index_revision,
        }),
    )
    db.add(job)
    db.add(document)
    db.commit()
    return job
```

### Job holen

Worker soll möglichst atomar einen Job claimen.

Startversion pragmatisch:

- `SELECT ... FOR UPDATE SKIP LOCKED`
- Statuswechsel von `pending` auf `running`

## Worker-Skelett, Vorschlag

```python
def run_index_worker():
    while True:
        with SessionLocal() as db:
            job = claim_next_job(db)
            if not job:
                sleep(2)
                continue

            try:
                process_index_job(db, job)
            except Exception as exc:
                mark_job_failed(db, job, exc)
```

## `process_index_job(...)`, Ablauf

```python
def process_index_job(db: Session, job: IndexJob):
    document = db.query(Document).where(Document.id == job.document_id).first()
    if not document:
        mark_job_discarded(db, job, "document missing")
        return

    extracted = extract_document_content(document)
    search_doc = build_search_document(document, extracted)
    opensearch_client.index_document(search_doc)
    mark_job_done(db, job, document)
```

## Extraktionsmodell, interne DTOs

### `ExtractedDocumentContent`

```python
@dataclass
class ExtractedDocumentContent:
    fulltext: str
    pages: list[dict]
    source_type: str
    used_ocr: bool
    language: str | None = None
```

## Suchdokument-Schema in Python

```python
@dataclass
class SearchDocument:
    document_id: str
    title: str
    filename: str
    document_type: str | None
    document_type_id: str | None
    cabinet_type: str | None
    cabinet_type_id: str | None
    cabinet: str | None
    cabinet_id: str | None
    register: str | None
    register_id: str | None
    metadata: dict
    metadata_labels: dict
    fulltext: str
    pages: list[dict]
    created_at: str | None
    updated_at: str | None
```

## Search API, konkrete Endpunkte

### Öffentliche Suche

```http
GET /api/search?q=...&document_type_id=...&cabinet_type_id=...&cabinet_id=...&page=1&page_size=20
```

### Facetten/Filter

Kann direkt in derselben Antwort enthalten sein.

### Einzeldokument erneut indexieren

```http
POST /api/internal/index/documents/{document_id}/reindex
```

### Jobstatus prüfen

```http
GET /api/internal/index/jobs/{job_id}
```

## FastAPI-Router, Zuschnitt

```python
# archiva/api/search_api.py
router = APIRouter(prefix="/api/search", tags=["search"])

# archiva/api/internal_index_api.py
router = APIRouter(prefix="/api/internal/index", tags=["index-internal"])
```

## OpenSearch-Mapping, Richtung

Wichtige Feldtypen:

- `text` für Volltextfelder
- `keyword` für Filterfelder
- `date` für Zeitfelder
- `nested` nur wenn wirklich nötig

Beispielrichtung:

```json
{
  "mappings": {
    "properties": {
      "document_id": {"type": "keyword"},
      "title": {"type": "text"},
      "filename": {"type": "text"},
      "document_type": {"type": "keyword"},
      "cabinet_type": {"type": "keyword"},
      "cabinet": {"type": "keyword"},
      "register": {"type": "keyword"},
      "metadata": {"type": "object", "enabled": true},
      "fulltext": {"type": "text"},
      "created_at": {"type": "date"},
      "updated_at": {"type": "date"}
    }
  }
}
```

## Docker-Compose, Vorschlag

Datei: `deploy/docker-compose.search.yml`

```yaml
version: "3.9"

services:
  opensearch:
    image: opensearchproject/opensearch:2
    environment:
      - discovery.type=single-node
      - plugins.security.disabled=true
      - OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m
    ports:
      - "9200:9200"
      - "9600:9600"
    volumes:
      - opensearch-data:/usr/share/opensearch/data

volumes:
  opensearch-data:
```

## Konfigurationswerte, Vorschlag

```yaml
search:
  engine: opensearch
  opensearch_url: http://localhost:9200
  index_name: archiva-documents-v1

indexer:
  worker_id: archiva-indexer-1
  poll_interval_seconds: 2
  max_attempts: 5
```

## Erste Implementierungsreihenfolge

### Schritt 1
- Datenbankfelder ergänzen
- `IndexJob` Modell anlegen
- Alembic-Migration schreiben

### Schritt 2
- Dispatcher bauen
- bei Dokumentablage Job erzeugen

### Schritt 3
- OpenSearch Client bauen
- Index anlegen können

### Schritt 4
- einfacher Worker für Plain Text / PDF mit Textlayer

### Schritt 5
- Search API + erste UI-Suche gegen OpenSearch

### Schritt 6
- OCR-Pipeline ergänzen

## Konkreter Minimal-Slice

Der kleinste brauchbare End-to-End-Slice wäre:

1. Dokument speichern
2. Job wird erzeugt
3. Worker extrahiert Text aus einfachen PDFs/Textdateien
4. OpenSearch wird befüllt
5. `/api/search` liefert Treffer
6. UI zeigt Trefferliste aus OpenSearch

## Empfehlung für den nächsten Coding-Schritt

Wenn wir das jetzt implementieren wollen, würde ich direkt mit diesen Artefakten starten:

1. SQLAlchemy-Modell `IndexJob`
2. Migration für `documents.index_*`
3. `archiva/indexer/dispatcher.py`
4. `archiva/indexer/worker.py`
5. `archiva/indexer/opensearch_client.py`
6. `archiva/api/search_api.py`

Damit hätten wir einen klaren technischen Startpunkt für die Umsetzung.
