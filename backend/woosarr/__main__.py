"""`python -m woosarr`: run the server with host/port from WOOSARR_* settings."""

from __future__ import annotations

import uvicorn

from woosarr.core.config import Settings
from woosarr.main import create_app


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Our own handlers (with redaction) own logging; don't let uvicorn install its own.
        log_config=None,
        proxy_headers=True,
        # docker stop sends SIGTERM and waits 10 s before SIGKILL.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
