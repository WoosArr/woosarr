"""OIDC single sign-on for admins: provider management and the sign-in flow (D21)."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from woosarr.api.auth import start_session
from woosarr.api.deps import AdminDep, DbDep, client_ip, require_admin
from woosarr.core import app_settings
from woosarr.core import auth as core_auth
from woosarr.core.secret_store import oidc_client_secret
from woosarr.db.models import AdminUser, OidcIdentity, OidcProvider, utcnow
from woosarr.services import audit, oidc_presets
from woosarr.services.oidc import CALLBACK_PATH, OidcError

log = logging.getLogger("woosarr.oidc")
public = APIRouter(prefix="/auth/oidc", tags=["sso"])
admin = APIRouter(prefix="/oidc", tags=["sso"], dependencies=[Depends(require_admin)])

STATE_COOKIE = "woosarr_oidc_state"
Kind = Literal["okta", "entra", "google", "adfs", "custom"]


def _svc(request: Request):
    return request.app.state.oidc


def redirect_uri(request: Request, db) -> str:
    base = app_settings.load(db).public_url.rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return base + CALLBACK_PATH


def usable_sso_admins(db, exclude_provider: int | None = None) -> int:
    """Active admins who have signed in through an enabled provider."""
    stmt = (select(func.count(func.distinct(OidcIdentity.admin_user_id)))
            .join(OidcProvider, OidcProvider.id == OidcIdentity.provider_id)
            .join(AdminUser, AdminUser.id == OidcIdentity.admin_user_id)
            .where(OidcProvider.enabled.is_(True), AdminUser.is_active.is_(True)))
    if exclude_provider is not None:
        stmt = stmt.where(OidcProvider.id != exclude_provider)
    return db.scalar(stmt) or 0


# ---- public: login page + flow ----------------------------------------------------


class PublicProvider(BaseModel):
    slug: str
    name: str
    kind: str


@public.get("/providers", response_model=list[PublicProvider])
def enabled_providers(db: DbDep) -> list[PublicProvider]:
    rows = db.scalars(select(OidcProvider).where(OidcProvider.enabled.is_(True)).order_by(func.lower(OidcProvider.name), OidcProvider.name))
    return [PublicProvider(slug=p.slug, name=p.name, kind=p.kind) for p in rows]


def _safe_next(path: str | None) -> str:
    """A same-site path to return to after sign-in, else "/". Browsers drop tabs and
    newlines from URLs ("/\t/evil" becomes "//evil"), so control characters are refused."""
    if (not path or len(path) > 512 or not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path)):
        return "/"
    return path


def _set_state_cookie(request: Request, response, state: str) -> None:
    mode = request.app.state.settings.cookie_secure
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/api/v1/auth/oidc",
                        secure=mode == "always" or (mode == "auto" and request.url.scheme == "https"))


@public.get("/{slug}/login", include_in_schema=False)
def login(slug: str, request: Request, db: DbDep, next: Annotated[str | None, Query()] = None) -> RedirectResponse:
    provider = db.scalar(select(OidcProvider).where(OidcProvider.slug == slug, OidcProvider.enabled.is_(True)))
    if provider is None:
        return RedirectResponse("/?sso_error=" + quote("unknown or disabled sign-in provider"), status_code=303)
    secret_set = request.app.state.secrets.is_set(db, oidc_client_secret(provider.id))
    try:
        url, state = _svc(request).start(provider, secret_set, redirect_uri(request, db), "login", _safe_next(next))
    except OidcError as exc:
        log.warning("sso start failed provider=%s: %s", slug, exc)
        return RedirectResponse("/?sso_error=" + quote(str(exc)), status_code=303)
    response = RedirectResponse(url, status_code=303)
    _set_state_cookie(request, response, state)
    return response


def _username(db, outcome) -> str:
    base = re.sub(r"[^A-Za-z0-9._@-]", "", (outcome.username or outcome.email or "sso-admin"))[:58] or "sso-admin"
    if len(base) < 3:
        base = f"{base}-sso"
    name, n = base, 1
    while db.scalar(select(AdminUser).where(AdminUser.username == name)) is not None:
        n += 1
        name = f"{base}-{n}"
    return name


@public.get("/callback", include_in_schema=False)
def callback(request: Request, db: DbDep, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None) -> RedirectResponse:
    svc = _svc(request)
    ip = client_ip(request)

    def fail(message: str, provider: OidcProvider | None = None, test_id: str | None = None) -> RedirectResponse:
        log.warning("sso sign-in failed provider=%s ip=%s: %s", provider.slug if provider else "?", ip, message)
        audit.record(db, f"ip:{ip}", "login_failed", "oidc_provider", provider.slug if provider else None,
                     reason=message[:300])
        target = (f"/settings?tab=sso&test={test_id}" if test_id else "/?sso_error=" + quote(message[:300]))
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
        return response

    try:
        pending = svc.take_pending(state or "", request.cookies.get(STATE_COOKIE))
    except OidcError as exc:
        return fail(str(exc))
    provider = db.get(OidcProvider, pending.provider_id)
    test_id = secrets.token_urlsafe(12) if pending.purpose == "test" else None
    if provider is None:
        return fail("the provider was removed during sign-in")
    if error:
        message = f"{provider.name} said: {error_description or error}"
        if test_id:
            svc.test_results[test_id] = {"ok": False, "error": message, "provider_id": provider.id}
        return fail(message, provider, test_id)
    try:
        secret = request.app.state.secrets.get(db, oidc_client_secret(provider.id)) or ""
        outcome = svc.finish(provider, secret, pending, code or "")
    except OidcError as exc:
        if test_id:
            svc.test_results[test_id] = {"ok": False, "error": str(exc), "provider_id": provider.id}
        return fail(str(exc), provider, test_id)

    if test_id:
        svc.test_results[test_id] = {
            "ok": outcome.allowed, "reason": outcome.reason, "provider_id": provider.id, "subject": outcome.subject,
            "username": outcome.username, "email": outcome.email, "groups": outcome.groups,
            "claims": {k: v for k, v in outcome.claims.items() if k not in ("nonce", "at_hash", "c_hash")},
        }
        audit.record(db, f"admin-id:{pending.admin_user_id}", "sso_test", "oidc_provider", provider.slug,
                     allowed=outcome.allowed, reason=outcome.reason)
        response = RedirectResponse(f"/settings?tab=sso&test={test_id}", status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
        return response

    if not outcome.allowed:
        return fail(f"{outcome.username or outcome.email or 'this account'} isn't allowed to sign in: {outcome.reason}",
                    provider)
    identity = db.scalar(select(OidcIdentity).where(OidcIdentity.provider_id == provider.id,
                                                   OidcIdentity.subject == outcome.subject))
    if identity is not None:
        user = db.get(AdminUser, identity.admin_user_id)
        if user is None or not user.is_active:
            return fail("this admin account is disabled", provider)
    else:
        user = AdminUser(username=_username(db, outcome), password_hash=None, auth_source="oidc")
        db.add(user)
        db.flush()
        identity = OidcIdentity(provider_id=provider.id, subject=outcome.subject, admin_user_id=user.id,
                                email=outcome.email)
        db.add(identity)
        audit.record(db, "system", "admin_created_sso", "admin", str(user.id), username=user.username,
                     provider=provider.slug, email=outcome.email)
    now = utcnow()
    identity.last_login_at = provider.last_login_at = user.last_login_at = now
    identity.email = outcome.email or identity.email
    old = request.cookies.get(core_auth.SESSION_COOKIE)
    if old:
        core_auth.end_session(db, old)
    response = RedirectResponse(pending.next_path, status_code=303)
    start_session(request, response, db, user)
    response.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
    audit.record(db, f"admin:{user.username}", "login", "admin", str(user.id), ip=ip, method="oidc",
                 provider=provider.slug, reason=outcome.reason)
    log.info("sso sign-in ok provider=%s admin=%s (%s)", provider.slug, user.username, outcome.reason)
    return response


# ---- admin: providers ----------------------------------------------------------------


class PresetFieldOut(BaseModel):
    key: str
    label: str
    hint: str
    placeholder: str
    pattern: str


class PresetOut(BaseModel):
    kind: str
    label: str
    fields: list[PresetFieldOut]
    scopes: str
    username_claim: str
    email_claim: str
    groups_claim: str
    require_email_verified: bool
    groups_supported: bool
    rule_hint: str
    requirements: list[str]
    steps: list[str]
    notes: list[str]


class PresetsOut(BaseModel):
    redirect_uri: str
    https: bool
    presets: list[PresetOut]


@admin.get("/presets", response_model=PresetsOut)
def presets(request: Request, db: DbDep) -> PresetsOut:
    uri = redirect_uri(request, db)
    return PresetsOut(
        redirect_uri=uri, https=uri.startswith("https://") or "//localhost" in uri,
        presets=[PresetOut(kind=p.kind, label=p.label, fields=[PresetFieldOut(**f.__dict__) for f in p.fields],
                           scopes=p.scopes, username_claim=p.username_claim, email_claim=p.email_claim,
                           groups_claim=p.groups_claim, require_email_verified=p.require_email_verified,
                           groups_supported=p.groups_supported, rule_hint=p.rule_hint,
                           requirements=list(p.requirements), steps=list(p.steps), notes=list(p.notes))
                 for p in oidc_presets.PRESETS.values()])


StrList = Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=100)]


class ProviderIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    slug: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")] = None
    kind: Kind
    settings: dict[str, str] = {}
    client_id: Annotated[str, Field(min_length=1, max_length=300)]
    # Write-only. Omit (or null) on update to keep the stored secret.
    client_secret: Annotated[str | None, Field(min_length=1, max_length=1000)] = None
    scopes: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    username_claim: Annotated[str, Field(max_length=100)] | None = None
    email_claim: Annotated[str, Field(max_length=100)] | None = None
    groups_claim: Annotated[str, Field(max_length=200)] | None = None
    allowed_groups: StrList = []
    allowed_emails: StrList = []
    allowed_domains: StrList = []
    require_email_verified: bool | None = None
    extra_params: dict[str, str] = {}
    enabled: bool = False


class ProviderOut(BaseModel):
    id: int
    name: str
    slug: str
    kind: str
    settings: dict[str, Any]
    issuer: str
    discovered_issuer: str | None
    client_id: str
    client_secret_set: bool
    scopes: str
    username_claim: str
    email_claim: str
    groups_claim: str
    allowed_groups: list[str]
    allowed_emails: list[str]
    allowed_domains: list[str]
    require_email_verified: bool
    extra_params: dict[str, Any]
    enabled: bool
    admins: int
    last_login_at: datetime | None
    login_url: str


def _out(request: Request, db, p: OidcProvider) -> ProviderOut:
    admins = db.scalar(select(func.count()).select_from(OidcIdentity).where(OidcIdentity.provider_id == p.id)) or 0
    return ProviderOut(
        id=p.id, name=p.name, slug=p.slug, kind=p.kind, settings=p.settings or {}, issuer=p.issuer,
        discovered_issuer=p.discovered_issuer, client_id=p.client_id,
        client_secret_set=request.app.state.secrets.is_set(db, oidc_client_secret(p.id)), scopes=p.scopes,
        username_claim=p.username_claim, email_claim=p.email_claim, groups_claim=p.groups_claim,
        allowed_groups=p.allowed_groups or [], allowed_emails=p.allowed_emails or [],
        allowed_domains=p.allowed_domains or [], require_email_verified=p.require_email_verified,
        extra_params=p.extra_params or {}, enabled=p.enabled, admins=admins, last_login_at=p.last_login_at,
        login_url=f"/api/v1/auth/oidc/{p.slug}/login",
    )


def _get(db, provider_id: int) -> OidcProvider:
    p = db.get(OidcProvider, provider_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    return p


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "sso"


def _apply(request: Request, db, p: OidcProvider, body: ProviderIn, creating: bool) -> None:
    preset = oidc_presets.PRESETS[body.kind]
    try:
        oidc_presets.validate_inputs(body.kind, body.settings)
        issuer = oidc_presets.build_issuer(body.kind, body.settings)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    rules = body.allowed_groups or body.allowed_emails or body.allowed_domains
    if body.enabled and not rules:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "add at least one allowed group, email or domain before enabling sign-in")
    if body.kind == "google" and body.allowed_groups:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Google doesn't send groups; use emails or a domain")
    slug = body.slug or (p.slug if not creating else _slugify(body.name))
    clash = db.scalar(select(OidcProvider).where(OidcProvider.slug == slug, OidcProvider.id != (p.id or 0)))
    if clash is not None:
        if body.slug:
            raise HTTPException(status.HTTP_409_CONFLICT, "another provider uses that slug")
        slug = f"{slug}-{secrets.token_hex(2)}"
    if p.issuer != issuer:
        p.discovered_issuer = None
    p.name, p.slug, p.kind, p.settings, p.issuer, p.client_id = body.name, slug, body.kind, body.settings, issuer, body.client_id
    p.scopes = body.scopes or preset.scopes
    p.username_claim = body.username_claim if body.username_claim is not None else preset.username_claim
    p.email_claim = body.email_claim if body.email_claim is not None else preset.email_claim
    p.groups_claim = body.groups_claim if body.groups_claim is not None else preset.groups_claim
    p.allowed_groups = [g.strip() for g in body.allowed_groups]
    p.allowed_emails = [e.strip().lower() for e in body.allowed_emails]
    p.allowed_domains = [d.strip().lower().lstrip("@") for d in body.allowed_domains]
    p.require_email_verified = (body.require_email_verified if body.require_email_verified is not None
                                else preset.require_email_verified)
    p.extra_params = body.extra_params
    p.enabled = body.enabled


@admin.get("/providers", response_model=list[ProviderOut])
def list_providers(request: Request, db: DbDep) -> list[ProviderOut]:
    return [_out(request, db, p) for p in db.scalars(select(OidcProvider).order_by(func.lower(OidcProvider.name), OidcProvider.name))]


@admin.post("/providers", response_model=ProviderOut, status_code=201)
def create_provider(body: ProviderIn, request: Request, db: DbDep, principal: AdminDep) -> ProviderOut:
    p = OidcProvider()
    _apply(request, db, p, body, creating=True)
    db.add(p)
    db.flush()
    if body.client_secret:
        request.app.state.secrets.set(db, oidc_client_secret(p.id), body.client_secret)
    audit.record(db, principal.actor, "sso_provider_created", "oidc_provider", p.slug, kind=p.kind, issuer=p.issuer)
    return _out(request, db, p)


@admin.put("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: int, body: ProviderIn, request: Request, db: DbDep, principal: AdminDep) -> ProviderOut:
    p = _get(db, provider_id)
    if p.enabled and not body.enabled:
        _guard_last_way_in(db, p.id)
    _apply(request, db, p, body, creating=False)
    if body.client_secret:
        request.app.state.secrets.set(db, oidc_client_secret(p.id), body.client_secret)
    db.flush()
    audit.record(db, principal.actor, "sso_provider_updated", "oidc_provider", p.slug, enabled=p.enabled,
                 secret_replaced=bool(body.client_secret))
    return _out(request, db, p)


def _guard_last_way_in(db, provider_id: int) -> None:
    if not app_settings.load(db).local_login_enabled and usable_sso_admins(db, exclude_provider=provider_id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "password login is off and this is the only working sign-in; turn password login on first")


@admin.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, request: Request, db: DbDep, principal: AdminDep) -> None:
    p = _get(db, provider_id)
    if p.enabled:
        _guard_last_way_in(db, p.id)
    request.app.state.secrets.delete(db, oidc_client_secret(p.id))
    db.delete(p)
    audit.record(db, principal.actor, "sso_provider_deleted", "oidc_provider", p.slug)


class DiscoveryOut(BaseModel):
    ok: bool
    issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    algorithms: list[str] = []
    error: str | None = None


@admin.post("/providers/{provider_id}/discover", response_model=DiscoveryOut)
def discover(provider_id: int, request: Request, db: DbDep) -> DiscoveryOut:
    """Check the provider's discovery document (issuer, endpoints, signing algorithms)."""
    p = _get(db, provider_id)
    try:
        d = _svc(request).discover(p.kind, p.issuer, refresh=True)
    except OidcError as exc:
        return DiscoveryOut(ok=False, error=str(exc))
    p.discovered_issuer = d.issuer
    return DiscoveryOut(ok=True, issuer=d.issuer, authorization_endpoint=d.authorization_endpoint,
                        token_endpoint=d.token_endpoint, userinfo_endpoint=d.userinfo_endpoint,
                        algorithms=d.algorithms)


class TestStart(BaseModel):
    url: str


@admin.post("/providers/{provider_id}/test", response_model=TestStart)
def start_test(provider_id: int, request: Request, db: DbDep, principal: AdminDep):
    """Start a test sign-in: shows the claims and the access decision, doesn't sign anyone in."""
    p = _get(db, provider_id)
    secret_set = request.app.state.secrets.is_set(db, oidc_client_secret(p.id))
    try:
        url, state = _svc(request).start(p, secret_set, redirect_uri(request, db), "test",
                                         admin_user_id=principal.user.id)
    except OidcError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    response = JSONResponse({"url": url})
    _set_state_cookie(request, response, state)
    return response


class TestResult(BaseModel):
    ok: bool
    provider_id: int
    error: str | None = None
    reason: str | None = None
    subject: str | None = None
    username: str | None = None
    email: str | None = None
    groups: list[str] = []
    claims: dict[str, Any] = {}


@admin.get("/test-results/{test_id}", response_model=TestResult)
def test_result(test_id: str, request: Request) -> TestResult:
    result = _svc(request).test_results.get(test_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "test result not found (they're kept until restart)")
    return TestResult(**result)
