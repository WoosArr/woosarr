"""Requests through Seerr (P13-01): client, member mapping, submit, status polling,
the configurable command name, the Discord flow and the admin API."""

import asyncio
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select

from tests.fake_seerr import KEY, FakeSeerr
from tests.helpers import AppHarness
from woosarr.core import app_settings, redaction
from woosarr.core import secret_store as names
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import AuditLog, DiscordLink, DiscordMember, MediaRequest, PlexUser, Secret
from woosarr.db.session import Database, make_engine, migrate
from woosarr.services import discord_requests
from woosarr.services.discord_bot import DiscordService, WooSarrBot
from woosarr.services.media_requests import RequestDenied, RequestService
from woosarr.services.seerr import SeerrClient, SeerrError, normalize_url, request_state

GUILD = 709961910967206019
URL = "http://seerr.local:5055"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.secrets = SecretStore(SecretBox(Fernet.generate_key()))
        self.seerr = FakeSeerr()
        self.alice_seerr = self.seerr.add_user(plex_id=11, email="alice@x", plex_username="alice")
        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="11", username="alice", email="alice@x"),
                       PlexUser(plex_user_id="12", username="bob", email="bob@x"),
                       PlexUser(plex_user_id="13", username="carol", email="carol@x")])
            s.flush()
            s.add_all([DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True,
                                   linked_by="self"),
                       DiscordLink(plex_user_id="12", discord_user_id="502", method="username", verified=False,
                                   linked_by="self"),
                       DiscordLink(plex_user_id="13", discord_user_id="503", method="oauth", verified=True,
                                   linked_by="self")])
            s.add_all([DiscordMember(discord_user_id="501", username="alice", role_ids=["900"]),
                       DiscordMember(discord_user_id="503", username="carol", role_ids=[])])
        self.svc = RequestService(self.db, self.secrets, session_factory=self.seerr.session)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def connect(self):
        return self.svc.save(URL, KEY)

    def settings(self, **changes):
        with self.db.session() as s:
            app_settings.update(s, changes)


class ClientTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(normalize_url(" http://h:5055/ "), "http://h:5055")
        self.assertEqual(normalize_url("https://h/seerr/api/v1/"), "https://h/seerr")
        for bad in ("h:5055", "ftp://h", "http://", "http://h/?x=1"):
            with self.assertRaises(SeerrError):
                normalize_url(bad)

    def test_search_is_strictly_encoded_and_skips_people(self):
        fake = FakeSeerr()
        hits = SeerrClient(URL, KEY, session=fake).search("inception (2010)!")
        self.assertEqual(hits, [])  # nothing matches, but Seerr accepted the encoded query
        hits = SeerrClient(URL, KEY, session=fake).search("Breaking Bad")
        self.assertEqual([(h.media_type, h.tmdb_id, h.title, h.year) for h in hits], [("tv", 1396, "Breaking Bad",
                                                                                       "2008")])
        self.assertIn("query=Breaking%20Bad", fake.calls[-1][2]["raw_query"])

    def test_bad_key_and_unreachable(self):
        fake = FakeSeerr()
        with self.assertRaisesRegex(SeerrError, "API key"):
            SeerrClient(URL, "wrong-key-wrong-key", session=fake).me()
        fake.down = True
        with self.assertRaisesRegex(SeerrError, "Can't reach Seerr"):
            SeerrClient(URL, KEY, session=fake).server_status()

    def test_label_keeps_status_note_when_shortening(self):
        from woosarr.services.seerr import Hit

        long = Hit("movie", 1, "A" * 150, "2020", "", None, "partially_available")
        label = long.label()
        self.assertEqual(len(label), 100)
        self.assertTrue(label.startswith("\N{POPCORN} AAA"))
        self.assertTrue(label.endswith("… (2020) · partly available"))
        self.assertEqual(len(long.label(97)), 97)
        self.assertTrue(long.label(97).endswith("· partly available"))
        show = Hit("tv", 2, "Short", "", "", None, "available")
        self.assertEqual(show.label(), "\N{TELEVISION} Short · available")

    def test_request_state(self):
        self.assertEqual(request_state({"status": 1, "media": {"status": 2}}), "pending")
        self.assertEqual(request_state({"status": 2, "media": {"status": 3}}), "approved")
        self.assertEqual(request_state({"status": 3, "media": {"status": 1}}), "declined")
        self.assertEqual(request_state({"status": 2, "media": {"status": 4}}), "partially_available")
        self.assertEqual(request_state({"status": 5, "media": {"status": 5}}), "available")
        self.assertEqual(request_state({"status": 4}), "failed")


