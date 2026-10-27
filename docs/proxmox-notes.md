# Proxmox VE notes (P2-18)

Proxmox VE has no app store for Docker apps, so WooSarr runs in a container or VM you create. Two ways:

| | Docker Compose in an LXC or VM (recommended) | OCI image as an LXC (Proxmox VE 9.1+) |
|---|---|---|
| Status in Proxmox | Standard, long-established | Technology preview |
| What you run | Docker inside a small Debian container or VM, then WooSarr's normal `compose.yaml` | WooSarr's image directly as a container, no Docker |
| Updates | `docker compose pull && docker compose up -d` | Pull the new image, recreate the container on the same `/config` volume |
| Health check | Docker's, as everywhere else | None (Proxmox ignores the image's `HEALTHCHECK`) |
| Verified by WooSarr | The compose file is tested in CI; not yet on a real Proxmox | Not yet on a real Proxmox |

Either way WooSarr only needs outbound internet (Plex, Discord) and one inbound port, 31337.

## Docker Compose in an LXC (recommended)

1. **Create the container.** Download the *Debian 13* template (*local* storage → *CT Templates* → **Templates**), then **Create CT**:
   - *Unprivileged container* ticked (the default);
   - 1 core, 1024 MB memory and 8 GB disk are plenty;
   - network: DHCP or a static IP on your LAN bridge.
2. **Allow Docker inside it.** Before starting it: the container → *Options* → **Features** → tick **keyctl** and **Nesting**. (CLI: `pct set <id> --features nesting=1,keyctl=1`.)
3. **Install Docker.** Start the container, open its *Console*, log in as root and run:

   ```bash
   apt update && apt install -y curl
   curl -fsSL https://get.docker.com | sh
   ```

4. **Run WooSarr** with the standard [Docker Compose](../README.md#docker-compose) steps:

   ```bash
   mkdir -p /opt/woosarr && cd /opt/woosarr
   curl -fsSLO https://raw.githubusercontent.com/WoosArr/woosarr/main/compose.yaml
   docker compose up -d
   docker compose logs woosarr   # the first-run setup code
   ```

5. Open `http://<container-ip>:31337` and follow [First run](../README.md#first-run).

**Updating:** `cd /opt/woosarr && docker compose pull && docker compose up -d`. Development builds: set `WOOSARR_IMAGE=ghcr.io/woosarr/woosarr:develop` in a `.env` beside `compose.yaml` (take a backup first; database changes don't go backwards).

**Backups:** WooSarr's data is in `/opt/woosarr/config` inside the container, so Proxmox's own container backups (*Datacenter → Backup*, or Proxmox Backup Server) include it. Keep a copy of `config/secrets/master.key` somewhere else too ([admin guide → Backup and restore](admin-guide.md#backup-and-restore-p12-03)).

**A VM instead?** Equally fine and fully isolated: any small Debian or Ubuntu VM, then steps 3-5. Proxmox's documentation recommends a VM over an LXC for Docker; the LXC above is lighter and works for an app like WooSarr, which needs no special kernel features.

## OCI image as an LXC (Proxmox VE 9.1+, technology preview)

Proxmox VE 9.1 can create a container straight from an OCI (Docker) image. WooSarr's image works this way in principle: one process, all data under `/config`, `PUID`/`PGID` applied by its entrypoint. **These steps haven't been tried on a real Proxmox yet** and the feature's screens may change while it's a preview; this page will say once they're verified (plan P2-18).

1. **Pull the image as a template:** *local* storage (or any storage that holds CT templates) → *CT Templates* → **Pull from OCI Registry** → reference `ghcr.io/woosarr/woosarr:latest` → **Pull**.
2. **Create CT** from that template:
   - unprivileged;
   - 1 core, 1024 MB memory;
   - root disk 4 GB;
   - network on your LAN bridge (it gets its own IP).
3. **Give `/config` its own volume** so updates don't lose data: the container → *Resources* → **Add → Mount Point** → a storage volume (e.g. 2 GB) with path `/config`. A storage-backed mount point is simplest; a bind mount from the host needs the unprivileged ID mapping (host ids shifted by 100000).
4. Optional, under the container's *Options* → environment variables:
   - `PUID`/`PGID` (default 99/100);
   - `TZ`;
   - `WOOSARR_LOG_LEVEL`.
5. **Start** it. The first-run setup code is in the container's console output / log (`pct console <id>` or the *Console* tab). Open `http://<container-ip>:31337`.

**Updating:** pull the newer image (step 1 again), create a new container from it with the same `/config` mount point (detach it from the old container first), start it, then remove the old one. Back up first: a container backup includes the mount point.

## To verify on a real Proxmox (human gate)

- Compose in a Debian 13 LXC with `nesting=1,keyctl=1`: setup code, WebUI, restart, `docker compose pull` update keeps settings.
- OCI image as an LXC on 9.1+: the template pulls, the entrypoint runs (as root, then drops to `PUID`), `/config` on a mount point survives recreating the container. Record the exact screen names here and drop the "haven't been tried" note.
