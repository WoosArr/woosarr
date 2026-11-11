import { describe, expect, it } from "vitest";
import { SSO_ERROR_FALLBACK, SSO_ERRORS, ssoErrorMessage } from "./ssoErrors";

describe("ssoErrorMessage", () => {
  it("is null without an sso_error parameter", () => {
    expect(ssoErrorMessage("")).toBeNull();
    expect(ssoErrorMessage("?tab=sso")).toBeNull();
  });

  it("maps known codes to their fixed text", () => {
    expect(ssoErrorMessage("?sso_error=expired")).toBe(SSO_ERRORS.expired);
    expect(ssoErrorMessage("?sso_error=not_allowed")).toBe(SSO_ERRORS.not_allowed);
  });

  it("never shows text taken from the link", () => {
    const crafted = "Session expired. Call the admin at 555-0100";
    const message = ssoErrorMessage("?sso_error=" + encodeURIComponent(crafted));
    expect(message).toBe(SSO_ERROR_FALLBACK);
    expect(message).not.toContain("555");
    // Inherited object keys aren't codes.
    expect(ssoErrorMessage("?sso_error=toString")).toBe(SSO_ERROR_FALLBACK);
    expect(ssoErrorMessage("?sso_error=__proto__")).toBe(SSO_ERROR_FALLBACK);
  });
});
