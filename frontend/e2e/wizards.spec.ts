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
  await expect(plex.getByText(/4 libraries, 3 users, 1 with shares/)).toBeVisible();

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
  await expect(discord.getByText("Connected")).toBeVisible();
  await expect(discord.getByText(/run \/link/)).toBeVisible();

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  await page.goto("/");
  await expect(page.getByText("Discord bot not set up yet")).toHaveCount(0);
});
