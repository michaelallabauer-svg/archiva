# Archiva

Lightweight Enterprise Content Management with Full-Text Search.

## Features

- 📄 Document upload and storage
- 🔍 Full-text search powered by PostgreSQL tsvector/tsquery
- 📁 Version tracking
- 🏷️ Metadata and tagging
- ⚙️ Fully configurable via YAML
- 🐳 Docker-ready

## Stack

- **Backend**: Python 3.11+, FastAPI
- **Database**: PostgreSQL 16 with native full-text search
- **ORM**: SQLAlchemy 2.0
- **Migrations**: Alembic

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 16
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

API available at `http://localhost:8000`
Docs at `http://localhost:8000/docs`

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
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check |
| POST | `/api/v1/documents` | Upload a document |
| GET | `/api/v1/documents` | List documents |
| GET | `/api/v1/documents/{id}` | Get document by ID |
| DELETE | `/api/v1/documents/{id}` | Delete document |
| GET | `/api/v1/search?q=` | Full-text search |

## License

MIT
