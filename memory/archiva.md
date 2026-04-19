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

### Update 2026-04-19, später am Vormittag
- Auf der Dokumentdetailseite wurde ein pragmatischer Move-Flow begonnen.
- Wenn der direkte Cabinet-Kontext des Dokuments nicht sauber ableitbar ist, soll die Auswahl nicht mehr nur aus dem aktuellen Kontext kommen.
- Stattdessen wird jetzt robuster versucht, die Cabinets über den Cabinettyp des DocumentType/Register-Kontexts zu ermitteln.
- Falls auch das nicht reicht, dient die gesamte Cabinet-Liste als Fallback, damit ein Dokument nicht an einem zu engen Dropdown scheitert.
- Der Move-Flow wurde auf einen zentralen fachlichen Cabinettyp-Resolver umgebaut.
  - dieselbe Auflösung wird jetzt sowohl für GET (Dropdown-Anzeige) als auch für POST (Speichern) verwendet
  - zuerst wird der fachliche Cabinettyp des Dokuments sauber bestimmt
  - Quellen dafür sind in Reihenfolge: aktuelles Cabinet, DocumentType/Register-Kontext, directes DocumentType-Cabinet, danach direkte DocumentType-Zuordnungen an echten Cabinets
  - Sonderfall Altmodell: ein Cabinet direkt unter `Bestand` wie `ERB` wird nicht als echtes Cabinet interpretiert, sondern als Legacy-Repräsentation eines Cabinettyps
  - wenn ein Legacy-/Pseudo-Cabinet wie `ERB` gefunden wird, wird dessen Name als Cabinettyp interpretiert
  - angeboten werden danach nur echte Cabinets dieses ermittelten Cabinettyps
  - Einträge, deren Name dem Cabinettyp entspricht, werden als Bridge-/Schein-Knoten konsequent ausgeschlossen
  - falls die neue Struktur für einen Typ noch nicht vollständig existiert, kann der Move-Flow Ziel-Cabinets temporär aus der Legacy-Struktur `Bestand -> Typ -> Register` ableiten, damit Altdaten trotzdem verschoben werden können
  - beim Speichern wird eine Legacy-Auswahl wie `2026` über denselben Resolver auf das echte Cabinet `2026` unter dem fachlichen Cabinettyp gemappt, z. B. `ERB -> 2026`
  - numerische Jahres-Cabinets werden in der Sortierung bevorzugt sauber eingeordnet
  - ein globaler Heuristik-Fallback auf beliebige Cabinets wurde für diesen Flow entfernt, damit keine Cabinettypen mehr ins Ziel-Dropdown zurückrutschen

## Wichtige technische Erkenntnis
Eine saubere Move-Funktion war vorher nicht möglich, weil Dokumente nur indirekt über `document_type_id` hingen. Mit `documents.cabinet_id` ist die Grundlage dafür jetzt gelegt.

## Altbestand-Repair
- Es gibt jetzt zusätzlich einen vollständigen Repair-Pfad für den Altbestand:
  - Route: `POST /ui/admin/repair-bestand`
  - führt Bestands-Migration und danach Document-Cabinet-Backfill zusammen aus
  - migriert `Bestand -> Typ -> Register` nach `Cabinettyp -> Cabinet`
  - hängt betroffene Dokumente auf echte Cabinets um, nicht nur Dokumenttypen und Felder
  - ist dafür gedacht, den Legacy-Bestand einmal sauber zu reparieren statt den Move-Flow dauerhaft mit Sonderfällen zu belasten

## Neustart auf sauberem Modellstand
- Die Datenbank wurde vollständig geleert, um nicht weiter an der Legacy-Struktur herumzureparieren.
- Zielmodell bleibt jetzt sauber getrennt:
  - Admin = Definitionsebene
  - App = konkrete Instanzen
