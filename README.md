# Archiva

Lightweight Enterprise Content Management with Full-Text Search.

## Features

- 📄 Document upload and storage
- 🧩 Admin-managed document/object definitions (Cabinets, Registers, Document Types, Metadata Fields)
- 📝 Dynamic capture flow: upload document + structured metadata in one step
- ✅ Server-side metadata validation driven by document type definitions
- 🔍 Full-text search powered by PostgreSQL tsvector/tsquery
- 📁 Version tracking
- 🏷️ Metadata and tagging
- ⚙️ Fully configurable via YAML
- 🐳 Docker-ready

## Stack

- **Backend**: Python 3.11+, FastAPI
- **Database**: PostgreSQL 16 with native full-text search
- **Search**: OpenSearch 2.x with PostgreSQL fallback
- **OCR / PDF extraction**: pypdf, Poppler (`pdftotext`), OCRmyPDF, Tesseract
- **ORM**: SQLAlchemy 2.0
- **Migrations**: Alembic

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 16
- Docker Desktop, if OpenSearch should run locally via `docker compose`
- Optional but recommended OCR/PDF tooling:
  - macOS: `brew install poppler tesseract tesseract-lang ocrmypdf`
- GitHub CLI (for cloning/contributing)

### Setup

```bash
# Clone the repository
gh repo clone michaelallabauer-svg/archiva
cd archiva

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Optional but recommended: start OpenSearch for full search indexing
docker compose up -d opensearch

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your database settings

# Create database
createdb archiva

# Run migrations / create tables
python -m archiva.database init

# Start server
python -m archiva.main
```

The app starts an internal queue worker for preview rendering and indexing. OpenSearch is used when available; if it is not reachable, Archiva still updates the PostgreSQL fallback index so local development does not get stuck with pending jobs.

API available at `http://localhost:8000`
Docs at `http://localhost:8000/docs`
UI at `http://localhost:8000/ui`

## Configuration

See `config.example.yaml` for all available options:

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

## OCR / PDF extraction

Archiva extracts text for indexing in this order:

1. PDF text layer via `pypdf`
2. PDF text via Poppler `pdftotext`
3. Scan/OCR fallback via `ocrmypdf`
4. Image OCR via `tesseract`

On macOS install the system tools with:

```bash
brew install poppler tesseract tesseract-lang ocrmypdf
```

`pypdf` is pinned in `pyproject.toml` as a Python dependency. See `archiva/OCR_SETUP.md` for more detail.

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

### First built-in UI

There is now a first server-rendered UI at `/ui` with separated surfaces:

- `/ui/admin` for structure, document types, and metadata model administration
- `/ui/app` for document intake and daily ECM usage
- `/ui/workflows` as placeholder for future object workflow handling

The app intake UI renders dynamic metadata fields directly as form controls instead of a raw JSON textarea.

### Combined capture flow

API upload to `/api/v1/documents` still uses `multipart/form-data` with:

- `file`: the binary upload
- `document_type_id`: UUID of the admin-defined document/object type
- `metadata`: JSON object as string, e.g. `{"invoice_number":"2026-001","amount":129.9}`
- optional classic fields like `title`, `author`, `description`, `tags`

The built-in UI at `/ui/app` now uses normal form fields for metadata and submits them as multipart form fields alongside the file upload. The server maps these form values back into the validated metadata object before saving.

The backend validates the metadata against the configured `MetadataField` definitions before saving.

## License

MIT
