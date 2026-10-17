"""OIDC provider presets: Okta, Microsoft Entra ID, Google, ADFS and Custom (D21).

Each preset says what the admin enters, how the issuer URL is built from it,
sensible claim/scope defaults and the setup steps shown in the wizard. `{redirect_uri}`
in the text is replaced with WooSarr's callback URL by the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PresetField:
    key: str
    label: str
    hint: str = ""
    placeholder: str = ""
    pattern: str = ""  # regex the value must match


@dataclass(frozen=True)
class Preset:
    kind: str
    label: str
    fields: tuple[PresetField, ...]
    scopes: str
    username_claim: str
    email_claim: str
    groups_claim: str
    require_email_verified: bool
    groups_supported: bool
    rule_hint: str
    requirements: tuple[str, ...]
    steps: tuple[str, ...]
    notes: tuple[str, ...] = ()
    extra_params: dict[str, str] = field(default_factory=dict)

    def issuer(self, values: dict[str, str]) -> str:
        return build_issuer(self.kind, values)


_HOST = r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
_TENANT = r"^([0-9a-fA-F-]{36}|[A-Za-z0-9.-]+\.[A-Za-z]{2,})$"

PRESETS: dict[str, Preset] = {
    "okta": Preset(
        kind="okta",
        label="Okta",
        fields=(
            PresetField("domain", "Okta domain", "Your Okta org address, without https://",
                        "dev-123456.okta.com", _HOST),
            PresetField("auth_server", "Authorization server",
                        "\"default\" (custom authorization server, needs API Access Management) or \"org\" "
                        "(the org server, always available)", "default", r"^(org|[A-Za-z0-9]{1,40})$"),
        ),
        scopes="openid profile email groups",
        username_claim="preferred_username",
        email_claim="email",
        groups_claim="groups",
        require_email_verified=True,
        groups_supported=True,
        rule_hint="Allow an Okta group (e.g. WooSarr Admins). The groups claim must be added to the ID token.",
        requirements=(
            "An Okta org where you can create app integrations (Admin role: Application Administrator or higher).",
            "WooSarr reachable over HTTPS (Okta allows http only for localhost).",
            "An Okta group for WooSarr admins (recommended).",
        ),
        steps=(
            "In the Okta Admin Console go to **Applications → Applications → Create App Integration**.",
            "Choose **OIDC - OpenID Connect** and **Web Application**, then **Next**.",
            "Name it **WooSarr**. Grant type: **Authorization Code** only.",
            "Sign-in redirect URI: `{redirect_uri}`. Leave sign-out redirect empty.",
            "Assignments: **Limit access to selected groups** and pick your WooSarr admins group. Save.",
            "Copy the **Client ID** and **Client secret** (General tab).",
            ("Groups claim — org server: **Sign On** tab → OpenID Connect ID Token → Groups claim filter: "
            "name `groups`, *Matches regex* `.*` (or a narrower filter). Custom server: **Security → API → "
            "Authorization Servers → default → Claims → Add Claim**: name `groups`, include in **ID Token** "
            "(Always), value type **Groups**, filter as above."),
        ),
        notes=(
            ("With the custom \"default\" server the issuer is https://<domain>/oauth2/default; with the org "
            "server it's https://<domain>."),
            "The `groups` scope only exists on the org server; WooSarr asks for it there and drops it otherwise.",
        ),
    ),
    "entra": Preset(
        kind="entra",
        label="Microsoft Entra ID",
        fields=(
            PresetField("tenant", "Tenant ID", "Directory (tenant) ID from the app's Overview page, or your "
                        "primary domain (e.g. contoso.onmicrosoft.com). Not \"common\" or \"organizations\".",
                        "00000000-0000-0000-0000-000000000000", _TENANT),
        ),
        scopes="openid profile email",
        username_claim="preferred_username",
        email_claim="email",
        groups_claim="roles",
        require_email_verified=False,
        groups_supported=True,
        rule_hint="Allow an app role value (e.g. WooSarr.Admin) via the `roles` claim — recommended — or group "
                  "object IDs via the `groups` claim.",
        requirements=(
            "An Entra ID tenant where you can register apps (Application Developer or Application Administrator).",
            "WooSarr reachable over HTTPS (Entra allows http only for localhost).",
            "To restrict access: an app role (recommended) or a security group.",
        ),
        steps=(
            "In the Entra admin center go to **Identity → Applications → App registrations → New registration**.",
            ("Name **WooSarr**; Supported account types: **Accounts in this organizational directory only** "
            "(single tenant)."),
            "Redirect URI: platform **Web**, `{redirect_uri}`. Register.",
            "Copy the **Application (client) ID** and **Directory (tenant) ID** from Overview.",
            "**Certificates & secrets → New client secret**; copy the secret **Value** (not the ID). Note its expiry.",
            ("Recommended: **App roles → Create app role**: display name WooSarr Admin, allowed member types "
            "**Users/Groups**, value `WooSarr.Admin`. Then **Enterprise applications → WooSarr → Users and "
            "groups → Add user/group** and assign the role."),
            ("Optional: **Enterprise applications → WooSarr → Properties → Assignment required? = Yes** so only "
            "assigned users can sign in at all."),
            ("Optional (groups instead of roles): **Token configuration → Add groups claim** → Security groups, "
            "ID token: Group ID. Set the groups claim below to `groups` and allow the group's Object ID."),
        ),
        notes=(
            ("Entra doesn't send `email_verified`; WooSarr trusts the tenant's directory instead (email rules still "
            "work). `preferred_username` is the user's UPN."),
            "Users in more than ~200 groups get no `groups` claim (overage); app roles avoid that.",
            "Client secrets expire (6–24 months): create a new one in Entra and paste it here before then.",
        ),
    ),
    "google": Preset(
        kind="google",
        label="Google",
        fields=(
            PresetField("hosted_domain", "Google Workspace domain (optional)",
                        "Only for Workspace: shows only accounts from this domain on Google's chooser. Access is "
                        "still decided by the rules below.", "example.com", r"^(|" + _HOST[1:-1] + r")$"),
        ),
        scopes="openid email profile",
        username_claim="email",
        email_claim="email",
        groups_claim="",
        require_email_verified=True,
        groups_supported=False,
        rule_hint="Google doesn't send groups: allow specific email addresses, or a Workspace domain (checked "
                  "against Google's verified `hd` claim).",
        requirements=(
            "A Google Cloud project (any Google account, or your Workspace admin).",
            ("WooSarr reachable over HTTPS with a real domain name (Google rejects IP-address redirect URIs; "
            "http is allowed only for localhost)."),
        ),
        steps=(
            "Open **console.cloud.google.com** and create or pick a project.",
            ("**APIs & Services → OAuth consent screen** (Google Auth Platform → Branding/Audience): app name "
            "WooSarr; User type **Internal** for Workspace, otherwise **External** and add your admins as "
            "**Test users** (or publish the app)."),
            "**APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Web application**.",
            "Authorized redirect URI: `{redirect_uri}`. Create.",
            "Copy the **Client ID** and **Client secret**.",
        ),
        notes=(
            ("Allowed domains are matched against the `hd` (hosted domain) claim, which only Workspace accounts "
            "have; for personal Gmail accounts list the email addresses."),
        ),
        extra_params={"prompt": "select_account"},
    ),
    "adfs": Preset(
        kind="adfs",
        label="ADFS",
        fields=(
            PresetField("host", "ADFS host", "Your federation service name, without https://",
                        "adfs.example.com", _HOST),
        ),
        scopes="openid profile email allatclaims",
        username_claim="upn",
        email_claim="email",
        groups_claim="role",
        require_email_verified=False,
        groups_supported=True,
        rule_hint="Allow the AD group names you send as the `role` claim (issuance transform rule).",
        requirements=(
            "AD FS on Windows Server 2016 or later (2019+ recommended), with admin access to AD FS Management.",
            "The ADFS server's certificate trusted by WooSarr's container (a public CA, or add your CA to the image).",
            "WooSarr reachable over HTTPS.",
        ),
        steps=(
            ("AD FS Management → **Application Groups → Add Application Group**: name **WooSarr**, template "
            "**Server application accessing a web API**. Next."),
            "Server application: copy the **Client Identifier**; Redirect URI `{redirect_uri}` → **Add**. Next.",
            "Configure Application Credentials: **Generate a shared secret** and copy it. Next.",
            ("Configure Web API: Identifier = the **Client Identifier** from above (so tokens are issued to "
            "WooSarr). Next."),
            "Access control policy: e.g. **Permit specific group** → your WooSarr admins group. Next.",
            "Permitted scopes: **openid**, **profile**, **email**, **allatclaims**. Finish.",
            ("Open the Web API's **Properties → Issuance Transform Rules → Add Rule**: *Send LDAP Attributes as "
            "Claims* (Active Directory): User-Principal-Name → **UPN**, E-Mail-Addresses → **E-Mail Address**, "
            "Token-Groups - Unqualified Names → **Role**."),
            "`allatclaims` makes ADFS copy those claims into the ID token, where WooSarr reads them.",
        ),
        notes=(
            "Issuer: https://<host>/adfs (WooSarr reads https://<host>/adfs/.well-known/openid-configuration).",
            "Claim names in the ID token are short names (upn, email, role) when the rules above are used.",
        ),
    ),
    "custom": Preset(
        kind="custom",
        label="Custom OIDC provider",
        fields=(
            PresetField("issuer", "Issuer URL",
                        "Exactly as in the provider's /.well-known/openid-configuration (Authentik, Keycloak, "
                        "Authelia, Zitadel, Auth0, Pocket ID...)", "https://auth.example.com/application/o/woosarr/",
                        r"^https?://\S+$"),
        ),
        scopes="openid email profile",
        username_claim="preferred_username",
        email_claim="email",
        groups_claim="groups",
        require_email_verified=True,
        groups_supported=True,
        rule_hint="Allow a group (most providers send `groups`), specific emails, or an email domain. Nested "
                  "claims work with dots, e.g. realm_access.roles for Keycloak.",
        requirements=(
            ("An OpenID Connect provider that supports the authorization code flow and publishes a discovery "
            "document."),
            "A confidential (web) client with a client secret.",
            "WooSarr reachable over HTTPS (most providers require it for redirect URIs).",
        ),
        steps=(
            "Create an OIDC / OAuth2 application of type **confidential / web**, grant **authorization code**.",
            "Redirect URI: `{redirect_uri}` (exact match).",
            "Scopes: openid, email, profile (and groups if your provider uses a scope for them).",
            "Copy the client ID, client secret and issuer URL.",
            ("Make sure group membership is included in the **ID token** (or the userinfo endpoint) if you want "
            "group-based access."),
        ),
    ),
}


def build_issuer(kind: str, values: dict[str, str]) -> str:
    v = {k: (val or "").strip() for k, val in values.items()}
    if kind == "okta":
        domain = re.sub(r"^https?://", "", v.get("domain", "")).rstrip("/")
        server = v.get("auth_server") or "default"
        return f"https://{domain}" if server == "org" else f"https://{domain}/oauth2/{server}"
    if kind == "entra":
        tenant = v.get("tenant", "")
        if tenant.lower() in ("common", "organizations", "consumers"):
            raise ValueError("use your tenant ID or domain, not common/organizations/consumers")
        return f"https://login.microsoftonline.com/{tenant}/v2.0"
    if kind == "google":
        return "https://accounts.google.com"
    if kind == "adfs":
        host = re.sub(r"^https?://", "", v.get("host", "")).rstrip("/")
        return f"https://{host}/adfs"
    if kind == "custom":
        return v.get("issuer", "")
    raise ValueError(f"unknown provider kind {kind!r}")


def validate_inputs(kind: str, values: dict[str, str]) -> None:
    preset = PRESETS[kind]
    for f in preset.fields:
        value = (values.get(f.key) or "").strip()
        if f.pattern and not re.match(f.pattern, value):
            raise ValueError(f"{f.label}: invalid value")


def preset_scopes(kind: str, values: dict[str, str], scopes: str) -> str:
    """Okta's `groups` scope only exists on the org authorization server."""
    if kind == "okta" and (values.get("auth_server") or "default") != "org":
        return " ".join(s for s in scopes.split() if s != "groups")
    return scopes


