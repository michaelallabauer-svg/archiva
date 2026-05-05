# Archiva

Lightweight Enterprise Content Management with Full-Text Search.

## Features

- 📄 Document upload and storage
- 🧩 Admin-managed document/object definitions: Cabinet Types, Cabinets, Registers, Document Types, Metadata Fields
- 🔐 Login-protected UI with local users, roles, password hashes and signed sessions
- 📝 App workspace for daily ECM work: browse structure, capture documents, edit object metadata values
- ✅ Server-side metadata validation driven by document type definitions
- 🔍 Full-text search with OpenSearch when available and PostgreSQL fallback indexing
- 🖼️ Preview/index queue worker started together with the app
- 📁 Version tracking
- 🔄 Workflow Designer, runtime tasks/history, invoice MVP seed and workflow inbox
- 🏷️ Metadata and tagging
- ⚙️ Configurable via YAML
- 🐳 Docker-ready for local OpenSearch

## Stack

- **Backend:** Python 3.11+, FastAPI
- **Database:** PostgreSQL 16
- **Search:** OpenSearch 2.x with PostgreSQL fallback
- **OCR / PDF extraction:** pypdf, Poppler (`pdftotext`), OCRmyPDF, Tesseract
- **ORM:** SQLAlchemy 2.0
- **Dependency manager:** `uv` recommended; `pip` also works

## Installation

These steps reflect the current working setup and the dependency issues we hit during development.

### 1. Prerequisites

Install the system tools first.

**macOS / Homebrew:**

```bash
brew install python@3.11 uv postgresql@16 poppler tesseract tesseract-lang ocrmypdf
```

Optional for local OpenSearch:

```bash
# Docker Desktop must be running
docker --version
```

Notes:

- Python **3.11+** is required. If several Python versions are installed, prefer `uv run ...` or an explicit Python binary.
- Do **not** rely on an old local `venv/` unless you just recreated it. We repeatedly saw `ModuleNotFoundError: uvicorn` when starting Archiva with the wrong interpreter/venv.
- `config.yaml` is local-only and ignored by git.

### 2. Clone

```bash
gh repo clone michaelallabauer-svg/archiva
cd archiva
```

If you do not use GitHub CLI:

```bash
git clone https://github.com/michaelallabauer-svg/archiva.git
cd archiva
```

### 3. Install Python dependencies

Recommended:

```bash
uv sync --dev
```

Then run all Archiva commands through `uv run`, e.g. `uv run python -m archiva.main`.

Alternative with plain `venv`/`pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

If `python -m archiva.main` fails with `No module named uvicorn`, you are using the wrong Python environment. Either activate the correct `.venv` or use `uv run python -m archiva.main`.

### 4. PostgreSQL

Start PostgreSQL and create the database/user expected by your config.

Simple local default:

```bash
createdb archiva
```

If PostgreSQL is managed by Homebrew:

```bash
brew services start postgresql@16
createdb archiva
```

Default connection settings are:

```yaml
database:
  host: "localhost"
  port: 5432
  name: "archiva"
  user: "postgres"
  password: "postgres"
```

Adjust `config.yaml` if your local PostgreSQL user/password differ.

### 5. Configure Archiva

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` if needed. On startup Archiva now loads `config.yaml` automatically from the repository directory when present. If it is absent, built-in defaults are used.

Important paths/ports:

- API: `http://localhost:8000/docs`
- Admin UI: `http://localhost:8000/ui/admin`
- App UI: `http://localhost:8000/ui/app`
- Login UI: `http://localhost:8000/ui/login`
- Workflow Designer: `http://localhost:8000/ui/workflow-designer`
- Workflow Inbox: `http://localhost:8000/ui/app/workflows/inbox`

### 6. Optional: OpenSearch

OpenSearch is optional for local development. If it is not reachable, Archiva still maintains the PostgreSQL fallback index so queue jobs do not stay permanently blocked.

```bash
docker compose up -d opensearch
```

Check it with:

```bash
curl http://localhost:9200
```

### 7. Start Archiva

Recommended:

```bash
uv run python -m archiva.main
```

Alternative after activating `.venv`:

```bash
python -m archiva.main
```

The app creates/updates required tables on startup, including compatibility columns such as:

- `documents.cabinet_id`
- `cabinets.metadata_json`
- `registers.metadata_json`
- indexing/status columns
- definition-model columns for cabinet/register/document type metadata

No separate Alembic command is required for the current local setup.

### 8. Restart after code changes

The main app on `:8000` may run without reload depending on `config.yaml` (`app.debug: false` by default). After changing server-rendered UI/code, restart the process:

```bash
# find the process
lsof -nP -iTCP:8000 -sTCP:LISTEN

# stop it, then start again
kill <PID>
uv run python -m archiva.main
```

If the browser still shows old UI, hard-refresh with `Cmd+Shift+R`.