- Im Admin wurden `RegisterType` plus definitionsseitige Zuordnungen für `DocumentType` vorbereitet.
- Das Definitionsmodell im Admin ist klickbar, hat Kontextmenüs und ein rechtes Detailpanel.
- Nächster wichtiger Baustein wurde ergänzt: Metadatenfelder können jetzt auch auf Definitionsebene an
  - `CabinetType`
  - `RegisterType`
  - `DocumentType`
  hängen.
- Dafür wurden Modell + DB-Bootstrap erweitert um:
  - `metadata_fields.cabinet_type_id`
  - `metadata_fields.register_type_id`
- Im Admin-Detailbereich sind Metadaten jetzt direkt auf Cabinettyp-, Registertyp- und Dokumenttyp-Ebene sichtbar und anlegbar.
- Der Definitionsbaum im Admin ist klickbar, hat Kontextmenüs pro Typ und ein rechtes Detailpanel.
- Der mittlere Block `Objekte anlegen` wurde UX-seitig beruhigt:
  - wieder Button-Leiste statt Formularwand
  - immer nur eine Maske sichtbar
  - aber jetzt mit Kontextvorbelegung aus dem Definitionsmodell
  - Cabinettyp-Auswahl öffnet standardmäßig Registertyp und befüllt passende Definition-Felder
  - Registertyp-Auswahl öffnet standardmäßig Dokumenttyp
  - Dokumenttyp-Auswahl öffnet standardmäßig Metadatenfeld
- Metadatenfelder sollen nach Option A bearbeitet werden:
  - bestehende Werte bleiben roh in JSON erhalten
  - Änderungen an Typ, Länge oder Validierung verändern die Interpretation, nicht die gespeicherten Rohwerte
  - keine stille Konvertierung und kein Datenverlust beim Bearbeiten eines Feldes
- Für Metadatenfelder im Admin wurde ein Edit-Flow begonnen:
  - Update-Route für `POST /ui/admin/metadata-fields/{id}` ergänzt
  - mittlere Bearbeitungsmaske `admin-form-metadata-field-edit` ergänzt
  - Hinweistext in der Edit-Maske erklärt die Option-A-Policy (bestehende JSON-Werte bleiben erhalten)
- Metadatenfelder im rechten Detailbereich sind anklickbar und sollen in der Mitte die Bearbeitungsmaske für das gewählte Feld öffnen.
- Wichtiger Bug im Edit-Flow:
  - Beim Klick auf ein Metadatenfeld gingen zunächst `selected_definition_kind` und `selected_definition_id` verloren, dadurch wurden rechter Detailbereich und Mitte leer.
  - Fix: Links der Metadatenfelder müssen immer den aktuellen Definitionskontext plus `selected_metadata_field_id` enthalten.
- Zweiter wichtiger Bug im Edit-Flow:
  - `selected_metadata_field_id` kam zwar in der URL an, wurde aber von `GET /ui/admin` und `GET /ui/admin/document-types/{id}` zunächst nicht entgegengenommen bzw. nicht an `_render_admin_page(...)` weitergereicht.
  - Fix: Beide GET-Routen müssen `selected_metadata_field_id` akzeptieren und an `_render_admin_page(...)` übergeben.
- Aktueller Stand kurz vor Context-Reset:
  - Klick auf Metadatenfeld erhält jetzt den Definitionskontext in der URL.
  - Die GET-Routen wurden angepasst, damit `selected_metadata_field_id` im Render ankommt.
  - Bitte nach Wiederaufnahme direkt prüfen, ob die mittige Edit-Maske jetzt tatsächlich sichtbar wird; falls nicht, liegt der Restfehler wahrscheinlich in der JS-Initialisierung bzw. im `default_target`/`openAdminCreateSection(...)`-Pfad.
- Unruhige Admin-Elemente wurden entfernt oder reduziert:
  - `Zuletzt erfasste Dokumente` entfernt
  - `Bereinigung zuerst` entfernt
  - `Nächster Schritt` entfernt
  - `Admin-Zusammenfassung` ausgeblendet
- Definitionsbaum wurde visuell beruhigt:
  - kein künstlicher Root-Knoten mehr
  - keine Meta-Zeilen mehr direkt im Baum
  - Kontextmenüs für Cabinettyp und Registertyp enthalten jetzt auch `Metadatenfeld anlegen`

