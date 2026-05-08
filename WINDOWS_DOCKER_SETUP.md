# Archiva unter Windows mit Docker Compose

Empfohlener Windows-Betrieb: **Docker Desktop mit WSL2-Backend**. Damit laufen Archiva, PostgreSQL und OpenSearch reproduzierbar in Containern, ohne lokale Python/PostgreSQL/OCR-Installation unter Windows.

## Voraussetzungen

1. Windows 10/11 mit aktivem WSL2
2. Docker Desktop installiert und gestartet
3. Git installiert

Prüfen:

```powershell
docker --version
docker compose version
```

## Start

```powershell
git clone https://github.com/michaelallabauer-svg/archiva.git
cd archiva

# Optional, aber für nicht-lokale Nutzung dringend setzen:
$env:ARCHIVA_SESSION_SECRET = "bitte-einen-langen-zufaelligen-wert-setzen"

docker compose up -d --build
```

Danach öffnen:

- Login: <http://localhost:8000/ui/login>
- App: <http://localhost:8000/ui/app>
- Admin: <http://localhost:8000/ui/admin>
- API Docs: <http://localhost:8000/docs>

## Ersten Admin-Benutzer anlegen

Nach dem ersten Start einmal ausführen:

```powershell
docker compose exec `
  -e ARCHIVA_ADMIN_EMAIL="admin@archiva.local" `
  -e ARCHIVA_ADMIN_NAME="Archiva Admin" `
  -e ARCHIVA_ADMIN_PASSWORD="change-me" `
  app python scripts/bootstrap-admin.py
```

Dann mit dieser E-Mail und dem gesetzten Passwort einloggen. Das Passwort anschließend in der UI ändern.

## Dienste

Das Compose-Setup startet:

- `app` — Archiva/FastAPI auf Port `8000`
- `postgres` — PostgreSQL 16 auf Port `5432`
- `opensearch` — OpenSearch 2.x auf Port `9200`

Persistente Docker-Volumes:

- `archiva-documents` — hochgeladene Dokumente
- `postgres-data` — Datenbankdaten
- `opensearch-data` — Suchindexdaten

## Logs und Betrieb

```powershell
# Status
docker compose ps

# Logs
docker compose logs -f app

# Neustart nur der App
docker compose restart app

# Alles stoppen, Daten behalten
docker compose down

# Alles stoppen UND Daten löschen - Vorsicht!
docker compose down -v
```

## Konfiguration

Die Container-Konfiguration liegt in `config/docker.yaml` und wird read-only als `/app/config.yaml` in den App-Container gemountet.

Wichtige Werte:

- Datenbank: Host `postgres`, DB/User/Passwort `archiva`
- OpenSearch: `http://opensearch:9200`
- Dokumentenspeicher: `/app/data/documents`

Für ernsthaften Betrieb unbedingt setzen:

```powershell
$env:ARCHIVA_SESSION_SECRET = "langer-zufaelliger-secret"
```

Optional kann eine `.env`-Datei neben `docker-compose.yml` genutzt werden:

```env
ARCHIVA_SESSION_SECRET=langer-zufaelliger-secret
OPENSEARCH_INITIAL_ADMIN_PASSWORD=ArchivaLocalAdmin123!
```

Hinweis: `.env` ist per `.gitignore` ausgeschlossen.

## Updates

```powershell
git pull
docker compose up -d --build
```

Die App erstellt/aktualisiert die benötigten Tabellen beim Start. Daten bleiben in den Docker-Volumes erhalten.
