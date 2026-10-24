"""How long Discord interactions take, so "The application did not respond" can be pinned on a side.

Discord drops an interaction the bot hasn't answered within 3 s of the member using it. Two things
add up to that: delivery (member → Discord → WooSarr, over the gateway) and WooSarr's answer (an
HTTP call back to Discord). This module times the answer from inside the bot's own HTTP session
(an aiohttp trace: DNS, new connection, total), and estimates the server clock's offset from
Discord's (their Date headers), because an interaction's age is measured against Discord's
timestamp and a slow server clock (the live one was 38 s behind) makes local ages meaningless.

Interaction callback URLs carry a token: only the interaction id is kept, never the URL.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from collections import OrderedDict, deque
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
import discord

log = logging.getLogger("woosarr.discord")

# /api/v10/interactions/<id>/<token>/callback
_CALLBACK = re.compile(r"/interactions/(\d{5,25})/[^/]+/callback$")


class DiscordTiming:
    def __init__(self) -> None:
        self.offset = 0.0  # Discord's clock minus ours, in seconds
        self._samples: deque[float] = deque(maxlen=15)
        self.calls: OrderedDict[str, dict[str, Any]] = OrderedDict()  # interaction id -> its answer's timings
        self.trace = aiohttp.TraceConfig()
        self.trace.on_request_start.append(self._start)
        self.trace.on_dns_resolvehost_start.append(self._dns_start)
        self.trace.on_dns_resolvehost_end.append(self._dns_end)
        self.trace.on_connection_create_start.append(self._connect_start)
        self.trace.on_connection_create_end.append(self._connect_end)
        self.trace.on_connection_reuseconn.append(self._reused)
        self.trace.on_request_end.append(self._end)
        self.trace.on_request_exception.append(self._failed)

    # ---- clock -----------------------------------------------------------------------------

    def now(self) -> float:
        """Discord's idea of the current time (epoch seconds)."""
        return time.time() + self.offset

    def age(self, created: Any) -> float | None:
        """Seconds since a Discord timestamp (an interaction's created_at), on Discord's clock."""
        if created is None:
            return None
        return self.now() - created.timestamp()

    def sample_clock(self, date_header: str | None, sent_at: float, took: float) -> None:
        if not date_header:
            return
        try:
            theirs = parsedate_to_datetime(date_header).timestamp() + 0.5  # whole seconds: take the middle
        except (TypeError, ValueError, IndexError):
            return
        self._samples.append(theirs - (sent_at + took / 2))
        self.offset = statistics.median(self._samples)

    # ---- aiohttp trace hooks (ctx is per request) -------------------------------------------

    async def _start(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.t0, ctx.wall, ctx.dns, ctx.connect, ctx.reused = time.monotonic(), time.time(), None, None, False

    async def _dns_start(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.dns_t0 = time.monotonic()

    async def _dns_end(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.dns = time.monotonic() - getattr(ctx, "dns_t0", time.monotonic())

    async def _connect_start(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.conn_t0 = time.monotonic()

    async def _connect_end(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.connect = time.monotonic() - getattr(ctx, "conn_t0", time.monotonic())

    async def _reused(self, _s: Any, ctx: Any, _p: Any) -> None:
        ctx.reused = True

    async def _end(self, _s: Any, ctx: Any, params: Any) -> None:
        took = time.monotonic() - getattr(ctx, "t0", time.monotonic())
        self.sample_clock(params.response.headers.get("Date"), getattr(ctx, "wall", time.time()), took)
        self._record(ctx, params, took, params.response.status)

    async def _failed(self, _s: Any, ctx: Any, params: Any) -> None:
        self._record(ctx, params, time.monotonic() - getattr(ctx, "t0", time.monotonic()), None)

    def _record(self, ctx: Any, params: Any, took: float, status: int | None) -> None:
        m = _CALLBACK.search(str(getattr(params.url, "path", "")))
        if not m:
            return
        self.calls[m[1]] = {"took": took, "dns": getattr(ctx, "dns", None), "connect": getattr(ctx, "connect", None),
                            "reused": getattr(ctx, "reused", False), "status": status}
        while len(self.calls) > 200:
            self.calls.popitem(last=False)

    def answer(self, interaction_id: int | str) -> str:
        """How the answer's HTTP call went, for the log: "reused connection" / "new connection: …"."""
        call = self.calls.pop(str(interaction_id), None)
        if call is None:
            return "no HTTP timing"
        if call["reused"]:
            how = "reused connection"
        else:
            parts = [f"DNS {call['dns']:.1f} s" if call["dns"] is not None else "DNS cached",
                     f"connect {call['connect']:.1f} s" if call["connect"] is not None else ""]
            how = "new connection: " + ", ".join(p for p in parts if p)
        return f"{how}; HTTP {call['status'] or 'failed'} after {call['took']:.1f} s"


# A slow answer worth a log line even when Discord still took it.
SLOW_ARRIVAL_SECONDS, SLOW_ANSWER_SECONDS = 2.0, 1.0
TOO_LATE_DM_EVERY_SECONDS = 60
_too_late_dms: dict[int, float] = {}


async def acknowledge(interaction: discord.Interaction, what: str, timing: DiscordTiming | None = None) -> bool:
    """Tell Discord the bot is on it before doing any work: an interaction not answered within 3 s
    of the member using it is dropped (error 10062) and they see "The application did not respond".

    Logs how long it took to reach WooSarr (on Discord's clock) and how long the answer took, so a
    failure says which side was slow. False if Discord had already dropped it: the member gets a
    DM asking them to try again. True straight away if it was already acknowledged."""
    if interaction.response.is_done():
        return True
    arrived = timing.age(getattr(interaction, "created_at", None)) if timing else None
    t0 = time.monotonic()
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = True
    except discord.NotFound:
        ok = False
    took = time.monotonic() - t0
    how = timing.answer(interaction.id) if timing else "no HTTP timing"
    late = f"{arrived:.1f} s" if arrived is not None else "an unknown time"
    clock = f"; server clock {timing.offset:+.0f} s off Discord's" if timing and abs(timing.offset) >= 2 else ""
    if ok:
        level = logging.WARNING if (arrived or 0) > SLOW_ARRIVAL_SECONDS or took > SLOW_ANSWER_SECONDS else logging.INFO
        log.log(level, "discord: %s from member %s acknowledged: reached WooSarr %s after use, answer took %.1f s "
                "(%s)%s", what, interaction.user.id, late, took, how, clock)
        return True
    cause = ("Discord (or the network to it) delivered it late" if arrived is not None and arrived >= 3
             else "sending the answer was slow" if took >= 1.5 else "Discord no longer accepted it")
    log.warning("discord: %s from member %s answered too late: %s. It reached WooSarr %s after use and the answer "
                "took %.1f s (%s); Discord allows 3 s in total%s. The member saw \"The application did not respond\"",
                what, interaction.user.id, cause, late, took, how, clock)
    await _tell_too_late(interaction, what)
    return False


async def _tell_too_late(interaction: discord.Interaction, what: str) -> None:
    """DM the member that their command timed out, at most once a minute each."""
    uid, now = interaction.user.id, time.monotonic()
    if now - _too_late_dms.get(uid, -1e9) < TOO_LATE_DM_EVERY_SECONDS:
        return
    _too_late_dms[uid] = now
    try:
        await interaction.user.send(f"Sorry, Discord didn't get my answer to {what} in time, so it showed "
                                    f"\"The application did not respond\". Please try {what} again.")
        log.info("discord: told member %s by DM to try %s again", uid, what)
    except discord.HTTPException as exc:
        log.info("discord: couldn't DM member %s about %s (%s)", uid, what, exc.status)
