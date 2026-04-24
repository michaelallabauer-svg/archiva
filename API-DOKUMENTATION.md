# Archiva API-Dokumentation

> Arbeitsstand: lebendes Dokument. Diese API-Dokumentation soll bei jeder neuen Route und jeder fachlichen Änderung erweitert werden.

## 1. Überblick

Archiva stellt aktuell mehrere API-Bereiche bereit:

- **`/api/v1`** → Haupt-API für Admin- und Dokumentfunktionen
- **`/api/search`** → neue Such-API über die Search-Service-Schicht
- **`/api/internal/index`** → interne Indexierungs-Endpunkte

---

## 2. API-Bereiche im Detail

### 2.1 `/api/v1`
Dieser Bereich enthält:
- Stammdaten / Definitionsdaten
- Dokument-Upload und Dokumentzugriff
- Capture-Definitionen für dynamische Formulare
- klassische CRUD-Endpunkte für zentrale Objekte

### 2.2 `/api/search`
Dieser Bereich liefert Suchergebnisse über die neuere Suchschicht.

### 2.3 `/api/internal/index`
Dieser Bereich ist für interne Indexierungsfunktionen gedacht.
Er ist eher technisch als fachlich und primär für Runtime-/Betriebsfunktionen relevant.

---

## 3. Basisverhalten der API

### Datenformate
- Standardmäßig **JSON**
- Dokument-Upload über **`multipart/form-data`**

### IDs
- Objekte verwenden in der Regel **UUIDs**

### Fehlerverhalten
Typische Fehler:
- `404` → Objekt nicht gefunden
- `422` → ungültige Eingabe / Validierungsproblem
- `500` → technischer Fehler, z. B. Storage nicht initialisiert

---

# 4. Haupt-API `/api/v1`

## 4.1 Health

### `GET /api/v1/health`
Prüft, ob die API grundsätzlich erreichbar ist.

**Zweck:**
- einfacher Healthcheck
- Monitoring
- Smoke-Test nach Neustart

**Antwort:**
```json
{
  "status": "healthy",
  "service": "archiva"
}
```

---

## 4.2 Cabinet Types

Cabinet Types definieren die fachliche Oberklasse von Cabinets.

Beispiele:
- `ERB`
- `Personal`
- `Verträge`

### `GET /api/v1/cabinet-types`
Liefert alle Cabinettypen sortiert nach Reihenfolge und Name.

**Funktion:**
- Übersicht der Definitionsstruktur
- Auswahlbasis für Admin- und Create-Flows

### `POST /api/v1/cabinet-types`
Legt einen neuen Cabinettyp an.

**Typische Eingaben:**
- `name`
- `description`
- `order`

**Funktion:**
- fachliche Struktur erweitern

### `PUT /api/v1/cabinet-types/{cabinet_type_id}`
Aktualisiert einen bestehenden Cabinettyp.

**Funktion:**
- Name, Beschreibung oder Reihenfolge ändern

---

## 4.3 Cabinets

Cabinets sind konkrete Instanzen innerhalb eines Cabinettyps.

Beispiel:
- Cabinettyp `ERB`
- konkrete Cabinets `2025`, `2026`

### `GET /api/v1/cabinets`
Liefert alle Cabinets mit verschachtelten Registern, Dokumenttypen und Metadatenfeldern.

**Funktion:**
- vollständige Strukturübersicht
- gut für UI-Aufbau und Tree-Darstellung

### `POST /api/v1/cabinets`
Legt ein neues Cabinet an.

**Voraussetzung:**
- gültiger `cabinet_type_id`

**Funktion:**
- konkrete Fachstruktur erzeugen

### `GET /api/v1/cabinets/{cabinet_id}`
Liefert ein einzelnes Cabinet mit seiner verschachtelten Struktur.

**Funktion:**
- Detailansicht für ein Cabinet
- Nachladen eines konkreten Teilbaums

### `PUT /api/v1/cabinets/{cabinet_id}`
Aktualisiert ein Cabinet.

**Funktion:**
- Name ändern
- Beschreibung ändern
- Reihenfolge ändern
- Cabinettyp-Zuordnung ändern

### `DELETE /api/v1/cabinets/{cabinet_id}`
Löscht ein Cabinet.

**Funktion:**
- entfernt das Cabinet samt Inhalt

**Achtung:**
- destruktiver Endpunkt
- wirkt auf untergeordnete Inhalte mit

---

## 4.4 Registers

Registers sind konkrete Untereinheiten innerhalb eines Cabinets.

### `POST /api/v1/registers`
Legt ein neues Register in einem Cabinet an.

**Voraussetzung:**
- gültiger `cabinet_id`

