# Archiva Start

## Terminal-Befehl auf macOS

Im Projektordner starten:

```bash
cd /Users/michaelallabauer/.openclaw/workspace/archiva
./.venv/bin/python -m uvicorn archiva.main:app --reload --port 8000
```

Danach öffnen:

```bash
open http://localhost:8000/ui/app
```

## Falls Datenbankmigrationen nötig sind

```bash
cd /Users/michaelallabauer/.openclaw/workspace/archiva
./.venv/bin/python -m alembic upgrade head
```

Hinweis: `uvicorn` liegt hier in der lokalen virtuellen Umgebung (`.venv`). Deshalb funktioniert `uvicorn ...` ohne aktiviertes venv nicht direkt im Terminal.

## Login / lokaler Bootstrap

Alle `/ui/*`-Seiten sind inzwischen login-geschützt. Der Einstieg ist:

```bash
open http://localhost:8000/ui/login
```

Danach weiter zu:

```bash
open http://localhost:8000/ui/app
open http://localhost:8000/ui/admin
open http://localhost:8000/ui/workflow-designer
```

### Initiales Passwort setzen

1. Mit bestehendem aktivem Benutzer anmelden.
2. Falls noch **kein** Benutzer ein Passwort gesetzt hat, ist ein einmaliger Dev-Bootstrap mit leerem Passwort möglich.
3. Danach: `Admin → Identity & Rollen → Benutzer → Bearbeiten`.
4. Feld **Neues Passwort** füllen und **Benutzer aktualisieren**.
5. Ab dann ist Login nur noch mit Passwort möglich.

Die UI-Session läuft über ein signiertes Cookie (`archiva_session`). Für lokale Entwicklung nutzt Archiva einen Dev-Secret-Fallback. Für ernsthafte Umgebungen sollte `ARCHIVA_SESSION_SECRET` gesetzt werden.

Passwörter werden mit Argon2 über die Python-Abhängigkeit `argon2-cffi` gehasht. Nach einem frischen Clone/Update deshalb sicherstellen:

```bash
cd /Users/michaelallabauer/.openclaw/workspace/archiva
uv sync --dev
```

oder bei klassischem venv:

```bash
./.venv/bin/python -m pip install -e ".[dev]"
```

Ältere lokale PBKDF2-Hashes bleiben lesbar, neue/geänderte Passwörter werden als Argon2-Hash gespeichert.

## Neue Metadaten-Feldtypen für den MVP

Archiva unterstützt zusätzlich zu Text/Zahl/Datum/Auswahl zwei MVP-Feldtypen:

### `identity_reference` — Benutzer/Team-Auswahl

Dieses Feld zeigt eine Auswahl aktiver Benutzer und vorhandener Teams aus **Admin → Identity & Rollen**. Gespeichert wird eine strukturierte Referenz mit Typ, ID und Anzeige-Label, z. B.:

```json
{"kind":"team","id":"...","label":"Buchhaltung"}
```

Damit können Indexdaten wie **Zuständig**, **Prüfer**, **Freigabe durch** oder **Team** direkt auf Archiva-Stammdaten verweisen.

### `auto_id` — automatische IDs

Dieses Feld wird beim Speichern automatisch erzeugt, wenn noch kein Wert vorhanden ist. Das Muster wird beim Metadatenfeld im Feld **ID-Muster / Regex** hinterlegt.

Empfohlene Beschreibungssprache:

- normaler Text bleibt gleich, z. B. `ER-`
- `{YYYY}` oder `%JAHR%` = vierstelliges Jahr, z. B. `2026`
- `{YY}` oder `%JJ%` = zweistelliges Jahr, z. B. `26`
- `{MM}` oder `%MONAT%` = Monat, z. B. `05`
- `{DD}` oder `%TAG%` = Tag, z. B. `05`
- `#` definiert den Zähler und seine Stellenanzahl

Beispiele:

- `ER-{YYYY}-{####}` → `ER-2026-0001`
- `ER-%JAHR%-####` → `ER-2026-0001`
- `VERTRAG-{YY}-{###}` → `VERTRAG-26-001`

Der Zähler wird pro Dokumenttyp und Feld aus den bereits vorhandenen Dokument-Metadaten ermittelt und zählt hoch. Für das Eingangsrechnungs-MVP erzeugt der Seed jetzt zusätzlich `Interne ER-ID` mit `ER-{YYYY}-{####}` und `Zuständig` als Benutzer-/Team-Referenz.
