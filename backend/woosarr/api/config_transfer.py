"""Config export and import (D23, P15-01/02): move as much or as little of a setup as wanted.

POST only, so read-only API keys (GET) can't pull member ids, history or encrypted secrets, and can't
import. Import is preview first (writes nothing), then apply (one transaction, dry-run on after).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from woosarr.api import status as status_api
from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import app_settings
from woosarr.services import audit, config_import, config_transfer

router = APIRouter(prefix="/config", tags=["config"], dependencies=[Depends(require_admin)])

Part = Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]


class PartInfo(BaseModel):
    name: str
    count: int


class ExportParts(BaseModel):
    parts: list[PartInfo]
    passphrase_min: int


class ExportIn(BaseModel):
    parts: list[Part] = Field(default_factory=list, max_length=len(config_transfer.PARTS),
                              description="Parts to include; empty = all")
    include_secrets: bool = False
    passphrase: str | None = Field(default=None, max_length=1024)


@router.get("/export/parts", response_model=ExportParts)
def export_parts(db: DbDep) -> ExportParts:
    counts = config_transfer.part_counts(db)
    return ExportParts(parts=[PartInfo(name=p, count=counts[p]) for p in config_transfer.PARTS],
                       passphrase_min=config_transfer.PASSPHRASE_MIN)


@router.post("/export", responses={200: {"content": {"application/json": {}},
                                         "description": "The export file (download)"}})
def export_config(body: ExportIn, request: Request, db: DbDep, principal: AdminDep) -> Response:
    try:
        doc = config_transfer.export(db, request.app.state.secrets, body.parts, body.passphrase, body.include_secrets)
    except config_transfer.TransferError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    summary = doc["summary"]
    audit.record(db, principal.actor, "config_exported", None, None, parts=summary["parts"],
                 counts=summary["counts"], encrypted=summary["encrypted"])
    return Response(json.dumps(doc, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{config_transfer.filename()}"',
                             "Cache-Control": "no-store"})


# ---- Import (P15-02) ------------------------------------------------------------------------------


class ImportIn(BaseModel):
    file: dict[str, Any] = Field(description="The export file, as downloaded")
    passphrase: str | None = Field(default=None, max_length=1024)
    parts: list[Part] = Field(default_factory=list, max_length=len(config_import.IMPORT_PARTS),
                              description="Parts to import (incl. 'secrets'); empty = everything in the file")


class ImportPartOut(BaseModel):
    name: str
    summary: str
    counts: dict[str, int]


class ImportPreview(BaseModel):
    format: int
    woosarr: str | None
    exported_at: str | None
    source: dict[str, Any]
    available: list[str]  # parts in the file ("secrets" if it has any)
    parts: list[ImportPartOut]
    warnings: list[str]
    secrets_in_file: int | None  # how many values the encrypted block holds (None: none in the file)
    secrets_found: list[str]  # what the passphrase opened (names, never values)
    secrets_readable: bool | None  # None: no passphrase given


class ImportResult(ImportPreview):
    reconnecting: list[str]  # "plex", "discord": connecting with imported tokens


_IMPORT_BODY = {"requestBody": {"required": True,
                                "content": {"application/json": {"schema": ImportIn.model_json_schema()}}}}


async def _import_body(request: Request) -> ImportIn:
    """The request body, read with a size cap (an export with audit history is big, but bounded)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config_import.MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "the file is larger than 25 MB")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > config_import.MAX_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "the file is larger than 25 MB")
    try:
        return ImportIn.model_validate(json.loads(raw))
    except ValueError as exc:  # bad JSON or a body that doesn't fit ImportIn
        msg = "the upload isn't valid JSON" if isinstance(exc, json.JSONDecodeError) else str(exc).splitlines()[0]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg) from None


def _preview_out(doc: config_import.ExportFile, result: config_import.Result) -> dict[str, Any]:
    secrets_in_file = doc.secrets.get("count") if doc.secrets and isinstance(doc.secrets.get("count"), int) else (
        0 if doc.secrets else None)
    available = [p for p in config_transfer.PARTS if getattr(doc.parts, p) is not None]
    if doc.secrets:
        available.append(config_import.SECRETS)
    return {"format": doc.format, "woosarr": doc.woosarr, "exported_at": doc.exported_at,
            "source": doc.source.model_dump(), "available": available,
            "parts": [ImportPartOut(name=p.name, summary=p.summary, counts=p.counts) for p in result.parts],
            "warnings": result.warnings, "secrets_in_file": secrets_in_file, "secrets_found": result.secrets_found,
            "secrets_readable": result.secrets_readable}


def _run(db: Any, store: Any, body: ImportIn, apply: bool, actor: str) -> tuple[config_import.ExportFile,
                                                                                  config_import.Result]:
    try:
        doc = config_import.parse(body.file)
        return doc, config_import.run(db, store, doc, body.parts, body.passphrase, apply, actor)
    except config_transfer.TransferError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


@router.post("/import/preview", response_model=ImportPreview, openapi_extra=_IMPORT_BODY)
async def import_preview(request: Request, db: DbDep, principal: AdminDep) -> dict[str, Any]:
    """What importing the file would change here. Writes nothing."""
    body = await _import_body(request)
    doc, result = await run_in_threadpool(_run, db, request.app.state.secrets, body, False, principal.actor)
    db.rollback()  # nothing to keep: a preview never writes
    return _preview_out(doc, result)


@router.post("/import", response_model=ImportResult, openapi_extra=_IMPORT_BODY)
async def import_config(request: Request, db: DbDep, principal: AdminDep, tasks: BackgroundTasks) -> dict[str, Any]:
    """Apply the chosen parts in one transaction; dry-run is on afterwards."""
    body = await _import_body(request)
    doc, result = await run_in_threadpool(_run, db, request.app.state.secrets, body, True, principal.actor)
    audit.record(db, principal.actor, "config_imported", None, None,
                 parts={p.name: p.counts for p in result.parts}, source_plex=doc.source.plex_server,
                 source_discord=doc.source.discord_guild, exported_at=doc.exported_at,
                 encrypted=result.encrypted_count, warnings=len(result.warnings))
    reconnecting = [name for name, on in (("plex", result.plex_token), ("discord", result.discord_token)) if on]
    # After the response, so the transaction above has committed.
    tasks.add_task(_after_import, request.app, result)
    return _preview_out(doc, result) | {"reconnecting": reconnecting}


async def _after_import(app: Any, result: config_import.Result) -> None:
    with app.state.db.session() as s:
        status_api.apply_runtime_settings(app, app_settings.load(s))
    plex = getattr(app.state, "plex", None)
    if result.plex_token and plex is not None and hasattr(plex, "token_replaced"):
        plex.token_replaced()
    discord = getattr(app.state, "discord", None)
    if discord is not None:
        if result.discord_token and hasattr(discord, "restart"):
            await discord.restart()
        elif hasattr(discord, "schedule_post_refresh"):
            discord.schedule_post_refresh()
    sync = getattr(app.state, "sync", None)
    if sync is not None:
        sync.trigger(None, "config import")
