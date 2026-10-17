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
    # Sessions (P6-01)
    session_idle_hours: int = 12
    session_max_days: int = 7
    # auto = Secure cookie when the request arrived over HTTPS (directly or via
    # a trusted proxy in forwarded_allow_ips).
    cookie_secure: Literal["auto", "always", "never"] = "auto"
    forwarded_allow_ips: str = "127.0.0.1"
    # First-run admin creation needs the one-time code printed to the container
    # log (P6-06). Disable only on a trusted network.
    setup_code_required: bool = True
    # Fake Plex server + Discord cache for trying the UI (woosarr/dev/demo.py).
    demo: bool = False

    @property
    def db_path(self) -> Path:
        return self.config_dir / "app.db"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "woosarr.log"