class UntrustedTextTests(unittest.TestCase):
    """TMDB titles/overviews (editable by anyone on TMDB) must not form links or mentions."""

    def test_safe_neutralizes_links_mentions_and_markdown(self):
        from woosarr.services.discord_text import safe

        for hostile in ("](https://evil.example) [x", "[free](https://evil.example)", "<@&123> @everyone @here",
                        "**bold** __u__ ~~s~~ `c` ||spoiler||", "line1\n# Heading\n> quote"):
            out = safe(hostile)
            self.assertNotRegex(out, r"(?<!\\)[\[\]()<]", hostile)  # every bracket and < escaped
            self.assertNotIn("\n", out)
            self.assertNotIn("@everyone", out)
            self.assertNotIn("@here", out)
            for ch in "*_~`|":
                self.assertNotRegex(out, rf"(?<!\\){re.escape(ch)}", hostile)
        self.assertEqual(safe("x" * 50, 10), "xxxxxxxxx…")

    def test_hostile_title_in_pick_list_and_notices(self):
        from woosarr.services.seerr import Hit

        evil = Hit("movie", 1, "Fine](https://evil.example) [x", "2020", "[click](https://evil.example)", None,
                   "unknown")
        line = discord_requests.pick_list([evil]).description
        self.assertTrue(line.endswith("](https://www.themoviedb.org/movie/1)"))
        self.assertIn("\\]\\(https://evil.example\\)", line)  # inert text, not a second link

    def test_urls_from_seerr_validated(self):
        from woosarr.services.seerr import _hit

        ok = _hit({"mediaType": "movie", "id": 1, "title": "x", "posterPath": "/abc_DEF-1.jpg", "releaseDate": "2020-01-01"})
        self.assertEqual(ok.poster, "https://image.tmdb.org/t/p/w342/abc_DEF-1.jpg")
        for bad in ("@evil.example/x.jpg", "/../../x.jpg", "//evil.example/x.jpg", "/x.jpg?y", "/x.svg"):
            self.assertIsNone(_hit({"mediaType": "movie", "id": 1, "title": "x", "posterPath": bad}).poster, bad)
        self.assertEqual(_hit({"mediaType": "movie", "id": 1, "title": "x", "releaseDate": "](x"}).year, "")


class SettingsTests(unittest.TestCase):
    def test_command_name_defaults_and_rules(self):
        self.assertEqual(app_settings.AppSettings().request_command_name, "request")
        for ok in ("plexrequest", "req", "woosarr-request", "a_b"):
            self.assertEqual(app_settings.AppSettings(request_command_name=ok).request_command_name, ok)
        for bad in ("Request", "my request", "/request", "", "x" * 33, "link", "unlink", "invite", "help"):
            with self.assertRaises(ValidationError, msg=bad):
                app_settings.AppSettings(request_command_name=bad)

    def test_public_needs_a_channel(self):
        self.assertEqual(app_settings.AppSettings().request_visibility, "private")
        with self.assertRaises(ValidationError):
            app_settings.AppSettings(request_visibility="public")
        s = app_settings.AppSettings(request_visibility="public", request_channel_id="123456789",
                                     request_channel_only=True)
        self.assertTrue(s.request_channel_only)


class ConnectionTests(Base):
    def test_save_stores_key_encrypted_and_status(self):
        info = self.connect()
        self.assertEqual((info["version"], info["users"], info["plex_users"]), ("2.7.3", 2, 2))
        with self.db.session() as s:
            raw = s.get(Secret, names.SEERR_API_KEY).ciphertext
        self.assertNotIn(KEY, raw)
        st = self.svc.status()
        self.assertEqual((st["configured"], st["connected"], st["url"]), (True, True, URL))
        self.assertTrue(self.svc.configured())

    def test_key_registered_for_redaction(self):
        self.connect()
        self.assertNotIn(KEY, redaction.redact(f"X-Api-Key: {KEY}"))

    def test_bad_key_not_saved(self):
        with self.assertRaises(SeerrError):
            self.svc.save(URL, "wrong-key-wrong-key")
        self.assertFalse(self.svc.configured())

    def test_test_reuses_stored_key_and_disconnect(self):
        self.connect()
        self.assertEqual(self.svc.test(URL + "/", None)["url"], URL)
        self.svc.disconnect()
        self.assertFalse(self.svc.configured())
        self.assertIsNone(self.svc.client())


