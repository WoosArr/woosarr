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
from sqlalchemy import delete, select

from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.db.models import DiscordLink, Reaction, ReactionPost, utcnow
from woosarr.db.session import Database
from woosarr.services import (
    audit,
    discord_avatar,
    discord_requests,
    invites,
    link_service,
    reaction_text,
)
from woosarr.services import discord_cache as cache
from woosarr.services import discord_features as features

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
DISCONNECT_GRACE_SECONDS = 60


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


def help_text(request_name: str | None) -> str:
    """What `/help` answers: every command the bot has registered, with its options."""
    lines = [
        "**WooSarr commands**",
        ("`/link`: link your Discord account to your Plex account. Sign in with Plex (verified) or type your "
         "Plex username. Once linked, your Discord roles decide which Plex libraries you can see."),
        "`/unlink`: remove that link. You're asked to confirm first.",
        ("`/invite email [member]`: send a Plex invite to an email address or Plex username. The libraries come "
         "from your Discord roles. Needs a role the server admins allow; Discord admins can set `member` to "
         "invite for someone else."),
    ]
    if request_name:
        lines.append(f"`/{request_name} title`: request a movie or TV show. Start typing and pick one of the "
                     "suggestions. Needs a linked Plex account.")
    lines.append("`/help`: show this message.")
    return "\n".join(lines)


def _role(r: discord.Role) -> cache.RoleData:
    return cache.RoleData(str(r.id), r.name, int(r.color.value), int(r.position), bool(r.managed))


def _member(m: discord.Member) -> cache.MemberData:
    roles = tuple(str(r.id) for r in m.roles if not r.is_default())
    return cache.MemberData(str(m.id), m.name, m.display_name, roles, is_bot=bool(m.bot))


class _Tree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError | Exception) -> None:
        """Discord drops interactions not answered within 3 s (10062 Unknown interaction). For
        suggestions that's routine (each keystroke replaces the last ask); for a command it's
        worth a warning, but not a traceback."""
        original = getattr(error, "original", error)
        if isinstance(original, discord.NotFound) and original.code == 10062:
            created = getattr(interaction, "created_at", None)
            age = (discord.utils.utcnow() - created).total_seconds() if created else -1
            name = interaction.command.name if interaction.command else "?"
            if interaction.type == discord.InteractionType.autocomplete:
                log.debug("discord: suggestions for /%s dropped by Discord (%.1f s old; the member typed on)",
                          name, age)
            else:
                log.warning("discord: /%s from member %s answered too late (%.1f s after they used it; Discord "
                            "allows 3 s)", name, interaction.user.id, age)
            return
        await super().on_error(interaction, error)


