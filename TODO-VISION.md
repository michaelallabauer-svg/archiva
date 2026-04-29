# Archiva TODO / Vision

Stand: 2026-04-29

Ziel dieses Dokuments: aus dem aktuellen Prototypen einen fokussierten MVP ableiten. Der erste fachliche MVP soll ein sauberer **Eingangsrechnungs-Workflow** sein. Nach dem MVP folgt als wichtige Ausbaustufe ein konfigurierbares Referenz-/Stammdatenmodell für Metadaten.

---

## 1. MVP-Zielbild

Archiva soll für den MVP einen vollständigen, stabilen Prozess für Eingangsrechnungen abbilden:

1. Eingangsrechnung hochladen oder erfassen
2. Pflicht-Metadaten strukturiert erfassen
3. Duplikate früh erkennen
4. Rechnung automatisch oder manuell in den Workflow geben
5. PDF mit Eingangsstempel versehen, perspektivisch über PDFStampede
6. Rechnung prüfen, freigeben oder zurückweisen
7. Status, Historie und Metadaten nachvollziehbar anzeigen
8. Dokument später zuverlässig wiederfinden

Nicht-MVP: universelles ECM für alle Aktenarten. Der MVP darf bewusst eng auf Eingangsrechnungen optimiert sein.

---

## 2. Aktueller Stand als Basis

Bereits vorhanden oder begonnen:

- Server-rendered Admin UI und App UI
- Strukturmodell mit Cabinet Types, Cabinets, Registers, Document Types und Metadata Fields
- App-Strukturbaum
- Dynamischer Arbeitsbereich in der App
- Dokument-Upload mit dynamischen Metadatenfeldern
- Metadatenwerte für aktive Cabinets/Register im App-Arbeitsbereich
- Workflow Designer mit Schritten und Transitionen
- PostgreSQL-basierte Persistenz
- OpenSearch-Integration mit PostgreSQL-Fallback
- Preview-/Index-Queue Worker
- MD5-/Hash-basierte Duplikatprüfung in Ansätzen
- PDFStampede als separates Projekt mit PDF-Stempel-/Template-Funktionalität

---

## 3. MVP-Fokus: Eingangsrechnung

### 3.1 Empfohlene fachliche Struktur

Minimalstruktur für den MVP:

- Cabinet Type: `Eingangsrechnungsbuch`
- Cabinets: Geschäftsjahre, z. B. `2025`, `2026`
- Optional Register:
  - `Neu`
  - `In Prüfung`
  - `Freigegeben`
  - `Zurückgewiesen`
  - `Gebucht`

Alternative: Status nicht als Register modellieren, sondern rein über Workflow-Status. Empfehlung für MVP: **Workflow-Status ist führend**, Register nur verwenden, wenn es für die Navigation wirklich hilft.

### 3.2 Dokumenttyp: Rechnung

Pflichtfelder für den Dokumenttyp `Rechnung`:

- Rechnungsnummer
- Rechnungsdatum
- Lieferant / Kreditorname als Text für MVP
- Betrag brutto
- Währung
- Leistungszeitraum oder Leistungsdatum
- Fälligkeitsdatum
- Geschäftsjahr
- Eingangsdatum
- Prüfstatus / Workflowstatus

Optionale Felder:

- Betrag netto
- Umsatzsteuerbetrag
- Bestellnummer
- Kostenstelle
- Projekt
- Buchungskonto
- Kommentar
- Skonto-Informationen
- IBAN / Zahlungsreferenz, nur wenn benötigt und datenschutzrechtlich ok

Wichtig: Für den MVP Lieferant zunächst als Textfeld oder Auswahlfeld lösen. Die Stammdaten-Referenz kommt danach als eigene Ausbaustufe.

---

## 4. Eingangsrechnungs-Workflow MVP

### 4.1 Minimaler Workflow

Empfohlene Schritte:

1. **Erfasst**
   - Dokument hochgeladen
   - Metadaten wurden mindestens technisch validiert
   - Eingangsstempel wurde erzeugt oder ist geplant

2. **Sachliche Prüfung**
   - Prüfer kontrolliert Inhalt, Leistung, Betrag
   - Ergebnis: freigeben oder zurückweisen

3. **Freigabe**
   - Verantwortliche Person gibt Zahlung/Buchung frei
   - Ergebnis: freigegeben oder zurück zur Prüfung

