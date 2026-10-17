# Discord notes (P0-05)

## Bot permissions by feature

Grant only what the features you use need. WooSarr reads the bot's actual permissions in your server and shows each feature as available or not (Setup → Discord → *Bot features and permissions*); unavailable features are disabled in the UI and refused with the missing permission named. The same page builds an invite link with exactly the permissions for the features you tick; using it again updates the bot's permissions without removing it.

| Feature | What it does | Permissions | Also needs |
|---------|--------------|-------------|------------|
| **Core** (always) | Read roles and members, react to role changes | *(none)* | **Server Members Intent** (privileged, Developer Portal → Bot) |
| **/link, /unlink, /invite** | Slash commands with private replies | *(none)* | `applications.commands` scope (in the invite link) |
| **Link-status roles** | Give/remove the Verified, Unverified and Linked roles | **Manage Roles** | The bot's highest role must be **above** those roles (Server Settings → Roles, drag the bot's role up) |
| **Create roles** | Create link-status roles and per-library roles from WooSarr | **Manage Roles** | New roles are created at the bottom, below the bot's role |
| **Reaction posts** | Post the message, add its reactions, give/remove roles when members react | **View Channels, Send Messages, Add Reactions, Read Message History** (in that channel) and **Manage Roles** | The bot's highest role must be **above** the roles the reactions give; guild reactions intent (not privileged; always on) |
| **Pin reaction posts** | Pin the post | **Pin Messages** | Optional; without it the post is still sent, just not pinned |
| **Create the reaction channel** | Create the channel if it doesn't exist | **Manage Channels** | Optional; pick an existing channel instead |

Permission integers (for building links by hand): Manage Roles `268435456`, Manage Channels `16`, View Channels `1024`, Send Messages `2048`, Add Reactions `64`, Read Message History `65536`, Pin Messages `2251799813685248`. All features: `2251800082189392`.

Channel overrides win over server permissions: if a reaction post channel denies the bot *Send Messages* or *Add Reactions*, the channel list marks it "bot can't post here".

WooSarr never needs Administrator, Kick/Ban, Manage Messages or Message Content.

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

## Link-status roles

*Settings → General → Discord roles for Plex link status*: pick (or **Create**) a role for **Verified**, **Unverified** and/or **Linked** members. After every sync the bot compares each member's roles with their link and only changes what differs (it never touches other roles). Bots are skipped. Needs *Link-status roles* (Manage Roles + role hierarchy).

These roles can themselves be mapped to libraries on the Roles page, e.g. give every verified member a library.

## Per-library roles

Libraries page → **Create role** creates `Plex · <library>` and maps it to that library. Assign the role in Discord and members get the library on the next sync. Needs *Create roles*.

## Reaction posts

Reactions page:

1. **New post**: message text, channel (existing, or a new channel name; an existing channel with that name is reused), pin on/off, and up to 20 reactions (Discord's limit). Each reaction gives exactly **one Discord role**, chosen from a dropdown (or *＋ Create new role…*); its label in WooSarr is always the role's name. Pick the emoji with the searchable picker (server emoji + common emoji) or paste any emoji / `<:name:id>`. Bot/integration roles can't be chosen.
2. **Save & post**: the bot sends (or edits) the message, adds each reaction, pins it if allowed. Saving again updates the same message; changing the channel posts a new one.
3. Members **react** to get the roles and **remove the reaction** to drop them; a role is kept if another reaction the member kept still gives it. Plex libraries then follow from the normal role → library mappings (Roles page) for linked members, so a reaction role is just like any other role.
4. On every (re)connect the bot re-reads each post's reactions and gives/removes roles to match (catches changes made while it was offline). Disabling a post stops reactions from changing roles; deleting a post leaves the roles members already have.
