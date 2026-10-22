"""Custom Emoji page: library indexes, previews, image preparation, add/rename/delete."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import AuditLog, Reaction, ReactionPost
from woosarr.services import emoji_library as el
from woosarr.services.discord_bot import DiscordConfigError, DiscordService


def png(size=(200, 200), color=(200, 30, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def gif(frames=3, size=(64, 64), noise=False) -> bytes:
    import random

    rnd = random.Random(1)
    ims = []
    for i in range(frames):
        im = Image.new("RGB", size, (i * 40 % 255, 80, 160))
        if noise:  # incompressible, so the GIF is big
            im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
        ims.append(im)
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=100, loop=0)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise el.requests.HTTPError(str(self.status_code))

    def json(self):
        return json.loads(self.body)


class FakeHttp:
    """requests.Session stand-in: url -> FakeResponse; records every GET."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, **kw):
        self.calls.append((url, params, kw))
        key = url if params is None else f"{url}?{params['query']}&{params['page']}"
        r = self.routes.get(key)
        if r is None:
            return FakeResponse(404)
        return r() if callable(r) else r


GG = [
    {"id": 1, "title": "Pepe Happy", "slug": "1_pepe_happy", "image": "https://cdn3.emoji.gg/emojis/1_pepe.png",
     "category": 13, "faves": 5, "submitted_by": "someone"},
    {"id": 2, "title": "pepe_dance", "slug": "2_pepe_dance", "image": "https://cdn3.emoji.gg/emojis/2_pepe.gif",
     "category": 8, "faves": 50},
    {"id": 3, "title": "pepe_lewd", "slug": "3", "image": "https://cdn3.emoji.gg/emojis/3.png", "category": 9},
    {"id": 4, "title": "evil", "slug": "4", "image": "https://evil.example/x.png", "category": 12},
]
OM = [
    {"hexcode": "1F37F", "annotation": "popcorn", "group": "food-drink", "emoji": "🍿", "tags": "snack"},
    {"hexcode": "1F44D-1F3FB", "annotation": "thumbs up: light", "skintone": "1"},
]


