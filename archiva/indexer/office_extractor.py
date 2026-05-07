"""Office document text extraction helpers for indexing."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

OOXML_WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OOXML_EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OOXML_POWERPOINT_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
LEGACY_OFFICE_MIME_TYPES = {
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
}
OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


def is_office_document(path: Path, mime_type: str | None = None) -> bool:
    normalized_mime = (mime_type or "").lower()
    return normalized_mime in {
        OOXML_WORD_MIME,
        OOXML_EXCEL_MIME,
        OOXML_POWERPOINT_MIME,
        *LEGACY_OFFICE_MIME_TYPES,
    } or path.suffix.lower() in OFFICE_EXTENSIONS


def extract_office_text(path: Path, mime_type: str | None = None) -> tuple[str, str | None]:
    suffix = path.suffix.lower()
    normalized_mime = (mime_type or "").lower()

    if suffix == ".docx" or normalized_mime == OOXML_WORD_MIME:
        text = _extract_docx_text(path)
        if text.strip():
            return text, "ooxml-docx"
    elif suffix == ".xlsx" or normalized_mime == OOXML_EXCEL_MIME:
        text = _extract_xlsx_text(path)
        if text.strip():
            return text, "ooxml-xlsx"
    elif suffix == ".pptx" or normalized_mime == OOXML_POWERPOINT_MIME:
        text = _extract_pptx_text(path)
        if text.strip():
            return text, "ooxml-pptx"

    text = _extract_office_text_via_libreoffice(path)
    if text.strip():
        return text, "libreoffice"
    return "", None


def _extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            parts = []
            for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
                try:
                    parts.append(_docx_xml_text(archive.read(name)))
                except KeyError:
                    continue
            return "\n".join(part for part in parts if part).strip()
    except Exception:
        return ""


def _docx_xml_text(xml: bytes) -> str:
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_xlsx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            sheet_names = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
            parts = []
            for index, sheet_name in enumerate(sheet_names, start=1):
                rows = _xlsx_rows(archive.read(sheet_name), shared_strings)
                if rows:
                    parts.append(f"Tabelle {index}")
                    parts.extend("\t".join(cell for cell in row if cell) for row in rows)
            return "\n".join(part for part in parts if part).strip()
    except Exception:
        return ""


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
                try:
                    shared_index = int(value_node.text)
                except ValueError:
                    value = ""
                else:
                    value = shared_strings[shared_index] if 0 <= shared_index < len(shared_strings) else ""
            else:
                value = value_node.text
            values.append(value)
        if any(values):
            rows.append(values)
    return rows


def _extract_pptx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            parts = []
            for slide_index, slide_name in enumerate(slide_names, start=1):
                root = ET.fromstring(archive.read(slide_name))
                texts = [node.text or "" for node in root.findall(".//a:t", ns) if node.text]
                if texts:
                    parts.append(f"Folie {slide_index}")
                    parts.append("\n".join(texts))
            return "\n".join(parts).strip()
    except Exception:
        return ""


def _extract_office_text_via_libreoffice(path: Path) -> str:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return ""
    with tempfile.TemporaryDirectory(prefix="archiva-office-index-") as tmpdir:
        outdir = Path(tmpdir)
        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "txt:Text",
                    "--outdir",
                    str(outdir),
                    str(path),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except Exception:
            return ""
        candidates = list(outdir.glob("*.txt"))
        if not candidates:
            return ""
        try:
            return candidates[0].read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
