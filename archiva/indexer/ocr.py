"""OCR helpers for Archiva indexing.

Freie Tooling-Strategie:
- bevorzugt `ocrmypdf` für durchsuchbare PDFs
- fallback `tesseract` für Bild-OCR
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class OCRResult:
    def __init__(self, text: str = "", used_ocr: bool = False, engine: str | None = None):
        self.text = text
        self.used_ocr = used_ocr
        self.engine = engine


def ocrmypdf_available() -> bool:
    return shutil.which("ocrmypdf") is not None


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def run_ocr_on_pdf(path: Path) -> OCRResult:
    if not ocrmypdf_available():
        return OCRResult()

    with tempfile.TemporaryDirectory() as tmpdir:
        output_pdf = Path(tmpdir) / "ocr-output.pdf"
        cmd = [
            "ocrmypdf",
            "--skip-text",
            "--sidecar",
            "-",
            str(path),
            str(output_pdf),
        ]
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except Exception:
            return OCRResult()

        if result.returncode != 0:
            return OCRResult()
        return OCRResult(text=(result.stdout or "").strip(), used_ocr=True, engine="ocrmypdf")


def run_ocr_on_image(path: Path) -> OCRResult:
    if not tesseract_available():
        return OCRResult()

    cmd = ["tesseract", str(path), "stdout"]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return OCRResult()

    if result.returncode != 0:
        return OCRResult()
    return OCRResult(text=(result.stdout or "").strip(), used_ocr=True, engine="tesseract")