def extra_params(kind: str, values: dict[str, str], configured: dict[str, Any]) -> dict[str, str]:
    params = dict(PRESETS[kind].extra_params) if kind in PRESETS else {}
    if kind == "google" and (values.get("hosted_domain") or "").strip():
        params["hd"] = values["hosted_domain"].strip()
    params.update({k: str(v) for k, v in (configured or {}).items()})
    return params


DOC_HEADER = """# Single sign-on (OIDC) for admins

<!-- Generated from woosarr/services/oidc_presets.py by `make sso-docs`; edit the presets, not this file. -->

Admins can sign in with one or more OpenID Connect providers: **Okta**, **Microsoft Entra ID**, **Google**,
**ADFS**, or any other OIDC provider (**Custom**: Authentik, Keycloak, Authelia, Zitadel, Auth0, Pocket ID...).
Set them up under **Settings → Single sign-on → Add provider**; the wizard shows the same steps as below with
your redirect URI filled in.

## How it works

- Authorization code flow with **PKCE**, `state` (bound to a short-lived cookie) and `nonce`.
- The ID token's signature is checked against the provider's published keys (asymmetric algorithms only), plus
  issuer, audience (and `azp`), expiry and issue time (2 minutes clock-skew leeway). Claims from the userinfo
  endpoint are merged when available.
- **Nobody gets in by default.** Each provider has access rules: allowed groups/roles, emails, or domains.
  Email and domain rules require `email_verified` unless turned off for directory-backed providers (Entra, ADFS).
- An admin account is created on first sign-in and linked to the provider account by its subject (`sub`), so a
  changed email address doesn't create a second admin or take over another one.
- Every sign-in and refusal is in the audit trail; **Test sign-in** shows the claims received and the decision
  without signing anyone in.
- Password login can be turned off once an admin has signed in with single sign-on; the last working provider
  can't then be disabled. Locked out anyway: `python -m woosarr.admin reset-password <user>` in the container.

## Requirements for every provider

- WooSarr reachable over **HTTPS** at a stable address (a reverse proxy). Set **Settings → Single sign-on →
  Public URL**; the redirect URI is `<public URL>/api/v1/auth/oidc/callback`.
- The container's clock in sync (tokens are time-limited).
"""


