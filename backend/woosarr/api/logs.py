"""Read application logs over the API.

The deployed server is not directly reachable, so this is the primary way to
diagnose it. Everything returned has already passed through RedactingFilter.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from woosarr.api.deps import DbDep, require_admin
from woosarr.core import logs as logcore
from woosarr.db.models import ErrorResolution
from woosarr.services import error_digest

router = APIRouter(prefix="/logs", tags=["logs"], dependencies=[Depends(require_admin)])

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogEntryOut(BaseModel):
    seq: int
    ts: str
    level: str
    logger: str
    message: str
    exc: str | None = None
    # Warnings and errors only: who resolved this occurrence ("WooSarr: Seerr answered again",
    # "admin:alice"), if anyone has. The line itself always stays.
    resolved_by: str | None = None


class LogPage(BaseModel):
    entries: list[LogEntryOut]
    # Pass as `since` on the next call to get only newer entries.
    latest_seq: int


@router.get("", response_model=LogPage)
def read_logs(
    db: DbDep,
    since: Annotated[int, Query(ge=0, description="Only entries with seq greater than this")] = 0,
    level: Annotated[Level, Query(description="Minimum level")] = "DEBUG",
    q: Annotated[str | None, Query(max_length=200, description="Case-insensitive substring")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> LogPage:
    entries, latest = logcore.buffer().query(
        since=since, min_level=logging.getLevelName(level), contains=q, limit=limit
    )
    resolutions = {r.fingerprint: r for r in db.scalars(select(ErrorResolution))}
    out = []
    for e in entries:
        row = LogEntryOut(**e.to_dict())
        if e.level in ("WARNING", "ERROR", "CRITICAL") and resolutions:
            res = resolutions.get(error_digest.fingerprint(e.level, e.logger, e.message))
            if res is not None and datetime.fromisoformat(e.ts) <= res.resolved_at:
                row.resolved_by = res.resolved_by
        out.append(row)
    return LogPage(entries=out, latest_seq=latest)


@router.get("/file", response_class=PlainTextResponse)
def download_log_file(request: Request,
                      name: Annotated[Literal["woosarr", "alerts"], Query()] = "woosarr") -> PlainTextResponse:
    """The current rotating log file (JSON lines), including entries from before a restart.

    name=alerts: only warnings and errors, kept much longer than the main log.
    """
    settings = request.app.state.settings
    path = settings.alerts_file if name == "alerts" else settings.log_file
    # Snapshot instead of streaming: the file keeps growing while it is being
    # sent, which breaks a Content-Length taken from stat(). Size is capped by
    # log_file_max_bytes.
    data = path.read_bytes() if path.exists() else b""
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"),
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )
