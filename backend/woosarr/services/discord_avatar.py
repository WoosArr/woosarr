"""The bot's avatar (and optionally the application icon): WooSarr's icon or an
admin-uploaded image, sent to Discord with the bot's own token.

Discord takes image data as a data URI (PNG, JPEG or GIF) on
`PATCH /users/@me` (avatar) and `PATCH /applications/@me` (icon). Avatar
changes are rate limited (about 2 per hour), which comes back as HTTP 429
with `retry_after`. Images are checked by their bytes, not by the name or
type the browser claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
from dataclasses import dataclass
from importlib import resources

import aiohttp

log = logging.getLogger("woosarr.discord")

API = "https://discord.com/api/v10"
MAX_BYTES = 8 * 1024 * 1024
MIN_SIDE = 128
CDN_AVATAR = "https://cdn.discordapp.com/avatars/{user_id}/{hash}.{ext}?size=256"


class AvatarError(Exception):
    """Unusable image or a Discord refusal; message is safe to show."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Image:
    data: bytes
    mime: str  # image/png | image/jpeg | image/gif
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        # SOF0..SOF15 except DHT (C4), JPG (C8) and DAC (CC) carry the size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    return None


def inspect(data: bytes) -> Image:
    """Type and size from the bytes themselves; AvatarError if unusable."""
    if not data:
        raise AvatarError("The image is empty.")
    if len(data) > MAX_BYTES:
        raise AvatarError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    size: tuple[int, int] | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        mime, size = "image/png", struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime, size = "image/gif", struct.unpack("<HH", data[6:10])
    elif data.startswith(b"\xff\xd8"):
        mime, size = "image/jpeg", _jpeg_size(data)
    else:
        raise AvatarError("Use a PNG, JPEG or GIF image.")
    if size is None:
        raise AvatarError("That image looks damaged (its size can't be read).")
    width, height = size
    if min(width, height) < MIN_SIDE:
        raise AvatarError(f"The image is {width}×{height} px; Discord needs at least {MIN_SIDE}×{MIN_SIDE}.")
    return Image(data, mime, width, height)


def decode_data_uri(uri: str) -> Image:
    """`data:image/...;base64,...` from the browser -> checked Image."""
    head, sep, payload = uri.partition(",")
    if not sep or not head.startswith("data:") or not head.endswith(";base64"):
        raise AvatarError("Send the image as a base64 data URL.")
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError:
        raise AvatarError("The image data isn't valid base64.") from None
    return inspect(data)


def default_image() -> Image:
    """WooSarr's icon, shipped inside the package (rendered by `make icons`)."""
    return inspect(resources.files("woosarr").joinpath("assets/bot-avatar.png").read_bytes())


async def _patch(session: aiohttp.ClientSession, token: str, path: str, body: dict) -> dict:
    async with session.patch(f"{API}{path}", json=body, headers={"Authorization": f"Bot {token}"}) as r:
        text = await r.text()
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if r.status == 429:
            wait = int(float((data or {}).get("retry_after") or 0)) + 1
            minutes = max(1, round(wait / 60))
            log.warning("discord %s rate limited; retry after %s s", path, wait)
            raise AvatarError(f"Discord allows only a few avatar changes per hour. Try again in about "
                              f"{minutes} minute{'s' if minutes != 1 else ''}.", 429)
        if r.status == 401:
            raise AvatarError("Discord rejected the bot token. Enter it again on the Setup page.", 409)
        if r.status >= 400:
            message = (data or {}).get("message") if isinstance(data, dict) else None
            errors = (data or {}).get("errors") if isinstance(data, dict) else None
            log.warning("discord %s -> %s %s %s", path, r.status, message, str(errors)[:300])
            raise AvatarError(f"Discord refused the image ({message or f'HTTP {r.status}'}).", 502)
        return data or {}


async def apply(token: str, image: Image, app_icon: bool) -> dict[str, str | None]:
    """Set the bot's avatar (and the application icon). Returns the new avatar URL."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        user = await _patch(session, token, "/users/@me", {"avatar": image.data_uri()})
        icon_set = None
        if app_icon:
            app = await _patch(session, token, "/applications/@me", {"icon": image.data_uri()})
            icon_set = app.get("icon")
    avatar = user.get("avatar")
    url = None
    if avatar and user.get("id"):
        url = CDN_AVATAR.format(user_id=user["id"], hash=avatar, ext="gif" if str(avatar).startswith("a_") else "png")
    log.info("discord avatar updated (%s %sx%s, sha256 %s…)%s", image.mime, image.width, image.height,
             image.sha256[:12], " and application icon" if icon_set else "")
    return {"avatar_url": url, "app_icon": icon_set}
