"""File storage management for Archiva."""

import hashlib
import shutil
from pathlib import Path
from typing import Optional

from fastapi import UploadFile


class StorageManager:
    """
    Manages document file storage on the local filesystem.

    Stores files in a configurable base directory with UUID-based
    subdirectory structure for efficient access.
    """

    def __init__(self, base_path: Path) -> None:
        """Initialize storage manager with base path."""
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def generate_path(self, filename: str) -> Path:
        """
        Generate a storage path for a file.

        Uses a UUID-based subdirectory structure:
        base_path/uu/uu/uu/filename

        This prevents too many files in a single directory.
        """
        import uuid

        uid = uuid.uuid4().hex
        subdir = Path(uid[:2]) / uid[2:4] / uid[4:6]
        return subdir / filename

    def full_path(self, relative_path: Path) -> Path:
        """Get the full filesystem path for a relative storage path."""
        return self.base_path / relative_path

    async def save(self, file: UploadFile, relative_path: Path) -> Path:
        """
        Save an uploaded file to storage.

        Creates subdirectories as needed and writes the file.
        Returns the full path where the file was saved.
        """
        full = self.full_path(relative_path)
        full.parent.mkdir(parents=True, exist_ok=True)

        # Read and write in chunks to handle large files
        with open(full, "wb") as dest:
            shutil.copyfileobj(file.file, dest)

        return full

    def delete(self, relative_path: Path) -> bool:
        """
        Delete a file from storage.

        Returns True if deleted, False if file didn't exist.
        """
        full = self.full_path(relative_path)
        if full.exists():
            full.unlink()
            return True
        return False

    def exists(self, relative_path: Path) -> bool:
        """Check if a file exists in storage."""
        return self.full_path(relative_path).exists()

    def get_checksum(self, relative_path: Path) -> Optional[str]:
        """
        Calculate SHA-256 checksum of a file.

        Returns None if file doesn't exist.
        """
        full = self.full_path(relative_path)
        if not full.exists():
            return None

        sha256 = hashlib.sha256()
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def get_size(self, relative_path: Path) -> int:
        """Get file size in bytes. Returns 0 if file doesn't exist."""
        full = self.full_path(relative_path)
        if full.exists():
            return full.stat().st_size
        return 0
