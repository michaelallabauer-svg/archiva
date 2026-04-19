"""Runtime status helpers for indexing/OCR/search tooling."""

from __future__ import annotations

import shutil


def indexing_runtime_status() -> dict:
    tools = {
        "tesseract": shutil.which("tesseract"),
        "ocrmypdf": shutil.which("ocrmypdf"),
        "pdftotext": shutil.which("pdftotext"),
    }

    try:
        import pypdf  # noqa: F401
        pypdf_available = True
    except Exception:
        pypdf_available = False

    return {
        "ocr": {
            "tesseract": {"available": tools["tesseract"] is not None, "path": tools["tesseract"]},
            "ocrmypdf": {"available": tools["ocrmypdf"] is not None, "path": tools["ocrmypdf"]},
            "pdftotext": {"available": tools["pdftotext"] is not None, "path": tools["pdftotext"]},
            "pypdf": {"available": pypdf_available},
        }
    }
