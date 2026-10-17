"""Plex integration: owner sign-in, server choice, gateway, checks (P3-01, P3-02, P9-02, P0-03, P0-07).

* The owner signs in with Plex OAuth in the browser (PIN flow): WooSarr never
  sees a password. The resulting token is stored encrypted (D5).
* Every install has its own stable X-Plex-Client-Identifier; Plex ties the
  token to it.
* `/link` sign-ins use a throwaway client id, read the Plex identity once and
  then sign the token out, proving it no longer works (N9).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import plexapi
from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin

from woosarr import __version__
from woosarr.core import kv
from woosarr.core import secret_store as names
from woosarr.db.session import Database
from woosarr.linking import PlexIdentity, capture_identity_once
from woosarr.plex_api_gateway import PlexApiGateway, PlexAuthError
from woosarr.plex_gateway import PlexGateway

log = logging.getLogger("woosarr.plex")

PIN_TTL_SECONDS = 600
K_CLIENT_ID = "plex.client_id"
K_SERVER = "plex.server"  # {"machine_id", "name"}
K_ACCOUNT = "plex.account"  # {"id", "username"} of the signed-in owner


def configure_plexapi(client_id: str) -> None:
    """Identify as WooSarr on every python-plexapi request."""
    plexapi.BASE_HEADERS.update(
        {
            "X-Plex-Client-Identifier": client_id,
            "X-Plex-Product": "WooSarr",
            "X-Plex-Version": __version__,
            "X-Plex-Device": "WooSarr",
            "X-Plex-Device-Name": "WooSarr",
            "X-Plex-Platform": "WooSarr",
        }
    )


@dataclass
class PendingPin:
    pin: Any
    created: float = field(default_factory=time.monotonic)
    purpose: str = "owner"


class PlexService:
    def __init__(
        self,
        db: Database,
        secrets: Any,
        pin_factory: Callable[..., Any] = MyPlexPinLogin,
        account_factory: Callable[..., Any] = MyPlexAccount,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._pin_factory = pin_factory
        self._account_factory = account_factory
        self._pins: dict[str, PendingPin] = {}
        self._lock = threading.Lock()
        self._gateway: PlexApiGateway | None = None
        self._gateway_key: tuple[str, str] | None = None
        with db.session() as s:
            client_id = kv.get(s, K_CLIENT_ID)
            if not client_id:
                client_id = f"woosarr-{uuid.uuid4()}"
                kv.put(s, K_CLIENT_ID, client_id)
        self.client_id = client_id
        configure_plexapi(client_id)

    # ---- state ----------------------------------------------------------------

    def _token(self) -> str | None:
        with self._db.session() as s:
            try:
                return self._secrets.get(s, names.PLEX_TOKEN)
            except Exception:  # undecryptable after a key change: treat as signed out
                log.exception("stored Plex token can't be read; sign in again")
                return None

    def server(self) -> dict[str, str] | None:
        with self._db.session() as s:
            return kv.get(s, K_SERVER)

    def account_info(self) -> dict[str, Any] | None:
        with self._db.session() as s:
            return kv.get(s, K_ACCOUNT)

    def gateway(self) -> PlexGateway | None:
        token, server = self._token(), self.server()
        if not token or not server:
            return None
        key = (token, server["machine_id"])
        with self._lock:
            if self._gateway is None or self._gateway_key != key:
                self._gateway = PlexApiGateway(lambda: self._account_factory(token=token), server["machine_id"])
                self._gateway_key = key
            return self._gateway

    def status(self) -> dict[str, Any]:
        token, server, acct = self._token(), self.server(), self.account_info()
        gw = self._gateway if self._gateway_key and server and self._gateway_key[1] == server["machine_id"] else None
        state = gw.state if gw else None
        if not token:
            detail = "Not signed in to Plex"
        elif not server:
            detail = "Signed in; choose a server"
        elif state and state.auth_failed:
            detail = "Plex rejected the stored sign-in; sign in again"
        elif state and state.ok is False:
            detail = f"Can't reach plex.tv: {state.last_error}"
        else:
            detail = None
        return {
            "configured": bool(token and server),
            "connected": bool(token and server and (state is None or state.ok is not False)),
            "signed_in": bool(token),
            "account": acct,
            "server": server,
            "detail": detail,
            "last_ok_at": state.last_ok_at if state else None,
            "last_error": state.last_error if state else None,
        }

    # ---- owner sign-in (P3-01) -----------------------------------------------

    def _expire_pins(self) -> None:
        now = time.monotonic()
        for pid in [p for p, v in self._pins.items() if now - v.created > PIN_TTL_SECONDS]:
            del self._pins[pid]

    def start_login(self, forward_url: str | None, purpose: str = "owner", client_id: str | None = None) -> tuple[str, str]:
        headers = {"X-Plex-Client-Identifier": client_id} if client_id else None
        pin = self._pin_factory(oauth=True, headers=headers)
        url = pin.oauthUrl(forward_url)
        pin_id = uuid.uuid4().hex
        with self._lock:
            self._expire_pins()
            self._pins[pin_id] = PendingPin(pin, purpose=purpose)
        log.info("plex sign-in started purpose=%s", purpose)
        return pin_id, url

    def poll_token(self, pin_id: str) -> tuple[str, str | None]:
        """("pending"|"done"|"expired", token-or-None). Token is never logged."""
        with self._lock:
            self._expire_pins()
            pending = self._pins.get(pin_id)
        if pending is None:
            return "expired", None
        pin = pending.pin
        if pin.checkLogin() and pin.token:
            with self._lock:
                self._pins.pop(pin_id, None)
            return "done", pin.token
        if pin.expired:
            with self._lock:
                self._pins.pop(pin_id, None)
            return "expired", None
        return "pending", None

    def complete_owner_login(self, token: str) -> dict[str, Any]:
        account = self._account_factory(token=token)
        info = {"id": str(account.id), "username": account.username or account.title or account.email}
        with self._db.session() as s:
            self._secrets.set(s, names.PLEX_TOKEN, token)
            kv.put(s, K_ACCOUNT, info)
        with self._lock:
            self._gateway = None
        log.info("plex owner signed in as %s", info["username"])
        return info

    def owner_account(self) -> Any:
        token = self._token()
        if not token:
            raise PlexAuthError("not signed in to Plex")
        try:
            return self._account_factory(token=token)
        except Unauthorized as exc:
            raise PlexAuthError("Plex rejected the stored sign-in; sign in again") from exc

    # ---- server choice (P3-02) -----------------------------------------------

    def list_servers(self) -> list[dict[str, Any]]:
        """Servers this account owns. Shared-with-you servers are left out: only an
        owner can change who a server's libraries are shared with."""
        out = []
        for r in self.owner_account().resources():
            if "server" not in (r.provides or "").split(",") or not r.owned:
                continue
            out.append({
                "machine_id": r.clientIdentifier,
                "name": r.name,
                "online": bool(r.presence),
                "version": r.productVersion,
                "platform": r.platform,
            })
        return out

    def select_server(self, machine_id: str) -> dict[str, str]:
        match = next((s for s in self.list_servers() if s["machine_id"] == machine_id), None)
        if match is None:
            raise ValueError("no server you own has that id")
        server = {"machine_id": machine_id, "name": match["name"]}
        with self._db.session() as s:
            kv.put(s, K_SERVER, server)
        log.info("plex server selected name=%s machine=%s", match["name"], machine_id)
        return server

    def disconnect(self) -> None:
        token = self._token()
        if token:
            try:
                self._account_factory(token=token).signout()
                log.info("plex owner token signed out")
            except Exception:  # best effort: the token is deleted locally either way
                log.warning("could not sign out the Plex token at plex.tv", exc_info=True)
        with self._db.session() as s:
            self._secrets.delete(s, names.PLEX_TOKEN)
            kv.delete(s, K_SERVER)
            kv.delete(s, K_ACCOUNT)
        with self._lock:
            self._gateway, self._gateway_key = None, None
        log.info("plex disconnected")

    # ---- checks (P9-02, P0-03) -------------------------------------------------

    def read_check(self) -> dict[str, Any]:
        """Read-only end-to-end check: libraries, users, every user's shares."""
        gw = self.gateway()
        if gw is None:
            raise PlexAuthError("Plex isn't set up yet")
        libs = gw.list_libraries()
        users = gw.list_users()
        per_user, errors = [], []
        for u in users:
            try:
                shared = sorted(gw.get_user_library_ids(u.plex_user_id))
                per_user.append({"plex_user_id": u.plex_user_id, "username": u.username, "libraries": shared})
            except Exception as exc:  # report every failure, keep going
                log.exception("read check failed for user %s", u.plex_user_id)
                errors.append(f"{u.username}: {type(exc).__name__}: {exc}")
        known = {lib.library_id for lib in libs}
        unknown = sorted({k for p in per_user for k in p["libraries"]} - known)
        result = {"libraries": [lib.__dict__ for lib in libs], "users": len(users), "shares": per_user,
                  "unknown_library_keys": unknown, "errors": errors}
        log.info("plex read check: %s libraries, %s users, %s errors, unknown keys=%s",
                 len(libs), len(users), len(errors), unknown)
        return result

    def write_check(self, plex_user_id: str) -> dict[str, Any]:
        """Verify share semantics on one test user, then restore it (P0-03 a, c, d).

        1. rewrite the same complete set: shares and per-friend settings unchanged
        2. add one library: the old ones must survive (replace vs merge)
        3. restore the original set
        Every step is logged; the original access is restored even on failure.
        """
        gw = self.gateway()
        if not isinstance(gw, PlexApiGateway):
            raise PlexAuthError("Plex isn't set up yet")
        steps: list[dict[str, Any]] = []
        gw._check_not_owner(plex_user_id)
        original = gw.get_user_library_ids(plex_user_id)
        settings_before = gw.user_settings(plex_user_id)
        libs = [lib.library_id for lib in gw.list_libraries()]
        extra = next((k for k in libs if k not in original), None)

        def step(name: str, wrote: frozenset[str] | None, expect: frozenset[str]) -> None:
            got = gw.get_user_library_ids(plex_user_id)
            settings_now = gw.user_settings(plex_user_id)
            # plex.tv only reports friend settings while the user has a share.
            comparable = settings_before is not None and settings_now is not None
            unchanged = settings_now == settings_before if comparable else True
            entry = {"step": name, "wrote": sorted(wrote) if wrote is not None else None,
                     "expected": sorted(expect), "got": sorted(got), "ok": got == expect,
                     "settings_unchanged": unchanged, "settings_compared": comparable}
            if not unchanged:
                entry["settings_after"] = settings_now
            steps.append(entry)
            log.info("plex write check user=%s %s", plex_user_id, entry)

        try:
            if original:
                gw.set_user_libraries(plex_user_id, original)
                step("rewrite same set", original, original)
            if extra is not None:
                bigger = original | {extra}
                gw.set_user_libraries(plex_user_id, bigger)
                step("add one library", bigger, bigger)
        finally:
            gw.set_user_libraries(plex_user_id, original)
            step("restore original", original, original)
        ok = all(s["ok"] and s["settings_unchanged"] for s in steps)
        log.log(logging.INFO if ok else logging.ERROR, "plex write check user=%s passed=%s", plex_user_id, ok)
        return {"plex_user_id": plex_user_id, "original": sorted(original),
                "settings_before": settings_before, "steps": steps, "passed": ok}

    # ---- /link identity (N9, P0-07) ---------------------------------------------

    def capture_link_identity(self, token: str) -> PlexIdentity:
        def read(tok: str) -> PlexIdentity:
            acct = self._account_factory(token=tok)
            return PlexIdentity(str(acct.id), acct.username or acct.title or "")

        def invalidate(tok: str) -> None:
            self._account_factory(token=tok).signout()
            try:
                self._account_factory(token=tok)
            except Unauthorized:
                log.info("link token invalidated and verified dead")
                return
            raise RuntimeError("Plex token still works after sign-out; link not completed")

        return capture_identity_once(token, read, invalidate)