def gg_routes(body=GG):
    return {el.EMOJIGG_INDEX: lambda: FakeResponse(body=json.dumps(body).encode()),
            el.OPENMOJI_LATEST: lambda: FakeResponse(body=b'{"version": "17.0.0"}'),
            el.OPENMOJI_INDEX.format(version="17.0.0"): lambda: FakeResponse(body=json.dumps(OM).encode())}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lib(self, routes):
        http = FakeHttp(routes)
        return el.EmojiLibrary(self.dir, session=http), http

    def test_indexes_parse_filter_nsfw_and_search(self):
        lib, _ = self.lib(gg_routes())
        lib.refresh(startup=True)
        items, _total, _ = lib.search("emojigg", "pepe", hide_animated=False, offset=0, limit=10)
        self.assertEqual([i.id for i in items], ["2", "1"])  # NSFW left out; more favourites first
        self.assertEqual(lib.item("emojigg", "1").name, "Pepe_Happy")
        self.assertTrue(lib.item("emojigg", "2").animated)
        self.assertEqual(lib.search("emojigg", "pepe", True, 0, 10)[1], 1)
        pop = lib.search("openmoji", "snack", True, 0, 10)[0]
        self.assertEqual([(e.id, e.name) for e in pop], [("1F37F", "popcorn")])  # skin tones skipped
        self.assertIn("@17.0.0/color/618x618/1F37F.png", pop[0].image)

    def test_refresh_is_daily_and_survives_a_restart_and_failures(self):
        lib, http = self.lib(gg_routes())
        lib.refresh(startup=True)
        n = len(http.calls)
        lib.refresh(startup=True)
        self.assertEqual(len(http.calls), n)  # just downloaded: nothing due
        later = lib._indexes["emojigg"].checked_at + el.REFRESH_SECONDS + 1
        self.assertEqual(sorted(lib.due(now=later)), ["emojigg", "openmoji"])
        # A restart loads the cached indexes; a failing source keeps them.
        lib2, _http2 = self.lib({})
        self.assertEqual(lib2.sources()[0]["count"], 3)
        lib2.refresh(force=True)
        self.assertIn("HTTP 404", lib2.sources()[0]["error"])
        self.assertEqual(lib2.search("emojigg", "", False, 0, 10)[1], 3)
        self.assertEqual(lib2.due(), [])  # failed: retried an hour later, not straight away

    def test_openmoji_index_downloaded_only_for_a_new_version(self):
        lib, http = self.lib(gg_routes())
        lib.refresh(force=True)
        lib.refresh(force=True)
        index_gets = [c for c in http.calls if c[0] == el.OPENMOJI_INDEX.format(version="17.0.0")]
        self.assertEqual(len(index_gets), 1)

    def test_previews_only_from_the_source_host_and_only_images(self):
        routes = gg_routes()
        routes["https://cdn3.emoji.gg/emojis/1_pepe.png"] = lambda: FakeResponse(body=png())
        routes["https://cdn3.emoji.gg/emojis/2_pepe.gif"] = lambda: FakeResponse(body=b"<svg onload=alert(1)>")
        lib, http = self.lib(routes)
        lib.refresh(force=True)
        _data, kind = lib.preview_bytes(lib.item("emojigg", "1"))
        self.assertEqual(kind, "image/png")
        lib.preview_bytes(lib.item("emojigg", "1"))
        self.assertEqual(sum(1 for c in http.calls if c[0].endswith("1_pepe.png")), 1)  # cached on disk
        self.assertFalse(http.calls[-1][2]["allow_redirects"])
        with self.assertRaisesRegex(el.EmojiLibraryError, "isn't a PNG"):
            lib.preview_bytes(lib.item("emojigg", "2"))
        with self.assertRaisesRegex(el.EmojiLibraryError, "image host"):
            lib.preview_bytes(lib.item("emojigg", "4"))
        self.assertFalse(any("evil.example" in c[0] for c in http.calls))
        with self.assertRaises(el.EmojiLibraryError):
            lib.item("emojigg", "3")

    def test_same_preview_fetched_at_once_by_many_requests(self):
        # Live 500s: two requests downloading one preview shared a temp file, and the second rename failed.
        import threading
        from concurrent.futures import ThreadPoolExecutor

        gate = threading.Barrier(8)

        def slow_png():
            gate.wait(timeout=5)  # every request is mid-download before any writes
            return FakeResponse(body=png())

        routes = gg_routes()
        routes["https://cdn3.emoji.gg/emojis/1_pepe.png"] = slow_png
        lib, _ = self.lib(routes)
        lib.refresh(force=True)
        item = lib.item("emojigg", "1")
        with mock.patch.object(el, "MAX_PARALLEL_PER_SOURCE", 8):
            lib._slots["emojigg"] = el.threading.BoundedSemaphore(8)
            with self.assertNoLogs("woosarr.emoji", "WARNING"), ThreadPoolExecutor(8) as pool:
                kinds = list(pool.map(lambda _: lib.preview_bytes(item)[1], range(8)))
        self.assertEqual(kinds, ["image/png"] * 8)
        self.assertEqual([p.name for p in (self.dir / "previews").iterdir() if p.suffix == ".tmp"], [])

    def test_images_gone_from_the_source_are_hidden_and_not_asked_for_again(self):
        routes = gg_routes()
        routes["https://cdn3.emoji.gg/emojis/1_pepe.png"] = lambda: FakeResponse(403)
        lib, http = self.lib(routes)
        lib.refresh(force=True)
        for _ in range(3):
            with self.assertRaisesRegex(el.EmojiLibraryError, "no longer has"):
                lib.preview_bytes(lib.item("emojigg", "1"))
        self.assertEqual(sum(1 for c in http.calls if c[0].endswith("1_pepe.png")), 1)
        self.assertEqual([i.id for i in lib.search("emojigg", "pepe", False, 0, 10)[0]], ["2"])
        with mock.patch.object(el.time, "time", return_value=el.time.time() + el.MISSING_SECONDS + 1):
            self.assertEqual(lib.search("emojigg", "pepe", False, 0, 10)[1], 2)  # asked again a day later

    def test_oversized_downloads_are_refused(self):
        routes = gg_routes()
        routes["https://cdn3.emoji.gg/emojis/1_pepe.png"] = lambda: FakeResponse(
            body=b"\x89PNG\r\n\x1a\n" + b"0" * (el.MAX_IMAGE_BYTES + 10))
        lib, _ = self.lib(routes)
        lib.refresh(force=True)
        with self.assertRaisesRegex(el.EmojiLibraryError, "too large"):
            lib.preview_bytes(lib.item("emojigg", "1"))

    def test_slackmojis_search_pages_and_checks_gif_frames(self):
        page = [{"id": 10 + i, "name": f"cat {i}", "image_url": f"https://emojis.slackmojis.com/e/{10 + i}/c.gif",
                 "category": {"name": "Random"}} for i in range(20)]
        routes = {f"{el.SLACKMOJIS_SEARCH}?cat&0": FakeResponse(body=json.dumps(page).encode()),
                  f"{el.SLACKMOJIS_SEARCH}?cat&1": FakeResponse(body=b"[]")}
        for i in range(20):  # even ids: one frame (static), odd: animated
            routes[f"https://emojis.slackmojis.com/e/{10 + i}/c.gif"] = (
                lambda i=i: FakeResponse(body=gif(frames=1 if i % 2 == 0 else 3)))
        lib, _ = self.lib(routes)
        items, total, more = lib.search("slackmojis", "cat", hide_animated=True, offset=0, limit=60)
        self.assertEqual((total, more), (10, False))
        self.assertTrue(all(i.animated is False for i in items))
        self.assertEqual(lib.item("slackmojis", "11").animated, True)  # seen: can be previewed and added
        self.assertEqual(lib.search("slackmojis", "  ", True, 0, 60), ([], 0, False))


