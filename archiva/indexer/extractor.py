"""Minimal text extraction for Archiva indexing."""

from __future__ import annotations

from pathlib import Path

from archiva.indexer.ocr import run_ocr_on_image, run_ocr_on_pdf


def extract_text_for_indexing(storage_path: str, mime_type: str | None = None) -> tuple[str, bool, str | None]:
    path = Path(storage_path)
    if not path.exists() or not path.is_file():
        return ""

    lower_name = path.name.lower()
    mime_type = (mime_type or "").lower()

    if mime_type.startswith("text/") or lower_name.endswith((".txt", ".md", ".csv", ".log", ".json")):
        try:
            return path.read_text(encoding="utf-8", errors="ignore"), False, None
        except OSError:
            return "", False, None

    if mime_type == "application/pdf" or lower_name.endswith(".pdf"):
        extracted = _extract_pdf_text(path)
        if extracted.strip():
            return extracted, False, "pypdf"
        extracted = _extract_pdf_text_via_pdftotext(path)
        if extracted.strip():
            return extracted, False, "pdftotext"
        ocr_result = run_ocr_on_pdf(path)
        return ocr_result.text or "", ocr_result.used_ocr, ocr_result.engine

    if mime_type.startswith("image/") or lower_name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")):
        ocr_result = run_ocr_on_image(path)
        return ocr_result.text or "", ocr_result.used_ocr, ocr_result.engine

    return "", False, None


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(part for part in parts if part).strip()
    except Exception:
        return ""


def _extract_pdf_text_via_pdftotext(path: Path) -> str:
    import shutil
    import subprocess

    pdftotext_bin = shutil.which("pdftotext")
    if not pdftotext_bin:
        return ""

    try:
        result = subprocess.run(
            [pdftotext_bin, str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return ""

    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()
