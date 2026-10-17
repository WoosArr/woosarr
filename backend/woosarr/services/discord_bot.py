"""Discord bot: lifecycle, guild cache, events, `/link` (plan P4-01..P4-05, P9-01, D6).

The bot token is entered once in the web UI, validated against the Discord
API, and stored encrypted. The bot runs in the API's event loop; every DB or
Plex call it makes runs in a worker thread so the gateway heartbeat never
stalls. Start/stop/restart happen without restarting the container.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord
from discord import app_commands

from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.db.session import Database
from woosarr.services import discord_cache as cache
from woosarr.services import invites, link_service

log = logging.getLogger("woosarr.discord")

# WooSarr never uses voice; don't warn about missing voice libraries on every start.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

API = "https://discord.com/api/v10"
K_GUILD = "discord.guild"  # {"id", "name"}
K_BOT = "discord.bot"  # {"id", "username", "application_id"}
# Application flags: the Server Members intent is enabled (or "limited" for unverified bots).
FLAG_GATEWAY_GUILD_MEMBERS = 1 << 14
FLAG_GATEWAY_GUILD_MEMBERS_LIMITED = 1 << 15
LINK_POLL_SECONDS = 2
LINK_TIMEOUT_SECONDS = 300


class DiscordConfigError(Exception):
    """Token or guild problem the admin must fix; message is safe to show."""


@dataclass(frozen=True)
class BotInfo:
    bot_user_id: str
    username: str
    application_id: str
    members_intent: bool
    invite_url: str


def invite_url(application_id: str) -> str:
    # No channel permissions needed: the bot reads roles/members (intent) and answers /link.
    return (f"https://discord.com/oauth2/authorize?client_id={application_id}"
            "&scope=bot%20applications.commands&permissions=0")


async def _get(session: aiohttp.ClientSession, token: str, path: str) -> Any:
    async with session.get(f"{API}{path}", headers={"Authorization": f"Bot {token}"}) as r:
        if r.status == 401:
            raise DiscordConfigError("Discord rejected this bot token. Copy it again from the Developer Portal (Bot → Reset Token).")
        if r.status >= 400:
            raise DiscordConfigError(f"Discord API error {r.status} for {path}")
        return await r.json()


async def validate_token(token: str) -> BotInfo:
    token = token.strip()
    if len(token) < 50 or " " in token:
        raise DiscordConfigError("That doesn't look like a bot token.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        me = await _get(session, token, "/users/@me")
        app = await _get(session, token, "/applications/@me")
    flags = int(app.get("flags") or 0)
    members = bool(flags & (FLAG_GATEWAY_GUILD_MEMBERS | FLAG_GATEWAY_GUILD_MEMBERS_LIMITED))
    return BotInfo(str(me["id"]), me.get("username", ""), str(app["id"]), members, invite_url(str(app["id"])))


async def list_guilds(token: str) -> list[dict[str, str]]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        guilds = await _get(session, token, "/users/@me/guilds")
    return [{"id": str(g["id"]), "name": g.get("name", "")} for g in guilds]


def _role(r: discord.Role) -> cache.RoleData:
    return cache.RoleData(str(r.id), r.name, int(r.color.value), int(r.position), bool(r.managed))


def _member(m: discord.Member) -> cache.MemberData:
    roles = tuple(str(r.id) for r in m.roles if not r.is_default())
    return cache.MemberData(str(m.id), m.name, m.display_name, roles)


class WooSarrBot(discord.Client):
    def __init__(self, service: DiscordService, guild_id: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.service = service
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        guild = discord.Object(id=self.guild_id)

        @self.tree.command(name="link", description="Link your Discord account to your Plex account", guild=guild)
        async def link(interaction: discord.Interaction) -> None:
            await self.service.handle_link_command(interaction)

        @self.tree.command(name="unlink", description="Unlink your Discord account from your Plex account",
                           guild=guild)
        async def unlink(interaction: discord.Interaction) -> None:
            await self.service.handle_unlink_command(interaction)

        @self.tree.command(name="invite", description="Invite a Plex account to this server", guild=guild)
        @app_commands.describe(email="Email or Plex username to invite",
                               member="Admins only: the Discord member this invite is for (default: you)")
        async def invite(interaction: discord.Interaction, email: str, member: discord.Member | None = None) -> None:
            await self.service.handle_invite_command(interaction, email, member)

    async def setup_hook(self) -> None:
        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        log.info("discord /link registered in guild %s", self.guild_id)

    def _ours(self, guild: discord.Guild | None) -> bool:
        return guild is not None and guild.id == self.guild_id

    async def on_ready(self) -> None:
        guild = self.get_guild(self.guild_id)
        if guild is None:
            self.service.set_error("The bot isn't in the selected Discord server. Invite it with the link on the Setup page.")
            return
        log.info("discord connected as %s to guild %s (%s members)", self.user, guild.name, guild.member_count)
        roles = [_role(r) for r in guild.roles if not r.is_default()]
        members = [_member(m) async for m in guild.fetch_members(limit=None)]
        await self.service.run_db(cache.full_sync, roles, members)
        self.service.connected(guild.name)
        self.service.trigger(None, "discord connected")

    async def on_resumed(self) -> None:
        log.info("discord session resumed")

    async def on_disconnect(self) -> None:
        log.warning("discord gateway disconnected; discord.py reconnects automatically")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._ours(after.guild):
            return
        if {r.id for r in before.roles} == {r.id for r in after.roles} and before.display_name == after.display_name:
            return
        pid = await self.service.run_db(cache.member_changed, _member(after))
        if pid:
            self.service.trigger([pid], f"discord roles changed for {after.id}")

    async def on_member_join(self, member: discord.Member) -> None:
        if self._ours(member.guild):
            pid = await self.service.run_db(cache.member_changed, _member(member))
            if pid:
                self.service.trigger([pid], f"discord member joined {member.id}")

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        if payload.guild_id == self.guild_id:
            pid = await self.service.run_db(cache.member_left, str(payload.user.id))
            if pid:
                self.service.trigger([pid], f"discord member left {payload.user.id}")

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            await self.service.run_db(cache.upsert_role, _role(role))

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if self._ours(after.guild):
            await self.service.run_db(cache.upsert_role, _role(after))

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            affected = await self.service.run_db(cache.role_deleted, str(role.id))
            if affected:
                self.service.trigger(affected, f"discord role {role.id} deleted")


class _UsernameModal(discord.ui.Modal, title="Link with your Plex username"):
    name = discord.ui.TextInput(label="Plex username or email", max_length=255)

    def __init__(self, service: DiscordService) -> None:
        super().__init__(timeout=300)
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = await self.service.run_db(link_service.link_by_username, str(interaction.user.id), str(self.name.value))
        if result.ok and result.plex_user_id:
            self.service.trigger([result.plex_user_id], f"self-link by discord {interaction.user.id}")
        await interaction.response.send_message(result.message, ephemeral=True)


class _LinkView(discord.ui.View):
    def __init__(self, service: DiscordService) -> None:
        super().__init__(timeout=300)
        self.service = service

    @discord.ui.button(label="Enter Plex username", style=discord.ButtonStyle.secondary)
    async def by_username(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_UsernameModal(self.service))

    @discord.ui.button(label="Sign in with Plex (verified)", style=discord.ButtonStyle.primary)
    async def by_oauth(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.start_oauth_link(interaction)


class _UnlinkView(discord.ui.View):
    def __init__(self, service: DiscordService, discord_id: str) -> None:
        super().__init__(timeout=120)
        self.service = service
        self.discord_id = discord_id

    @discord.ui.button(label="Unlink", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("This isn't your /unlink.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await asyncio.to_thread(self.service.unlink_member, self.discord_id)
        await interaction.followup.send(message, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message("Nothing changed.", ephemeral=True)
        self.stop()


class DiscordService:
    def __init__(self, db: Database, secrets: Any, trigger: Callable[..., None], plex: Any,
                 sync: Any = None) -> None:
        self._db = db
        self._secrets = secrets
        self.trigger = trigger
        self._plex = plex
        self._sync = sync
        self._client: WooSarrBot | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._error: str | None = None
        self._guild_name: str | None = None
        self._ready_at: float | None = None

    # ---- helpers ---------------------------------------------------------------

    async def run_db(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run fn(session, *args) in a worker thread inside one DB transaction."""
        def work() -> Any:
            with self._db.session() as s:
                return fn(s, *args)
        return await asyncio.to_thread(work)

    def set_error(self, message: str) -> None:
        self._error = message
        self._ready_at = None
        log.error("discord: %s", message)

    def connected(self, guild_name: str) -> None:
        self._error, self._guild_name, self._ready_at = None, guild_name, time.time()

    def _token(self) -> str | None:
        with self._db.session() as s:
            try:
                return self._secrets.get(s, names.DISCORD_BOT_TOKEN)
            except Exception:
                log.exception("stored Discord token can't be read; enter it again")
                return None

    def config(self) -> tuple[dict | None, dict | None]:
        with self._db.session() as s:
            return kv.get(s, K_BOT), kv.get(s, K_GUILD)

    def status(self) -> dict[str, Any]:
        bot, guild = self.config()
        has_token = self._token() is not None
        ready = bool(self._client and self._client.is_ready() and self._ready_at)
        if not has_token:
            detail = "Bot token not set"
        elif not guild:
            detail = "Choose the Discord server"
        elif self._error:
            detail = self._error
        elif not ready:
            detail = "Connecting…"
        else:
            detail = None
        return {
            "configured": bool(has_token and guild),
            "connected": ready,
            "bot": bot,
            "guild": {"id": guild["id"], "name": self._guild_name or guild.get("name")} if guild else None,
            "invite_url": invite_url(bot["application_id"]) if bot else None,
            "detail": detail,
        }

    # ---- configuration (P9-01) ---------------------------------------------------

    async def set_token(self, token: str) -> BotInfo:
        info = await validate_token(token)
        if not info.members_intent:
            raise DiscordConfigError(
                "Turn on the Server Members Intent: Developer Portal → your app → Bot → Privileged Gateway Intents.")
        def save(s):
            self._secrets.set(s, names.DISCORD_BOT_TOKEN, token.strip())
            kv.put(s, K_BOT, {"id": info.bot_user_id, "username": info.username, "application_id": info.application_id})
        await self.run_db(save)
        log.info("discord bot token saved for %s (app %s)", info.username, info.application_id)
        await self.restart()
        return info

    async def guilds(self) -> list[dict[str, str]]:
        token = self._token()
        if not token:
            raise DiscordConfigError("Enter the bot token first.")
        return await list_guilds(token)

    async def set_guild(self, guild_id: str) -> dict[str, str]:
        match = next((g for g in await self.guilds() if g["id"] == guild_id), None)
        if match is None:
            raise DiscordConfigError("The bot isn't in that server. Invite it first.")
        await self.run_db(lambda s: kv.put(s, K_GUILD, match))
        log.info("discord guild selected %s (%s)", match["name"], match["id"])
        await self.restart()
        return match

    async def disconnect(self) -> None:
        await self.stop()
        def clear(s):
            self._secrets.delete(s, names.DISCORD_BOT_TOKEN)
            kv.delete(s, K_BOT)
            kv.delete(s, K_GUILD)
        await self.run_db(clear)
        self._error = None
        log.info("discord disconnected and token deleted")

    # ---- lifecycle (P4-01, P4-05) ------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        token = self._token()
        _bot, guild = self.config()
        if not token or not guild:
            return
        if self._task and not self._task.done():
            return
        self._error = None
        self._client = WooSarrBot(self, int(guild["id"]))
        self._task = asyncio.create_task(self._run(self._client, token), name="woosarr-discord")

    async def _run(self, client: WooSarrBot, token: str) -> None:
        try:
            await client.start(token, reconnect=True)
        except discord.PrivilegedIntentsRequired:
            self.set_error("The Server Members Intent is off. Enable it in the Developer Portal (Bot → Privileged "
                           "Gateway Intents), then press Restart.")
        except discord.LoginFailure:
            self.set_error("Discord rejected the bot token. Enter it again on the Setup page.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface anything else in the UI instead of dying silently
            log.exception("discord bot crashed")
            self.set_error(f"Bot stopped: {type(exc).__name__}: {exc}")

    async def stop(self) -> None:
        client, task = self._client, self._task
        self._client, self._task, self._ready_at = None, None, None
        if client is not None:
            await client.close()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ---- /unlink ---------------------------------------------------------------

    async def handle_unlink_command(self, interaction: discord.Interaction) -> None:
        found = await self.run_db(link_service.current_link, str(interaction.user.id))
        if found is None:
            await interaction.response.send_message("Your Discord account isn't linked to a Plex account.",
                                                    ephemeral=True)
            return
        _link, plex = found
        policy = (await self.run_db(app_settings.load)).unlink_policy
        effect = ("Libraries your Discord roles gave you will be removed; manual grants and exempt libraries stay."
                  if policy == "drop_role_access" else "Your Plex access stays as it is, but roles stop updating it.")
        await interaction.response.send_message(
            f"Unlink from Plex account **{plex.username if plex else '?'}**? {effect}",
            view=_UnlinkView(self, str(interaction.user.id)), ephemeral=True)

    def unlink_member(self, discord_id: str) -> str:
        """Self-unlink with the configured policy (runs in a worker thread)."""
        with self._db.session() as s:
            policy = app_settings.load(s).unlink_policy
            found = link_service.current_link(s, discord_id)
        if found is None:
            return "Your Discord account isn't linked anymore."
        link, _plex = found
        plex_id = link.plex_user_id

        def unlink() -> None:
            with self._db.session() as s:
                link_service.unlink_self(s, discord_id, policy)

        if policy == "drop_role_access" and self._sync is not None:
            report = self._sync.drop_role_access(plex_id, f"self-unlink by discord {discord_id}", then=unlink)
            if report.errors:
                return "Unlinked, but removing role access hit an error; an admin will see it in the logs."
            note = " (dry-run: nothing changed in Plex)" if report.dry_run and report.users_changed else ""
            return f"Unlinked. Role-granted libraries were removed{note}."
        unlink()
        return "Unlinked. Your Plex access stays as it is; Discord roles no longer change it."

    # ---- /invite -----------------------------------------------------------------

    async def handle_invite_command(self, interaction: discord.Interaction, email: str,
                                    member: discord.Member | None) -> None:
        actor = interaction.user
        perms = getattr(actor, "guild_permissions", None)
        is_admin = bool(perms and (perms.manage_guild or perms.administrator))
        target = member or actor
        await interaction.response.defer(ephemeral=True, thinking=True)
        gateway = self._sync._gateway() if self._sync is not None else None

        def work() -> invites.InviteResult:
            with self._db.session() as s:
                if isinstance(target, discord.Member):  # make sure the cache knows the target's roles
                    cache.member_changed(s, _member(target))
                return invites.create(s, gateway, actor_discord_id=str(actor.id), actor_is_admin=is_admin,
                                      target_discord_id=str(target.id), identifier=email)

        result = await asyncio.to_thread(work)
        await interaction.followup.send(result.message, ephemeral=True)

    # ---- /link (P4-04) -----------------------------------------------------------

    async def handle_link_command(self, interaction: discord.Interaction) -> None:
        wait = await self.run_db(link_service.cooldown_remaining, str(interaction.user.id))
        if wait:
            await interaction.response.send_message(f"Please wait {wait} s before trying to link again.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Link your Plex account so your roles give you the right libraries.\n"
            "• **Sign in with Plex**: verified. WooSarr only reads your Plex username, then signs that sign-in out.\n"
            "• **Enter Plex username**: quicker, but unverified.",
            view=_LinkView(self),
            ephemeral=True,
        )

    async def start_oauth_link(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        try:
            pin_id, url = await asyncio.to_thread(
                self._plex.start_login, None, "link", f"woosarr-link-{uuid.uuid4()}")
        except Exception:
            log.exception("could not start Plex sign-in for /link")
            await interaction.response.send_message("Plex sign-in is unavailable right now. Try the username option.",
                                                    ephemeral=True)
            return
        view = discord.ui.View(timeout=LINK_TIMEOUT_SECONDS)
        view.add_item(discord.ui.Button(label="Open Plex sign-in", url=url))
        await interaction.response.send_message("Sign in on the Plex page, then come back here. This expires in 5 minutes.",
                                                view=view, ephemeral=True)
        asyncio.create_task(self._finish_oauth_link(interaction, discord_id, pin_id))

    async def _finish_oauth_link(self, interaction: discord.Interaction, discord_id: str, pin_id: str) -> None:
        deadline = time.monotonic() + LINK_TIMEOUT_SECONDS
        token: str | None = None
        while time.monotonic() < deadline:
            state, token = await asyncio.to_thread(self._plex.poll_token, pin_id)
            if state != "pending":
                break
            await asyncio.sleep(LINK_POLL_SECONDS)
        if not token:
            await interaction.followup.send("The Plex sign-in expired. Run `/link` again.", ephemeral=True)
            return
        try:
            identity = await asyncio.to_thread(self._plex.capture_link_identity, token)
        except Exception:
            log.exception("/link: reading or invalidating the Plex sign-in failed; link not completed")
            await interaction.followup.send("Something went wrong with the Plex sign-in. Please try again.", ephemeral=True)
            return
        finally:
            token = None
        result = await self.run_db(link_service.link_by_identity, discord_id, identity)
        if result.ok and result.plex_user_id:
            self.trigger([result.plex_user_id], f"verified self-link by discord {discord_id}")
        await interaction.followup.send(result.message, ephemeral=True)
