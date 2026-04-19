"""Preview rendering queue and artifact helpers for Archiva."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from html import escape
from io import StringIO
from pathlib import Path

from sqlalchemy.orm import Session

from archiva.models import Document, PreviewArtifact, PreviewJob, PreviewJobStatus
from archiva.storage import StorageManager

TEXTLIKE_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/sql",
}
CSV_MIME_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
}
OFFICE_MIME_TYPES = {
    "application/msword": "Word-Dokument",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word-Dokument",
    "application/vnd.ms-excel": "Excel-Datei",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel-Datei",
    "application/vnd.ms-powerpoint": "PowerPoint-Datei",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint-Datei",
}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".txt", ".yaml", ".yml", ".toml", ".ini", ".sh", ".sql", ".xml", ".json", ".csv"}
DIRECT_MEDIA_TYPES = {"application/pdf"}


@dataclass
class PreviewPayload:
    content: bytes
    media_type: str
    kind: str
    page_count: int = 1


def enqueue_preview_job(db: Session, document: Document) -> PreviewJob:
    existing = (
        db.query(PreviewJob)
        .where(PreviewJob.document_id == document.id)
        .order_by(PreviewJob.created_at.desc())
        .first()
    )
    if existing and existing.status in {PreviewJobStatus.PENDING, PreviewJobStatus.PROCESSING}:
        return existing

    job = PreviewJob(document_id=document.id, status=PreviewJobStatus.PENDING)
    db.add(job)
    db.flush()
    return job


def get_latest_preview_job(db: Session, document_id) -> PreviewJob | None:
    return (
        db.query(PreviewJob)
        .where(PreviewJob.document_id == document_id)
        .order_by(PreviewJob.created_at.desc())
        .first()
    )


def get_latest_preview_artifact(db: Session, document_id) -> PreviewArtifact | None:
    return (
        db.query(PreviewArtifact)
        .where(PreviewArtifact.document_id == document_id)
        .order_by(PreviewArtifact.created_at.desc())
        .first()
    )


def process_pending_preview_jobs(db: Session, storage: StorageManager) -> int:
    jobs = (
        db.query(PreviewJob)
        .where(PreviewJob.status == PreviewJobStatus.PENDING)
        .order_by(PreviewJob.created_at.asc())
        .limit(10)
        .all()
    )
    processed = 0
    for job in jobs:
        document = db.query(Document).where(Document.id == job.document_id).first()
        if not document:
            job.status = PreviewJobStatus.FAILED
            job.error_message = "Document not found"
            job.finished_at = datetime.utcnow()
            processed += 1
            continue

        source_path = storage.full_path(Path(document.storage_path))
        if not source_path.exists():
            job.status = PreviewJobStatus.FAILED
            job.error_message = "Stored file not found"
            job.finished_at = datetime.utcnow()
            processed += 1
            continue

        try:
            job.status = PreviewJobStatus.PROCESSING
            job.started_at = datetime.utcnow()
            payload = render_preview_payload(source_path, document.mime_type)
            artifact_rel_path = _artifact_relative_path(document.id, payload.kind)
            artifact_full_path = storage.full_path(artifact_rel_path)
            artifact_full_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_full_path.write_bytes(payload.content)

            artifact = get_latest_preview_artifact(db, document.id)
            if artifact is None:
                artifact = PreviewArtifact(document_id=document.id)
                db.add(artifact)

            artifact.kind = payload.kind
            artifact.mime_type = payload.media_type
            artifact.storage_path = str(artifact_rel_path)
            artifact.status = "ready"
            artifact.page_count = payload.page_count
            artifact.updated_at = datetime.utcnow()
            artifact.created_at = artifact.created_at or datetime.utcnow()

            job.status = PreviewJobStatus.COMPLETED
            job.finished_at = datetime.utcnow()
            job.error_message = None
            processed += 1
        except Exception as exc:
            job.status = PreviewJobStatus.FAILED
            job.error_message = str(exc)
            job.finished_at = datetime.utcnow()
            processed += 1

    return processed


def render_preview_payload(path: Path, mime_type: str | None) -> PreviewPayload:
    detected_mime = (mime_type or "application/octet-stream").lower()
    suffix = path.suffix.lower()

    if detected_mime in DIRECT_MEDIA_TYPES or detected_mime.startswith("image/"):
        kind = "pdf" if detected_mime == "application/pdf" else "image"
        return PreviewPayload(content=path.read_bytes(), media_type=detected_mime, kind=kind)

    if detected_mime in CSV_MIME_TYPES or suffix == ".csv":
        return PreviewPayload(content=_render_csv_preview(path), media_type="text/html; charset=utf-8", kind="html")

    if detected_mime.startswith("text/") or detected_mime in TEXTLIKE_MIME_TYPES or suffix in CODE_EXTENSIONS:
        return PreviewPayload(content=_render_text_preview(path, detected_mime), media_type="text/html; charset=utf-8", kind="html")

    if detected_mime in OFFICE_MIME_TYPES:
        return PreviewPayload(content=_render_office_preview(path, detected_mime), media_type="text/html; charset=utf-8", kind="html")

    return PreviewPayload(content=_render_generic_preview(path, detected_mime), media_type="text/html; charset=utf-8", kind="html")


def _artifact_relative_path(document_id, kind: str) -> Path:
    suffix = ".html" if kind == "html" else (".pdf" if kind == "pdf" else ".bin")
    return Path("previews") / str(document_id) / f"preview{suffix}"


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _preview_shell(title: str, body: str, subtitle: str | None = None) -> bytes:
    subtitle_html = f'<p class="subtitle">{escape(subtitle)}</p>' if subtitle else ""
    html = f"""
