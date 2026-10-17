# Discord notes (P0-05)

Implementation: `woosarr/services/discord_bot.py` (discord.py 2.7).

## Setup the admin does (Setup page)

1. Developer Portal → New Application → **Bot**.
2. **Privileged Gateway Intents → Server Members Intent: on.** WooSarr checks this before saving the token: `GET /applications/@me` → `flags` must contain `GATEWAY_GUILD_MEMBERS` (1<<14) or `GATEWAY_GUILD_MEMBERS_LIMITED` (1<<15).
3. **Reset Token** and paste it into WooSarr. It's validated with `GET /users/@me`, stored encrypted as `discord.bot_token`, and never shown again.
4. Invite with the generated URL: `scope=bot applications.commands`, `permissions=0`. The bot needs no channel permissions: it only reads roles and members (intent) and answers `/link` with ephemeral replies.
5. Pick the server (`GET /users/@me/guilds`). One guild in v1 (D11).

## Runtime

- Intents: `guilds` + `members` only (no message content).
- `/link` is registered as a **guild** command (`tree.sync(guild=…)`), so it appears immediately (global commands can take up to an hour).
- On connect: full cache sync (`guild.roles`, `guild.fetch_members()`), then a full reconcile.
- Events → sync queue: `on_member_update` (roles), `on_member_join`, `on_raw_member_remove` (works without the member in cache), `on_guild_role_create/update/delete`.
- Reconnect/resume is discord.py's (`reconnect=True`). `PrivilegedIntentsRequired` and `LoginFailure` are shown on the dashboard and Setup page; **Restart bot** retries without a container restart.
- DB work runs in `asyncio.to_thread`; Plex calls run on the sync worker, so the gateway heartbeat never waits on Plex.

## `/link`

Ephemeral reply with two buttons:

- **Sign in with Plex (verified)**: WooSarr starts a Plex PIN with a throwaway client id and replies with a link button; it polls for up to 5 min, reads the identity, signs the token out and verifies it's dead (see `plex-notes.md`), then links with `method=oauth`, `verified=true`.
- **Enter Plex username**: modal with one text field; matches a known Plex user by username or email (case-insensitive); `method=username`, `verified=false`.

Rules (`services/link_service.py`, `linking.decide_link`): one Plex ↔ one Discord account; verified supersedes an unverified claim; a Discord account already linked elsewhere must be unlinked by an admin; per-user cooldown (Settings); every attempt audited (`link_self` / `link_failed`).
