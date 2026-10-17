# Single sign-on (OIDC) for admins

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


## Okta

**You'll enter:** Okta domain (Your Okta org address, without https://); Authorization server ("default" (custom authorization server, needs API Access Management) or "org" (the org server, always available)); client ID; client secret.

**Defaults:** scopes `openid profile email groups`, username claim `preferred_username`, email claim `email`, groups claim `groups`, email must be verified: yes.

**Access:** Allow an Okta group (e.g. WooSarr Admins). The groups claim must be added to the ID token.

### Requirements

- An Okta org where you can create app integrations (Admin role: Application Administrator or higher).
- WooSarr reachable over HTTPS (Okta allows http only for localhost).
- An Okta group for WooSarr admins (recommended).

### Steps

1. In the Okta Admin Console go to **Applications → Applications → Create App Integration**.
2. Choose **OIDC - OpenID Connect** and **Web Application**, then **Next**.
3. Name it **WooSarr**. Grant type: **Authorization Code** only.
4. Sign-in redirect URI: `https://<your WooSarr address>/api/v1/auth/oidc/callback`. Leave sign-out redirect empty.
5. Assignments: **Limit access to selected groups** and pick your WooSarr admins group. Save.
6. Copy the **Client ID** and **Client secret** (General tab).
7. Groups claim — org server: **Sign On** tab → OpenID Connect ID Token → Groups claim filter: name `groups`, *Matches regex* `.*` (or a narrower filter). Custom server: **Security → API → Authorization Servers → default → Claims → Add Claim**: name `groups`, include in **ID Token** (Always), value type **Groups**, filter as above.

### Good to know

- With the custom "default" server the issuer is https://<domain>/oauth2/default; with the org server it's https://<domain>.
- The `groups` scope only exists on the org server; WooSarr asks for it there and drops it otherwise.

## Microsoft Entra ID

**You'll enter:** Tenant ID (Directory (tenant) ID from the app's Overview page, or your primary domain (e.g. contoso.onmicrosoft.com). Not "common" or "organizations".); client ID; client secret.

**Defaults:** scopes `openid profile email`, username claim `preferred_username`, email claim `email`, groups claim `roles`, email must be verified: no.

**Access:** Allow an app role value (e.g. WooSarr.Admin) via the `roles` claim — recommended — or group object IDs via the `groups` claim.

### Requirements

- An Entra ID tenant where you can register apps (Application Developer or Application Administrator).
- WooSarr reachable over HTTPS (Entra allows http only for localhost).
- To restrict access: an app role (recommended) or a security group.

### Steps

1. In the Entra admin center go to **Identity → Applications → App registrations → New registration**.
2. Name **WooSarr**; Supported account types: **Accounts in this organizational directory only** (single tenant).
3. Redirect URI: platform **Web**, `https://<your WooSarr address>/api/v1/auth/oidc/callback`. Register.
4. Copy the **Application (client) ID** and **Directory (tenant) ID** from Overview.
5. **Certificates & secrets → New client secret**; copy the secret **Value** (not the ID). Note its expiry.
6. Recommended: **App roles → Create app role**: display name WooSarr Admin, allowed member types **Users/Groups**, value `WooSarr.Admin`. Then **Enterprise applications → WooSarr → Users and groups → Add user/group** and assign the role.
7. Optional: **Enterprise applications → WooSarr → Properties → Assignment required? = Yes** so only assigned users can sign in at all.
8. Optional (groups instead of roles): **Token configuration → Add groups claim** → Security groups, ID token: Group ID. Set the groups claim below to `groups` and allow the group's Object ID.

### Good to know

- Entra doesn't send `email_verified`; WooSarr trusts the tenant's directory instead (email rules still work). `preferred_username` is the user's UPN.
- Users in more than ~200 groups get no `groups` claim (overage); app roles avoid that.
- Client secrets expire (6–24 months): create a new one in Entra and paste it here before then.

## Google

**You'll enter:** Google Workspace domain (optional) (Only for Workspace: shows only accounts from this domain on Google's chooser. Access is still decided by the rules below.); client ID; client secret.

**Defaults:** scopes `openid email profile`, username claim `email`, email claim `email`, email must be verified: yes.

**Access:** Google doesn't send groups: allow specific email addresses, or a Workspace domain (checked against Google's verified `hd` claim).

### Requirements