class PrepareImageTests(unittest.TestCase):
    def test_static_becomes_small_png(self):
        out, animated = el.prepare_image(png((1024, 512)))
        im = Image.open(io.BytesIO(out))
        self.assertEqual((im.format, im.size, animated), ("PNG", (128, 64), False))

    def test_one_frame_gif_is_static(self):
        out, animated = el.prepare_image(gif(frames=1))
        self.assertEqual((Image.open(io.BytesIO(out)).format, animated), ("PNG", False))

    def test_small_animation_kept_big_one_resized_under_the_limit(self):
        small = gif()
        self.assertEqual(el.prepare_image(small), (small, True))
        big = gif(frames=12, size=(256, 256), noise=True)
        self.assertGreater(len(big), el.DISCORD_MAX_BYTES)
        out, animated = el.prepare_image(big)
        im = Image.open(io.BytesIO(out))
        self.assertTrue(animated)
        self.assertLessEqual(len(out), el.DISCORD_MAX_BYTES)
        self.assertLessEqual(max(im.size), 128)
        self.assertEqual(im.n_frames, 12)

    def test_non_images_and_bombs_are_refused(self):
        for data in (b"<svg/>", b"GIF89a broken", b""):
            with self.assertRaises(el.EmojiLibraryError):
                el.prepare_image(data)
        with mock.patch.object(el, "MAX_PIXELS", 100), self.assertRaisesRegex(el.EmojiLibraryError, "too large"):
            el.prepare_image(png((20, 20)))

    def test_names(self):
        self.assertEqual(el.emoji_name("thumbs up: light", "x"), "thumbs_up_light")
        self.assertEqual(el.emoji_name("🍿", "1f37f"), "emoji_1f37f")
        self.assertEqual(len(el.emoji_name("a" * 50, "x")), 32)