### `GET /api/v1/registers/{register_id}`
Liefert ein Register mit verschachtelten Dokumenttypen.

**Funktion:**
- Detailansicht eines Registers

### `PUT /api/v1/registers/{register_id}`
Aktualisiert ein Register.

**Funktion:**
- Name, Beschreibung, Reihenfolge oder Cabinet-Zuordnung ändern

### `DELETE /api/v1/registers/{register_id}`
Löscht ein Register.

**Funktion:**
- entfernt das Register samt Inhalt

---

## 4.5 Document Types

Document Types definieren die Objekt- bzw. Dokumentklassen.

Beispiele:
- Eingangsrechnung
- Vertrag
- Personalakte

Ein Dokumenttyp kann aktuell genau einem von zwei Zielen zugeordnet werden:
- `cabinet_id`
- `register_id`

Genau **eine** dieser beiden Angaben muss gesetzt sein.

### `POST /api/v1/document-types`
Legt einen neuen Dokumenttyp an.

**Validierung:**
- genau eines von `cabinet_id` oder `register_id` muss gesetzt sein

### `GET /api/v1/document-types/{document_type_id}`
Liefert einen Dokumenttyp mit seinen Metadatenfeldern.

**Funktion:**
- Detailansicht
- Grundlage für dynamische Formulare

### `GET /api/v1/document-types/{document_type_id}/layout`
Liefert ein automatisch generiertes Formular-Layout für einen Dokumenttyp.

**Funktion:**
- UI-Generierung
- Form-Renderer / Capture-Oberflächen

### `PUT /api/v1/document-types/{document_type_id}`
Aktualisiert einen Dokumenttyp.

**Funktion:**
- Name, Beschreibung, Icon, Reihenfolge ändern
- Zielzuordnung ändern

### `DELETE /api/v1/document-types/{document_type_id}`
Löscht einen Dokumenttyp.

**Funktion:**
- entfernt den Dokumenttyp samt Feldern

---

## 4.6 Metadata Fields

Metadatenfelder definieren die strukturierte Datenerfassung.

Ein Feld kann aktuell genau einem Ziel zugeordnet sein:
- `document_type_id`
- `cabinet_id`
- `register_id`

Genau **eine** dieser drei Angaben muss gesetzt sein.

### `POST /api/v1/metadata-fields`
Legt ein neues Metadatenfeld an.

**Funktion:**
- strukturiertes Datenmodell aufbauen
- Validierungslogik definieren

**Unterstützte Aspekte:**
- Feldtyp
- Label
- Pflichtfeld
- Eindeutigkeit
- Reihenfolge
- Placeholder
- Default-Wert
- Optionen
- Min/Max-Werte
- Min/Max-Länge
- Pattern
- Breite

### `GET /api/v1/metadata-fields/{field_id}`
Liefert ein einzelnes Metadatenfeld.

### `PUT /api/v1/metadata-fields/{field_id}`
Aktualisiert ein Metadatenfeld.

**Funktion:**
- Darstellungs- und Validierungslogik ändern

### `DELETE /api/v1/metadata-fields/{field_id}`
Löscht ein Metadatenfeld.

---

## 4.7 Dokumente

Dieser Bereich kümmert sich um Upload, Lesen, Listen und Löschen von Dokumenten.

### `POST /api/v1/documents`
Lädt ein neues Dokument hoch.

**Format:**
- `multipart/form-data`

**Typische Felder:**
- `file`
- `title`
- `author`
- `description`
- `tags`
- `document_type_id`
- `metadata` (JSON als String)

**Funktion intern:**
1. Metadaten-JSON wird geparst
2. falls Metadaten vorhanden sind, ist `document_type_id` Pflicht
3. Metadaten werden gegen den Dokumenttyp validiert
4. Datei wird im Storage gespeichert
5. Dokumenttyp wird erkannt / Dateityp abgeleitet
6. Text wird soweit möglich extrahiert
7. Dokument wird gespeichert
8. Volltext-/Indexlogik wird angestoßen
9. Index-Job wird enqueued

**Wichtig:**
- wenn `metadata` übergeben wird, aber `document_type_id` fehlt, kommt `422`

### `GET /api/v1/documents`
Liefert eine paginierte Dokumentliste.

**Filter:**
- `page`
- `page_size`
- `doc_type`
- `document_type_id`

**Funktion:**
- Listenansichten
- Filterung nach Dokumentart oder Dokumenttyp

### `GET /api/v1/documents/{document_id}`
Liefert ein einzelnes Dokument.

**Funktion:**
- Detailansicht
- Abruf strukturierter Metadaten

### `DELETE /api/v1/documents/{document_id}`
Löscht ein Dokument.

