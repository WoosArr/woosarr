import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// One fresh demo backend per run; tests run in order and share its state.
test.describe.configure({ mode: "serial" });

const PASSWORD = "e2e-" + "long-password-1";
let errors: string[] = [];

function watchConsole(page: Page) {
  errors = [];
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("401")) errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(String(e)));
}

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(PASSWORD);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/login")),
    page.getByRole("button", { name: "Sign in" }).click(),
  ]);
  expect(res.status(), await res.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test.beforeEach(({ page }) => watchConsole(page));
test.afterEach(() => expect(errors, "browser console errors / CSP violations").toEqual([]));

test("first-run setup creates the admin and lands on the dashboard", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /create the first admin/i })).toBeVisible();
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: /create admin/i }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Dry-run is on.")).toBeVisible();
  // The startup sync loads the demo inventory ~1 s after boot; the dashboard polls every 10 s.
  await expect(page.getByRole("link", { name: /Plex users\s*5/ })).toBeVisible({ timeout: 15_000 });
});

test("sidebar navigation reaches every page", async ({ page }) => {
  await login(page);
  for (const name of ["Users", "Roles", "Libraries", "Reactions", "Requests", "Logs", "Settings", "Setup", "Dashboard"]) {
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name }).click();
    await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  }
});

test("admin links a user and maps a role with an impact preview", async ({ page }) => {
  await login(page);
  await page.goto("/users");
  const alice = page.getByRole("row", { name: /alice/ });
  await alice.getByRole("button", { name: "Link" }).click();
  await page.getByLabel("Search Discord members").fill("ali");
  const linkButtons = page.getByRole("dialog").getByRole("button", { name: "Link" });
  await expect(linkButtons).toHaveCount(1); // search narrowed to alice
  await linkButtons.click();
  await expect(alice.getByText("Verified")).toBeVisible();
  await expect(alice.getByText("Movie Night")).toBeVisible();

  await page.goto("/roles");
  await page.getByRole("row", { name: /Movie Night/ }).getByRole("button", { name: "Edit libraries" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("4K Movies").check();
  await expect(dialog.getByText(/1 gain/)).toBeVisible();
  await expect(dialog.getByText(/alice/)).toBeVisible();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("row", { name: /Movie Night/ }).getByText("4K Movies")).toBeVisible();

  await page.goto("/users");
  const row = page.getByRole("row", { name: /alice/ });
  await expect(row.getByText("4K Movies")).toBeVisible();
  await expect(row.getByText("pending")).toBeVisible(); // dry run: granted but not shared yet
});

test("manual grant, exemption and unlink with confirmation", async ({ page }) => {
  await login(page);
  await page.goto("/users");
  await page.getByLabel("Select erin").check();
  await page.getByRole("button", { name: /Add libraries/ }).click();
  await page.getByRole("dialog").getByLabel("Music").check();
  await page.getByRole("dialog").getByRole("button", { name: "Add" }).click();
  await expect(page.getByRole("row", { name: /erin/ }).getByText("manual")).toBeVisible();

  await page.goto("/libraries");
  await page.getByRole("row", { name: /Family Videos/ }).getByRole("switch").click();
  await page.getByRole("dialog").getByRole("button", { name: "Exempt" }).click();
  await expect(page.getByRole("row", { name: /Family Videos/ }).getByText("Exempt", { exact: true })).toBeVisible();

  await page.goto("/users");
  await page.getByRole("row", { name: /alice/ }).getByRole("button", { name: "Unlink" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Unlink" }).click();
  await expect(page.getByRole("row", { name: /alice/ }).getByText("Not linked")).toBeVisible();
});

test("reaction post draft with emoji and roles", async ({ page }) => {
  await login(page);
  await page.goto("/reactions");
  await page.getByRole("button", { name: "New post" }).click();
  const dialog = page.getByRole("dialog");
  const pickRole = async (n: number, name: string) => {
    const select = dialog.getByLabel("Gives role").nth(n);
    const value = await select.locator("option", { hasText: name }).getAttribute("value");
    await select.selectOption(value!);
  };
  await pickRole(0, "Movie Night");
  await dialog.getByRole("button", { name: "Add reaction" }).click();
  await dialog.getByRole("button", { name: "Emoji 2: choose emoji" }).click();
  await dialog.getByLabel("Search emoji").fill("popcorn");
  await dialog.getByRole("button", { name: /🍿/ }).click();
  await expect(dialog.getByLabel("Emoji 2", { exact: true })).toHaveValue("🍿");
  await dialog.getByLabel("Legend heading (optional)").fill("Pick one:");
  await pickRole(1, "Series Fans");
  const preview = dialog.getByLabel("Message preview");
  await expect(preview).toContainText("Pick one:");
  await expect(preview).toContainText("@Movie Night");
  await expect(preview).toContainText("🍿 @Series Fans");
  await expect(dialog.getByText(/\d+ \/ 2000 characters/)).toBeVisible();
  await dialog.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByRole("heading", { name: "Plex libraries" })).toBeVisible();
  await expect(page.getByText("Draft")).toBeVisible();
  await expect(page.getByText(/Series Fans · 0/)).toBeVisible();
});

test("remove libraries: tri-state boxes, impact preview, undo", async ({ page }) => {
  await login(page);
  await page.goto("/users");
  await page.getByLabel("Select alice").check();
  await page.getByLabel("Select bob").check();
  await page.getByRole("button", { name: /Remove libraries/ }).click();
  const dialog = page.getByRole("dialog");
  // alice: Movies + TV Shows, bob: Movies + Family Videos (demo shares)
  const box = (name: string) => dialog.getByRole("checkbox", { name, exact: true });
  await expect(box("Movies")).toBeChecked();
  expect(await box("TV Shows").evaluate((el) => (el as HTMLInputElement).indeterminate)).toBe(true);
  await expect(box("Music")).toBeDisabled();
  await box("Family Videos").click();
  await expect(dialog.getByText("— will be removed")).toBeVisible();
  await expect(dialog.getByText(/bob\s*loses Family Videos/)).toBeVisible();
  await dialog.getByRole("button", { name: "Reset" }).click();
  await expect(dialog.getByText("— will be removed")).toHaveCount(0);
  await box("Family Videos").click();
  await dialog.getByRole("button", { name: "Remove 1 library" }).click();
  await expect(dialog.getByText(/Dry-run is on/)).toBeVisible();
  await expect(dialog.getByText(/removed Family Videos/)).toBeVisible();
  await dialog.getByRole("button", { name: "Undo" }).click();
  await expect(dialog.getByText(/Reverted/)).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
});

test("settings show the impact of going live", async ({ page }) => {
  await login(page);
  await page.goto("/settings");
  await page.getByRole("switch", { name: /Dry-run/ }).click();
  await expect(page.getByRole("heading", { name: "Impact of these changes" })).toBeVisible();
  // Either a gain/lose summary or "nothing changes", depending on links at this point in the run.
  await expect(page.getByText(/\d+ gain/).first().or(page.getByText("No linked user's access changes."))).toBeVisible();
  await page.getByRole("button", { name: "Discard" }).click();
  await expect(page.getByRole("heading", { name: "Impact of these changes" })).toHaveCount(0);
});

test("logs and audit trail", async ({ page }) => {
  await login(page);
  await page.goto("/logs");
  await expect(page.getByRole("log").getByText(/DEMO MODE/)).toBeVisible();
  await page.getByRole("tab", { name: "Audit trail" }).click();
  await expect(page.getByRole("cell", { name: "Role mapping changed" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "Unlinked by admin" }).first()).toBeVisible();
});

test("theme, colour-vision palette and API key", async ({ page }) => {
  await login(page);
  await page.goto("/settings?tab=appearance");
  await page.getByRole("button", { name: "Dark", exact: true }).click();
  await page.getByLabel(/Red-green friendly/).check();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-palette", "redgreen");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.goto("/settings?tab=keys");
  await page.getByLabel("Key name").fill("e2e");
  await page.getByRole("button", { name: "Create key" }).click();
  const key = (await page.getByTestId("new-api-key").textContent()) ?? "";
  expect(key).toMatch(/^wsk_[0-9a-f]{8}_/);
  const res = await page.request.get("/api/v1/logs?limit=1", { headers: { Authorization: `Bearer ${key}` } });
  expect(res.status()).toBe(200);
});

test("going live needs confirmation; sign out returns to login", async ({ page }) => {
  await login(page);
  await page.goto("/settings");
  await page.getByRole("switch", { name: /Dry-run/ }).click();
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Go live" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();
  await page.goto("/");
  await expect(page.getByText("Dry-run is on.")).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});

test("no serious accessibility violations on any page (light and dark)", async ({ page }) => {
  test.setTimeout(150_000); // 13 pages x 2 themes
  await login(page);
  const problems: string[] = [];
  for (const theme of ["light", "dark"]) {
    await page.evaluate((mode) => localStorage.setItem("woosarr.theme", JSON.stringify({ mode })), theme);
    for (const path of ["/", "/users", "/roles", "/libraries", "/reactions", "/requests", "/logs", "/logs?tab=audit", "/settings", "/settings?tab=requests", "/settings?tab=appearance", "/settings?tab=keys", "/setup"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
      for (const v of results.violations.filter((v) => v.impact === "serious" || v.impact === "critical")) {
        problems.push(`${theme} ${path}: ${v.id} (${v.nodes.length}) ${v.nodes[0]?.target.join(" ")}`);
      }
    }
  }
  expect(problems).toEqual([]);
});

test("request command name can be renamed but not to a reserved name", async ({ page }) => {
  await login(page);
  await page.goto("/settings?tab=requests");
  const name = page.getByLabel("Command name");
  await expect(name).toHaveValue("request");
  await name.fill("invite");
  await expect(page.getByText("/invite is already one of WooSarr's commands.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
  await name.fill("my request");
  await expect(page.getByText(/1–32 characters/)).toBeVisible();
  await name.fill("plexrequest");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("dialog")).toContainText("Server Settings → Integrations");
  await page.getByRole("dialog").getByRole("button", { name: "Rename" }).click();
  await expect(page.getByText("Saved. Discord shows /plexrequest within a few seconds.")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Command name")).toHaveValue("plexrequest");
  await page.goto("/requests");
  await expect(page.getByText("Connect Seerr on the")).toBeVisible();
});

test("mobile layout uses a drawer for navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await login(page);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Libraries" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Libraries" })).toBeVisible();
});
