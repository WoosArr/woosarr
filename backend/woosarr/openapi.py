"""`python -m woosarr.openapi [path]`: write the OpenAPI schema for the frontend (P1-07).

The frontend generates its TypeScript types from the committed snapshot
(frontend/openapi.json); tests/test_openapi.py fails when it is stale.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from woosarr.core.config import Settings
from woosarr.main import create_app


def schema_json() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            config_dir=Path(tmp), static_dir=Path(tmp) / "none", log_format="text", setup_code_required=False
        )
        app = create_app(settings)
        try:
            return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
        finally:
            app.state.db.engine.dispose()


if __name__ == "__main__":
    out = schema_json()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(out)
    else:
        sys.stdout.write(out)
