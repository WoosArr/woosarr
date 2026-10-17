"""Request/response models shared by several routers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints

Username = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
]
Password = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class AdminOut(BaseModel):
    id: int
    username: str
    auth_source: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
    locked: bool = False


class Ok(BaseModel):
    ok: bool = True
