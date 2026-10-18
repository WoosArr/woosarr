"""The request slash command (default `/request`, name set in Settings) (plan P13-01).

Flow (all replies private): search-as-you-type over Seerr -> pick a title
(or choose from a menu when the text wasn't picked from the suggestions) ->
details with poster and status -> seasons for shows -> Request. The request
is made as the member's own Seerr user (services/media_requests.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from woosarr.core import app_settings
from woosarr.services import audit
from woosarr.services.media_requests import STATE_TEXT, Notice, RequestDenied, RequestService
from woosarr.services.seerr import Details, Hit, SeerrError

if TYPE_CHECKING:
    from woosarr.services.discord_bot import DiscordService

log = logging.getLogger("woosarr.discord.requests")

AUTOCOMPLETE_SECONDS = 2.5  # Discord drops autocomplete answers after 3 s
PICK = re.compile(r"^(movie|tv):(\d{1,10})$")
REQUESTABLE = ("unknown", "deleted")
STATUS_LINE = {
    "unknown": "Not on Plex yet",
    "deleted": "Not on Plex yet",
    "pending": "Already requested, waiting for approval",
    "processing": "Already requested, on its way",
    "partially_available": "Partly available on Plex",
    "available": "Available on Plex",
    "blocklisted": "Can't be requested (blocklisted)",
}
COLOR = 0x8B5CF6


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


def embed(item: Details) -> discord.Embed:
    kind = "Movie" if item.media_type == "movie" else "TV show"
    title = f"{item.title} ({item.year})" if item.year else item.title
    overview = item.overview if len(item.overview) <= 400 else item.overview[:399] + "…"
    e = discord.Embed(title=title[:256], url=item.tmdb_url, description=overview or None, color=COLOR)
    e.add_field(name="Type", value=kind)
    e.add_field(name="Status", value=STATUS_LINE.get(item.status, item.status))
    if item.seasons:
        missing = [s.number for s in item.seasons if s.status in REQUESTABLE]
        e.add_field(name="Seasons", value=f"{len(item.seasons)} ({len(missing)} not on Plex yet)")
    if item.poster:
        e.set_thumbnail(url=item.poster)
    return e


def link_view(item: Details) -> discord.ui.View:
    """Only link buttons: safe to leave on a public post (nothing reaches the bot)."""
    view = discord.ui.View(timeout=None)
    for label, url in item.links():
        view.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))
    return view


def status_text(state: str) -> str:
    text = STATE_TEXT.get(state, state)
    return text[:1].upper() + text[1:]


def set_status(e: discord.Embed, state: str) -> discord.Embed:
    for i, f in enumerate(e.fields):
        if f.name == "Status":
            e.set_field_at(i, name="Status", value=status_text(state), inline=f.inline)
            return e
    return e.add_field(name="Status", value=status_text(state))


def requestable_seasons(item: Details) -> list[int]:
    return [s.number for s in item.seasons if s.status in REQUESTABLE]


def can_request(item: Details) -> bool:
    if item.media_type == "tv":
        return bool(requestable_seasons(item)) and item.status != "blocklisted"
    return item.status in REQUESTABLE


class _Owned(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your request.", ephemeral=True)
            return False
        return True


class PickView(_Owned):
    """A menu of search results, for text that wasn't picked from the suggestions."""

    def __init__(self, service: DiscordService, owner_id: int, hits: list[Hit]) -> None:
        super().__init__(owner_id)
        self.service = service
        select = discord.ui.Select(placeholder="Choose a title", min_values=1, max_values=1, options=[
            discord.SelectOption(label=f"{n}. {h.label()}"[:100], value=h.key,
                                 description=(h.overview[:97] + "…") if len(h.overview) > 100 else (h.overview or None))
            for n, h in enumerate(hits[:25], 1)])
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        kind, tmdb = self.select.values[0].split(":")
        await interaction.response.defer()
        await self.service.show_details(interaction, kind, int(tmdb))
        self.stop()


def pick_list(hits: list[Hit]) -> discord.Embed:
    """Numbered titles linking to TMDB, shown above the pick menu. Links only open the
    page; choosing still happens in the menu (menu options can't hold links)."""
    lines = [f"{n}. [{discord.utils.escape_markdown(h.label())}]({h.tmdb_url})" for n, h in enumerate(hits[:25], 1)]
    return discord.Embed(description="\n".join(lines)[:4096], color=COLOR)