class WooSarrBot(discord.Client):
    def __init__(self, service: DiscordService, guild_id: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_reactions = True  # reaction posts (not privileged)
        # Emoji added, renamed or deleted (in Discord or from the Custom Emoji page) reach the
        # cache right away; without this, guild.emojis stayed as it was at connect time.
        intents.emojis_and_stickers = True  # not privileged
        super().__init__(intents=intents)
        self.service = service
        self.guild_id = guild_id
        self.tree = _Tree(self)
        self.request_name: str | None = None  # registered name of the request command
        self._disconnect_watch: asyncio.Task | None = None
        self._gateway_up = False
        self._commands_lock = asyncio.Lock()
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

        @self.tree.command(name="help", description="Show WooSarr's commands and how to use them", guild=guild)
        async def help_(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(help_text(self.request_name), ephemeral=True)

    async def setup_hook(self) -> None:
        await self.sync_commands(force=True)

    async def sync_commands(self, force: bool = False) -> None:
        """(Re)register the request command under its configured name (None = Seerr not set up)."""
        async with self._commands_lock:
            want = await self.service.run_db(self.service.request_command_name)
            if want == self.request_name and not force:
                return
            guild = discord.Object(id=self.guild_id)
            if self.request_name:
                self.tree.remove_command(self.request_name, guild=guild)
            if want:
                self.tree.add_command(discord_requests.build_command(self.service, want), guild=guild)
            before, self.request_name = self.request_name, want
            await self.tree.sync(guild=guild)
            names = ["link", "unlink", "invite", "help"] + ([want] if want else [])
            log.info("discord commands registered in guild %s: %s%s", self.guild_id,
                     ", ".join(f"/{n}" for n in names),
                     f" (request command was /{before})" if before and before != want else "")

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
        await self.service.reconcile_reactions()
        self.service.trigger(None, "discord connected")
        self.service.schedule_role_sync()

    async def on_connect(self) -> None:
        self._gateway_up = True

    async def on_resumed(self) -> None:
        self._gateway_up = True
        log.info("discord session resumed")

    async def on_disconnect(self) -> None:
        self._gateway_up = False
        # Discord rotates gateway sessions routinely; discord.py resumes within a
        # second or two. Only a disconnect that lasts is worth a warning.
        log.info("discord gateway disconnected; discord.py reconnects automatically")
        if self._disconnect_watch is None or self._disconnect_watch.done():
            self._disconnect_watch = asyncio.create_task(self._warn_if_still_down())

    async def _warn_if_still_down(self) -> None:
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        if not self.is_closed() and not self._gateway_up:
            log.warning("discord gateway still disconnected after %s s; discord.py keeps retrying",
                        DISCONNECT_GRACE_SECONDS)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._ours(after.guild):
            return
        if {r.id for r in before.roles} == {r.id for r in after.roles} and before.display_name == after.display_name:
            return
        pid = await self.service.run_db(cache.member_changed, _member(after))
        if pid:
            self.service.trigger([pid], f"discord roles changed for {after.display_name}")

    async def on_member_join(self, member: discord.Member) -> None:
        if self._ours(member.guild):
            pid = await self.service.run_db(cache.member_changed, _member(member))
            if pid:
                self.service.trigger([pid], f"discord member joined: {member.display_name}")

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        if payload.guild_id == self.guild_id:
            pid = await self.service.run_db(cache.member_left, str(payload.user.id))
            if pid:
                self.service.trigger([pid], f"discord member left: {payload.user.name}")

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            await self.service.run_db(cache.upsert_role, _role(role))

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if self._ours(after.guild):
            await self.service.run_db(cache.upsert_role, _role(after))
            if before.name != after.name:
                self.service.schedule_post_refresh()  # {role_name} in legends

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id == self.guild_id and self.user and payload.user_id != self.user.id:
            await self.service.reaction_changed(str(payload.message_id), str(payload.user_id),
                                                str(payload.emoji), added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id == self.guild_id and self.user and payload.user_id != self.user.id:
            await self.service.reaction_changed(str(payload.message_id), str(payload.user_id),
                                                str(payload.emoji), added=False)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            affected = await self.service.run_db(cache.role_deleted, str(role.id))
            if affected:
                self.service.trigger(affected, f"discord role {role.name} deleted")
            self.service.schedule_post_refresh()


class _UsernameModal(discord.ui.Modal, title="Link with your Plex username"):
    name = discord.ui.TextInput(label="Plex username or email", max_length=255)

    def __init__(self, service: DiscordService) -> None:
        super().__init__(timeout=300)
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await acknowledge(interaction, "Plex username form"):
            return
        result = await self.service.run_db(link_service.link_by_username, str(interaction.user.id), str(self.name.value))
        if result.ok and result.plex_user_id:
            self.service.trigger([result.plex_user_id], f"self-link by {interaction.user.display_name}")
        await interaction.followup.send(result.message, ephemeral=True)


class _LinkView(discord.ui.View):
    def __init__(self, service: DiscordService, allow_username: bool = True) -> None:
        super().__init__(timeout=300)
        self.service = service
        if not allow_username:
            self.remove_item(self.by_username)

    @discord.ui.button(label="Enter Plex username", style=discord.ButtonStyle.secondary)
    async def by_username(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_UsernameModal(self.service))

    @discord.ui.button(label="Sign in with Plex (verified)", style=discord.ButtonStyle.primary)
    async def by_oauth(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.start_oauth_link(interaction)


def reaction_channel_missing(channel: Any, guild: Any, emojis: list[str]) -> list[str]:
    """Permission names the bot lacks in the channel to post a reaction post with these emoji."""
    p = channel.permissions_for(guild.me)
    need = {"View Channel": p.view_channel, "Send Messages": p.send_messages, "Add Reactions": p.add_reactions,
            "Read Message History": p.read_message_history}
    own = {str(e.id) for e in getattr(guild, "emojis", [])}
    external = [e for e in emojis if e.startswith("<") and e.rstrip(">").rsplit(":", 1)[-1] not in own]
    if external:
        need["Use External Emojis"] = getattr(p, "use_external_emojis", False)
    return [name for name, ok in need.items() if not (ok or getattr(p, "administrator", False))]


def option_role_ids(option: dict) -> set[str]:
    """The role a reaction option gives (one per option; older options stored a list)."""
    if option.get("role_id"):
        return {str(option["role_id"])}
    return {str(r) for r in option.get("role_ids") or []}


def option_emojis(option: dict) -> list[str]:
    """The option's emoji, then its former ones (old reactions kept working after an emoji change)."""
    return [normalize_emoji(e) for e in [option.get("emoji", ""), *(option.get("former") or [])] if e]


async def acknowledge(interaction: discord.Interaction, what: str) -> bool:
    """Tell Discord the bot is on it before doing any work: an interaction not answered within 3 s
    is dropped (error 10062) and the member sees "The application did not respond". False if it
    already was; the member has to try again."""
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        return True
    except discord.NotFound:
        created = getattr(interaction, "created_at", None)
        age = (discord.utils.utcnow() - created).total_seconds() if created else -1
        log.warning("discord: %s from member %s answered too late (%.1f s after they used it; Discord allows 3 s); "
                    "they saw \"The application did not respond\"", what, interaction.user.id, age)
        return False


def _emoji_dict(e: discord.Emoji) -> dict[str, object]:
    return {"id": str(e.id), "name": e.name, "animated": bool(e.animated), "text": str(e),
            "url": str(e.url), "available": bool(getattr(e, "available", True))}


def normalize_emoji(text: str) -> str:
    """Canonical form used for storage and comparison (unicode or <:name:id>)."""
    return str(discord.PartialEmoji.from_str(text.strip()))


class _GuildFeatures:
    """Guild-level features of DiscordService: permissions, roles, channels,
    reaction posts, link-status roles (see services/discord_features.py)."""

    def _guild(self) -> discord.Guild:
        if self._client is None or not self._client.is_ready():
            raise DiscordConfigError("The Discord bot isn't connected.")
        guild = self._client.get_guild(self._client.guild_id)
        if guild is None:
            raise DiscordConfigError("The bot isn't in the selected Discord server.")
        return guild

    def _permissions(self) -> tuple[dict[str, bool], dict[str, bool | None]]:
        """(granted permissions, per-feature "bot's role is above the roles it assigns")."""
        try:
            guild = self._guild()
        except DiscordConfigError:
            return {}, {}
        me = guild.me
        perms = me.guild_permissions
        granted = {name: bool(getattr(perms, name, False) or perms.administrator) for name in features.PERMISSION_BITS}
        with self._db.session() as s:
            st = app_settings.load(s)
            reaction_roles = {r for p in s.scalars(select(ReactionPost).where(ReactionPost.enabled.is_(True)))
                              for o in p.options or [] for r in option_role_ids(o)}
        top = me.top_role.position

        def above(role_ids) -> bool | None:
            roles = [guild.get_role(int(r)) for r in role_ids]
            positions = [r.position for r in roles if r is not None]
            return top > max(positions) if positions else None

        link_roles = [r for r in (st.verified_role_id, st.unverified_role_id, st.linked_role_id) if r]
        return granted, {"link_roles": above(link_roles), "reaction_posts": above(reaction_roles)}

    def feature_status(self) -> list[dict[str, object]]:
        granted, hierarchy = self._permissions()
        if not granted:
            # Unknown permissions: features that need none (slash commands) still work.
            return [f if not f["permissions"] else dict(f, available=False, missing=[], notes=["bot not connected"])
                    for f in features.feature_report({}, None)]
        return features.feature_report(granted, hierarchy)

    def require(self, key: str) -> None:
        ok, why = features.available(self.feature_status(), key)
        if not ok:
            raise DiscordConfigError(why)

    async def change_member_roles(self, member_ids: list[str], add: list[str], remove: list[str],
                                  actor: str) -> list[dict[str, Any]]:
        """Admin bulk edit (Users → Discord): give and/or take roles for many members.

        Every role is checked first (exists, not @everyone, not managed by an integration, below the
        bot's highest role) so nothing changes if one can't be used. Members who already have (or
        lack) a role are skipped for it. The member cache is updated right away and their linked
        Plex accounts are synced.
        """
        self.require("assign_roles")
        if set(add) & set(remove):
            raise DiscordConfigError("A role can't be added and removed at the same time.")
        guild = self._guild()
        top = guild.me.top_role.position
        roles: dict[str, discord.Role] = {}
        problems = []
        for rid in dict.fromkeys([*add, *remove]):
            role = guild.get_role(int(rid))
            if role is None:
                problems.append(f"role {rid} isn't in the server")
            elif role.is_default():
                problems.append("@everyone can't be given or taken")
            elif role.managed:
                problems.append(f"{role.name} is managed by a bot or integration")
            elif role.position >= top:
                problems.append(f"{role.name} is above the bot's highest role (move the bot's role up in "
                                "Server Settings → Roles)")
            else:
                roles[rid] = role
        if problems:
            raise DiscordConfigError("; ".join(problems))
        reason = f"WooSarr: changed by {actor}"[:500]
        results: list[dict[str, Any]] = []
        updated: list[cache.MemberData] = []
        for mid in member_ids:
            member = guild.get_member(int(mid))
            if member is None:
                results.append({"discord_user_id": mid, "added": [], "removed": [], "error": "not in the server"})
                continue
            if member.bot:
                results.append({"discord_user_id": mid, "added": [], "removed": [], "error": "bots are skipped"})
                continue
            have = {str(r.id) for r in member.roles}
            to_add = [r for r in add if r not in have]
            to_remove = [r for r in remove if r in have]
            row: dict[str, Any] = {"discord_user_id": mid, "added": [], "removed": [], "error": None}
            try:
                if to_add:
                    await member.add_roles(*[roles[r] for r in to_add], reason=reason)
                    row["added"] = to_add
                if to_remove:
                    await member.remove_roles(*[roles[r] for r in to_remove], reason=reason)
                    row["removed"] = to_remove
            except discord.Forbidden:
                row["error"] = "Discord refused (Manage Roles missing or role hierarchy)"
            except discord.HTTPException as exc:
                row["error"] = f"Discord error {exc.status}"
            if row["added"] or row["removed"]:
                new_roles = (have | set(row["added"])) - set(row["removed"])
                updated.append(cache.MemberData(mid, member.name, member.display_name,
                                                tuple(r for r in new_roles if r != str(guild.id))))
            results.append(row)

        def save(s) -> list[str]:
            pids = [cache.member_changed(s, m) for m in updated]
            audit.record(s, actor, "discord_roles_changed_admin", "discord_role", ",".join(roles)[:100],
                         add=list(add), remove=list(remove), members=len(member_ids),
                         changed=[{k: r[k] for k in ("discord_user_id", "added", "removed")}
                                  for r in results if r["added"] or r["removed"]][:200],
                         errors=[{"discord_user_id": r["discord_user_id"], "error": r["error"]}
                                 for r in results if r["error"]][:200])
            return [p for p in pids if p]

        affected = await self.run_db(save)
        changed = sum(1 for r in results if r["added"] or r["removed"])
        failed = sum(1 for r in results if r["error"])
        log.info("bulk role change by %s: +%s -%s on %s of %s members (%s errors)", actor, add, remove, changed,
                 len(member_ids), failed)
        if affected:
            self.trigger(affected, f"discord roles changed by {actor}")
        return results

    async def create_role(self, name: str, color: int | None = None) -> tuple[str, str]:
        self.require("create_roles")
        guild = self._guild()
        try:
            kwargs = {"colour": discord.Colour(color)} if color is not None else {}
            role = await guild.create_role(name=name[:100], mentionable=False, reason="Created by WooSarr", **kwargs)
        except discord.Forbidden as exc:
            raise DiscordConfigError("Discord refused to create the role (Manage Roles missing?)") from exc
        await self.run_db(cache.upsert_role, _role(role))
        log.info("discord role created %s (%s)", role.name, role.id)
        return str(role.id), role.name

    async def list_emojis(self) -> list[dict[str, object]]:
        """The server's custom emoji, for the reaction post picker."""
        guild = self._guild()
        return [_emoji_dict(e) for e in guild.emojis]

    def emoji_slots(self) -> dict[str, int]:
        guild = self._guild()
        return {"limit": int(guild.emoji_limit), "static": sum(1 for e in guild.emojis if not e.animated),
                "animated": sum(1 for e in guild.emojis if e.animated)}

    async def add_emoji(self, name: str, image: bytes, animated: bool, reason: str) -> dict[str, object]:
        """Upload a custom emoji (Custom Emoji page). Static and animated emoji have separate slots."""
        self.require("custom_emoji")
        guild = self._guild()
        slots = self.emoji_slots()
        kind = "animated" if animated else "static"
        if slots[kind] >= slots["limit"]:
            raise DiscordConfigError(f"The server has no free {kind} emoji slots ({slots[kind]} of {slots['limit']}).")
        if any(e.name == name for e in guild.emojis):
            raise DiscordConfigError(f"The server already has an emoji called :{name}:.")
        try:
            emoji = await guild.create_custom_emoji(name=name, image=image, reason=reason[:500])
        except discord.HTTPException as exc:
            log.warning("discord: emoji upload refused name=%s bytes=%d animated=%s status=%s code=%s",
                        name, len(image), animated, exc.status, exc.code)
            raise DiscordConfigError(f"Discord refused the emoji: {exc.text or exc.status}") from None
        log.info("discord: emoji added id=%s name=%s animated=%s bytes=%d", emoji.id, emoji.name, emoji.animated,
                 len(image))
        return _emoji_dict(emoji)

    def _server_emoji(self, emoji_id: str) -> discord.Emoji:
        emoji = next((e for e in self._guild().emojis if str(e.id) == emoji_id), None)
        if emoji is None:
            raise DiscordConfigError("That emoji isn't on the server any more.")
        return emoji

    async def rename_emoji(self, emoji_id: str, name: str, reason: str) -> tuple[dict[str, object], str]:
        """(the renamed emoji, its old text)."""
        self.require("custom_emoji")
        emoji = self._server_emoji(emoji_id)
        old = str(emoji)
        try:
            emoji = await emoji.edit(name=name, reason=reason[:500])
        except discord.HTTPException as exc:
            log.warning("discord: emoji rename refused id=%s status=%s code=%s", emoji_id, exc.status, exc.code)
            raise DiscordConfigError(f"Discord refused the rename: {exc.text or exc.status}") from None
        log.info("discord: emoji renamed id=%s %s -> %s", emoji_id, old, emoji.name)
        return _emoji_dict(emoji), old

    async def delete_emoji(self, emoji_id: str, reason: str) -> dict[str, object]:
        self.require("custom_emoji")
        emoji = self._server_emoji(emoji_id)
        gone = _emoji_dict(emoji)
        try:
            await emoji.delete(reason=reason[:500])
        except discord.HTTPException as exc:
            log.warning("discord: emoji delete refused id=%s status=%s code=%s", emoji_id, exc.status, exc.code)
            raise DiscordConfigError(f"Discord refused to delete the emoji: {exc.text or exc.status}") from None
        log.info("discord: emoji deleted id=%s name=%s", emoji_id, gone["name"])
        return gone

    async def list_channels(self) -> list[dict[str, str]]:
        guild = self._guild()
        me = guild.me
        out = []
        for ch in guild.text_channels:
            p = ch.permissions_for(me)
            out.append({"id": str(ch.id), "name": ch.name, "category": ch.category.name if ch.category else "",
                        "can_post": bool(p.view_channel and p.send_messages and p.add_reactions),
                        "can_post_requests": bool(p.view_channel and p.send_messages and getattr(p, "embed_links", False)
                                                  and getattr(p, "read_message_history", False))})
        return out

    async def publish_post(self, post_id: int, new_channel_name: str | None = None) -> dict:
        """Post (or update) a reaction post, add its reactions, pin it if wanted."""
        self.require("reaction_posts")
        guild = self._guild()

        def load(s):
            post = s.get(ReactionPost, post_id)
            if post is None:
                raise DiscordConfigError("reaction post not found")
            return {"channel_id": post.channel_id, "message_id": post.message_id,
                    "content": reaction_text.render_post(s, post),
                    "pin": post.pin, "options": list(post.options or []), "title": post.title,
                    "clear": post.clear_old_reactions}

        post = await self.run_db(load)
        if not post["options"]:
            raise DiscordConfigError("Add at least one emoji option first.")
        channel = None
        if new_channel_name:
            self.require("create_channel")
            name = new_channel_name.strip().lower().replace(" ", "-")[:100]
            channel = discord.utils.get(guild.text_channels, name=name)
            if channel is None:
                channel = await guild.create_text_channel(name, reason="WooSarr reaction post")
                log.info("discord channel created #%s (%s)", channel.name, channel.id)
        elif post["channel_id"]:
            channel = guild.get_channel(int(post["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            raise DiscordConfigError("Choose a text channel (or a name for a new one).")
        emojis = [normalize_emoji(o["emoji"]) for o in post["options"]]
        missing = reaction_channel_missing(channel, guild, emojis)
        if missing:
            log.warning("reaction post %s not published: bot lacks %s in #%s", post_id, ", ".join(missing),
                        channel.name)
            raise DiscordConfigError(f"The bot is missing {', '.join(missing)} in #{channel.name}. Allow it in the "
                                     "channel's permissions (Edit Channel → Permissions → the bot's role), then try "
                                     "again.")
        message = None
        pinned = False
        notes: list[str] = []
        try:
            if post["message_id"] and str(channel.id) == post["channel_id"]:
                try:
                    message = await channel.fetch_message(int(post["message_id"]))
                    await message.edit(content=post["content"], allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(post["content"], allowed_mentions=discord.AllowedMentions.none())
            present = {str(r.emoji) for r in message.reactions if r.me}
            for emoji in emojis:
                if emoji not in present:
                    try:
                        await message.add_reaction(discord.PartialEmoji.from_str(emoji))
                    except discord.HTTPException as exc:
                        if exc.status == 400 or getattr(exc, "code", 0) == 10014:  # unknown / unusable emoji
                            raise DiscordConfigError(f"Discord can't use the emoji {emoji} (deleted, or from a "
                                                     "server the bot isn't in). Pick another one.") from None
                        raise
            notes += await self._retire_reactions(message, guild, post_id, set(emojis),
                                                  {e for o in post["options"] for e in option_emojis(o)}, post["clear"])
            if post["pin"]:
                ok, why = features.available(self.feature_status(), "pin_posts")
                if ok:
                    if not message.pinned:
                        await message.pin(reason="WooSarr reaction post")
                    pinned = True
                else:
                    log.warning("reaction post %s not pinned: %s", post_id, why)
        except discord.Forbidden as exc:
            log.warning("reaction post %s: Discord refused (%s) in #%s", post_id, exc.text or exc, channel.name)
            raise DiscordConfigError(f"Discord refused: {exc.text or 'missing permissions'} in #{channel.name}. Check "
                                     "the bot's permissions in that channel (View Channel, Send Messages, Add "
                                     "Reactions, Read Message History, and Use External Emojis for other servers' "
                                     "emoji).") from None
        except discord.HTTPException as exc:
            log.warning("reaction post %s: Discord error %s in #%s: %s", post_id, exc.status, channel.name, exc.text)
            raise DiscordConfigError(f"Discord answered {exc.status}: {exc.text or 'error'}. Try again.") from None

        def save(s):
            row = s.get(ReactionPost, post_id)
            row.channel_id, row.message_id, row.published_at = str(channel.id), str(message.id), utcnow()
            row.rendered_content = post["content"]
            audit.record(s, "system", "reaction_post_published", "reaction_post", str(post_id),
                         channel=channel.name, message_id=str(message.id), pinned=pinned)

        await self.run_db(save)

        # Roles members had through emoji this publish retires are removed by the reconcile below.
        def retire(s):
            before = self._reaction_roles(s, with_retired=True)[1]
            row = s.get(ReactionPost, post_id)
            if row.retired_options:
                log.info("reaction post %s: emoji retired: %s", post_id,
                         " ".join(str(r.get("emoji")) for r in row.retired_options))
            row.retired_options, row.clear_old_reactions = [], False
            return before

        await self.reconcile_reactions(before=await self.run_db(retire))
        return {"channel_id": str(channel.id), "channel": channel.name, "message_id": str(message.id),
                "pinned": pinned, "jump_url": message.jump_url, "notes": notes}

    async def _retire_reactions(self, message: discord.Message, guild: discord.Guild, post_id: int,
                                current: set[str], counted: set[str], clear: bool) -> list[str]:
        """Emoji the post no longer shows: the bot takes its own reaction off (so nobody picks a dead
        one); with clear, everyone's reactions for emoji that no longer count are removed too."""
        notes: list[str] = []
        clear_ok, why = features.available(self.feature_status(), "clear_reactions")
        removed_own, cleared = [], []
        for reaction in message.reactions:
            emoji = normalize_emoji(str(reaction.emoji))
            if emoji in current:
                continue
            if clear and emoji not in counted and clear_ok:
                try:
                    await message.clear_reaction(reaction.emoji)
                    cleared.append(emoji)
                    continue
                except discord.HTTPException as exc:
                    log.warning("reaction post %s: couldn't clear %s reactions: %s %s", post_id, emoji, exc.status,
                                exc.text)
                    clear_ok, why = False, f"Discord refused ({exc.status})"
            if reaction.me:
                try:
                    await message.remove_reaction(reaction.emoji, guild.me)
                    removed_own.append(emoji)
                except discord.HTTPException as exc:
                    log.warning("reaction post %s: couldn't take the bot's %s reaction off: %s %s", post_id, emoji,
                                exc.status, exc.text)
        if clear and not clear_ok and any(normalize_emoji(str(r.emoji)) not in counted for r in message.reactions):
            log.warning("reaction post %s: old reactions not cleared: %s", post_id, why)
            notes.append(f"Old reactions weren't removed ({why}); they stay on the message but no longer give "
                         "roles.")
        if removed_own or cleared:
            log.info("reaction post %s: bot reactions removed=%s, all reactions cleared=%s", post_id,
                     " ".join(removed_own) or "-", " ".join(cleared) or "-")
        return notes

    async def unpublish_post(self, channel_id: str | None, message_id: str | None) -> None:
        if not channel_id or not message_id:
            return
        try:
            guild = self._guild()
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(int(message_id))
                await msg.delete()
        except (discord.NotFound, discord.Forbidden, DiscordConfigError) as exc:
            log.warning("could not delete reaction post message %s: %s", message_id, exc)

    @staticmethod
    def _reaction_roles(session, override: dict[int, list[dict]] | None = None, with_retired: bool = False
                        ) -> tuple[dict[tuple[str, str], set[str]], dict[str, set[str]]]:
        """(message_id, emoji) -> role ids for enabled posts, and member -> roles wanted by reactions.
        override: post id -> options to use instead of the saved ones (previews). with_retired: also
        count emoji taken out since the last publish (what members have now, before that publish)."""
        options: dict[tuple[str, str], set[str]] = {}
        by_post: dict[int, str] = {}
        for post in session.scalars(select(ReactionPost).where(ReactionPost.enabled.is_(True),
                                                               ReactionPost.message_id.is_not(None))):
            by_post[post.id] = post.message_id
            opts = list((override or {}).get(post.id, post.options) or [])
            for o in opts + (list(post.retired_options or []) if with_retired else []):
                for emoji in option_emojis(o):
                    options.setdefault((post.message_id, emoji), set()).update(option_role_ids(o))
        wanted: dict[str, set[str]] = {}
        for r in session.scalars(select(Reaction)):
            roles = options.get((by_post.get(r.post_id, ""), r.emoji))
            if roles:
                wanted.setdefault(r.discord_user_id, set()).update(roles)
        return options, wanted

    async def _apply_member_roles(self, guild: discord.Guild, discord_user_id: str, add: set[str],
                                  remove: set[str], reason: str) -> None:
        member = guild.get_member(int(discord_user_id))
        if member is None:
            return
        have = {str(r.id) for r in member.roles}
        add, remove = add - have, remove & have
        try:
            if add:
                await member.add_roles(*[discord.Object(int(r)) for r in add], reason=reason)
            if remove:
                await member.remove_roles(*[discord.Object(int(r)) for r in remove], reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("reaction roles for %s not updated (%s); check Manage Roles and role order", discord_user_id, exc)
            return
        if add or remove:
            log.info("reaction roles for %s: +%s -%s", discord_user_id, sorted(add), sorted(remove))

    async def reaction_changed(self, message_id: str, discord_user_id: str, emoji: str,
                               added: bool) -> None:
        """React -> give the option's roles; un-react -> remove them unless another kept
        reaction still gives them. Libraries then follow from the role mappings."""
        emoji = normalize_emoji(emoji)

        def work(s):
            post = s.scalar(select(ReactionPost).where(ReactionPost.message_id == message_id))
            if post is None or not any(emoji in option_emojis(o) for o in post.options or []):
                return None
            row = s.get(Reaction, (post.id, discord_user_id, emoji))
            if added and row is None:
                s.add(Reaction(post_id=post.id, discord_user_id=discord_user_id, emoji=emoji))
            elif not added and row is not None:
                s.delete(row)
            s.flush()
            options, wanted = self._reaction_roles(s)
            roles = options.get((message_id, emoji), set()) if post.enabled else set()
            audit.record(s, f"discord:{discord_user_id}", "reaction_added" if added else "reaction_removed",
                         "reaction_post", str(post.id), emoji=emoji, role_ids=sorted(roles))
            return roles, wanted.get(discord_user_id, set())

        result = await self.run_db(work)
        if not result:
            return
        roles, still_wanted = result
        if not roles:
            return
        ok, why = features.available(self.feature_status(), "reaction_posts")
        if not ok:
            log.warning("reaction recorded but roles not changed: %s", why)
            return
        guild = self._guild()
        if added:
            await self._apply_member_roles(guild, discord_user_id, roles, set(), "WooSarr reaction role")
        else:
            await self._apply_member_roles(guild, discord_user_id, set(), roles - still_wanted,
                                           "WooSarr reaction role removed")

    async def reconcile_reactions(self, before: dict[str, set[str]] | None = None) -> None:
        """Re-read every published post's reactions (catches changes made while offline)
        and give/remove the reaction roles to match. before: the roles members had from reactions,
        when the caller knows better than the saved options (a publish retiring emoji)."""
        try:
            guild = self._guild()
        except DiscordConfigError:
            return
        posts = await self.run_db(lambda s: [(p.id, p.channel_id, p.message_id,
                                              # Retired emoji are still read: the next publish needs them.
                                              [e for o in [*(p.options or []), *(p.retired_options or [])]
                                               for e in option_emojis(o)])
                                             for p in s.scalars(select(ReactionPost).where(
                                                 ReactionPost.message_id.is_not(None)))])
        if before is None:
            before = await self.run_db(lambda s: self._reaction_roles(s)[1])
        for post_id, channel_id, message_id, emojis in posts:
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                log.warning("reaction post %s: channel %s not found", post_id, channel_id)
                continue
            try:
                msg = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden) as exc:
                log.warning("reaction post %s: message %s unavailable (%s)", post_id, message_id, exc)
                continue
            wanted = set(emojis)
            rows: set[tuple[str, str]] = set()
            for reaction in msg.reactions:
                emoji = normalize_emoji(str(reaction.emoji))
                if emoji not in wanted:
                    continue
                async for user in reaction.users(limit=None):
                    if not user.bot:
                        rows.add((str(user.id), emoji))

            def replace(s, post_id=post_id, rows=rows):
                s.execute(delete(Reaction).where(Reaction.post_id == post_id))
                for uid, emoji in rows:
                    s.add(Reaction(post_id=post_id, discord_user_id=uid, emoji=emoji))
                # A former emoji nobody reacts with any more has done its job.
                used = {emoji for _, emoji in rows}
                post = s.get(ReactionPost, post_id)
                options = [dict(o, former=[e for e in o.get("former") or [] if normalize_emoji(e) in used])
                           if o.get("former") else o for o in post.options or []]
                if options != post.options:
                    dropped = [e for o in post.options or [] for e in o.get("former") or []
                               if normalize_emoji(e) not in used]
                    post.options = [{k: v for k, v in o.items() if k != "former" or v} for o in options]
                    log.info("reaction post %s: old emoji no longer used, dropped: %s", post_id, " ".join(dropped))

            await self.run_db(replace)
            log.info("reaction post %s: %s reactions read", post_id, len(rows))
        after = await self.run_db(lambda s: self._reaction_roles(s)[1])
        if not features.available(self.feature_status(), "reaction_posts")[0]:
            return
        for uid in set(before) | set(after):
            add, remove = after.get(uid, set()), before.get(uid, set()) - after.get(uid, set())
            if add or remove:
                await self._apply_member_roles(guild, uid, add, remove, "WooSarr reaction roles (resync)")

    def schedule_post_refresh(self) -> None:
        """Thread-safe: re-render published posts soon and edit those whose text changed."""
        loop = self._loop
        if loop is None or self._client is None:
            return

        def start() -> None:
            if self._post_task is None or self._post_task.done():
                self._post_task = asyncio.create_task(self.refresh_posts())
            else:
                self._post_again = True

        loop.call_soon_threadsafe(start)

    async def refresh_posts(self) -> int:
        """Edit published reaction posts whose legend changed (role renamed, role
        -> library mapping changed, library renamed...). Returns posts edited."""
        edited = 0
        while True:
            self._post_again = False
            try:
                guild = self._guild()
            except DiscordConfigError:
                return edited

            def due(s):
                out = []
                for p in s.scalars(select(ReactionPost).where(ReactionPost.message_id.is_not(None))):
                    text = reaction_text.render_post(s, p)
                    if text != p.rendered_content and len(text) <= reaction_text.MAX_MESSAGE:
                        out.append((p.id, p.channel_id, p.message_id, text))
                return out

            for post_id, channel_id, message_id, text in await self.run_db(due):
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("reaction post %s not updated: %s", post_id, exc)
                    continue

                def save(s, post_id=post_id, text=text):
                    row = s.get(ReactionPost, post_id)
                    if row is not None:
                        row.rendered_content = text
                        audit.record(s, "system", "reaction_post_updated_text", "reaction_post", str(post_id))

                await self.run_db(save)
                edited += 1
                log.info("reaction post %s text updated in Discord", post_id)
            if not self._post_again:
                return edited

    def schedule_command_sync(self) -> None:
        """Thread-safe: re-register slash commands (request command renamed, Seerr connected or removed)."""
        loop, client = self._loop, self._client
        if loop is None or client is None:
            return

        def start() -> None:
            if client.is_ready():
                task = asyncio.create_task(client.sync_commands())
                task.add_done_callback(lambda t: not t.cancelled() and t.exception() and log.error(
                    "discord command sync failed: %s", t.exception()))

        loop.call_soon_threadsafe(start)

    def schedule_role_sync(self) -> None:
        """Thread-safe: refresh link-status roles soon (coalesces bursts)."""
        loop = self._loop
        if loop is None or self._client is None:
            return

        def start() -> None:
            if self._role_task is None or self._role_task.done():
                self._role_task = asyncio.create_task(self.sync_link_roles())
            else:
                self._role_again = True
        loop.call_soon_threadsafe(start)

    async def sync_link_roles(self) -> dict[str, int]:
        """Give/remove the configured Verified/Unverified/Linked roles to match links."""
        done = {"added": 0, "removed": 0, "errors": 0}
        while True:
            self._role_again = False
            st = await self.run_db(app_settings.load)
            if not any((st.verified_role_id, st.unverified_role_id, st.linked_role_id)):
                return done
            ok, why = features.available(self.feature_status(), "link_roles")
            if not ok:
                if why != self._role_problem:
                    log.warning("link-status roles not applied: %s", why)
                self._role_problem = why
                return done
            self._role_problem = None
            guild = self._guild()
            links = await self.run_db(lambda s: dict(s.execute(select(DiscordLink.discord_user_id,
                                                                      DiscordLink.verified)).all()))
            members = {str(m.id): frozenset(str(r.id) for r in m.roles) for m in guild.members if not m.bot}
            changes = features.desired_link_roles(members, links, st.verified_role_id, st.unverified_role_id,
                                                  st.linked_role_id)
            for uid, (add, remove) in changes.items():
                member = guild.get_member(int(uid))
                if member is None:
                    continue
                try:
                    if add:
                        await member.add_roles(*[discord.Object(int(r)) for r in add], reason="WooSarr: Plex link status")
                        done["added"] += len(add)
                    if remove:
                        await member.remove_roles(*[discord.Object(int(r)) for r in remove],
                                                  reason="WooSarr: Plex link status")
                        done["removed"] += len(remove)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    done["errors"] += 1
                    log.warning("could not update link roles for %s: %s", uid, exc)
            if changes:
                log.info("link-status roles: +%s -%s (errors %s)", done["added"], done["removed"], done["errors"])
            if not self._role_again:
                return done


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


class DiscordService(_GuildFeatures, discord_requests.RequestCommands):
    def __init__(self, db: Database, secrets: Any, trigger: Callable[..., None], plex: Any,
                 sync: Any = None, requests: Any = None) -> None:
        self.requests = requests
        self._latest_suggestion: dict[str, int] = {}  # member -> newest suggestion ask (request command)
        self._db = db
        self._secrets = secrets
        self.trigger = trigger
        self._plex = plex
        self._sync = sync
        self._role_task: asyncio.Task | None = None
        self._role_again = False
        self._role_problem: str | None = None
        self._post_task: asyncio.Task | None = None
        self._post_again = False
        self._link_tasks: set[asyncio.Task] = set()
        if sync is not None:
            sync.after_run.append(lambda _report: self.schedule_role_sync())
            # Role mappings / library names may have changed: keep reaction post legends current.
            sync.after_run.append(lambda _report: self.schedule_post_refresh())
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
            # Set up, no error, not ready yet: the UI shows this as in progress rather than a problem.
            "connecting": bool(has_token and guild and not self._error and not ready),
            "bot": bot,
            "guild": {"id": guild["id"], "name": self._guild_name or guild.get("name")} if guild else None,
            "invite_url": invite_url(bot["application_id"]) if bot else None,
            "detail": detail,
            "avatar_url": self._avatar_url(),
        }

    def _avatar_url(self) -> str | None:
        client = self._client
        user = getattr(client, "user", None) if client is not None and client.is_ready() else None
        avatar = getattr(user, "display_avatar", None)
        return str(avatar.replace(size=256).url) if avatar is not None else None

    async def set_avatar(self, image: discord_avatar.Image, app_icon: bool) -> dict[str, str | None]:
        """Set the bot's avatar (and optionally the application icon) with the stored token."""
        token = self._token()
        if not token:
            raise discord_avatar.AvatarError("Enter the bot token first.", 409)
        return await discord_avatar.apply(token, image, app_icon)

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
        except discord.ConnectionClosed as exc:
            if exc.code == 4004:  # authentication failed: token reset or revoked
                self.set_error("Discord rejected the bot token (it was reset or revoked). Enter the new token on the "
                               "Setup page.")
            else:
                log.exception("discord gateway closed the connection")
                self.set_error(f"Discord closed the connection (code {exc.code}). Press Restart bot to try again.")
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
        if not await acknowledge(interaction, "/unlink"):
            return
        found = await self.run_db(link_service.current_link, str(interaction.user.id))
        if found is None:
            await interaction.followup.send("Your Discord account isn't linked to a Plex account.", ephemeral=True)
            return
        link, plex = found
        policy = (await self.run_db(app_settings.load)).unlink_policy
        shared = await self.run_db(link_service.other_members, link.plex_user_id, str(interaction.user.id))
        if shared:
            effect = ("The account stays linked to the other members who share it. "
                      + ("Libraries only your roles give will be removed." if policy == "drop_role_access"
                         else "Libraries only your roles give stay as they are."))
        else:
            effect = ("Libraries your Discord roles gave you will be removed; manual grants and exempt libraries stay."
                      if policy == "drop_role_access" else "Your Plex access stays as it is, but roles stop updating it.")
        await interaction.followup.send(
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

        with self._db.session() as s:
            shared = len(link_service.other_members(s, plex_id, discord_id))
        if shared:
            # A family account: it stays linked to the others and keeps what their roles give.
            with self._db.session() as s:
                kept = (link_service.keep_member_libraries(s, plex_id, discord_id, f"discord:{discord_id}")
                        if policy == "keep_all" else [])
                link_service.unlink_self(s, discord_id, policy)
            self.trigger([plex_id], f"self-unlink by discord {discord_id} (shared account)")
            others = f"{shared} other member{'s' if shared != 1 else ''}"
            if policy == "keep_all":
                extra = (f" {len(kept)} librar{'ies' if len(kept) != 1 else 'y'} only your roles gave stay as manual "
                         "grants." if kept else "")
                return f"Unlinked. The Plex account stays linked to the {others} who share it.{extra}"
            return (f"Unlinked. The Plex account stays linked to the {others} who share it; libraries only your "
                    "roles gave are removed.")

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
        if not await acknowledge(interaction, "/link"):
            return
        wait = await self.run_db(link_service.cooldown_remaining, str(interaction.user.id))
        if wait:
            await interaction.followup.send(f"Please wait {wait} s before trying to link again.", ephemeral=True)
            return
        member = interaction.user

        def allowed() -> bool:
            with self._db.session() as s:
                if isinstance(member, discord.Member):  # the cache may not have the member's latest roles yet
                    cache.member_changed(s, _member(member))
                return link_service.username_link_allowed(s, str(member.id))

        by_username = await asyncio.to_thread(allowed)
        text = ("Link your Plex account so your roles give you the right libraries.\n"
                "• **Sign in with Plex**: verified. WooSarr only reads your Plex username, then signs that sign-in out.")
        if by_username:
            text += "\n• **Enter Plex username**: quicker, but unverified."
        await interaction.followup.send(text, view=_LinkView(self, by_username), ephemeral=True)

    async def start_oauth_link(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        # Answer Discord first: it drops interactions not answered within 3 s, and
        # starting the Plex sign-in is a plex.tv round trip.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pin_id, url = await asyncio.to_thread(
                self._plex.start_login, None, "link", f"woosarr-link-{uuid.uuid4()}")
        except Exception:
            log.exception("could not start Plex sign-in for /link")
            await interaction.followup.send("Plex sign-in is unavailable right now. Try the username option.",
                                            ephemeral=True)
            return
        view = discord.ui.View(timeout=LINK_TIMEOUT_SECONDS)
        view.add_item(discord.ui.Button(label="Open Plex sign-in", url=url))
        await interaction.followup.send("Sign in on the Plex page, then come back here. This expires in 5 minutes.",
                                        view=view, ephemeral=True)
        task = asyncio.create_task(self._finish_oauth_link(interaction, discord_id, pin_id))
        self._link_tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._link_tasks.discard)

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

