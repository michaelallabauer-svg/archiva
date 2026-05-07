"""Preview rendering queue and artifact helpers for Archiva."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from html import escape
from io import StringIO
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from archiva.models import Document, PreviewArtifact, PreviewJob, PreviewJobStatus
from archiva.email_utils import parse_eml, render_email_preview_html
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
OFFICE_EXTENSIONS = {
    ".doc": "Word-Dokument",
    ".docx": "Word-Dokument",
    ".xlsx": "Excel-Datei",
    ".xls": "Excel-Datei",
    ".pptx": "PowerPoint-Datei",
    ".ppt": "PowerPoint-Datei",
}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".txt", ".yaml", ".yml", ".toml", ".ini", ".sh", ".sql", ".xml", ".json", ".csv"}
DIRECT_MEDIA_TYPES = {"application/pdf"}
EMAIL_MIME_TYPES = {"message/rfc822", "application/eml", "application/vnd.ms-outlook"}


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

    if detected_mime in EMAIL_MIME_TYPES or suffix == ".eml":
        parsed = parse_eml(path)
        return PreviewPayload(content=render_email_preview_html(parsed, path.name), media_type="text/html; charset=utf-8", kind="html")

    if detected_mime in DIRECT_MEDIA_TYPES or detected_mime.startswith("image/"):
        kind = "pdf" if detected_mime == "application/pdf" else "image"
        return PreviewPayload(content=path.read_bytes(), media_type=detected_mime, kind=kind)

    if detected_mime in CSV_MIME_TYPES or suffix == ".csv":
        return PreviewPayload(content=_render_csv_preview(path), media_type="text/html; charset=utf-8", kind="html")

    if detected_mime.startswith("text/") or detected_mime in TEXTLIKE_MIME_TYPES or suffix in CODE_EXTENSIONS:
        return PreviewPayload(content=_render_text_preview(path, detected_mime), media_type="text/html; charset=utf-8", kind="html")

    if detected_mime in OFFICE_MIME_TYPES or suffix in OFFICE_EXTENSIONS:
        office_pdf = _convert_office_to_pdf(path)
        if office_pdf:
            return PreviewPayload(content=office_pdf, media_type="application/pdf", kind="pdf")
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
    label = OFFICE_MIME_TYPES.get(mime_type) or OFFICE_EXTENSIONS.get(path.suffix.lower(), "Office-Datei")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        body = _render_docx_html(path)
        subtitle = "Word/Office365 Fallback-Vorschau aus OOXML-Inhalt"
    elif suffix == ".xlsx":
        body = _render_xlsx_html(path)
        subtitle = "Excel/Office365 Fallback-Vorschau aus OOXML-Inhalt"
    elif suffix == ".pptx":
        body = _render_pptx_html(path)
        subtitle = "PowerPoint/Office365 Fallback-Vorschau aus OOXML-Inhalt"
    else:
        body = f"""
<div class=\"meta-list\">
  <div class=\"meta-row\"><div class=\"meta-key\">Erkanntes Format</div><div>{escape(label)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Dateiname</div><div>{escape(path.name)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Status</div><div>Für dieses ältere Binärformat ist LibreOffice/soffice nötig. Wenn es installiert ist, rendert Archiva automatisch als PDF.</div></div>
</div>
"""
        subtitle = f"{label} Vorschau"
    return _preview_shell(path.name, body, subtitle)


def _convert_office_to_pdf(path: Path) -> bytes | None:
    """Render Office docs through LibreOffice when available."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix="archiva-office-preview-") as tmpdir:
        tmp_path = Path(tmpdir)
        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmp_path),
                    str(path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        pdf_path = tmp_path / f"{path.stem}.pdf"
        if not pdf_path.exists():
            candidates = list(tmp_path.glob("*.pdf"))
            pdf_path = candidates[0] if candidates else pdf_path
        return pdf_path.read_bytes() if pdf_path.exists() else None


def _render_docx_html(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
        paragraphs = []
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for paragraph in root.findall(".//w:p", ns):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
            if text:
                paragraphs.append(f"<p>{escape(text)}</p>")
        return "".join(paragraphs[:300]) or "<p>Keine Textinhalte gefunden.</p>"
    except Exception as exc:
        return _office_fallback_error(path, "DOCX", exc)


def _render_xlsx_html(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            sheet_names = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
            if not sheet_names:
                return "<p>Keine Tabellenblätter gefunden.</p>"
            sections = []
            for sheet_index, sheet_name in enumerate(sheet_names[:5], start=1):
                rows = _xlsx_rows(archive.read(sheet_name), shared_strings)
                table = f"<h2>Tabelle {sheet_index}</h2><table><tbody>"
                for row in rows[:50]:
                    table += "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row[:20]) + "</tr>"
                table += "</tbody></table>"
                sections.append(table)
            return "".join(sections)
    except Exception as exc:
        return _office_fallback_error(path, "XLSX", exc)


def _render_pptx_html(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            sections = []
            for slide_index, slide_name in enumerate(slide_names[:50], start=1):
                root = ET.fromstring(archive.read(slide_name))
                texts = [node.text or "" for node in root.findall(".//a:t", ns) if node.text]
                content = "<br>".join(escape(text) for text in texts) or "Keine Textinhalte gefunden."
                sections.append(f"<h2>Folie {slide_index}</h2><p>{content}</p>")
            return "".join(sections) or "<p>Keine Folien gefunden.</p>"
    except Exception as exc:
        return _office_fallback_error(path, "PPTX", exc)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for item in root.findall("main:si", ns):
        strings.append("".join(node.text or "" for node in item.findall(".//main:t", ns)))
    return strings


def _xlsx_rows(sheet_xml: bytes, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(sheet_xml)
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//main:row", ns):
        values = []
        for cell in row.findall("main:c", ns):
            value_node = cell.find("main:v", ns)
            inline_node = cell.find("main:is/main:t", ns)
            if inline_node is not None and inline_node.text is not None:
                value = inline_node.text
            elif value_node is None or value_node.text is None:
                value = ""
            elif cell.get("t") == "s":
                index = int(value_node.text)
                value = shared_strings[index] if 0 <= index < len(shared_strings) else ""
            else:
                value = value_node.text
            values.append(value)
        if any(values):
            rows.append(values)
    return rows


def _office_fallback_error(path: Path, format_label: str, exc: Exception) -> str:
    return f"""
<div class=\"meta-list\">
  <div class=\"meta-row\"><div class=\"meta-key\">Dateiname</div><div>{escape(path.name)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Format</div><div>{escape(format_label)}</div></div>
  <div class=\"meta-row\"><div class=\"meta-key\">Status</div><div>Office-Fallback konnte die Datei nicht lesen: {escape(str(exc))}</div></div>
</div>
"""


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