**Funktion intern:**
- Datei im Storage löschen
- zugehörige `DocumentVersion`-Einträge löschen
- Dokumentdatensatz löschen

---

## 4.8 Capture-Definitionen

### `GET /api/v1/document-types/{document_type_id}/capture`
Liefert die Capture-Definition eines Dokumenttyps.

**Funktion:**
- dynamische Intake-Formulare erzeugen
- UI kann daraus Formularfelder direkt rendern

**Enthält typischerweise:**
- Dokumenttyp-Metadaten
- Feldliste
- Feldtypen
- Pflichtstatus
- Placeholder
- Optionen
- Validierungsparameter

---

## 4.9 Alte Suche innerhalb `/api/v1`

### `GET /api/v1/search`
Sucht Dokumente über die ältere Suchlogik.

**Parameter:**
- `q`
- optional `doc_type`
- `limit`

**Funktion:**
- Volltextsuche auf Basis der älteren Suchschicht

**Hinweis:**
- parallel existiert bereits die neuere Such-API unter `/api/search`
- der Suchbereich ist aktuell noch im Übergang / Hybridzustand

---

# 5. Neue Such-API `/api/search`

### `GET /api/search`
Liefert Suchergebnisse über `SearchService`.

**Parameter:**
- `q` → Freitextsuche
- `document_type_id`
- `cabinet_type_id`
- `cabinet_id`
- `page`
- `page_size`

**Funktion:**
- moderne Suchschicht
- filterbare Suchergebnisse
- gedacht als konsolidierte Such-API für UI und weitere Ausbauten

**Technischer Hinweis:**
- nutzt `SearchService(db).search(...)`

---

# 6. Interne Index-API `/api/internal/index`

## `GET /api/internal/index/status`
Liefert den Runtime-Status der Indexierungsumgebung.

**Funktion:**
- prüfen, ob Indexierung/OCR grundsätzlich arbeitsfähig ist
- Diagnose für Runtime-Probleme

## `POST /api/internal/index/documents/{document_id}/reindex`
Stößt für ein Dokument eine erneute Indexierung an.

**Funktion:**
- manueller Reindex
- hilfreich nach Fixes oder bei fehlerhaften Indexständen

**Antwort im Erfolgsfall:**
- `ok: true`
- `job_id`
- `document_id`

**Antwort wenn Dokument fehlt:**
- `ok: false`
- `error: document_not_found`

---

# 7. Technische Funktionsbausteine hinter der API

## 7.1 Upload- und Speicherlogik
Im Dokument-Upload laufen u. a. diese Funktionen:
- Storage-Pfad erzeugen
- Datei speichern
- Text extrahieren
- Volltext aktualisieren
- Index-Job enqueueen

## 7.2 Metadatenvalidierung
Metadaten werden über die Validierungslogik geprüft und normalisiert.

**Zweck:**
- strukturierte Eingaben absichern
- Pflichtfelder prüfen
- Datentypen vereinheitlichen

## 7.3 Layout-Generierung
Für Dokumenttypen kann ein Formularlayout automatisch abgeleitet werden.

**Zweck:**
- UI nicht statisch bauen müssen
- Formulare dynamisch aus dem Modell erzeugen

## 7.4 Suche
Aktuell existieren zwei Ebenen:
- ältere Suche unter `/api/v1/search`
- neuere Service-Suche unter `/api/search`

Das sollte langfristig konsolidiert werden.

---

# 8. Bekannte fachliche und technische Hinweise

- Die API ist bereits gut nutzbar, aber in Teilen noch im Umbau.
- Besonders die Suche ist noch ein **Hybridzustand**.
- UI, API und Workflow-Bereich entwickeln sich parallel; Dokumentation sollte deshalb nach jedem Ausbau mitgezogen werden.
- Interne Endpunkte unter `/api/internal/index` sind eher für Technik/Betrieb gedacht als für normale Fachanwender.

---

# 9. Empfehlungen für spätere Erweiterung dieser Doku

Sinnvolle nächste Kapitel:
- Beispiel-Requests mit `curl`
- Beispiel-Responses pro Endpunkt
- Auth-/Rechtemodell, sobald vorhanden
- Fehlercodes pro Route
- Endpunkt-Matrix nach Fachbereich
- Changelog der API
- Unterschiede zwischen alter und neuer Suche
- Workflow-API, sobald sie als echte API und nicht nur als UI-Flow vorliegt

---

# 10. Änderungslog

### 2026-04-24
Erste erweiterbare API-Dokumentation angelegt mit:
- Struktur der API-Bereiche
- Beschreibung der wichtigsten Endpunkte
- Funktionsbeschreibung der einzelnen API-Bausteine
- Einordnung von Suche und interner Index-API