4. **Buchhaltung / Verbuchung**
   - Rechnung wird final markiert
   - Optional Export/Weitergabe an Buchhaltungssystem später

5. **Abgeschlossen**
   - Rechnung ist final verarbeitet
   - Dokument bleibt unveränderlich bzw. Änderungen werden versioniert

### 4.2 Transitionen

- `Erfasst` → `Sachliche Prüfung`
- `Sachliche Prüfung` → `Freigabe`
- `Sachliche Prüfung` → `Zurückgewiesen`
- `Zurückgewiesen` → `Sachliche Prüfung`
- `Freigabe` → `Buchhaltung / Verbuchung`
- `Freigabe` → `Sachliche Prüfung`
- `Buchhaltung / Verbuchung` → `Abgeschlossen`

### 4.3 MVP-Funktionalität je Workflow-Schritt

Für den MVP braucht jeder Schritt:

- sichtbaren Status am Dokument
- zuständige Rolle/Person oder zumindest eine Platzhalter-Zuweisung
- Kommentar-/Notizfeld bei Transitionen
- Zeitstempel: wann in Schritt gekommen, wann verlassen
- einfache Historie am Dokument
- Aktion im UI, um den nächsten Workflow-Schritt auszulösen

Noch nicht zwingend für MVP:

- komplexe SLA-/Fristen-Logik
- E-Mail-Benachrichtigungen
- Eskalationen
- parallele Freigaben
- Vertreterregelungen

---

## 5. PDFStampede-Integration für Eingangsstempel

### 5.1 Ziel

Beim Eingang einer Rechnung soll Archiva PDFStampede verwenden können, um das PDF mit einem Eingangsstempel zu versehen.

Der Stempel sollte mindestens enthalten:

- Text: `Eingegangen`
- Eingangsdatum
- Archiva-Dokument-ID oder Kurz-ID
- optional Workflowstatus
- optional Benutzer/Quelle
- optional QR-Code oder Barcode für spätere Identifikation

### 5.2 MVP-Integrationsvariante

Empfehlung: HTTP-basierte Integration zwischen Archiva und PDFStampede.

Ablauf:

1. Rechnung wird in Archiva hochgeladen.
2. Archiva speichert Originaldatei unverändert.
3. Archiva ruft PDFStampede mit Template-ID und Stempeldaten auf.
4. PDFStampede liefert gestempeltes PDF zurück.
5. Archiva speichert gestempeltes PDF als:
   - neue Version des Dokuments, oder
   - zusätzliches Rendition/Artifact neben dem Original.

Empfehlung für MVP: **Original immer behalten**, gestempeltes PDF als Artifact/Rendition speichern. Das reduziert Risiko.

### 5.3 Benötigte Archiva-Konfiguration

Neue Konfigurationsoptionen:

```yaml
pdf_stampede:
  enabled: true
  base_url: "http://localhost:8001"
  default_template_id: "eingangsrechnung-standard"
  timeout_seconds: 30
  store_mode: "artifact" # artifact | version
```

### 5.4 Benötigte Archiva-Funktionen

- Client-Modul für PDFStampede
- Queue Job: `stamp_document`
- UI-Anzeige: Original / gestempelte Version öffnen
- Fehlerstatus, falls Stempelung fehlschlägt
- Retry-Möglichkeit im Admin/App-Kontext

### 5.5 Offene Entscheidung

Soll der Eingangsstempel sofort beim Upload erzeugt werden oder erst beim Übergang `Erfasst` → `Sachliche Prüfung`?

Empfehlung: Für MVP direkt beim Upload bzw. nach erfolgreicher Metadatenvalidierung erzeugen. Dann ist jedes eingegangene Dokument sofort eindeutig markiert.

---

## 6. MVP-Todo-Liste

### Priorität A — Muss für MVP

- [ ] Seed-/Setup-Funktion für Eingangsrechnungs-Struktur erstellen
  - [ ] Cabinet Type `Eingangsrechnungsbuch`
  - [ ] Cabinet pro Geschäftsjahr
  - [ ] Document Type `Rechnung`
  - [ ] Standard-Metadatenfelder
- [ ] Workflow-Vorlage `Eingangsrechnung` definieren
  - [ ] Schritte
  - [ ] Transitionen
  - [ ] Default-Zuweisungen/Rollen
