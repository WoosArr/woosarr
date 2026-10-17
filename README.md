# WooSarr

Automatically grants and removes **Plex library access** based on **Discord roles**.

- Role grants are **additive** (a user keeps a library if any role, manual grant, or exemption keeps it)
- Per-library **exemptions** that role changes never touch
- Manual per-user grants from the web UI
- Only Plex users with a **linked Discord account** are managed
- Users link themselves with `/link` (Plex username, or Plex sign-in for a verified link; no Plex tokens are stored)
- Web UI on port **2000** with first-run admin setup and optional SAML SSO
- Runs as a single Docker container, with an Unraid template in `unraid/`

> **Status:** early development. See [docs/action-plan.md](docs/action-plan.md) for the full plan, decisions and task status.

## Run

```bash
docker run -d --name woosarr -p 2000:2000 -v /path/to/appdata/woosarr:/config ghcr.io/woosarr/woosarr:latest
```

Open `http://<host>:2000` and follow the setup wizard. Back up the `/config` volume: it holds the database and the key that encrypts your stored secrets.

## Develop

```bash
cd backend && pip install -e ".[dev]" && python -m pytest
cd frontend && npm install && npm run dev
```

## Releases

Pushes to `main` and version tags (`v1.2.3`) build a multi-arch image and publish it to GitHub Container Registry via `.github/workflows/docker-publish.yml`.

## License

MIT
