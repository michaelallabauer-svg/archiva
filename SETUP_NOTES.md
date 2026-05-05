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