<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background:#0b1020; color:#eef2ff; }}
    .page {{ padding:20px; }}
    .card {{ border-radius:18px; background:#121933; border:1px solid #2d3b69; overflow:hidden; }}
    .header {{ padding:18px 20px; border-bottom:1px solid rgba(255,255,255,.06); background:#11182f; }}
    .header h1 {{ margin:0; font-size:1.05rem; }}
    .subtitle {{ margin:8px 0 0; color:#a8b2d1; }}
    .body {{ padding:20px; }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; font: 0.92rem/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    table {{ width:100%; border-collapse:collapse; font-size:.92rem; }}
    th, td {{ border:1px solid rgba(255,255,255,.08); padding:10px; text-align:left; vertical-align:top; }}
    th {{ background:#0f1630; }}
    .meta-list {{ display:grid; gap:12px; }}
    .meta-row {{ display:grid; grid-template-columns:180px 1fr; gap:12px; padding:10px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .meta-key {{ color:#a8b2d1; font-weight:600; }}
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"card\">
      <div class=\"header\"><h1>{escape(title)}</h1>{subtitle_html}</div>
      <div class=\"body\">{body}</div>
    </div>
  </div>
</body>
</html>
"""
    return html.encode("utf-8")


def _render_text_preview(path: Path, mime_type: str) -> bytes:
    content = _read_text_file(path)
    if path.suffix.lower() == ".json" or mime_type == "application/json":
        try:
            parsed = json.loads(content)
            content = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    body = f"<pre>{escape(content[:120000])}</pre>"
    return _preview_shell(path.name, body, f"Textbasierte Vorschau, MIME-Type: {mime_type}")


def _render_csv_preview(path: Path) -> bytes:
    content = _read_text_file(path)
    sample = content[:120000]
    reader = csv.reader(StringIO(sample))
    rows = []
    for idx, row in enumerate(reader):
        rows.append(row)
        if idx >= 49:
            break
    if not rows:
        return _preview_shell(path.name, '<p>CSV-Datei ist leer.</p>', 'Tabellarische Vorschau')
    header = rows[0]
    body_rows = rows[1:] if len(rows) > 1 else []
    table = '<table><thead><tr>' + ''.join(f'<th>{escape(col)}</th>' for col in header) + '</tr></thead><tbody>'
    for row in body_rows:
        padded = row + [''] * max(0, len(header) - len(row))
        table += '<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in padded[:len(header)]) + '</tr>'
    table += '</tbody></table>'
    return _preview_shell(path.name, table, 'CSV-Vorschau, erste Zeilen')


def _render_office_preview(path: Path, mime_type: str) -> bytes:
    label = OFFICE_MIME_TYPES.get(mime_type, 'Office-Datei')
    body = f"""
<div class=\"meta-list\">
  <div class=\"meta-row\"><div class=\"meta-key\">Erkanntes Format</div><div>{escape(label)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Dateiname</div><div>{escape(path.name)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Status</div><div>Queue-basierte Vorschau vorbereitet, echte Konvertierung noch nicht angeschlossen.</div></div>
</div>
"""
    return _preview_shell(path.name, body, f"{label} Vorschau")


def _render_generic_preview(path: Path, mime_type: str) -> bytes:
    stat = path.stat()
    body = f"""
<div class=\"meta-list\">
  <div class=\"meta-row\"><div class=\"meta-key\">Dateiname</div><div>{escape(path.name)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">MIME-Type</div><div>{escape(mime_type)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Größe</div><div>{escape(str(stat.st_size))} Bytes</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Status</div><div>Kein spezialisierter Renderer hinterlegt, deshalb generische Vorschaukarte.</div></div>
</div>
"""
    return _preview_shell(path.name, body, "Generische Vorschau")