class EmojiApiTests(unittest.TestCase):
    """Demo mode: made-up library and an in-memory server, no network."""

    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def test_search_preview_add_rename_delete(self):
        srcs = {s["key"]: s for s in self.c.get("/api/v1/emoji/sources").json()}
        self.assertTrue(srcs["slackmojis"]["live"])
        found = self.c.get("/api/v1/emoji/search", params={"source": "emojigg", "q": "party", "hide_animated": False})
        self.assertEqual(found.status_code, 200, found.text)
        item = found.json()["items"][0]
        self.assertTrue(item["animated"])
        self.assertEqual(self.c.get("/api/v1/emoji/search", params={"source": "emojigg", "q": "party"}).json()["total"], 0)
        p = self.c.get(f"/api/v1/emoji/preview/emojigg/{item['id']}")
        self.assertEqual((p.status_code, p.headers["content-type"]), (200, "image/gif"))
        self.assertIn("max-age", p.headers["cache-control"])
        self.assertEqual(self.c.get("/api/v1/emoji/preview/emojigg/..%2Fx").status_code, 404)

        r = self.c.post("/api/v1/emoji/add", json={"source": "emojigg", "id": item["id"], "name": "party_parrot"})
        self.assertEqual(r.status_code, 200, r.text)
        added = r.json()
        self.assertTrue(added["text"].startswith("<a:party_parrot:"))
        self.assertEqual(self.c.get("/api/v1/discord/emojis/slots").json()["animated"], 1)
        self.assertEqual(self.c.post("/api/v1/emoji/add", json={"source": "emojigg", "id": item["id"],
                                                                 "name": "party_parrot"}).status_code, 409)
        self.assertEqual(self.c.post("/api/v1/emoji/add", json={"source": "emojigg", "id": item["id"],
                                                                 "name": "no spaces"}).status_code, 422)

        # A reaction post using it follows a rename; deleting it is refused while the post uses it.
        db = self.h.app.state.db
        with db.session() as s:
            post = ReactionPost(title="Pick", content="x", options=[{"emoji": added["text"], "role_id": "1"}])
            s.add(post)
            s.flush()
            s.add(Reaction(post_id=post.id, discord_user_id="5", emoji=added["text"]))
        r = self.c.patch(f"/api/v1/discord/emojis/{added['id']}", json={"name": "parrot"})
        self.assertEqual(r.status_code, 200, r.text)
        with db.session() as s:
            self.assertEqual(s.scalar(select(ReactionPost)).options[0]["emoji"], f"<a:parrot:{added['id']}>")
            self.assertEqual(s.scalar(select(Reaction)).emoji, f"<a:parrot:{added['id']}>")
        r = self.c.delete(f"/api/v1/discord/emojis/{added['id']}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Pick", r.json()["detail"])
        with db.session() as s:
            s.delete(s.scalar(select(ReactionPost)))
        self.assertEqual(self.c.delete(f"/api/v1/discord/emojis/{added['id']}").status_code, 204)
        self.assertEqual(self.c.get("/api/v1/discord/emojis").json(), [])
        with db.session() as s:
            actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.target_type == "emoji"))]
        self.assertEqual(actions, ["discord_emoji_added", "discord_emoji_renamed", "discord_emoji_deleted"])

    def test_admin_only(self):
        anon = self.h.new_client()
        self.assertEqual(anon.get("/api/v1/emoji/search", params={"source": "openmoji"}).status_code, 401)
        self.assertEqual(anon.get("/api/v1/emoji/preview/openmoji/1F300").status_code, 401)


class BotEmojiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.svc = DiscordService(self.h.app.state.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda *a: None, plex=None)
        self.emojis = []

        async def create(name, image, reason):
            e = SimpleNamespace(id=777, name=name, animated=image.startswith(b"GIF"), url="u", available=True)
            self.emojis.append(e)
            return e

        self.guild = SimpleNamespace(emoji_limit=2, emojis=self.emojis, create_custom_emoji=mock.AsyncMock(side_effect=create))

    def tearDown(self):
        self.h.close()

    def run_(self, coro_fn):
        with mock.patch.object(self.svc, "_guild", return_value=self.guild), mock.patch.object(self.svc, "require"):
            return asyncio.run(coro_fn())

    def test_add_checks_slots_and_names(self):
        out = self.run_(lambda: self.svc.add_emoji("cat", png(), False, "r"))
        self.assertEqual(out["name"], "cat")
        with self.assertRaisesRegex(DiscordConfigError, "already"):
            self.run_(lambda: self.svc.add_emoji("cat", png(), False, "r"))
        self.emojis.append(SimpleNamespace(id=1, name="dog", animated=False, url="u", available=True))
        with self.assertRaisesRegex(DiscordConfigError, "no free static"):
            self.run_(lambda: self.svc.add_emoji("fox", png(), False, "r"))
        self.run_(lambda: self.svc.add_emoji("fox", gif(), True, "r"))  # animated slots are separate

    def test_needs_the_feature(self):
        with self.assertRaises(DiscordConfigError):
            asyncio.run(self.svc.add_emoji("cat", png(), False, "r"))
