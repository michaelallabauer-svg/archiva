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
