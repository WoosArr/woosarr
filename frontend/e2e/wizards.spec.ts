import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });
const PASSWORD = "wizard-" + "long-password";

test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  (page as unknown as { _errs: string[] })._errs = errors;
});

test("plex wizard: sign in, pick server, read and write checks", async ({ page, context }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: /create admin/i }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Not signed in to Plex")).toBeVisible();

  await page.goto("/setup");
  const plex = page.getByRole("region", { name: "Plex" }).or(page.locator("section", { has: page.getByRole("heading", { name: "Plex" }) }));
  const popupPromise = context.waitForEvent("page");
  await plex.getByRole("button", { name: "Sign in with Plex" }).click();
  const popup = await popupPromise;
  // Plex would show its sign-in page; the fake sends the popup straight back to /setup?plex=signed-in.
  await popup.waitForLoadState().catch(() => undefined);
  await expect(page.getByText(/Signed in to Plex as owner/)).toBeVisible({ timeout: 15_000 });

  await expect(plex.getByText("Choose your server")).toBeVisible();
  await expect(plex.getByText("Friend's")).toHaveCount(0); // servers you don't own aren't offered
  await plex.getByLabel(/Home/).check();
  await plex.getByRole("button", { name: "Use this server" }).click();
  await expect(plex.getByText("Connected")).toBeVisible();

  await plex.getByRole("button", { name: "Run read check" }).click();
  await expect(plex.getByText("Read check passed")).toBeVisible();
  await expect(plex.getByText(/4 libraries, 4 users, 1 with shares/)).toBeVisible();

  await plex.getByRole("button", { name: /Verify sharing/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Test user").locator("option", { hasText: "alice" })).toHaveCount(1, { timeout: 15_000 });
  await dialog.getByLabel("Test user").selectOption({ label: "alice" });
  await dialog.getByRole("button", { name: "Run check" }).click();
  await expect(dialog.getByText("Sharing behaves as expected")).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();

  await page.goto("/libraries");
  await expect(page.getByText(/Plex Media Server 1\.41/)).toBeVisible();
});

test("discord wizard: bad token, good token, invite, pick server", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await page.goto("/setup");
  const discord = page.locator("section", { has: page.getByRole("heading", { name: "Discord" }) });
  await discord.getByLabel("Bot token").fill("bad-" + "x".repeat(60));
  await discord.getByRole("button", { name: "Save token" }).click();
  await expect(discord.getByText(/rejected this bot token/)).toBeVisible();

  await discord.getByLabel("Bot token").fill("good-" + "x".repeat(60));
  await discord.getByRole("button", { name: "Save token" }).click();
  await expect(discord.getByText(/Bot TestBot is ready/)).toBeVisible();
  await expect(discord.getByRole("link", { name: "Invite the bot" })).toHaveAttribute("href", /client_id=4242/);
  await discord.getByLabel("Discord server").selectOption({ label: "Test Guild" });
  await discord.getByRole("button", { name: "Use this server" }).click();
  await expect(discord.getByText("Connected", { exact: true })).toBeVisible();
  await expect(discord.getByText(/run \/link/)).toBeVisible();

  // Bot avatar: custom upload previews; applying sends the image (intercepted: never reaches Discord).
  const sent: { source?: string; image?: string | null; app_icon?: boolean }[] = [];
  await page.route("**/api/v1/discord/avatar", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ json: { avatar_url: null, app_icon: true, width: 192, height: 192 } });
  });
  await discord.getByText("Bot avatar").click();
  await expect(discord.getByRole("img", { name: "New bot avatar" })).toHaveAttribute("src", "/logo-192.webp");
  await discord.getByLabel(/My own image/).check();
  await discord.getByLabel("Image file").setInputFiles(path.resolve("public/icon-192.png"));
  await expect(discord.getByRole("img", { name: "New bot avatar" })).toHaveAttribute("src", /^data:image\/png;base64,/);
  await discord.getByLabel(/Also use it as the application icon/).check();
  await discord.getByRole("button", { name: "Update avatar" }).click();
  await expect(page.getByText(/Bot avatar updated \(and the application icon\)/)).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0].source).toBe("custom");
  expect(sent[0].app_icon).toBe(true);
  expect(sent[0].image).toMatch(/^data:image\/png;base64,/);

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  await page.goto("/");
  await expect(page.getByText("Discord bot not set up yet")).toHaveCount(0);
});

test("sso wizard: add a provider, test it, sign in with it", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  await page.goto("/settings?tab=sso");
  await expect(page.getByText(/Okta only accept|only accept/)).toBeVisible(); // http warning
  await page.getByRole("button", { name: "Add provider" }).click();
  const dialog = page.getByRole("dialog");
  for (const kind of ["Okta", "Microsoft Entra ID", "Google", "ADFS", "Custom OIDC provider"]) {
    await expect(dialog.getByRole("radio", { name: new RegExp(kind) })).toBeVisible();
  }
  await dialog.getByRole("radio", { name: /Custom OIDC provider/ }).check();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByRole("heading", { name: "Requirements" })).toBeVisible();
  await expect(dialog.getByText("/api/v1/auth/oidc/callback").first()).toBeVisible();
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByLabel("Name").fill("Test IdP");
  await dialog.getByLabel("Issuer URL").fill("https://idp.test");
  await dialog.getByLabel("Client ID").fill("woosarr-client");
  await dialog.getByLabel("Client secret").fill("s3cret-xxxxxxxx");
  await dialog.getByRole("button", { name: /Save & check connection/ }).click();
  await expect(dialog.getByText("Found https://idp.test")).toBeVisible();
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByLabel("Allowed groups").fill("WooSarr Admins");
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByRole("button", { name: "Enable" }).click();
  await expect(page.getByText("Enabled: the login page offers it now.")).toBeVisible();

  // Test sign-in: claims + decision, no login
  await page.getByRole("button", { name: "Test sign-in" }).click();
  await expect(page.getByText(/would be allowed in/)).toBeVisible();
  await expect(page.getByText("WooSarr Admins", { exact: true })).toBeVisible(); // the Groups row

  // Real sign-in from the login page
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("link", { name: "Sign in with Test IdP" }).click();
  // Back where the admin was (next=...), now signed in as the SSO admin.
  await expect(page.getByRole("navigation", { name: "Main" }).getByText("alice")).toBeVisible();
  await expect(page).toHaveURL(/\/settings/);

  await page.goto("/settings?tab=sso");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
});