- A Google Cloud project (any Google account, or your Workspace admin).
- WooSarr reachable over HTTPS with a real domain name (Google rejects IP-address redirect URIs; http is allowed only for localhost).

### Steps

1. Open **console.cloud.google.com** and create or pick a project.
2. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding/Audience): app name WooSarr; User type **Internal** for Workspace, otherwise **External** and add your admins as **Test users** (or publish the app).
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Web application**.
4. Authorized redirect URI: `https://<your WooSarr address>/api/v1/auth/oidc/callback`. Create.
5. Copy the **Client ID** and **Client secret**.

### Good to know

- Allowed domains are matched against the `hd` (hosted domain) claim, which only Workspace accounts have; for personal Gmail accounts list the email addresses.

## ADFS

**You'll enter:** ADFS host (Your federation service name, without https://); client ID; client secret.

**Defaults:** scopes `openid profile email allatclaims`, username claim `upn`, email claim `email`, groups claim `role`, email must be verified: no.

**Access:** Allow the AD group names you send as the `role` claim (issuance transform rule).

### Requirements

- AD FS on Windows Server 2016 or later (2019+ recommended), with admin access to AD FS Management.
- The ADFS server's certificate trusted by WooSarr's container (a public CA, or add your CA to the image).
- WooSarr reachable over HTTPS.

### Steps

1. AD FS Management → **Application Groups → Add Application Group**: name **WooSarr**, template **Server application accessing a web API**. Next.
2. Server application: copy the **Client Identifier**; Redirect URI `https://<your WooSarr address>/api/v1/auth/oidc/callback` → **Add**. Next.
3. Configure Application Credentials: **Generate a shared secret** and copy it. Next.
4. Configure Web API: Identifier = the **Client Identifier** from above (so tokens are issued to WooSarr). Next.
5. Access control policy: e.g. **Permit specific group** → your WooSarr admins group. Next.
6. Permitted scopes: **openid**, **profile**, **email**, **allatclaims**. Finish.
7. Open the Web API's **Properties → Issuance Transform Rules → Add Rule**: *Send LDAP Attributes as Claims* (Active Directory): User-Principal-Name → **UPN**, E-Mail-Addresses → **E-Mail Address**, Token-Groups - Unqualified Names → **Role**.
8. `allatclaims` makes ADFS copy those claims into the ID token, where WooSarr reads them.

### Good to know

- Issuer: https://<host>/adfs (WooSarr reads https://<host>/adfs/.well-known/openid-configuration).
- Claim names in the ID token are short names (upn, email, role) when the rules above are used.

## Custom OIDC provider

**You'll enter:** Issuer URL (Exactly as in the provider's /.well-known/openid-configuration (Authentik, Keycloak, Authelia, Zitadel, Auth0, Pocket ID...)); client ID; client secret.

**Defaults:** scopes `openid email profile`, username claim `preferred_username`, email claim `email`, groups claim `groups`, email must be verified: yes.

**Access:** Allow a group (most providers send `groups`), specific emails, or an email domain. Nested claims work with dots, e.g. realm_access.roles for Keycloak.

### Requirements

- An OpenID Connect provider that supports the authorization code flow and publishes a discovery document.
- A confidential (web) client with a client secret.
- WooSarr reachable over HTTPS (most providers require it for redirect URIs).

### Steps

1. Create an OIDC / OAuth2 application of type **confidential / web**, grant **authorization code**.
2. Redirect URI: `https://<your WooSarr address>/api/v1/auth/oidc/callback` (exact match).
3. Scopes: openid, email, profile (and groups if your provider uses a scope for them).
4. Copy the client ID, client secret and issuer URL.
5. Make sure group membership is included in the **ID token** (or the userinfo endpoint) if you want group-based access.

## Troubleshooting

| Message | Meaning / fix |
|---------|---------------|
| *state mismatch* | The sign-in started in another browser/tab or cookies are blocked; start again from the login page. |
| *issuer mismatch* | The issuer you entered doesn't match the provider's discovery document (e.g. wrong Okta authorization server, Entra `common`). |
| *different application (audience)* | The client ID doesn't match the application that issued the token. |
| *expired / isn't valid yet* | The clocks of the container and the provider differ by more than 2 minutes. |
| *not in an allowed group, email or domain* | Use **Test sign-in** to see which claims arrive; add the groups claim to the ID token or adjust the rules. |
| *redirect URI mismatch* (at the provider) | The redirect URI registered at the provider must match exactly, including https and the path. |
