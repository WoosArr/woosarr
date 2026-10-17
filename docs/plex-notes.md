# Plex notes (P0-02, P0-03, P0-07)

Sources: python-plexapi 4.18.3 source (`plexapi/myplex.py`), read in full for the parts below. Items marked **LIVE** are confirmed only by running WooSarr's checks against a real server; their results appear in the logs (`GET /api/v1/logs?q=plex+write+check`) and the audit trail (`plex_write_check`).

## Owner sign-in (P0-02)

- Flow: `MyPlexPinLogin(oauth=True)` → `oauthUrl(forwardUrl)` (opens `https://app.plex.tv/auth/#!?clientID=…&code=…`) → poll `checkLogin()` until `token` is set. WooSarr polls from the browser every 2 s; pins expire after 10 min on our side.
- The token is bound to `X-Plex-Client-Identifier`. WooSarr generates `woosarr-<uuid4>` once per install (`settings` key `plex.client_id`) and puts it in `plexapi.BASE_HEADERS` (the dict every plexapi request copies), along with `X-Plex-Product: WooSarr`.
- Stored session: only the owner token, Fernet-encrypted as secret `plex.token`. Account id/username and the chosen server are plain settings.
- `MyPlexJWTLogin` (JWT device auth) isn't needed: PIN/OAuth tokens work for every call WooSarr makes. Revisit if Plex retires classic tokens.
- Revocation: a 401 from plex.tv (`Unauthorized`) marks the gateway `auth_failed`; the dashboard and Setup page say "sign in again". Disconnect calls `signout()` before deleting the token.

## Sharing semantics (P0-03)

| Question | Answer | How we know |
|----------|--------|-------------|
| (a) Does a share update replace or merge? | `PUT …/shared_servers/{id}` with `library_section_ids` sends the full list; WooSarr treats it as **replace**. | Source; **LIVE** step "add one library" checks the old ones survive and nothing else appears. |
| (b) What does `MyPlexServerShare.sections()` return? | **All** libraries, each with `shared` true/false. Only `shared=True` count. | Source (`Section.shared`). |
| (c) How to revoke the last library? | `DELETE …/shared_servers/{shareId}`. `updateFriend(removeSections=True)` without `sections` hits the "warning, do nothing" branch for a user who already has a share, so WooSarr doesn't use `updateFriend`. | Source; **LIVE** step "restore original" for a user who started with no libraries. |
| (d) Do updates reset `allowSync`/`allowChannels`/`allowCameraUpload`/filters? | WooSarr never calls `api/v2/sharings/{userId}` (where those live) and compares them before/after every write-check step. | Source; **LIVE** `settings_unchanged` per step. |
| (e) Home/managed users? | They appear in `account.users()` with `home=1` and no email, and have server shares like friends. | Source; **LIVE** read check lists them. |
| (f) Map `LibrarySection` ↔ shared `Section`? | `Section.key` == the server's library section key. plex.tv's own section id comes from `GET https://plex.tv/api/servers/{machineId}` (children `Section id= key= title= type=`), which also lists all libraries, so **no LAN connection to the server is needed**. WooSarr writes by key, never by title (two libraries can share a title). | Source (`_getSectionIds`, `Section`). |

Write paths in `woosarr/plex_api_gateway.py`:

- user has a share, non-empty set → `PUT shared_servers/{shareId}` `{"server_id", "shared_server": {"library_section_ids": [...]}}`
- user has no share, non-empty set → `POST shared_servers` `{"server_id", "shared_server": {"library_section_ids": [...], "invited_id": <user id>}}`
- empty set → `DELETE shared_servers/{shareId}` (nothing if there's no share)

### Verifying on your server

Setup → Plex → **Verify sharing on a test user…** rewrites the user's current set, adds one library, then restores the original, checking shares and per-friend settings after each step. Keep dry-run on until it passes.

## Link token invalidation (P0-07, N9)

- `/link` → "Sign in with Plex" uses a **throwaway** client identifier (`woosarr-link-<uuid4>`) so the user's sign-in is a separate device.
- WooSarr reads `MyPlexAccount(token).id/username`, then calls `signout()` (`DELETE https://plex.tv/api/v2/users/signout`, which plexapi documents as "Invalidates the authentication token") and **proves** it: a fresh `MyPlexAccount(token)` must raise `Unauthorized`. If it doesn't, the link isn't completed and an error is logged.
- The token is never stored, logged (redaction) or returned; `capture_identity_once` invalidates it even when reading the identity fails.
- **LIVE**: log line `link token invalidated and verified dead` after a real `/link`.
