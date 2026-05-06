#!/usr/bin/env bash
set -euo pipefail

# Install a fresh, empty local Archiva system.
# Safe default: refuses to overwrite an existing database unless --reset-db is passed.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_NAME="${ARCHIVA_DB_NAME:-archiva}"
DB_USER="${ARCHIVA_DB_USER:-postgres}"
DB_PASSWORD="${ARCHIVA_DB_PASSWORD:-postgres}"
DB_HOST="${ARCHIVA_DB_HOST:-localhost}"
DB_PORT="${ARCHIVA_DB_PORT:-5432}"
APP_HOST="${ARCHIVA_APP_HOST:-0.0.0.0}"
APP_PORT="${ARCHIVA_APP_PORT:-8000}"
STORAGE_PATH="${ARCHIVA_STORAGE_PATH:-./data/documents}"
PDF_STAMPEDE_ENABLED="${ARCHIVA_PDF_STAMPEDE_ENABLED:-true}"
PDF_STAMPEDE_BASE_URL="${ARCHIVA_PDF_STAMPEDE_BASE_URL:-http://localhost:8001}"
PDF_STAMPEDE_DEFAULT_TEMPLATE_ID="${ARCHIVA_PDF_STAMPEDE_DEFAULT_TEMPLATE_ID:-}"
ADMIN_EMAIL="${ARCHIVA_ADMIN_EMAIL:-admin@archiva.local}"
ADMIN_NAME="${ARCHIVA_ADMIN_NAME:-Archiva Admin}"
ADMIN_PASSWORD="${ARCHIVA_ADMIN_PASSWORD:-}"
RESET_DB=false
FORCE_CONFIG=false
WITH_OPENSEARCH=false
START_APP=false

usage() {
  cat <<USAGE
Usage: scripts/install-empty-system.sh [options]

Creates a fresh local Archiva installation with an empty database and one admin user.

Options:
  --reset-db                 Drop and recreate the configured database if it already exists.
  --force-config            Replace config.yaml; the old file is backed up with a timestamp.
  --with-opensearch         Start the bundled OpenSearch container via docker compose.
  --start                   Start Archiva after installation.
  --db-name NAME            Database name. Default: ${DB_NAME}
  --db-user USER            Database user. Default: ${DB_USER}
  --db-password PASSWORD    Database password written to config.yaml. Default: ${DB_PASSWORD}
  --db-host HOST            Database host. Default: ${DB_HOST}
  --db-port PORT            Database port. Default: ${DB_PORT}
  --admin-email EMAIL       Initial admin email. Default: ${ADMIN_EMAIL}
  --admin-name NAME         Initial admin display name. Default: ${ADMIN_NAME}
  --admin-password PASSWORD Initial admin password. If omitted, first login uses an empty password.
  --pdfstampede-url URL     PDFStampede base URL. Default: ${PDF_STAMPEDE_BASE_URL}
  --no-pdfstampede          Disable PDFStampede in config.yaml.
  -h, --help                Show this help.

Environment variables with the ARCHIVA_* names shown in the script can also be used.

Examples:
  scripts/install-empty-system.sh --reset-db --admin-password 'change-me'
  scripts/install-empty-system.sh --reset-db --with-opensearch --start
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset-db) RESET_DB=true ;;
    --force-config) FORCE_CONFIG=true ;;
    --with-opensearch) WITH_OPENSEARCH=true ;;
    --start) START_APP=true ;;
    --db-name) DB_NAME="${2:?Missing value for --db-name}"; shift ;;
    --db-user) DB_USER="${2:?Missing value for --db-user}"; shift ;;
    --db-password) DB_PASSWORD="${2:?Missing value for --db-password}"; shift ;;
    --db-host) DB_HOST="${2:?Missing value for --db-host}"; shift ;;
    --db-port) DB_PORT="${2:?Missing value for --db-port}"; shift ;;
    --admin-email) ADMIN_EMAIL="${2:?Missing value for --admin-email}"; shift ;;
    --admin-name) ADMIN_NAME="${2:?Missing value for --admin-name}"; shift ;;
    --admin-password) ADMIN_PASSWORD="${2:?Missing value for --admin-password}"; shift ;;
    --pdfstampede-url) PDF_STAMPEDE_BASE_URL="${2:?Missing value for --pdfstampede-url}"; shift ;;
    --no-pdfstampede) PDF_STAMPEDE_ENABLED=false ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    echo "Install prerequisites first. On macOS: brew install uv postgresql@16 poppler tesseract ocrmypdf" >&2
    exit 1
  fi
}

log() { printf '\n==> %s\n' "$*"; }

need_cmd uv
need_cmd psql
need_cmd createdb
need_cmd dropdb

export PGHOST="$DB_HOST"
export PGPORT="$DB_PORT"
export PGUSER="$DB_USER"
export PGPASSWORD="$DB_PASSWORD"

log "Installing Python dependencies"
uv sync --dev

