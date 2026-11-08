from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configured via environment variables prefixed with SHELF_."""

    model_config = SettingsConfigDict(env_prefix="SHELF_", env_file=".env", extra="ignore")

    library_dir: Path = Path("./library")  # where .wacz files live (scanned recursively)
    data_dir: Path = Path("./data")  # database + downloaded covers
    scan_on_startup: bool = True
    allow_uploads: bool = True  # allow uploading .wacz files through the web UI
    fetch_covers: bool = True  # download cover images referenced by metadata
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'shelf.db'}"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"


settings = Settings()
