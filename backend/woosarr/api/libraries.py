"""Libraries page API: server libraries, exemptions, referencing roles (P7-03)."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.db.models import DiscordRole, Library, LibraryState, RoleLibrary
from woosarr.services import audit

router = APIRouter(prefix="/libraries", tags=["libraries"], dependencies=[Depends(require_admin)])


class RoleName(BaseModel):
    role_id: str
    name: str


class LibraryOut(BaseModel):
    library_id: str
    title: str
    type: str
    is_exempt: bool
    removed: bool
    roles: list[RoleName]
    shared_with: int


class LibraryPatchIn(BaseModel):
    is_exempt: bool


def _all(db) -> list[LibraryOut]:
    role_names = dict(db.execute(select(DiscordRole.role_id, DiscordRole.name)).all())
    by_lib: dict[str, list[RoleName]] = defaultdict(list)
    for row in db.scalars(select(RoleLibrary)):
        by_lib[row.library_id].append(RoleName(role_id=row.role_id, name=role_names.get(row.role_id, row.role_id)))
    shared = dict(db.execute(select(LibraryState.library_id, func.count()).group_by(LibraryState.library_id)).all())
    return [
        LibraryOut(library_id=lib.library_id, title=lib.title, type=lib.type, is_exempt=lib.is_exempt,
                   removed=lib.removed, roles=sorted(by_lib.get(lib.library_id, []), key=lambda r: r.name.lower()),
                   shared_with=shared.get(lib.library_id, 0))
        for lib in db.scalars(select(Library).order_by(func.lower(Library.title), Library.title))
    ]


@router.get("", response_model=list[LibraryOut])
def list_libraries(db: DbDep) -> list[LibraryOut]:
    return _all(db)


@router.patch("/{library_id}", response_model=LibraryOut)
def patch_library(library_id: str, body: LibraryPatchIn, request: Request, db: DbDep, principal: AdminDep) -> LibraryOut:
    lib = db.get(Library, library_id)
    if lib is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    if lib.is_exempt != body.is_exempt:
        lib.is_exempt = body.is_exempt
        audit.record(db, principal.actor, "library_exempt" if body.is_exempt else "library_unexempt",
                     "library", library_id, title=lib.title)
        request.app.state.sync.trigger(None, f"exemption changed for library {library_id}")
    db.flush()
    return next(item for item in _all(db) if item.library_id == library_id)