class RequesterTests(Base):
    def setUp(self):
        super().setUp()
        self.connect()

    def who(self, uid, admin=False):
        with self.db.session() as s:
            return self.svc.requester(s, uid, admin)[1]

    def test_linked_verified_member_maps_by_plex_id(self):
        self.assertEqual(self.who("501").seerr_user_id, self.alice_seerr)

    def test_maps_by_email_then_username(self):
        with self.db.session() as s:
            s.get(PlexUser, "13").email = "c@x"
        uid = self.seerr.add_user(plex_id=None, email="c@x")
        self.assertEqual(self.who("503").seerr_user_id, uid)

    def test_not_linked_unverified_and_roles(self):
        with self.assertRaisesRegex(RequestDenied, "/link"):
            self.who("999")
        with self.assertRaisesRegex(RequestDenied, "verified"):
            self.who("502")
        self.settings(request_require_verified=False)
        self.seerr.add_user(plex_id=12, email="bob@x", plex_username="bob")
        self.assertTrue(self.who("502").seerr_user_id)
        self.settings(request_all_members=False, request_role_ids=["900"])
        self.assertEqual(self.who("501").seerr_user_id, self.alice_seerr)
        with self.assertRaisesRegex(RequestDenied, "roles"):
            self.who("503")
        self.seerr.plex_friends["13"] = {"username": "carol", "email": "carol@x"}
        self.assertTrue(self.who("503", admin=True).seerr_user_id)  # Discord admins skip the role check

    def test_imports_missing_seerr_user_when_allowed(self):
        self.seerr.plex_friends["13"] = {"username": "carol", "email": "carol@x"}
        self.settings(request_import_users=False)
        with self.assertRaisesRegex(RequestDenied, "Seerr account"):
            self.who("503")
        self.settings(request_import_users=True)
        carol = self.who("503").seerr_user_id
        self.assertEqual(self.seerr.users[carol]["plexId"], 13)

    def test_seerr_down(self):
        self.svc._forget()
        self.seerr.down = True
        with self.assertRaisesRegex(RequestDenied, "isn't answering"):
            self.who("501")
        self.assertIn("Can't reach", self.svc.status()["detail"])


class SubmitTests(Base):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_movie_request_made_as_member(self):
        r = self.svc.submit("501", False, "movie", 27205)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.state, "pending")
        self.assertIn("Inception", r.message)
        post = next(c for c in self.seerr.calls if c[0] == "POST" and c[1] == "/request")
        self.assertEqual(post[2]["headers"]["X-API-User"], str(self.alice_seerr))
        self.assertEqual(post[2]["json"], {"mediaType": "movie", "mediaId": 27205, "is4k": False})
        with self.db.session() as s:
            row = s.scalars(select(MediaRequest)).one()
            self.assertEqual((row.discord_user_id, row.seerr_user_id, row.title, row.status),
                             ("501", self.alice_seerr, "Inception", "pending"))
            self.assertTrue(s.scalar(select(AuditLog).where(AuditLog.action == "request_created")))

    def test_duplicate_quota_permission_and_nothing_left(self):
        self.assertTrue(self.svc.submit("501", False, "movie", 27205).ok)
        dup = self.svc.submit("501", False, "movie", 27205)
        self.assertFalse(dup.ok)
        self.assertIn("already requested", dup.message)
        self.seerr.quota_exceeded.add(self.alice_seerr)
        self.assertIn("Quota", self.svc.submit("501", False, "movie", 157336).message)
        self.seerr.quota_exceeded.clear()
        self.seerr.users[self.alice_seerr]["can_request"] = False
        self.assertIn("permission", self.svc.submit("501", False, "movie", 157336).message)
        self.seerr.users[self.alice_seerr]["can_request"] = True
        self.assertTrue(self.svc.submit("501", False, "tv", 1396).ok)  # all seasons
        again = self.svc.submit("501", False, "tv", 1396)
        self.assertFalse(again.ok)
        self.assertIn("already requested or available", again.message)
        with self.db.session() as s:
            self.assertEqual(len(s.scalars(select(MediaRequest)).all()), 2)

    def test_tv_seasons(self):
        r = self.svc.submit("501", False, "tv", 1396, [2, 1])
        self.assertTrue(r.ok)
        body = next(c for c in self.seerr.calls if c[1] == "/request")[2]["json"]
        self.assertEqual(body["seasons"], [1, 2])
        details = self.svc.details("tv", 1396)
        self.assertEqual([s.number for s in details.seasons], [1, 2, 3, 4, 5])  # specials left out
        self.assertEqual(discord_requests.requestable_seasons(details), [3, 4, 5])

    def test_denied_is_audited(self):
        r = self.svc.submit("999", False, "movie", 27205)
        self.assertFalse(r.ok)
        with self.db.session() as s:
            self.assertTrue(s.scalar(select(AuditLog).where(AuditLog.action == "request_denied")))


