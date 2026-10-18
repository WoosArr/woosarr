"""Bot avatar: image checks, the Discord calls (against a local fake), and the API."""

import base64
import struct
import unittest
import zlib
from unittest import mock

from aiohttp import web
from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.db.models import AuditLog
from woosarr.services import discord_avatar as A


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\0" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\0" * 10


def uri(data, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class ImageTests(unittest.TestCase):
    def test_types_and_sizes_from_bytes(self):
        self.assertEqual((A.inspect(png(512, 512)).mime, A.inspect(png(512, 300)).height), ("image/png", 300))
        j = A.inspect(jpeg(640, 480))
        self.assertEqual((j.mime, j.width, j.height), ("image/jpeg", 640, 480))
        g = A.inspect(gif(128, 128))
        self.assertEqual((g.mime, g.width), ("image/gif", 128))

    def test_refused(self):
        for data, why in ((b"", "empty"), (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "PNG, JPEG or GIF"),
                          (b"RIFF\0\0\0\0WEBPVP8 ", "PNG, JPEG or GIF"), (png(64, 64), "at least 128"),
                          (b"\xff\xd8\xff\xd9", "damaged"), (png(512, 512) + b"\0" * A.MAX_BYTES, "larger than")):
            with self.assertRaisesRegex(A.AvatarError, why):
                A.inspect(data)

    def test_type_is_taken_from_bytes_not_the_claim(self):
        self.assertEqual(A.decode_data_uri(uri(jpeg(256, 256), "image/png")).mime, "image/jpeg")
        with self.assertRaisesRegex(A.AvatarError, "PNG, JPEG or GIF"):
            A.decode_data_uri(uri(b"<html>", "image/png"))
        for bad in ("not a data url", "data:image/png,rawtext", "data:image/png;base64,@@@"):
            with self.assertRaises(A.AvatarError):
                A.decode_data_uri(bad)

    def test_default_is_the_packaged_woosarr_icon(self):
        img = A.default_image()
        self.assertEqual((img.mime, img.width, img.height), ("image/png", 512, 512))


class FakeDiscord:
    """Local stand-in for PATCH /users/@me and /applications/@me."""

    def __init__(self):
        self.calls = []
        self.rate_limited = False

    async def users(self, request):
        return await self._handle(request, "avatar", {"id": "4242", "avatar": "abc123", "username": "WooSarr"})

    async def apps(self, request):
        return await self._handle(request, "icon", {"id": "77", "icon": "def456"})

    async def _handle(self, request, field, ok):
        body = await request.json()
        self.calls.append((request.path, request.headers.get("Authorization"), body[field][:22]))
        if request.headers.get("Authorization") != "Bot good-token":
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        if self.rate_limited:
            return web.json_response({"message": "You are being rate limited.", "retry_after": 1500.5}, status=429)
        if not body[field].startswith("data:image/"):
            return web.json_response({"message": "Invalid Form Body", "errors": {field: "bad"}}, status=400)
        return web.json_response(ok)


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeDiscord()
        app = web.Application()
        app.router.add_patch("/users/@me", self.fake.users)
        app.router.add_patch("/applications/@me", self.fake.apps)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        patcher = mock.patch.object(A, "API", f"http://127.0.0.1:{port}")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_avatar_and_app_icon(self):
        out = await A.apply("good-token", A.inspect(png(512, 512)), app_icon=True)
        self.assertEqual(out, {"avatar_url": "https://cdn.discordapp.com/avatars/4242/abc123.png?size=256",
                               "app_icon": "def456"})
        self.assertEqual([c[0] for c in self.fake.calls], ["/users/@me", "/applications/@me"])
        self.assertEqual(self.fake.calls[0][2], "data:image/png;base64,")

    async def test_avatar_only(self):
        out = await A.apply("good-token", A.inspect(gif(256, 256)), app_icon=False)
        self.assertIsNone(out["app_icon"])
        self.assertEqual([c[0] for c in self.fake.calls], ["/users/@me"])

    async def test_rate_limit_and_bad_token(self):
        self.fake.rate_limited = True
        with self.assertRaisesRegex(A.AvatarError, "about 25 minutes") as caught:
            await A.apply("good-token", A.inspect(png(512, 512)), app_icon=False)
        self.assertEqual(caught.exception.status, 429)
        with self.assertRaisesRegex(A.AvatarError, "rejected the bot token"):
            await A.apply("bad-token", A.inspect(png(512, 512)), app_icon=False)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.svc = self.h.app.state.discord
        self.applied = []

        async def fake_set(image, app_icon):
            self.applied.append((image.mime, image.width, app_icon))
            return {"avatar_url": "https://cdn.discordapp.com/avatars/1/x.png?size=256", "app_icon": "i" if app_icon else None}

        self.svc.set_avatar = fake_set

    def tearDown(self):
        self.h.close()

    def actions(self):
        with self.h.app.state.db.session() as s:
            return [a.action for a in s.scalars(select(AuditLog).where(AuditLog.action.like("discord_avatar%")))]

    def test_default_and_custom(self):
        r = self.c.put("/api/v1/discord/avatar", json={"source": "default"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["width"], r.json()["app_icon"]), (512, False))
        r = self.c.put("/api/v1/discord/avatar", json={"source": "custom", "image": uri(jpeg(300, 300)),
                                                        "app_icon": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["app_icon"])
        self.assertEqual(self.applied, [("image/png", 512, False), ("image/jpeg", 300, True)])
        self.assertEqual(self.actions(), ["discord_avatar_set", "discord_avatar_set"])

    def test_bad_upload_and_discord_refusal(self):
        r = self.c.put("/api/v1/discord/avatar", json={"source": "custom"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "Choose an image to upload."))
        r = self.c.put("/api/v1/discord/avatar", json={"source": "custom", "image": uri(png(64, 64))})
        self.assertEqual(r.status_code, 400)

        async def limited(image, app_icon):
            raise A.AvatarError("Discord allows only a few avatar changes per hour.", 429)

        self.svc.set_avatar = limited
        r = self.c.put("/api/v1/discord/avatar", json={"source": "default"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.actions(), ["discord_avatar_failed"] * 3)
        self.assertEqual(self.applied, [])

    def test_needs_admin_and_csrf(self):
        anon = self.h.new_client()
        self.assertEqual(anon.put("/api/v1/discord/avatar", json={"source": "default"}).status_code, 401)
        no_csrf = self.h.new_client()
        no_csrf.cookies = self.c.cookies
        self.assertEqual(no_csrf.put("/api/v1/discord/avatar", json={"source": "default"}).status_code, 403)