- [ ] Dokumentdetail/App-Ansicht um Workflowstatus erweitern
- [ ] Workflow-Aktionen in der App ausführbar machen
- [ ] Workflow-Historie am Dokument anzeigen
- [ ] Upload/Capture so härten, dass Pflichtfelder, Duplikate und Fehler klar angezeigt werden
- [ ] PDFStampede-Client in Archiva ergänzen
- [ ] Eingangsstempel-Job implementieren
- [ ] Gestempeltes PDF als Artifact/Rendition speichern
- [ ] UI-Link: Original öffnen / gestempeltes PDF öffnen
- [ ] Smoke-Test-Dokumentation für kompletten Rechnungsdurchlauf

### Priorität B — Sollte für guten MVP

- [ ] Rollen/Zuweisungen für Prüfer/Freigeber vereinfachen
- [ ] Dashboard: offene Rechnungen je Status
- [ ] Filter nach Geschäftsjahr, Lieferant, Status, Fälligkeit
- [ ] Warnung bei fehlgeschlagener Stempelung
- [ ] Manuelle Retry-Aktion für Stempelung/Indexierung
- [ ] Audit-Log für Workflow-Transitionen
- [ ] Suche um typische Rechnungsfelder optimieren

### Priorität C — Nach MVP

- [ ] E-Mail-/Inbox-Import
- [ ] OCR-gestützte automatische Metadaten-Vorschläge
- [ ] Export an Buchhaltungssystem
- [ ] Mehrstufige Freigaben
- [ ] Fristen/Eskalationen
- [ ] Lieferanten-Stammdaten als Referenz-Metadaten
- [ ] Berechtigungen pro Cabinet/Workflow-Schritt

---

## 7. Konzept nach MVP: Referenz-Metadaten auf andere Strukturen

### 7.1 Zielbild

Metadatenfelder sollen nicht nur primitive Werte enthalten (`text`, `number`, `date`, `selection`), sondern auch auf Objekte in anderen Strukturen zeigen können.

Beispiel:

- Dokumenttyp: `Eingangsrechnung`
- Feld: `Lieferant`
- Feldtyp: `reference`
- Zielstruktur: Cabinet Type `Lieferanten`
- Auswahl: ein konkretes Lieferanten-Dokument oder Lieferanten-Objekt mit Stammdaten

Der Benutzer wählt also nicht freien Text, sondern einen bestehenden Lieferanten aus dem Lieferanten-Schrank.

### 7.2 Fachliches Beispiel

Struktur:

- Cabinet Type: `Lieferanten`
- Cabinet/Register optional nach Alphabet, Kategorie oder Status
- Document/Object Type: `Lieferantenstamm`

Lieferantenstamm-Metadaten:

- Lieferantennummer
- Name
- UID/VAT-ID
- Adresse
- IBAN
- Zahlungsbedingungen
- Standard-Kostenstelle
- Ansprechpartner
- Status aktiv/inaktiv

Eingangsrechnung:

- Feld `Lieferant` referenziert einen Lieferantenstamm
- Beim Auswählen können Stammdaten angezeigt werden
- Perspektivisch können Werte übernommen oder validiert werden

### 7.3 Datenmodell-Vorschlag

Neue Feldtypen:

- `reference` — genau ein Zielobjekt
- `multi_reference` — mehrere Zielobjekte

Erweiterung `metadata_fields`:

```text
field_type = reference | multi_reference
reference_target_kind = cabinet_type | document_type | saved_search
reference_target_id = UUID
reference_label_template = string
reference_filter_json = JSON
reference_display_fields_json = JSON
reference_required_status = optional
```

Alternative: separate Tabelle `metadata_field_references`, um das Kernmodell sauberer zu halten.

Empfehlung: Für langfristige Sauberkeit separate Tabelle.

```text
metadata_field_references
- id
- metadata_field_id
- target_kind
- target_id
- label_template
- filter_json
- display_fields_json
- allow_create_inline
- created_at
- updated_at
```

### 7.4 Speicherung der Werte

In `metadata_json` nicht nur Label speichern, sondern stabile Referenz:

```json
{
  "lieferant": {
    "id": "uuid-des-lieferantenobjekts",
    "kind": "document",
    "label": "Muster GmbH",
    "snapshot": {
      "lieferantennummer": "L-10042",
      "uid": "ATU..."
    }
  }
}
```

Warum Snapshot?

- Anzeige bleibt stabil, auch wenn sich Stammdaten später ändern.
- Trotzdem kann Archiva den aktuellen Datensatz über `id` nachladen.
- Audit und Nachvollziehbarkeit werden einfacher.