class PollTests(Base):
    def setUp(self):
        super().setUp()
        self.connect()
        self.svc.submit("501", False, "movie", 27205)
        self.rid = max(self.seerr.requests)

    def test_status_changes_notify_once(self):
        self.assertEqual(self.svc.poll(), [])
        self.seerr.set_request_status(self.rid, status=2, media_status=3)
        notes = self.svc.poll()
        self.assertEqual([(n.discord_user_id, n.text, n.state, n.channel_id) for n in notes],
                         [("501", "Your request for **Inception** (2010) is approved, on its way.", "approved", None)])
        self.assertEqual(self.svc.poll(), [])
        self.seerr.set_request_status(self.rid, media_status=5)
        self.assertIn("available on Plex", self.svc.poll()[0].text)
        self.seerr.set_request_status(self.rid, media_status=3)
        self.assertEqual(self.svc.poll(), [])  # closed requests aren't polled again
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(MediaRequest)).one().status, "available")

    def test_notify_off_and_deleted(self):
        self.settings(request_notify=False)
        self.seerr.set_request_status(self.rid, status=3)
        self.assertEqual(self.svc.poll(), [])
        self.svc.submit("501", False, "movie", 157336)
        del self.seerr.requests[max(self.seerr.requests)]
        self.svc.poll()
        with self.db.session() as s:
            self.assertEqual(sorted(r.status for r in s.scalars(select(MediaRequest))), ["declined", "deleted"])


# ---- Discord ----------------------------------------------------------------------


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def defer(self, **kw):
        self.log.append(("defer", kw))

    async def send_message(self, content=None, **kw):
        self.log.append(("send_message", content, kw))

    async def edit_message(self, **kw):
        self.log.append(("edit_message", kw))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        self.log.append(("followup", content, kw))


class FakeInteraction:
    def __init__(self, user_id=501, admin=False, component=False):
        self.log = []
        perms = SimpleNamespace(manage_guild=admin, administrator=False)
        self.user = SimpleNamespace(id=user_id, guild_permissions=perms)
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)
        self.type = discord.InteractionType.component if component else discord.InteractionType.application_command
        self.channel_id = 1
        self.message = object() if component else None

    async def edit_original_response(self, **kw):
        self.log.append(("edit_original", kw))


