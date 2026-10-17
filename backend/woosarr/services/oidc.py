"""OpenID Connect sign-in for admins (D21): authorization code flow + PKCE.

Security checks, in order:
  * state (random, single use, 10 min) must match the cookie set when the flow
    started (login CSRF) and the pending entry kept server-side
  * the code is exchanged with the client secret and the PKCE verifier
  * the ID token's signature is verified with the provider's JWKS (asymmetric
    algorithms only), plus iss (discovered issuer), aud (+ azp if several),
    exp/iat with 120 s leeway, and nonce
  * access is granted only by the provider's rules (groups/emails/domains);
    a provider without rules lets nobody in
  * admins are linked by (provider, sub), never by email
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
import requests

from woosarr.services import oidc_presets

log = logging.getLogger("woosarr.oidc")

PENDING_TTL = 600
LEEWAY = 120
ASYMMETRIC_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
CALLBACK_PATH = "/api/v1/auth/oidc/callback"


class OidcError(Exception):
    """Sign-in failed; the message is safe to show the admin."""


@dataclass
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    algorithms: list[str]
    token_auth_methods: list[str]
    fetched_at: float = field(default_factory=time.monotonic)


@dataclass
class Pending:
    provider_id: int
    nonce: str
    verifier: str
    redirect_uri: str
    purpose: str  # "login" | "test"
    next_path: str = "/"
    admin_user_id: int | None = None  # test runs: who started it
    created: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Outcome:
    allowed: bool
    reason: str
    subject: str
    username: str | None
    email: str | None
    groups: list[str]
    claims: dict[str, Any]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claim(claims: dict[str, Any], path: str) -> Any:
    """Read a claim by name or dotted path (e.g. realm_access.roles)."""
    if not path:
        return None
    if path in claims:
        return claims[path]
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _issuer_ok(kind: str, configured: str, discovered: str) -> bool:
    a, b = configured.rstrip("/"), discovered.rstrip("/")
    if a == b:
        return True
    if "{" in b:  # e.g. Entra's multi-tenant "{tenantid}" issuers
        return False
    if kind == "entra":
        # A tenant given as domain name resolves to the tenant GUID in the issuer.
        pa, pb = urlparse(a), urlparse(b)
        return pa.netloc == pb.netloc == "login.microsoftonline.com" and pb.path.endswith("/v2.0")
    return False


def evaluate(provider: Any, claims: dict[str, Any]) -> Outcome:
    """Apply the provider's access rules to verified claims."""
    sub = str(claims.get("sub") or "")
    email = claim(claims, provider.email_claim or "email")
    email = str(email).strip().lower() if email else None
    username = claim(claims, provider.username_claim or "") or email or None
    groups = as_list(claim(claims, provider.groups_claim)) if provider.groups_claim else []
    verified = truthy(claims.get("email_verified")) or not provider.require_email_verified

    def out(allowed: bool, reason: str) -> Outcome:
        return Outcome(allowed, reason, sub, str(username) if username else None, email, groups, claims)

    if not sub:
        return out(False, "the ID token has no subject (sub)")
    allowed_groups = {g.lower() for g in provider.allowed_groups or []}
    allowed_emails = {e.lower() for e in provider.allowed_emails or []}
    allowed_domains = {d.lower().lstrip("@") for d in provider.allowed_domains or []}
    if not (allowed_groups or allowed_emails or allowed_domains):
        return out(False, "no access rules are configured for this provider")
    if allowed_groups and {g.lower() for g in groups} & allowed_groups:
        return out(True, "member of an allowed group")
    if allowed_emails and email and email in allowed_emails:
        if not verified:
            return out(False, "the email address isn't verified by the provider")
        return out(True, "allowed email address")
    if allowed_domains:
        hd = claims.get("hd")
        if provider.kind == "google":
            # Only the verified hosted-domain claim counts (Workspace accounts).
            if hd and str(hd).lower() in allowed_domains:
                return out(True, "allowed Google Workspace domain")
        elif email and email.rsplit("@", 1)[-1] in allowed_domains:
            if not verified:
                return out(False, "the email address isn't verified by the provider")
            return out(True, "allowed email domain")
    return out(False, "not in an allowed group, email or domain")