### 7.5 UI-Konzept

Für `reference`-Felder:

- Suchfeld mit Autocomplete
- Ergebnisliste mit konfigurierbaren Anzeigezeilen
- Detail-Peek beim Hover/Klick
- Button `Auswählen`
- Optional später: `Neuen Lieferanten anlegen`

Für `multi_reference`:

- Token-/Chip-Auswahl
- Mehrfachauswahl mit Suche

Konfigurierbar im Admin:

- Zielstruktur
- Suchfelder
- Anzeigefelder
- Filter, z. B. nur aktive Lieferanten
- Label-Template, z. B. `{lieferantennummer} · {name}`

### 7.6 API-Konzept

Neue Endpoints:

- `GET /api/v1/references/search?field_id=...&q=...`
- `GET /api/v1/references/{kind}/{id}`
- optional `POST /api/v1/references/resolve`

Response-Beispiel:

```json
{
  "items": [
    {
      "id": "...",
      "kind": "document",
      "label": "L-10042 · Muster GmbH",
      "metadata": {
        "lieferantennummer": "L-10042",
        "name": "Muster GmbH",
        "uid": "ATU..."
      }
    }
  ]
}
```

### 7.7 Validierung

Beim Speichern eines referenzierten Feldes:

- Ziel-ID muss existieren
- Ziel muss zum konfigurierten Target passen
- Filterbedingungen müssen weiterhin erfüllt sein, z. B. `status = aktiv`
- Optional: Warnung statt Fehler, wenn Stammdatensatz später inaktiv wurde

### 7.8 Such- und Indexierungsverhalten

Referenzfelder sollten indexiert werden über:

- Label
- Ziel-ID
- ausgewählte Snapshot-Felder
- optional komplette Ziel-Metadaten für Suche

Beispiel: Suche nach Lieferantennummer findet alle Rechnungen dieses Lieferanten.

### 7.9 Risiken / Entscheidungen

Offene Entscheidungen:

- Referenziert ein Feld ein Dokument, ein Objekt, ein Cabinet/Register oder alles?
- Gibt es künftig echte objektartige Datensätze ohne PDF-Datei?
- Werden Stammdatenänderungen automatisch in Rechnungsanzeigen aktualisiert oder bleibt der Snapshot führend?
- Darf man referenzierte Stammdaten inline anlegen?

Empfehlung:

1. Für die erste Ausbaustufe nur `Document/Object Type` als Ziel erlauben.
2. Snapshot speichern, aber aktuelle Daten bei Anzeige nachladen, wenn verfügbar.
3. Inline-Anlage erst später, nicht im ersten Referenz-MVP.

---

## 8. Empfohlene nächste konkrete Arbeitspakete

1. **Workflow Runtime fertigstellen**
   - Designer existiert, aber App muss Workflow-Schritte wirklich ausführen können.

2. **Eingangsrechnungs-Seed bauen**
   - Ein Klick oder Kommando erzeugt Struktur, Felder und Workflow.

3. **PDFStampede-Anbindung schneiden**
   - Minimaler HTTP-Client + Artifact-Speicherung.

4. **Dokumentdetail für Rechnungen verbessern**
   - Workflowstatus, Stempelstatus, relevante Rechnungsfelder prominent anzeigen.

5. **End-to-End Smoke Test dokumentieren**
   - Von Upload bis Abschluss mit gestempeltem PDF.

6. **Danach Referenzfelder spezifizieren und umsetzen**
   - Zuerst Lieferantenstamm als Pilot.

---

## 9. Definition of Done für den MVP

Der MVP ist erreicht, wenn folgender Ablauf ohne manuelle Datenbankeingriffe funktioniert:

1. Admin/Seed richtet Eingangsrechnungsbuch ein.
2. Benutzer lädt eine Rechnung hoch.
3. Benutzer erfasst Rechnungs-Metadaten.
4. Archiva erkennt offensichtliche Duplikate.
5. Archiva erzeugt einen Eingangsstempel über PDFStampede oder zeigt einen klaren Fehler mit Retry.
6. Benutzer startet/führt den Workflow durch.
7. Prüfen, Freigeben, Buchen/Abschließen sind im UI sichtbar und ausführbar.
8. Workflow-Historie ist am Dokument nachvollziehbar.
9. Original-PDF und gestempeltes PDF sind abrufbar.
10. Rechnung ist über Suche und Filter wieder auffindbar.