class DiscordTests(Base):
    def setUp(self):
        super().setUp()
        self.discord = DiscordService(self.db, self.secrets, lambda *a, **k: None, plex=None, requests=self.svc)

    def run_async(self, coro):
        return asyncio.run(coro)

    def registered(self, bot):
        return sorted(c.name for c in bot.tree.get_commands(guild=discord.Object(GUILD)))

    def test_request_command_registered_under_configured_name(self):
        bot = WooSarrBot(self.discord, GUILD)
        with mock.patch.object(bot.tree, "sync", new=mock.AsyncMock()) as sync:
            self.run_async(bot.sync_commands(force=True))
            self.assertEqual(self.registered(bot), ["help", "invite", "link", "unlink"])  # Seerr not set up yet
            self.connect()
            self.run_async(bot.sync_commands())
            self.assertEqual(self.registered(bot), ["help", "invite", "link", "request", "unlink"])
            calls = sync.await_count
            self.run_async(bot.sync_commands())
            self.assertEqual(sync.await_count, calls)  # unchanged: Discord isn't asked again
            self.settings(request_command_name="plexrequest")
            self.run_async(bot.sync_commands())
            self.assertEqual(self.registered(bot), ["help", "invite", "link", "plexrequest", "unlink"])
            self.assertEqual(bot.request_name, "plexrequest")
            self.svc.disconnect()
            self.run_async(bot.sync_commands())
            self.assertEqual(self.registered(bot), ["help", "invite", "link", "unlink"])

    def test_suggestions(self):
        self.connect()
        choices = self.run_async(self.discord.request_suggestions("inter"))
        self.assertEqual([(c.name, c.value) for c in choices], [("\N{POPCORN} Interstellar (2014)",
                                                                 "movie:157336")])
        self.assertEqual(self.run_async(self.discord.request_suggestions("i")), [])
        self.svc._forget()
        self.seerr.down = True
        self.assertEqual(self.run_async(self.discord.request_suggestions("inter")), [])

    def test_suggestions_only_for_members_who_may_request(self):
        self.connect()
        linked, unlinked, unverified = FakeInteraction(501), FakeInteraction(999), FakeInteraction(502)
        self.assertTrue(self.run_async(self.discord.request_suggestions("inter", linked)))
        searches = sum(1 for c in self.seerr.calls if c[1] == "/search")
        self.svc._forget()
        self.assertEqual(self.run_async(self.discord.request_suggestions("inter", unlinked)), [])
        self.assertEqual(self.run_async(self.discord.request_suggestions("inter", unverified)), [])
        self.assertEqual(sum(1 for c in self.seerr.calls if c[1] == "/search"), searches)  # no Seerr call

    def test_search_rate_limit_per_member(self):
        self.connect()
        from woosarr.services import media_requests

        for i in range(media_requests.SEARCHES_PER_WINDOW):
            self.svc.search(f"query {i}", member="501")
        with self.assertRaises(SeerrError) as caught:
            self.svc.search("one more", member="501")
        self.assertEqual(caught.exception.status, 429)
        self.svc.search("cached is free", member="503")  # other members have their own budget
        self.svc.search("query 0", member="501")  # cached results don't count
        it = FakeInteraction(501)
        self.run_async(self.discord.handle_request_command(it, "brand new text"))
        self.assertEqual(it.log[-1][1], "Too many searches; wait a minute and try again.")

    def test_slow_search_gives_no_suggestions(self):
        self.connect()

        def slow(*_a, **_k):
            time.sleep(0.3)
            return []

        with mock.patch.object(discord_requests, "AUTOCOMPLETE_SECONDS", 0.05), \
                mock.patch.object(self.svc, "search", slow):
            self.assertEqual(self.run_async(self.discord.request_suggestions("inter")), [])

    def test_picked_suggestion_shows_details_with_request_button(self):
        self.connect()
        it = FakeInteraction()
        self.run_async(self.discord.handle_request_command(it, "movie:27205"))
        kind, _content, kw = it.log[-1]
        self.assertEqual(kind, "followup")
        self.assertEqual(kw["embed"].title, "Inception (2010)")
        self.assertEqual(kw["embed"].url, "https://www.themoviedb.org/movie/27205")
        view = kw["view"]
        self.assertIsInstance(view, discord_requests.RequestView)
        self.assertFalse(view.button.disabled)
        press = FakeInteraction()
        self.run_async(view._request(press))
        self.assertIn("Requested **Inception** (2010)", press.log[-1][1]["content"])

    def test_typed_text_offers_a_menu_and_denied_member_is_told(self):
        self.connect()
        it = FakeInteraction()
        self.run_async(self.discord.handle_request_command(it, "in"))
        _kind, content, kw = it.log[-1]
        self.assertEqual(content, "Which one? Titles link to TMDB; choose in the menu.")
        self.assertIsInstance(kw["view"], discord_requests.PickView)
        # Numbered TMDB links above the menu; menu options are numbered to match and carry no links.
        listing = kw["embed"].description.splitlines()
        # Brackets are escaped inside the link text (Discord shows "\(" as "(").
        self.assertEqual(listing[0], "1. [\N{POPCORN} Inception \\(2010\\)](https://www.themoviedb.org/movie/27205)")
        self.assertTrue(any("(https://www.themoviedb.org/tv/1396)" in line for line in listing))
        labels = [o.label for o in kw["view"].select.options]
        self.assertTrue(labels[0].startswith("1. "))
        self.assertEqual(len(labels), len(listing))
        nobody = FakeInteraction(user_id=999)
        self.run_async(self.discord.handle_request_command(nobody, "inception"))
        self.assertIn("/link", nobody.log[-1][1])

    def test_show_season_menu_and_unavailable_titles(self):
        self.connect()
        item = self.svc.details("tv", 1396)
        view = discord_requests.RequestView(self.discord, 501, item)
        self.assertEqual(len(view.season_select.options), 5)
        self.assertEqual(view.button.label, "Request all missing seasons")
        links = {b.label: b.url for b in view.children if isinstance(b, discord.ui.Button) and b.url}
        self.assertEqual(links, {"TMDB": "https://www.themoviedb.org/tv/1396",
                                 "TVDB": "https://thetvdb.com/?tab=series&id=81189",
                                 "IMDb": "https://www.imdb.com/title/tt0903747/"})
        movie = discord_requests.RequestView(self.discord, 501, self.svc.details("movie", 27205))
        self.assertEqual([b.label for b in movie.children if isinstance(b, discord.ui.Button) and b.url],
                         ["TMDB", "IMDb"])  # no TVDB for movies
        self.seerr.media[("movie", 27205)] = {"status": 5}
        self.assertFalse(discord_requests.can_request(self.svc.details("movie", 27205)))

    def test_dm_closed(self):
        user = SimpleNamespace(send=mock.AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")))
        self.discord._client = SimpleNamespace(is_ready=lambda: True, get_user=lambda _i: user)
        self.assertFalse(self.run_async(self.discord.send_dm("501", "hi")))
        user.send = mock.AsyncMock()
        self.assertTrue(self.run_async(self.discord.send_dm("501", "hi")))


class FakeMessage:
    def __init__(self, mid, content, embed):
        self.id, self.content, self.embeds, self.edits = mid, content, [embed] if embed else [], []

    async def edit(self, embed=None, **_kw):
        self.edits.append(embed)
        self.embeds = [embed]

    def to_reference(self, fail_if_not_exists=True):
        return ("ref", self.id)


class FakeChannel:
    def __init__(self, cid=424242424242, name="requests", **perms):
        self.id, self.name, self.sent = cid, name, []
        self.perms = {"view_channel": True, "send_messages": True, "embed_links": True,
                      "read_message_history": True} | perms

    def permissions_for(self, _member):
        return SimpleNamespace(**self.perms)

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None, reference=None):
        msg = FakeMessage(9000 + len(self.sent), content, embed)
        self.sent.append({"content": content, "embed": embed, "view": view, "mentions": allowed_mentions,
                          "reference": reference, "message": msg})
        return msg

    async def fetch_message(self, mid):
        return next(x["message"] for x in self.sent if x["message"].id == mid)