class OidcService:
    def __init__(self, http: Any = None, now: Callable[[], float] = time.time) -> None:
        self._http = http or requests.Session()
        self._now = now
        self._pending: dict[str, Pending] = {}
        self._discovery: dict[str, Discovery] = {}
        self._jwks: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.test_results: dict[str, dict[str, Any]] = {}

    # ---- discovery / keys ---------------------------------------------------

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            r = self._http.get(url, timeout=10, headers={"Accept": "application/json"})
        except requests.RequestException as exc:
            raise OidcError(f"can't reach {urlparse(url).netloc}: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise OidcError(f"{url} answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise OidcError(f"{url} didn't return JSON") from exc

    def discover(self, kind: str, issuer: str, refresh: bool = False) -> Discovery:
        key = issuer.rstrip("/")
        with self._lock:
            cached = self._discovery.get(key)
        if cached and not refresh and time.monotonic() - cached.fetched_at < 3600:
            return cached
        doc = self._get_json(f"{key}/.well-known/openid-configuration")
        for name in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(name):
                raise OidcError(f"the discovery document has no {name}")
        if not _issuer_ok(kind, issuer, doc["issuer"]):
            raise OidcError(f"issuer mismatch: configured {issuer}, provider says {doc['issuer']}")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if urlparse(doc[name]).scheme != "https" and urlparse(doc[name]).hostname not in ("localhost", "127.0.0.1"):
                raise OidcError(f"{name} must use https")
        algs = [a for a in doc.get("id_token_signing_alg_values_supported") or ["RS256"] if a in ASYMMETRIC_ALGS]
        if not algs:
            raise OidcError("the provider offers no asymmetric ID token signing algorithm")
        d = Discovery(doc["issuer"], doc["authorization_endpoint"], doc["token_endpoint"], doc["jwks_uri"],
                      doc.get("userinfo_endpoint"), algs,
                      doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"])
        with self._lock:
            self._discovery[key] = d
        return d

    def _jwk(self, jwks_uri: str, kid: str | None, alg: str) -> Any:
        def find(keys: dict[str, Any]) -> Any:
            for k in keys.get("keys", []):
                if (kid is None or k.get("kid") == kid) and k.get("use", "sig") == "sig":
                    return jwt.PyJWK.from_dict(k, algorithm=alg)
            return None

        with self._lock:
            cached = self._jwks.get(jwks_uri)
        if cached and time.monotonic() - cached[0] < 3600:
            key = find(cached[1])
            if key is not None:
                return key
        keys = self._get_json(jwks_uri)  # refresh once (key rotation)
        with self._lock:
            self._jwks[jwks_uri] = (time.monotonic(), keys)
        key = find(keys)
        if key is None:
            raise OidcError("the ID token was signed with an unknown key")
        return key

    # ---- flow -------------------------------------------------------------------

    def start(self, provider: Any, client_secret_present: bool, redirect_uri: str, purpose: str = "login",
              next_path: str = "/", admin_user_id: int | None = None) -> tuple[str, str]:
        """(authorization URL, state). The caller sets the state cookie."""
        if not client_secret_present:
            raise OidcError("this provider has no client secret yet")
        d = self.discover(provider.kind, provider.issuer)
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        settings = provider.settings or {}
        scopes = oidc_presets.preset_scopes(provider.kind, settings, provider.scopes or "openid")
        if "openid" not in scopes.split():
            scopes = "openid " + scopes
        params = {
            "response_type": "code", "client_id": provider.client_id, "redirect_uri": redirect_uri,
            "scope": scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
            **oidc_presets.extra_params(provider.kind, settings, provider.extra_params or {}),
        }
        with self._lock:
            now = time.monotonic()
            for k in [k for k, v in self._pending.items() if now - v.created > PENDING_TTL]:
                del self._pending[k]
            self._pending[state] = Pending(provider.id, nonce, verifier, redirect_uri, purpose,
                                           next_path, admin_user_id)
        sep = "&" if "?" in d.authorization_endpoint else "?"
        return f"{d.authorization_endpoint}{sep}{urlencode(params)}", state

    def take_pending(self, state: str, cookie_state: str | None) -> Pending:
        if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
            raise OidcError("the sign-in didn't start in this browser (state mismatch); try again")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.monotonic() - pending.created > PENDING_TTL:
            raise OidcError("the sign-in expired; try again")
        return pending

    def finish(self, provider: Any, client_secret: str, pending: Pending, code: str) -> Outcome:
        d = self.discover(provider.kind, provider.issuer)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.verifier}
        auth = None
        if "client_secret_basic" in d.token_auth_methods:
            auth = (provider.client_id, client_secret)
        else:
            data.update(client_id=provider.client_id, client_secret=client_secret)
        try:
            r = self._http.post(d.token_endpoint, data=data, auth=auth, timeout=15,
                                headers={"Accept": "application/json"})
        except requests.RequestException as exc:
            raise OidcError(f"can't reach the token endpoint: {type(exc).__name__}") from exc
        try:
            tokens = r.json()
        except ValueError:
            tokens = {}
        if r.status_code != 200 or "id_token" not in tokens:
            err = tokens.get("error_description") or tokens.get("error") or f"HTTP {r.status_code}"
            raise OidcError(f"the provider refused the sign-in code: {str(err)[:200]}")
        claims = self.verify_id_token(provider, d, tokens["id_token"], pending.nonce)
        if d.userinfo_endpoint and tokens.get("access_token"):
            claims = self._merge_userinfo(d.userinfo_endpoint, tokens["access_token"], claims)
        return evaluate(provider, claims)

    def verify_id_token(self, provider: Any, d: Discovery, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError("the ID token is malformed") from exc
        alg = header.get("alg")
        if alg not in d.algorithms:
            raise OidcError(f"the ID token uses an unexpected algorithm ({alg})")
        key = self._jwk(d.jwks_uri, header.get("kid"), alg)
        try:
            claims = jwt.decode(
                id_token, key=key.key, algorithms=[alg], audience=provider.client_id, issuer=d.issuer,
                leeway=LEEWAY, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OidcError("the ID token has expired (check the server clock)") from exc
        except jwt.InvalidAudienceError as exc:
            raise OidcError("the ID token was issued for a different application (audience)") from exc
        except jwt.InvalidIssuerError as exc:
            raise OidcError("the ID token comes from a different issuer") from exc
        except jwt.ImmatureSignatureError as exc:
            raise OidcError("the ID token isn't valid yet (check the server clock)") from exc
        except jwt.PyJWTError as exc:
            raise OidcError(f"the ID token is invalid ({type(exc).__name__})") from exc
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != provider.client_id:
            raise OidcError("the ID token's authorized party (azp) isn't WooSarr")
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OidcError("the ID token's nonce doesn't match (replayed sign-in?)")
        return claims

    def _merge_userinfo(self, url: str, access_token: str, claims: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._http.get(url, headers={"Authorization": f"Bearer {access_token}",
                                             "Accept": "application/json"}, timeout=10)
            info = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            info = {}
        if not isinstance(info, dict) or (info.get("sub") and info.get("sub") != claims.get("sub")):
            return claims  # ignore userinfo for a different subject
        merged = dict(info)
        merged.update(claims)  # the signed ID token wins
        return merged
