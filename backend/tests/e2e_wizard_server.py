"""Backend for the Playwright setup-wizard tests: real app, fake plex.tv and Discord.

Test-only (lives under tests/). Plex OAuth "completes" on the second poll and
sends the popup straight back to /setup; Discord REST and the gateway are
stubbed. Nothing leaves the machine.
"""

import os
from types import SimpleNamespace

import uvicorn

from tests.fake_plextv import FakePlexTV
from tests.test_integrations import OWNER, FakeAccounts
from woosarr.core.config import Settings
from woosarr.main import create_app
from woosarr.services import discord_bot
from woosarr.services.plex import PlexService


class AutoPin:
    def __init__(self, oauth=False, headers=None):
        self.token, self.expired, self._polls = None, False, 0

    def oauthUrl(self, forward=None):  # plexapi's method name
        return forward or "about:blank"

    def checkLogin(self):
        self._polls += 1
        if self._polls >= 2:
            self.token = OWNER
        return self.token is not None


async def fake_validate(token):
    if token.startswith("bad"):
        raise discord_bot.DiscordConfigError("Discord rejected this bot token.")
    return discord_bot.BotInfo("42", "TestBot", "4242", members_intent=True,
                               invite_url=discord_bot.invite_url("4242"))


async def fake_guilds(_token):
    return [{"id": "709961910967206019", "name": "Test Guild"}]


async def fake_start(self):
    _bot, guild = self.config()
    if self._token() and guild:
        self._client = SimpleNamespace(is_ready=lambda: True, close=lambda: None)
        self.connected(guild["name"])


async def fake_stop(self):
    self._client, self._ready_at = None, None


def main() -> None:
    discord_bot.validate_token = fake_validate
    discord_bot.list_guilds = fake_guilds
    discord_bot.DiscordService.start = fake_start
    discord_bot.DiscordService.stop = fake_stop
    settings = Settings()
    app = create_app(settings)
    tv = FakePlexTV()
    tv.shares["11"] = {"id": 7, "sections": {101}}
    plex = PlexService(app.state.db, app.state.secrets, pin_factory=AutoPin, account_factory=FakeAccounts(tv))
    app.state.plex = plex
    app.state.plex_status = plex.status
    app.state.sync._gateway = plex.gateway
    app.state.discord._plex = plex
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WOOSARR_PORT", "31340")), log_config=None)


if __name__ == "__main__":
    main()