def markdown() -> str:
    out = [DOC_HEADER]
    uri = "`https://<your WooSarr address>/api/v1/auth/oidc/callback`"
    for p in PRESETS.values():
        out.append(f"\n## {p.label}\n")
        out.append("**You'll enter:** " + "; ".join(f"{f.label} ({f.hint})" if f.hint else f.label
                                                    for f in p.fields) + "; client ID; client secret.\n")
        out.append(f"**Defaults:** scopes `{p.scopes}`, username claim `{p.username_claim}`, email claim "
                   f"`{p.email_claim}`" + (f", groups claim `{p.groups_claim}`" if p.groups_claim else "") +
                   f", email must be verified: {'yes' if p.require_email_verified else 'no'}.\n")
        out.append(f"**Access:** {p.rule_hint}\n")
        out.append("### Requirements\n")
        out.extend(f"- {r}" for r in p.requirements)
        out.append("\n### Steps\n")
        out.extend(f"{i}. {step.replace('`{redirect_uri}`', uri)}" for i, step in enumerate(p.steps, 1))
        if p.notes:
            out.append("\n### Good to know\n")
            out.extend(f"- {n}" for n in p.notes)
    out.append("""
## Troubleshooting

| Message | Meaning / fix |
|---------|---------------|
| *state mismatch* | The sign-in started in another browser/tab or cookies are blocked; start again from the login page. |
| *issuer mismatch* | The issuer you entered doesn't match the provider's discovery document (e.g. wrong Okta authorization server, Entra `common`). |
| *different application (audience)* | The client ID doesn't match the application that issued the token. |
| *expired / isn't valid yet* | The clocks of the container and the provider differ by more than 2 minutes. |
| *not in an allowed group, email or domain* | Use **Test sign-in** to see which claims arrive; add the groups claim to the ID token or adjust the rules. |
| *redirect URI mismatch* (at the provider) | The redirect URI registered at the provider must match exactly, including https and the path. |
""")
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    text = markdown()
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
