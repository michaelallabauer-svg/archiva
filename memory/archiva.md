# Archiva

Stand: 2026-04-19

## Projektziel
Archiva ist ein ECM mit getrennten Flächen für Admin, App, Workflows und Preview. Zielstruktur ist:

Cabinettyp → Cabinet → Register → Dokumenttyp → Dokument

Wichtige fachliche Regel:
- `ERB` ist ein Cabinettyp
- konkrete Jahresordner wie `2026` sind Cabinets darunter
- Register dürfen erst innerhalb eines konkreten Cabinets angelegt werden

## Lokaler Workspace
- Codebasis: `/Users/clawdia/.openclaw/workspace/archiva`
- Startskript: `/Users/clawdia/.openclaw/workspace/dev/start_archiva_dev.sh`
- Start:
  1. `cd /Users/clawdia/.openclaw/workspace`
  2. `source venv/bin/activate`
  3. `bash dev/start_archiva_dev.sh`

## Wichtige Dateien
- `archiva/ui.py` → servergerenderte UI, Admin/App/Tree/Quick-Create/Detailseiten
- `archiva/main.py` → App-Start
- `archiva/preview_worker.py` → Preview-Worker
- `archiva/models.py` → Datenmodell
- `archiva/database.py` → DB-Bootstrap
- `archiva/api_documents.py` → Dokument-API
- `CHAT-HANDOFF.md` → letzter stabiler Übergabestand

## Was zuletzt gemacht wurde

### 1. UI-Struktur und Kontextmenü
- Cabinettyp-Knoten im Baum wurden gefixt, damit sie als echte `cabinet_type`-Nodes funktionieren.
- Der generische Zwischenknoten `Bestand` wird in der UI ausgeblendet, wenn er nur als Bridge-Struktur existiert.
- Erste Ebene unter `Bestand` wird UI-seitig promoted, damit z. B. `ERB` als Cabinettyp behandelt wird.
- Kontextmenü bei `ERB` bietet jetzt `Cabinet anlegen` statt `Register anlegen`.
- Quick-Create im App-Bereich zeigt fachlich passende Formulare:
  - bei Cabinettyp nur Cabinet anlegen
  - bei Cabinet nur Register anlegen
- Für Cabinet-Anlage wird das Namensfeld automatisch mit dem aktuellen Jahr vorbelegt.

### 2. UX-Verbesserungen
- Seitenränder im UI wurden reduziert, damit die Breite besser genutzt wird.
- Das Kontextmenü wurde von einem kaputten Viewport-Overlay auf ein echtes Dropdown direkt am Baumknoten umgestellt.
- Menü soll wie ein Aufklappen unter den drei Punkten wirken.

### 3. Datenstruktur / Migration
- Es gibt jetzt eine Bestands-Migration in `archiva/ui.py`:
  - Route: `POST /ui/admin/migrate-bestand`
  - Zweck: alte Bridge-Struktur `Bestand -> ERB -> 2026` in das Zielmodell überführen
- Migrationslogik:
  - alte Cabinets unter `Bestand` werden zu Cabinettypen
  - alte Register darunter werden zu Cabinets
  - DocumentTypes/MetadataFields an Registern werden auf die neuen Cabinets umgehängt
  - direkte Cabinet-Artefakte werden bevorzugt an `2026` gehängt, sonst an das erste migrierte Cabinet

### 4. Direkte Cabinet-Zuordnung für Dokumente
- `Document` hat jetzt ein direktes Feld `cabinet_id`.
- `archiva/models.py` wurde entsprechend erweitert.
- `archiva/database.py` ergänzt die Spalte robust beim Start:
  - `ADD COLUMN IF NOT EXISTS`
  - FK nur wenn noch nicht vorhanden
  - Index nur wenn noch nicht vorhanden
- `archiva/api_documents.py` setzt bei neuen Uploads `cabinet_id` automatisch aus dem DocumentType/Register-Kontext.

### 5. Backfill für bestehende Dokumente
- Route: `POST /ui/admin/backfill-document-cabinets`
- Zweck: bestehende Dokumente per `document_type -> register/cabinet` auf `documents.cabinet_id` zurückfüllen
- Dieser Backfill lief ohne Fehler durch, aber mindestens eine Rechnung blieb fachlich noch falsch eingeordnet.

## Aktueller offener Punkt
Es gibt mindestens eine Rechnung, die im UX noch nicht unter `ERB -> 2026` hängt, obwohl sie dort fachlich hin soll.

Wahrscheinliche Ursache:
- Altlast im Bestand
- Dokumenttyp oder direkte Zuordnung war nicht sauber genug, um automatisch abgeleitet zu werden

## Nächster geplanter Schritt
Als Nächstes die **Verschieben-Funktionalität** bauen, zuerst pragmatisch im UX:
- auf der Dokumentdetailseite Cabinet anzeigen
- Cabinet manuell zuweisen/ändern
- später daraus eine echte Move-Funktion bzw. Drag-and-Drop weiterentwickeln

## Wichtige technische Erkenntnis
Eine saubere Move-Funktion war vorher nicht möglich, weil Dokumente nur indirekt über `document_type_id` hingen. Mit `documents.cabinet_id` ist die Grundlage dafür jetzt gelegt.