class RequestView(_Owned):
    def __init__(self, service: DiscordService, owner_id: int, item: Details) -> None:
        super().__init__(owner_id)
        self.service = service
        self.item = item
        self.seasons: list[int] = []
        if item.media_type == "tv":
            missing = requestable_seasons(item)
            if 1 < len(missing) <= 25:
                select = discord.ui.Select(
                    placeholder="Seasons (leave empty for every missing season)", min_values=0,
                    max_values=len(missing),
                    options=[discord.SelectOption(label=f"Season {s.number}", value=str(s.number),
                                                  description=f"{s.episodes} episodes" if s.episodes else None)
                             for s in item.seasons if s.number in missing])
                select.callback = self._seasons
                self.season_select = select
                self.add_item(select)
        button = discord.ui.Button(label=self._label(), style=discord.ButtonStyle.primary,
                                   disabled=not can_request(item))
        button.callback = self._request
        self.button = button
        self.add_item(button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)
        # Link buttons open the page in a browser; they never reach the bot.
        for label, url in item.links():
            self.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link, row=4))

    def _label(self) -> str:
        if self.item.media_type != "tv":
            return "Request"
        if not self.seasons:
            return "Request all missing seasons"
        return f"Request {len(self.seasons)} season{'s' if len(self.seasons) != 1 else ''}"

    async def _seasons(self, interaction: discord.Interaction) -> None:
        self.seasons = sorted(int(v) for v in self.season_select.values)
        self.button.label = self._label()
        await interaction.response.edit_message(view=self)

    async def _request(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Sending the request…", view=None)
        seasons = self.seasons or None
        result = await asyncio.to_thread(self.service.requests.submit, str(interaction.user.id),
                                         is_admin(interaction), self.item.media_type, self.item.tmdb_id, seasons)
        message = result.message
        if result.ok and result.row_id is not None:
            where = await self.service.announce_request(interaction.user.id, self.item, result.state or "pending",
                                                        result.row_id, seasons)
            if where:
                message += f" Posted in <#{where}>."
        await interaction.edit_original_response(content=message, view=None)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Nothing requested.", embed=None, view=None)
        self.stop()


def build_command(service: DiscordService, name: str) -> app_commands.Command:
    @app_commands.describe(title="Movie or TV show; pick one of the suggestions")
    async def request(interaction: discord.Interaction, title: str) -> None:
        await service.handle_request_command(interaction, title)

    command = app_commands.Command(name=name, description="Request a movie or TV show for the Plex server",
                                   callback=request)

    @command.autocomplete("title")
    async def suggest(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await service.request_suggestions(current)

    return command


class RequestCommands:
    """Request handlers of DiscordService (needs .requests, .run_db)."""

    requests: RequestService | None

    def request_command_name(self, session: Any) -> str | None:
        """The name to register, or None while Seerr isn't set up."""
        if self.requests is None or not self.requests.configured():
            return None
        return app_settings.load(session).request_command_name

    async def request_suggestions(self, current: str) -> list[app_commands.Choice[str]]:
        if self.requests is None or len(current.strip()) < 2:
            return []
        try:
            hits = await asyncio.wait_for(asyncio.to_thread(self.requests.search, current, AUTOCOMPLETE_SECONDS),
                                          AUTOCOMPLETE_SECONDS)
        except (TimeoutError, SeerrError) as exc:
            log.info("request suggestions for %r unavailable: %s", current[:50], exc or "timeout")
            return []
        return [app_commands.Choice(name=h.label(), value=h.key) for h in hits[:25]]

    async def handle_request_command(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.requests is None:
            await interaction.followup.send("Requests aren't set up yet. Ask an admin.", ephemeral=True)
            return
        uid, admin = str(interaction.user.id), is_admin(interaction)
        settings = await self.run_db(app_settings.load)
        if (settings.request_visibility == "public" and settings.request_channel_only
                and str(interaction.channel_id) != settings.request_channel_id):
            log.info("request by %s refused: used in channel %s, not %s", uid, interaction.channel_id,
                     settings.request_channel_id)
            await interaction.followup.send(f"Please make requests in <#{settings.request_channel_id}>.",
                                            ephemeral=True)
            return

        def check(s):
            self.requests.requester(s, uid, admin)

        try:
            await self.run_db(check)
        except RequestDenied as exc:
            reason = str(exc)
            await self.run_db(lambda s: audit.record(s, f"discord:{uid}", "request_denied", "discord_user", uid,
                                                     reason=reason))
            await interaction.followup.send(reason, ephemeral=True)
            return
        picked = PICK.match(title.strip())
        if picked:
            await self.show_details(interaction, picked.group(1), int(picked.group(2)))
            return
        try:
            hits = await asyncio.to_thread(self.requests.search, title)
        except SeerrError as exc:
            await interaction.followup.send(f"Seerr isn't answering right now ({exc}).", ephemeral=True)
            return
        if not hits:
            await interaction.followup.send(f"Nothing found for **{discord.utils.escape_markdown(title[:100])}**.",
                                            ephemeral=True)
            return
        if len(hits) == 1:
            await self.show_details(interaction, hits[0].media_type, hits[0].tmdb_id)
            return
        await interaction.followup.send("Which one? Titles link to TMDB; choose in the menu.", embed=pick_list(hits),
                                        view=PickView(self, interaction.user.id, hits), ephemeral=True)

    async def show_details(self, interaction: discord.Interaction, media_type: str, tmdb_id: int) -> None:
        """Answer (or replace the menu) with the title's details and the Request button."""
        try:
            item = await asyncio.to_thread(self.requests.details, media_type, tmdb_id)
        except SeerrError as exc:
            await interaction.followup.send(f"Couldn't load that title from Seerr ({exc}).", ephemeral=True)
            return
        view = RequestView(self, interaction.user.id, item)
        note = None if can_request(item) else "Nothing left to request for this title."
        if interaction.message is not None and interaction.type == discord.InteractionType.component:
            await interaction.edit_original_response(content=note, embed=embed(item), view=view)
        else:
            await interaction.followup.send(content=note or "", embed=embed(item), view=view, ephemeral=True)

    def _request_channel(self, channel_id: str | None) -> discord.TextChannel | None:
        """The requests channel if the bot can post (and edit/reply) there, else None (logged)."""
        client = getattr(self, "_client", None)
        if not channel_id or client is None or not client.is_ready():
            return None
        guild = client.get_guild(client.guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel):
            log.warning("requests channel %s not found; set it again in Settings → Requests", channel_id)
            return None
        p = channel.permissions_for(guild.me)
        missing = [n for n in ("view_channel", "send_messages", "embed_links", "read_message_history")
                   if not getattr(p, n, False)]
        if missing:
            log.warning("bot can't post requests in #%s: missing %s", channel.name, ", ".join(missing))
            return None
        return channel

    async def announce_request(self, user_id: int, item: Details, state: str, row_id: int,
                               seasons: list[int] | None) -> str | None:
        """Public requests: post in the requests channel mentioning the member. Returns the channel id."""
        settings = await self.run_db(app_settings.load)
        if settings.request_visibility != "public":
            return None
        channel = self._request_channel(settings.request_channel_id)
        if channel is None:
            return None
        what = f"**{discord.utils.escape_markdown(item.title)}**" + (f" ({item.year})" if item.year else "")
        if item.media_type == "tv":
            what += f", seasons {', '.join(map(str, seasons))}" if seasons else ", all missing seasons"
        e = set_status(embed(item), state)
        try:
            msg = await channel.send(f"<@{user_id}> requested {what}", embed=e, view=link_view(item),
                                     allowed_mentions=discord.AllowedMentions(users=[discord.Object(user_id)],
                                                                              everyone=False, roles=False))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("public request post in #%s failed: %s", channel.name, exc)
            return None
        await asyncio.to_thread(self.requests.set_post, row_id, str(channel.id), str(msg.id))
        log.info("request %s posted in #%s (message %s)", row_id, channel.name, msg.id)
        return str(channel.id)

    async def send_notice(self, notice: Notice) -> bool:
        """Status change: reply to the public post (mentioning the member) and update its
        Status line; private requests (or a missing post/channel) get a DM instead."""
        if notice.channel_id and notice.message_id:
            channel = self._request_channel(notice.channel_id)
            if channel is not None:
                try:
                    post = await channel.fetch_message(int(notice.message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.info("request post %s gone (%s); replying without it", notice.message_id, exc)
                    post = None
                mention = discord.AllowedMentions(users=[discord.Object(int(notice.discord_user_id))],
                                                  everyone=False, roles=False)
                try:
                    if post is not None and post.embeds:
                        await post.edit(embed=set_status(post.embeds[0], notice.state))
                    await channel.send(f"<@{notice.discord_user_id}> {notice.text}", allowed_mentions=mention,
                                       reference=post.to_reference(fail_if_not_exists=False) if post else None)
                    log.info("request notice for %s posted in #%s", notice.discord_user_id, channel.name)
                    return True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("request notice in #%s failed (%s); sending a DM", channel.name, exc)
        return await self.send_dm(notice.discord_user_id, notice.text)

    async def send_dm(self, discord_user_id: str, text: str) -> bool:
        client = getattr(self, "_client", None)
        if client is None or not client.is_ready():
            log.info("request notice for %s not sent: bot not connected", discord_user_id)
            return False
        try:
            user = client.get_user(int(discord_user_id)) or await client.fetch_user(int(discord_user_id))
            await user.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.info("request notice for %s not sent: member doesn't accept DMs from the bot", discord_user_id)
            return False
        except (discord.NotFound, discord.HTTPException) as exc:
            log.warning("request notice for %s not sent: %s", discord_user_id, exc)
            return False
        log.info("request notice sent to %s", discord_user_id)
        return True
