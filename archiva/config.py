"""Configuration management for Archiva."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    host: str = "localhost"
    port: int = 5432
    name: str = "archiva"
    user: str = "postgres"
    password: str = "postgres"

    @property
    def url(self) -> str:
        """Return SQLAlchemy database URL."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class StorageSettings(BaseSettings):
    """Document storage settings."""

    base_path: Path = Path("./data/documents")


class SearchSettings(BaseSettings):
    """Full-text search settings."""

    max_results: int = 100
    highlight_fragment_size: int = 150
    engine: str = "opensearch"
    opensearch_url: str = "http://localhost:9200"
    index_name: str = "archiva-documents-v1"


class AppSettings(BaseSettings):
    """Application settings."""

    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    md5_duplicate_check: bool = Field(default=True, description="Globale MD5-Duplikatprüfung beim Dokument-Upload")


class Settings(BaseSettings):
    """Root settings container."""

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    app: AppSettings = Field(default_factory=AppSettings)

    @classmethod
    def from_yaml(cls, path: Path) -> "Settings":
        """Load settings from YAML file."""
        data = yaml.safe_load(path.read_text())
        return cls(**data)


def load_settings(config_path: str | None = None) -> Settings:
    """Load settings from config file or environment."""
    if config_path and Path(config_path).exists():
        return Settings.from_yaml(Path(config_path))
    return Settings()
