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

## `/unlink`

Ephemeral confirmation (Unlink / Cancel). Effect follows **Settings → When a member unlinks themselves**:

- *Remove role-granted libraries* (default): a one-user sync with no roles runs first (manual grants and exempt libraries stay; dry-run respected), then the link is deleted, inside the same sync lock so no queued sync can re-grant in between.
- *Leave their access as it is*: the link is deleted; WooSarr stops managing the account.

Audited as `unlink_self` (with the policy).

## `/invite email [member]`

- Members holding a role in **Settings → Roles allowed to /invite** may invite **themselves**. Discord admins (*Manage Server* or *Administrator*) may invite for any member (`member` option).
- Libraries = what the target member's roles grant (exempt libraries are never granted by roles). No libraries → refused.
- Refused in dry-run, if the member is already linked or has a pending invite, if the email/username is already on the server (use `/link`), and within the link cooldown. Every attempt is audited (`invite_sent` / `invite_failed`).
- Sends `POST shared_servers` with `invited_email` (python-plexapi `inviteFriend`). When the person accepts, a full sync finds them by email/username and links them (`method=invite`; verified when an admin sent it). Pending invites are listed on the Users page.