log "Writing config.yaml"
if [[ -f config.yaml ]]; then
  if [[ "$FORCE_CONFIG" == true || "$RESET_DB" == true ]]; then
    backup="config.yaml.bak.$(date +%Y%m%d%H%M%S)"
    cp config.yaml "$backup"
    echo "Backed up existing config.yaml to $backup"
  else
    echo "config.yaml already exists. Re-run with --force-config to replace it." >&2
    exit 1
  fi
fi
cat > config.yaml <<YAML
database:
  host: "${DB_HOST}"
  port: ${DB_PORT}
  name: "${DB_NAME}"
  user: "${DB_USER}"
  password: "${DB_PASSWORD}"

app:
  host: "${APP_HOST}"
  port: ${APP_PORT}
  debug: false
  md5_duplicate_check: true

storage:
  base_path: "${STORAGE_PATH}"

search:
  max_results: 100
  highlight_fragment_size: 150
  engine: "opensearch"
  opensearch_url: "http://localhost:9200"
  index_name: "archiva-documents-v1"

pdf_stampede:
  enabled: ${PDF_STAMPEDE_ENABLED}
  base_url: "${PDF_STAMPEDE_BASE_URL}"
  default_template_id: "${PDF_STAMPEDE_DEFAULT_TEMPLATE_ID}"
  timeout_seconds: 30
  store_mode: "artifact"
YAML

log "Preparing PostgreSQL database ${DB_NAME}"
if psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  if [[ "$RESET_DB" != true ]]; then
    echo "Database '${DB_NAME}' already exists. Re-run with --reset-db to create an empty system." >&2
    exit 1
  fi
  psql -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" >/dev/null
  dropdb --if-exists "$DB_NAME"
fi
createdb "$DB_NAME"

if [[ "$WITH_OPENSEARCH" == true ]]; then
  need_cmd docker
  log "Starting OpenSearch"
  docker compose up -d opensearch
fi

log "Creating schema and stamping Alembic head"
uv run python - <<'PY'
from archiva.config import load_settings
from archiva.database import create_tables, init_db
settings = load_settings('config.yaml')
init_db(settings)
create_tables()
PY
uv run alembic stamp head

log "Creating initial admin user"
ARCHIVA_BOOTSTRAP_ADMIN_EMAIL="$ADMIN_EMAIL" \
ARCHIVA_BOOTSTRAP_ADMIN_NAME="$ADMIN_NAME" \
ARCHIVA_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
uv run python - <<'PY'
import os
import uuid
from argon2 import PasswordHasher
from sqlalchemy import text
from archiva.config import load_settings
from archiva.database import get_session, init_db
from archiva.models import Role, User, UserRoleAssignment

settings = load_settings('config.yaml')
init_db(settings)
email = os.environ['ARCHIVA_BOOTSTRAP_ADMIN_EMAIL'].strip().lower()
name = os.environ['ARCHIVA_BOOTSTRAP_ADMIN_NAME'].strip() or email
password = os.environ.get('ARCHIVA_BOOTSTRAP_ADMIN_PASSWORD', '')
password_hash = PasswordHasher().hash(password) if password else None

with get_session() as db:
    admin_role = db.query(Role).where(Role.name == 'Admin').first()
    if admin_role is None:
        admin_role = Role(name='Admin', description='Voller Zugriff auf Administration und Systemkonfiguration', is_system=True, permissions_json='["admin:*", "app:*", "workflow:*", "identity:*"]')
        db.add(admin_role)
        db.flush()

    user = db.query(User).where(User.email == email).first()
    if user is None:
        user = User(email=email, display_name=name, auth_source='local', status='active', password_hash=password_hash)
        db.add(user)
        db.flush()
    else:
        user.display_name = name
        user.status = 'active'
        user.password_hash = password_hash

    exists = db.query(UserRoleAssignment).where(UserRoleAssignment.user_id == user.id, UserRoleAssignment.role_id == admin_role.id).first()
    if exists is None:
        db.add(UserRoleAssignment(user_id=user.id, role_id=admin_role.id))

    db.execute(text('''
        INSERT INTO assignment_targets (id, target_type, user_id, label, description, created_at, updated_at)
        VALUES (:id, 'user', :user_id, :label, :description, NOW(), NOW())
        ON CONFLICT DO NOTHING
    '''), {'id': uuid.uuid4(), 'user_id': user.id, 'label': user.display_name, 'description': user.email})
    db.commit()

print(f"Initial admin user: {email}" + (" (password set)" if password else " (empty password for first bootstrap login)"))
PY

log "Installation complete"
echo "Open:  http://localhost:${APP_PORT}/ui/login"
echo "Login: ${ADMIN_EMAIL}"
if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "Password: empty (set one immediately in Admin → Identity & Rollen)"
else
  echo "Password: the value passed via --admin-password / ARCHIVA_ADMIN_PASSWORD"
fi

echo "Start later with: uv run python -m archiva.main"

if [[ "$START_APP" == true ]]; then
  log "Starting Archiva"
  exec uv run python -m archiva.main
fi