## Admin-UI: Klickbares Definitionsmodell (2026-04-19 Nachmittag)

### Ziel
Definitionsbaum auf der Admin-Seite klickbar machen, mit Auswahlzustand, Kontextmenüs und einem Detailpanel rechts.

### Implementierte Features

#### 1. Klickbarer Definitionsbaum
- Jeder Knoten (Cabinettyp, Registertyp, Dokumenttyp) ist anklickbar
- Klick navigiert mit GET-Parametern `selected_definition_kind` und `selected_definition_id`
- Aktiver Knoten wird visuell hervorgehoben (lila Glow statt blau)
- CSS-Klassen `.def-node`, `.def-node.active`, `.def-node.depth-*` für Hierarchie

#### 2. Auswahlzustand
- `ui_admin_home` und `ui_admin_document_type_detail` akzeptieren `selected_definition_kind` und `selected_definition_id` Query-Parameter
- Aktiver Knoten wird in lilafarbenem Stil dargestellt
- Grid-Layout der Admin-Seite wurde auf 3 Spalten erweitert: Definitionsbaum | Main | Detailpanel

#### 3. Kontextmenüs im Definitionsmodell
- Jeder Knoten hat ein ⋯-Menü (`.def-menu-btn`)
- Aktionen je nach Knotentyp:
  - **Cabinettyp**: Cabinet anlegen, Dokumenttyp anlegen, Löschen
  - **Registertyp**: Dokumenttyp anlegen, Löschen
  - **Dokumenttyp**: Metadatenfeld anlegen, Standardfelder Rechnung, Löschen
- Löschen-Aktionen sind als `danger: true` markiert und werden im Menü rot hervorgehoben
- Kontextmenü wird per JavaScript dynamisch am ⋯-Button positioniert

#### 4. Detailpanel rechts
- Zeigt Metadatenübersicht für den ausgewählten Knoten
- **Cabinettyp**: Name, Beschreibung, Reihenfolge, Anzahl Registertypen, Anzahl Dokumenttypen
- **Registertyp**: Name, Beschreibung, Cabinettyp, Anzahl Dokumenttypen
- **Dokumenttyp**: Name, Beschreibung, Icon, Pfad im Baum, Anzahl Felder, Metadatenfeld-Liste
- Metadatenfelder werden mit Name, Typ und Flags (Pflicht/Unique) angezeigt
- Jedes Feld hat einen Delete-Button (×)

#### 5. Neue Delete-Routen
- `POST /ui/admin/cabinet-types/{id}/delete`
- `POST /ui/admin/register-types/{id}/delete`
- `POST /ui/admin/document-types/{id}/delete`
- `POST /ui/admin/metadata-fields/{id}/delete` (redirectet auf übergeordneten Dokumenttyp)

#### 6. CSS und Layout
- Admin-Seite: Grid mit 3 Spalten (`300px 1fr 300px`), responsive auf `1300px` und `1100px`
- Neue CSS-Variablen: `--accent-3` (lila), `--glow-purple`, `--danger`, `--danger-bg`
- `.def-context-menu` als globales Float-Overlay außerhalb des Grid-Layouts im Body

### Geänderte Funktionen
- `_render_definition_structure()` – komplett neu geschrieben mit `def_node()` Helfer und `data-kind/data-id/data-actions` Attributen
- `_render_definition_detail()` – neue Funktion für das rechte Detailpanel
- `_render_admin_page()` – neue Parameter `selected_definition_kind`, `selected_definition_id`; Grid auf 3 Spalten; `definition_detail_html` im rechten Panel
- `_render_admin_script()` – umfangreich erweitert: Klick-Handler, Kontextmenü-Logik, Delete-Form-Submit
- `ui_admin_home()` – neue Query-Parameter
- `ui_admin_document_type_detail()` – neue Query-Parameter, navigiert mit `selected_definition_kind=document_type`
