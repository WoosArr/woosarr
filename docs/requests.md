# Requests through Seerr (P13-01)

Members request movies and TV shows from Discord with a slash command (default **`/request`**). WooSarr hands every request to [Seerr](https://github.com/seerr-team/seerr) (Overseerr and Jellyseerr use the same API and work too) **as the member's own Seerr user**. Seerr's permissions, quotas, auto-approval, 4K settings and Sonarr/Radarr routing all apply as if the member had used Seerr's website.

## Setup

1. In Seerr: **Settings → General → API Key**, copy it.
2. In WooSarr: **Integrations → Seerr (requests)**. Enter the address WooSarr can reach Seerr on, for example `http://10.0.1.101:5055`. Use Seerr's own address, not an SSO or login portal in front of it. Paste the key, press **Test**, then **Save**.
   - *Test* calls `GET /api/v1/status` (is this Seerr?), `GET /api/v1/auth/me` (is the key valid?) and `GET /api/v1/user` (how many users come from Plex?). Nothing is saved until **Save**.
   - The key is stored encrypted like the Plex and Discord tokens, redacted from logs and never returned by the API.
3. The bot registers the request command within a few seconds; Setup and the Requests page show the name it registered.
4. Check **Settings → Requests** (below).

## What members see

- `/request title:` suggests matching movies and shows as they type (up to 25, with year and "available" / "requested" notes; plain text, as Discord allows no links there). Picking a suggestion opens it directly; typing something and sending it shows a numbered list of matches linking to TMDB, with a menu (numbered the same) to choose from. Clicking a link only opens the page; it never selects the title.
- A private card with poster, overview, type and status. The title links to TMDB, and link buttons open TMDB, TVDB (shows) and IMDb; link buttons open the browser and never send a request. Shows get a season menu: leave it empty for every season that isn't on Plex yet, or pick seasons. Specials (season 0) aren't offered.
- **Request** sends it to Seerr; the reply says whether it's waiting for approval or already approved. Titles that are available or already requested have the button disabled.
- When the request is approved, declined, fails, or becomes (partly) available, the member gets a DM from the bot. Members who don't accept DMs from server members just don't get it; the Requests page still shows the status.

All replies are ephemeral (only the member sees them).

## Private or public (Settings → Requests → Visibility)

Searching, the "Which one?" list and the details card are always private to the member (they're personal menus). What happens after **Request** depends on the visibility:

| | Private (default) | Public |
|---|---|---|
| The request | Only the member sees the confirmation | Also posted in the **requests channel**: "@member requested **Title** (year)" with the poster card, a *Status* line and TMDB/TVDB/IMDb link buttons. Only the requester is mentioned (no @everyone or role pings). |
| Status changes | DM to the member | Reply to that post mentioning the member, and the post's *Status* line is updated. Falls back to a DM if the channel or post is gone or the bot can't post there. |
| **Only in this channel** | n/a | Optional. `/request` used anywhere else answers "Please make requests in #channel" (privately). To also hide the command in other channels, use *Server Settings → Integrations → WooSarr → /request → Channels*. A bot can't set that itself. |

Public needs a channel. The bot needs **View Channels, Send Messages, Embed Links and Read Message History** there (*Public requests* in Integrations → Bot features); the channel list marks channels where it can't post. If it can't post, the request is still made and the problem is logged (`bot can't post requests in #… missing …`). Whether a request was public is decided when it's made, so switching later doesn't move existing requests' updates.

## Who may request (Settings → Requests)

| Setting | Default | Meaning |
|---------|---------|---------|
| **Command name** | `request` | 1–32 characters: lowercase letters, numbers, `-`, `_`. Can't be `link`, `unlink` or `invite`. See *Another bot already has /request*. |
| **All linked members** | on | Off = only members with one of the ticked roles. Discord admins (Manage Server) can always request. |
| **Verified links only** | on | Only members whose link is verified (Sign in with Plex) may request. A username-only `/link` is a claim anyone could make, and requests are made as that Plex account's Seerr user. |
| **Create missing Seerr users** | on | If the member's Plex account has no Seerr user yet, WooSarr imports it (`POST /api/v1/user/import-from-plex` with that Plex id). Seerr gives it its **default permissions** (Seerr → Settings → Users). Off = the member is told to ask an admin. |
| **Notify members** | on | DM status changes. |

A member always needs a link: requests are made as their Seerr user, which is found from their Plex account. Matching order: Seerr user with the same **Plex id**, then the same **email**, then the same **Plex username**.

## Another bot already has /request

Discord lets every bot have its own `/request`, so nothing breaks. Members see both in the command picker, each labelled with the bot's name, and may pick the wrong one. Options:

1. **Rename WooSarr's command** in Settings → Requests (for example `/plexrequest` or `/req`). The bot re-registers it right away. Discord treats a renamed command as a new one, so permissions set for the old name under *Server Settings → Integrations → WooSarr* must be set again.
2. **Hide the other bot's command**: *Server Settings → Integrations → (other bot) → Commands → /request*, then turn it off for @everyone or limit it to roles or channels. This needs no change in WooSarr.
3. Limit who sees WooSarr's command the same way (Integrations → WooSarr).

Discord doesn't let one bot see another bot's commands, so WooSarr can't detect the clash itself.

## Status tracking

WooSarr remembers the requests made through Discord (`media_requests` table) and re-reads the open ones from Seerr (`GET /api/v1/request/{id}`) every 5 minutes for 60 days: pending → approved → (partly) available, or declined / failed. Requests deleted in Seerr show as *Removed in Seerr*. **Requests → Check status now** runs the check immediately. Requests made on Seerr's website aren't listed. Seerr is the source of truth for those.

## Diagnostics (logs API)

Every step logs under `woosarr.seerr`, `woosarr.requests` and `woosarr.discord.requests`, and requests are audited (`request_created`, `request_denied`, `request_failed`, `request_status`, `seerr_connected`):

```bash
curl -H "Authorization: Bearer $KEY" "http://<host>:31337/api/v1/logs?q=seerr&limit=200"
curl -H "Authorization: Bearer $KEY" "http://<host>:31337/api/v1/logs?q=request&limit=200"
```

| Log line | Meaning |
|----------|---------|
| `discord commands registered in guild … /request` | The command is live (also shows a rename: `request command was /old`). |
| `seerr GET /search -> 403` | API key wrong or regenerated in Seerr; enter it again on Setup. |
| `no seerr user for plex <name>` | Member's Plex account isn't in Seerr and import is off, or Seerr couldn't import it (not a Plex friend with library access). |
| `request <id> created for discord … as seerr user …` | Request made; the Seerr user id it ran as. |
| `request suggestions … unavailable: timeout` | Seerr didn't answer within Discord's 3 s autocomplete limit; members can still send the text and pick from the menu. |
| `request notice for … not sent: member doesn't accept DMs` | The member has DMs from server members turned off. |
| `request poll: N open, M changed, K to notify` | Periodic status check. |
| `request <id> posted in #channel` / `request notice for … posted in #channel` | Public request post and status reply. |
| `bot can't post requests in #… missing …` | Give the bot those permissions in the requests channel. |
| `request by … refused: used in channel …` | *Only in this channel* is on and the member used another channel. |

## API details used (Seerr develop, 2026-10)

- `X-Api-Key` authenticates as user 1 (the admin). `X-API-User: <id>` acts as that user, which is how quotas and auto-approve follow the member.
- `GET /api/v1/user` returns `plexId` and `email` to callers with Manage Users (the API key does).
- `POST /api/v1/request {mediaType, mediaId, seasons?, is4k: false}`: 201 created, 403 no permission / quota / blocklisted, 409 already requested, 202 no seasons left.
- `GET /api/v1/search?query=` needs strict percent-encoding (WooSarr encodes `!'()*` and spaces as `%20`).
- Request status 1 pending, 2 approved, 3 declined, 4 failed, 5 completed; media status 2 pending, 3 processing, 4 partly available, 5 available.

Not built yet: 4K requests, Ombi, direct Sonarr/Radarr, audiobooks (Listenarr), Seerr webhooks (polling is used instead, so Seerr doesn't need to reach WooSarr).
