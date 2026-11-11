// Failed SSO sign-ins come back as /?sso_error=<code> (backend api/oidc.py SsoErrorCode).
// Only text from this table is shown, never the query string itself, so a crafted link
// can't put its own words on the login page. The detailed reason is in WooSarr's log.
const SEE_LOG = " The reason is in WooSarr's log (Logs page).";

export const SSO_ERRORS: Readonly<Record<string, string>> = {
  unavailable: "That sign-in provider isn't available right now." + SEE_LOG,
  expired: "The sign-in expired or didn't start in this browser. Please try again.",
  provider_error: "The sign-in provider refused the sign-in." + SEE_LOG,
  failed: "Single sign-on failed." + SEE_LOG,
  not_allowed: "This account isn't allowed to sign in to WooSarr.",
  disabled: "This admin account is disabled.",
};

export const SSO_ERROR_FALLBACK = "Single sign-on failed." + SEE_LOG;

/** The message for `?sso_error=<code>` in a location search string, or null when there is none. */
export function ssoErrorMessage(search: string): string | null {
  const code = new URLSearchParams(search).get("sso_error");
  if (code === null) return null;
  return Object.prototype.hasOwnProperty.call(SSO_ERRORS, code) ? SSO_ERRORS[code] : SSO_ERROR_FALLBACK;
}
