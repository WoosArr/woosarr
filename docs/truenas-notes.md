# TrueNAS notes (P2-17, P12-10)

How to run WooSarr on TrueNAS SCALE / Community Edition 24.10 (Electric Eel) or later, where apps are Docker containers. Two ways: **Install via YAML** (works today) and, once accepted, the **TrueNAS Apps catalog** (the folder for it is ready in [`truenas/catalog/woosarr`](../truenas/catalog/woosarr)).

## How WooSarr runs on TrueNAS

TrueNAS apps run as the built-in **apps** user (uid/gid **568**), never as root. WooSarr supports that start directly: when the container starts as a non-root user, `docker/entrypoint.sh` skips `PUID`/`PGID` and the ownership fix and runs WooSarr as that user. It needs no Linux capabilities, so all of them are dropped. `/config` must already belong to that user; a dataset you create for it (below) or an ixVolume (catalog app) does.

CI tests this start on every build (`scripts/smoke-test.sh` as 568 with `--cap-drop ALL`, and `truenas/compose.yaml` started as it is).

## Install via YAML

1. **Create a dataset for WooSarr's data** (database, logs, `secrets/master.key`), e.g. `tank/apps/woosarr`: *Datasets* → select the parent → **Add Dataset** → name `woosarr`, Dataset Preset **Apps** → Save.
2. **Make the apps user its owner**: select the dataset → *Permissions* → **Edit** → Owner `apps`, Group `apps`, tick *Apply User* and *Apply Group* → Save. (Or in *System → Shell*: `chown 568:568 /mnt/tank/apps/woosarr`.)
3. *Apps* → **Discover Apps** → **⋮** (top right) → **Install via YAML**. Name it `woosarr` and paste [`truenas/compose.yaml`](../truenas/compose.yaml), changing `/mnt/tank/apps/woosarr` to your dataset's path (and the host port, if 31337 is taken). **Save**.
4. When the app shows *Running*: *Apps* → *Installed* → WooSarr → *Workloads* → the logs icon. The **first-run setup code** is near the top:

   ```
   ================================================================
    WooSarr first-run setup code: 1a2b-3c4d-5e6f
   ```

5. Open `http://<truenas-ip>:31337`, enter the code and carry on with the README's [First run](../README.md#first-run).

### Updating

The YAML app uses `ghcr.io/woosarr/woosarr:latest`. *Apps* → *Installed* → WooSarr → **Update** (or *Pull image* and restart, depending on your TrueNAS version) fetches the newest release. To pin a version, edit the YAML and change `:latest` to e.g. `:1.1.1`.

**Development builds:** change the image to `ghcr.io/woosarr/woosarr:develop`. Take a snapshot of the dataset first: a development build may migrate the database forward, and an older version can't read it afterwards. Change back to `:latest` once a release includes those changes.

### Backups

