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
  for (const name of ["Users", "Roles", "Libraries", "Reactions", "Logs", "Settings", "Setup", "Dashboard"]) {
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
  await dialog.getByRole("group", { name: "Roles for reaction 1" }).getByLabel(/Movie Night/).check();
  await dialog.getByRole("button", { name: "Add reaction" }).click();
  await dialog.getByLabel("Emoji").nth(1).fill("📺");
  await dialog.getByRole("group", { name: "Roles for reaction 2" }).getByLabel(/Series Fans/).check();
  await dialog.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByRole("heading", { name: "Plex libraries" })).toBeVisible();
  await expect(page.getByText("Draft")).toBeVisible();
  await expect(page.getByText(/Series Fans · 0/)).toBeVisible();
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
  test.setTimeout(120_000); // 11 pages x 2 themes
  await login(page);
  const problems: string[] = [];
  for (const theme of ["light", "dark"]) {
    await page.evaluate((mode) => localStorage.setItem("woosarr.theme", JSON.stringify({ mode })), theme);
    for (const path of ["/", "/users", "/roles", "/libraries", "/reactions", "/logs", "/logs?tab=audit", "/settings", "/settings?tab=appearance", "/settings?tab=keys", "/setup"]) {
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

test("mobile layout uses a drawer for navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await login(page);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Libraries" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Libraries" })).toBeVisible();
});