class PublicTests(Base):
    def setUp(self):
        super().setUp()
        self.discord = DiscordService(self.db, self.secrets, lambda *a, **k: None, plex=None, requests=self.svc)
        patcher = mock.patch("discord.TextChannel", FakeChannel)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channel = FakeChannel()
        guild = SimpleNamespace(me=object(), get_channel=lambda cid: self.channel if cid == self.channel.id else None)
        self.dms = []
        user = SimpleNamespace(send=mock.AsyncMock(side_effect=lambda text, **_k: self.dms.append(text)))
        self.discord._client = SimpleNamespace(is_ready=lambda: True, guild_id=GUILD, get_guild=lambda _g: guild,
                                               get_user=lambda _i: user)
        self.connect()
        self.settings(request_visibility="public", request_channel_id=str(self.channel.id))

    def run_async(self, coro):
        return asyncio.run(coro)

    def request_inception(self):
        view = discord_requests.RequestView(self.discord, 501, self.svc.details("movie", 27205))
        press = FakeInteraction()
        self.run_async(view._request(press))
        return press.log[-1][1]["content"]

    def test_request_posted_in_channel_mentioning_member(self):
        reply = self.request_inception()
        self.assertIn(f"Posted in <#{self.channel.id}>.", reply)
        post = self.channel.sent[0]
        self.assertEqual(post["content"], "<@501> requested **Inception** (2010)")
        self.assertEqual([u.id for u in post["mentions"].users], [501])
        self.assertFalse(post["mentions"].everyone)
        status = next(f for f in post["embed"].fields if f.name == "Status")
        self.assertEqual(status.value, "Waiting for approval")
        self.assertEqual([b.label for b in post["view"].children], ["TMDB", "IMDb"])  # link buttons only
        with self.db.session() as s:
            row = s.scalars(select(MediaRequest)).one()
            self.assertEqual((row.channel_id, row.message_id), (str(self.channel.id), "9000"))
        self.assertTrue(self.c_list()[0]["public"])

    def c_list(self):
        from woosarr.services import media_requests
        with self.db.session() as s:
            return media_requests.recent(s)

    def test_status_change_replies_to_post_and_updates_it(self):
        self.request_inception()
        self.seerr.set_request_status(max(self.seerr.requests), status=2, media_status=5)
        notice = self.svc.poll()[0]
        self.assertTrue(self.run_async(self.discord.send_notice(notice)))
        reply = self.channel.sent[1]
        self.assertEqual(reply["content"], "<@501> Your request for **Inception** (2010) is available on Plex.")
        self.assertEqual(reply["reference"], ("ref", 9000))
        edited = self.channel.sent[0]["message"].edits[0]
        self.assertEqual(next(f for f in edited.fields if f.name == "Status").value, "Available on Plex")
        self.assertEqual(self.dms, [])

    def test_private_requests_and_unusable_channel_fall_back(self):
        self.settings(request_visibility="private")
        self.assertNotIn("Posted in", self.request_inception())
        self.assertEqual(self.channel.sent, [])
        self.seerr.set_request_status(max(self.seerr.requests), status=3)
        self.run_async(self.discord.send_notice(self.svc.poll()[0]))
        self.assertEqual(len(self.dms), 1)  # private -> DM
        self.settings(request_visibility="public")
        self.channel.perms["embed_links"] = False
        self.assertNotIn("Posted in", self.request_interstellar())  # can't embed: request still made

    def request_interstellar(self):
        view = discord_requests.RequestView(self.discord, 501, self.svc.details("movie", 157336))
        press = FakeInteraction()
        self.run_async(view._request(press))
        return press.log[-1][1]["content"]

    def test_channel_only(self):
        self.settings(request_channel_only=True)
        it = FakeInteraction()
        it.channel_id = 1
        self.run_async(self.discord.handle_request_command(it, "movie:27205"))
        self.assertEqual(it.log[-1][1], f"Please make requests in <#{self.channel.id}>.")
        ok = FakeInteraction()
        ok.channel_id = self.channel.id
        self.run_async(self.discord.handle_request_command(ok, "movie:27205"))
        self.assertIsInstance(ok.log[-1][2]["view"], discord_requests.RequestView)
        self.settings(request_channel_only=False)
        anywhere = FakeInteraction()
        anywhere.channel_id = 1
        self.run_async(self.discord.handle_request_command(anywhere, "movie:27205"))
        self.assertIsInstance(anywhere.log[-1][2]["view"], discord_requests.RequestView)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.seerr = FakeSeerr()
        self.h.app.state.requests._session_factory = self.seerr.session

    def tearDown(self):
        self.h.close()

    def test_connect_list_disconnect(self):
        r = self.c.get("/api/v1/requests/seerr")
        self.assertEqual(r.json()["configured"], False)
        r = self.c.post("/api/v1/requests/seerr/test", json={"url": URL, "api_key": KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], "2.7.3")
        self.assertFalse(self.c.get("/api/v1/requests/seerr").json()["configured"])  # test doesn't save
        bad = self.c.put("/api/v1/requests/seerr", json={"url": URL, "api_key": "wrong-key-wrong-key"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("API key", bad.json()["detail"])
        r = self.c.put("/api/v1/requests/seerr", json={"url": URL, "api_key": KEY})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["configured"], body["url"]), (True, URL))
        self.assertNotIn(KEY, r.text)
        self.assertEqual(self.c.put("/api/v1/requests/seerr", json={"url": URL + "/"}).status_code, 200)  # keep key
        self.assertEqual(self.c.get("/api/v1/requests").json(), [])
        self.assertEqual(self.c.post("/api/v1/requests/poll").json(), {"notified": 0})
        self.assertFalse(self.c.delete("/api/v1/requests/seerr").json()["configured"])

    def test_public_without_channel_rejected(self):
        self.assertEqual(self.c.patch("/api/v1/settings", json={"request_visibility": "public"}).status_code, 422)
        r = self.c.patch("/api/v1/settings", json={"request_visibility": "public", "request_channel_id": "123456789",
                                                   "request_channel_only": True})
        self.assertEqual(r.status_code, 200, r.text)

    def test_settings_reject_reserved_command_name(self):
        r = self.c.patch("/api/v1/settings", json={"request_command_name": "invite"})
        self.assertEqual(r.status_code, 422)
        r = self.c.patch("/api/v1/settings", json={"request_command_name": "plexrequest"})
        self.assertEqual(r.json()["request_command_name"], "plexrequest")