## Login and Identity

The server-rendered UI under `/ui/*` is protected by a login guard.

- Login: `http://localhost:8000/ui/login`
- Logout: available as **Abmelden** in App/Admin/Workflow Designer
- Identity admin: `http://localhost:8000/ui/admin/identity`

Users are managed in **Admin → Identity & Rollen**. When creating a user you can set an **Initiales Passwort**; when editing a user you can set **Neues Passwort**. Passwords are stored as Argon2 hashes via `argon2-cffi`.

Local bootstrap rule: if no user has a password hash yet, an existing active user can log in with an empty password. As soon as a password is set, normal password verification is required. For non-local deployments set `ARCHIVA_SESSION_SECRET` so signed session cookies do not use the development fallback secret.

Auth-related dependencies:

- `argon2-cffi` — password hashing and verification using Argon2.
- Python stdlib `hmac`/`hashlib` — signed session cookie verification and legacy PBKDF2 hash compatibility.

Legacy note: older local PBKDF2 password hashes are still accepted for verification, but new/changed passwords are written as Argon2 hashes.

## Workflow Runtime and Inbox

Archiva now has both definition-time and runtime workflow support:

- Workflow definitions live in `/ui/workflow-designer`.
- Runtime instances can be started on documents.
- Each active instance keeps the current step, open task, assignment target and history events.
- The App hero links to `/ui/app/workflows/inbox` and shows how many workflows are active.
- Workflow actions record the logged-in user as actor.
- The invoice MVP setup creates the `Eingangsrechnung` workflow and roles such as `Rechnungsprüfung`, `Rechnungsfreigabe` and `Buchhaltung`.

## Common dependency problems and fixes

### `ModuleNotFoundError: No module named 'uvicorn'`

Cause: Archiva was started with a Python interpreter that does not have project dependencies installed.

Fix:

```bash
uv sync --dev
uv run python -m archiva.main
```

or recreate the venv:

```bash
rm -rf .venv
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m archiva.main
```

### OpenSearch not running

Archiva can still run. Start OpenSearch only if you need the OpenSearch-backed search path:

```bash
docker compose up -d opensearch
```

### OCR/PDF extraction missing tools

Install the system tools:

```bash
brew install poppler tesseract tesseract-lang ocrmypdf
```

Without these, text extraction may fall back or be incomplete for scanned PDFs/images.

### Port `8000` already in use

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
kill <PID>
uv run python -m archiva.main
```

## Configuration reference

See `config.example.yaml`:

```yaml
database:
  host: "localhost"
  port: 5432
  name: "archiva"
  user: "postgres"
  password: "postgres"

app:
  host: "0.0.0.0"
  port: 8000
  debug: false

storage:
  base_path: "./data/documents"

search:
  max_results: 100
  highlight_fragment_size: 150
  engine: "opensearch"
  opensearch_url: "http://localhost:9200"
  index_name: "archiva-documents-v1"
```

## Roadmap / Vision

See [`TODO-VISION.md`](TODO-VISION.md) for the MVP plan around the Eingangsrechnungs-Workflow, PDFStampede integration, and the post-MVP concept for reference metadata/stammdaten links.

## UI surfaces

- `/ui/admin` — structure, document types, and metadata model administration
- `/ui/app` — daily archive work: browse Cabinets/Register, capture documents, edit metadata values of the active object
- `/ui/workflows` — workflow designer/placeholder surface

Important distinction:

- Metadata **field definitions** are maintained in Admin.
- Metadata **values** for the selected Cabinet/Register are edited in the App workspace.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/cabinets` | List admin structure with nested registers/types |
| POST | `/api/v1/cabinets` | Create cabinet |
| POST | `/api/v1/registers` | Create register |
| POST | `/api/v1/document-types` | Create document/object type |
| GET | `/api/v1/document-types/{id}/layout` | Get generated form layout |
| GET | `/api/v1/document-types/{id}/capture` | Get capture definition for intake UI |
| POST | `/api/v1/documents` | Upload a document with optional `document_type_id` + JSON `metadata` |
| GET | `/api/v1/documents` | List documents |
| GET | `/api/v1/documents/{id}` | Get document by ID |
| DELETE | `/api/v1/documents/{id}` | Delete document |
| GET | `/api/v1/search?q=` | Full-text search |

## Combined capture flow

API upload to `/api/v1/documents` uses `multipart/form-data` with:

- `file`: the binary upload
- `document_type_id`: UUID of the admin-defined document/object type
- `metadata`: JSON object as string, e.g. `{"invoice_number":"2026-001","amount":129.9}`
- optional classic fields like `title`, `author`, `description`, `tags`

The built-in UI at `/ui/app` uses normal form fields for metadata and submits them as multipart fields alongside the file upload. The server maps these form values back into the validated metadata object before saving.

## License

MIT
