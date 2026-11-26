from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configured via environment variables prefixed with SHELF_."""

    model_config = SettingsConfigDict(env_prefix="SHELF_", env_file=".env", extra="ignore")

    library_dir: Path = Path("./library")  # where .wacz files live (scanned recursively)
    data_dir: Path = Path("./data")  # database + downloaded covers
    scan_on_startup: bool = True
    allow_uploads: bool = True  # allow uploading .wacz files through the web UI
    fetch_covers: bool = True  # download cover images referenced by metadata
    # Sign-in (see auth.py). Neither set: Shelf is open to anyone who can reach it.
    password: SecretStr | None = None
    auth_header: str = ""  # e.g. "Remote-User", set by a login proxy in front of Shelf
    # Addresses the reverse proxy connects from (IPs, networks like 172.16.0.0/12, or *):
    # their auth header and X-Forwarded-For/-Proto are believed.
    trusted_proxies: str = "127.0.0.1,::1"
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'shelf.db'}"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"


settings = Settings()