Everything WooSarr keeps is in the dataset: snapshot or replicate it like any other. The master key in `secrets/master.key` decrypts the stored Plex and Discord tokens; keep a copy outside the dataset too ([admin guide → Backup and restore](admin-guide.md#backup-and-restore-p12-03)).

### Reverse proxy

Port 31337 is plain HTTP. Behind TrueNAS's *Nginx Proxy Manager* app (or any other proxy), set one of the two commented variables in the YAML so session cookies are marked `Secure` (README → [HTTPS and reverse proxies](../README.md#https-and-reverse-proxies)).

## TrueNAS Apps catalog (P12-10)

**Status: not submitted.** The app folder is in [`truenas/catalog/woosarr`](../truenas/catalog/woosarr), laid out as `ix-dev/community/woosarr/` in [truenas/apps](https://github.com/truenas/apps):

| File | What it holds |
|------|---------------|
| `app.yaml` | Title, description, version, runs as 568, no capabilities; icon and screenshots as `media.sys.truenas.net/apps/woosarr/…` URLs (their tools accept no others; the reviewer uploads the files there, see below) |
| `item.yaml` | Catalog card: categories, icon, screenshots, tags |
| `ix_values.yaml` | Image `ghcr.io/woosarr/woosarr` at the current release, `tag@digest` (their Renovate bot keeps it current from our semver tags) |
| `questions.yaml` | The install form: timezone, extra environment variables, user/group (568), web port (31337, free in their catalog), `/config` storage (ixVolume or host path), labels, resource limits |
| `templates/docker-compose.yaml` | Their library: one container as `run_as`, `WOOSARR_PORT` = the chosen port, Python health check (the image has no curl/wget), the permissions helper that makes `/config` belong to the user |
| `templates/test_values/basic-values.yaml` | What their CI installs |

The **TrueNAS catalog check** workflow ([`.github/workflows/truenas-catalog.yml`](../.github/workflows/truenas-catalog.yml)) runs whenever the folder changes on `develop`. It copies the folder into a checkout of truenas/apps, then runs:

- their port check;
- their catalog validator;
- their `ci.py`, which renders the app with their library and starts it until it's healthy.

Its last step prints the metadata their tools regenerate (`lib_version`, `lib_version_hash`, `version`), to copy back here before submitting. To try a development image, run it by hand with a `tag` (e.g. `develop`).

### Submitting (human steps)

TrueNAS's [contribution guide](https://github.com/truenas/apps/blob/master/CONTRIBUTIONS.md) asks for an issue first ("open an issue or comment before starting work"), then a pull request, ideally opened as a draft so maintainers can steer early. Only the `community` train takes outside apps.

1. **Issue** in [truenas/apps](https://github.com/truenas/apps/issues/new), title *Add WooSarr to the community train*:

   > WooSarr (https://github.com/WoosArr/woosarr, AGPL-3.0) gives Discord members access to Plex libraries based on their Discord roles, and keeps that access in step as roles change: members link their Plex account with a slash command, admins map roles to libraries in a web UI.
   >
   > - Image: `ghcr.io/woosarr/woosarr`, amd64 and arm64, semver release tags (e.g. `1.1.1`).
   > - Runs as any non-root user (568 by default) with no capabilities; one port (31337, free in the catalog); one volume (`/config`); health endpoint `/api/health`.
   > - An app folder in the community-train layout is ready and passes `ci.py --test-file basic-values.yaml`: https://github.com/WoosArr/woosarr/tree/main/truenas/catalog/woosarr
   >
   > I'm the maintainer and would like to open the PR.

2. **Pull request** (draft first). Fork `truenas/apps`, copy `truenas/catalog/woosarr` to `ix-dev/community/woosarr`, then in the fork:

   ```bash
   devbox run copy-lib                    # vendors the library, fills lib_version_hash
   devbox run metadata community woosarr  # capabilities, item.yaml, version
   devbox run app-test community woosarr  # the same test as the workflow above
   ```

   Commit what they generate (the guide says the vendored library ships with the app). Open the PR with their *app_addition* template, which asks for:
   - the issue link;
   - **AI:** tick *Part or All of this PR was generated by an LLM* (these files were);
   - upstream https://github.com/WoosArr/woosarr, docs (the README), app version;
   - the icon and screenshots, which the reviewer uploads to their CDN (fix the URLs in `app.yaml`/`item.yaml` if theirs differ): icon https://raw.githubusercontent.com/WoosArr/woosarr/main/unraid/icon.png; screenshots 1-4 `docs/images/dashboard-dark.png`, `roles.png`, `libraries.png`, `users.png` (raw URLs on `main`, same pattern);
   - special notes: *the first-run setup code is printed in the app's logs*.

3. After it merges: update the status above, add the catalog install to the README, and copy their final `app.yaml`/`item.yaml` back here. Their Renovate bot bumps the image in their copy on each release.

## To verify on a real TrueNAS (human gate)

- Install via YAML on a dataset owned by apps; setup code in the app logs; WebUI opens.
- App restart and *Update* keep settings and secrets.
- After a catalog listing: install from *Discover Apps* with an ixVolume and with a host path.
