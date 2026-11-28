"""Config export (D23, P15-01): download as much or as little of this server's setup as wanted.

POST only, so read-only API keys (GET) can't pull member ids, history or encrypted secrets.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.services import audit, config_transfer

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
