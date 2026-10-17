"""Runtime configuration (plan P1-02).

Process-level settings come from WOOSARR_* environment variables only. Nothing
here is a secret, and no secret ever gets a default: tokens and keys live in the
encrypted secrets store (core/secrets.py). Everything persistent lives under
config_dir, so the container only needs a /config volume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PORT = 31337


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WOOSARR_", extra="ignore")

    config_dir: Path = Path("/config")
    static_dir: Path = Path("static")
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    # In-memory buffer served by the logs API; the rotating file keeps history.
    log_buffer_size: int = 5000
    log_file_max_bytes: int = 5 * 1024 * 1024
    log_file_backups: int = 5

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "woosarr.log"
