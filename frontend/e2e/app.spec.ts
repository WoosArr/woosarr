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
  for (const name of ["Users", "Roles", "Libraries", "Reaction Posts", "Custom Emoji", "Requests", "Logs", "Settings", "Integrations", "Dashboard"]) {
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
  // A family: Frank shares alice's Plex account, then is removed again.
  const alice = page.getByRole("row", { name: /alice/ });
  await alice.getByRole("button", { name: "Add member" }).click();
  await page.getByLabel("Search Discord members").fill("frank");
  const addButtons = page.getByRole("dialog").getByRole("button", { name: "Link" });
  await expect(addButtons).toHaveCount(1);
  await addButtons.click();
  await expect(alice.getByText("Shared by 2")).toBeVisible();
  await alice.getByRole("button", { name: "Unlink Frank from alice" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove member" }).click();
  await expect(alice.getByText("Shared by 2")).toHaveCount(0);

  await alice.getByRole("button", { name: /^Unlink / }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Unlink" }).click();
  await expect(alice.getByText("Not linked")).toBeVisible();
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
  await expect(page.getByRole("dialog")).toHaveCount(0);  // saving closes the editor
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

test("settings tabs: username linking limited to a role", async ({ page }) => {
  await login(page);
  await page.goto("/settings?tab=linking");
  const picker = page.getByRole("group", { name: /link by typing a Plex username/ });
  await picker.getByRole("checkbox", { name: /All members/ }).uncheck();
  await picker.getByRole("checkbox").nth(1).check();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();
  const saved = await (await page.request.get("/api/v1/settings")).json();
  expect(saved.username_link_all_members).toBe(false);
  expect(saved.username_link_role_ids).toHaveLength(1);
  await expect(page.getByRole("group", { name: "Plex Invites" })).toBeVisible();
  await expect(page.getByLabel("When a member unlinks themselves")).toBeVisible();
  await page.goto("/settings?tab=logging");  // merged into General; old links still work
  await expect(page.getByRole("tab", { name: "General" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Log level")).toBeVisible();
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
  test.setTimeout(180_000); // 16 pages x 2 themes
  await login(page);
  const problems: string[] = [];
  for (const theme of ["light", "dark"]) {
    await page.evaluate((mode) => localStorage.setItem("woosarr.theme", JSON.stringify({ mode })), theme);
    for (const path of ["/", "/users", "/users?tab=discord", "/roles", "/libraries", "/reactions", "/emoji", "/requests", "/logs", "/logs?tab=audit", "/settings", "/settings?tab=linking", "/settings?tab=requests", "/settings?tab=appearance", "/settings?tab=keys", "/setup"]) {
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

test("custom emoji: search, hide animated, add, use in a reaction post, rename, delete", async ({ page }) => {
  await login(page);
  await page.goto("/emoji");
  const results = page.getByRole("list", { name: "Emoji results" });
  await expect(results.getByRole("button", { name: "blob_wave: add to server" })).toBeVisible();
  // Animated emoji are hidden until the switch is turned off.
  await expect(results.getByRole("button", { name: /party_parrot/ })).toHaveCount(0);
  await page.getByRole("switch", { name: "Hide animated" }).uncheck();
  await expect(results.getByRole("button", { name: /party_parrot/ })).toBeVisible();
  // Previews come from WooSarr, not the source site.
  const src = await results.getByRole("button", { name: /party_parrot/ }).locator("img").getAttribute("src");
  expect(src).toMatch(/^\/api\/v1\/emoji\/preview\/emojigg\//);

  await page.getByRole("tab", { name: "OpenMoji" }).click();
  await page.getByLabel("Search emoji").fill("popcorn");
  await results.getByRole("button", { name: "popcorn: add to server" }).click();
  const dialog = page.getByRole("dialog", { name: "Add to Discord" });
  await dialog.getByLabel("Name").fill("bad name");
  await expect(dialog.getByRole("button", { name: "Add to server" })).toBeDisabled();
  await dialog.getByLabel("Name").fill("movie_popcorn");
  await dialog.getByRole("button", { name: "Add to server" }).click();
  await expect(page.getByText(":movie_popcorn: added to the server.")).toBeVisible();
  const server = page.getByRole("list", { name: "Server emoji" });
  await expect(server.getByText(":movie_popcorn:")).toBeVisible();
  await expect(page.getByText(/Static 1 of 50/)).toBeVisible();

  await page.getByRole("tab", { name: "slackmojis" }).click();
  await expect(page.getByText("Search slackmojis", { exact: true })).toBeVisible();

  // The reaction post picker fetches the server's emoji each time it opens.
  await page.goto("/reactions");
  await page.getByRole("button", { name: "New post" }).click();
  const editor = page.getByRole("dialog");
  await editor.getByRole("button", { name: "Emoji 1: choose emoji" }).click();
  await editor.getByRole("button", { name: ":movie_popcorn:" }).click();
  await expect(editor.getByLabel("Emoji 1", { exact: true })).toHaveValue(/^<:movie_popcorn:\d+>$/);
  await editor.getByRole("button", { name: "Close" }).click();

  await page.goto("/emoji");
  await server.getByRole("button", { name: "Rename :movie_popcorn:" }).click();
  const rename = page.getByRole("dialog", { name: "Rename :movie_popcorn:" });
  await rename.getByLabel("Name").fill("popcorn_time");
  await rename.getByRole("button", { name: "Rename" }).click();
  await expect(server.getByText(":popcorn_time:")).toBeVisible();
  await server.getByRole("button", { name: "Delete :popcorn_time:" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No custom emoji yet.", { exact: false })).toBeVisible();
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
  // Public requests need a channel before they can be saved.
  await page.getByLabel(/^Public/).check();
  await expect(page.getByText("Choose the channel for public requests.")).toBeVisible();
  await expect(page.getByLabel("Requests channel", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
  await expect(page.getByText("Only in this channel")).toBeVisible();
  await page.getByLabel(/^Private/).check();
  await expect(page.getByLabel("Requests channel", { exact: true })).toHaveCount(0);
  await page.goto("/requests");
  await expect(page.getByText("Connect Seerr on the")).toBeVisible();
});

test("dashboard widgets can be hidden, moved, added back and reset", async ({ page }) => {
  await login(page);
  await expect(page.getByRole("link", { name: /Plex users\s*5/ })).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Customize" }).click();
  const a11y = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(a11y.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  // Hide the Plex users tile, add the Security widget, move Linked users first.
  await page.locator("section", { has: page.getByText("Plex users", { exact: true }) }).getByRole("button", { name: "Hide" }).click();
  await page.getByRole("region", { name: "More widgets" }).getByRole("listitem").filter({ hasText: "Security" }).getByRole("button", { name: "Add" }).click();
  await page.getByRole("button", { name: "Move Linked users earlier" }).click(); // past Connections
  await page.getByRole("button", { name: "Move Linked users earlier" }).click(); // past Setup checklist: now first
  await expect(page.getByRole("button", { name: "Move Linked users earlier" })).toBeDisabled();
  await page.getByLabel("Size of Linked users").selectOption("m");
  // Libraries: eighth width, drawn as a circle; Link coverage: bars.
  await page.getByLabel("Size of Libraries").selectOption("xs");
  await page.getByLabel("Chart for Libraries").selectOption("donut");
  await page.getByLabel("Chart for Link coverage").selectOption("bar");
  const a11yCharts = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(a11yCharts.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  await page.getByRole("button", { name: "Save layout" }).click();
  await expect(page.getByText("Dashboard saved.")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Security" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Plex users\s*5/ })).toHaveCount(0);
  const first = page.locator(".dash-grid > section").first();
  await expect(first).toContainText("Linked users");
  await expect(first).toHaveClass(/w-m/);
  const libs = page.locator(".dash-grid > section", { has: page.getByRole("heading", { name: "Libraries", exact: true }) });
  await expect(libs).toHaveClass(/w-xs/);
  await expect(libs.getByRole("img", { name: /Libraries by how they're managed: Managed by roles \d+, Exempt \d+, No role \d+/ })).toBeVisible();
  await expect(page.getByRole("list", { name: /Plex users by link status: Verified \d+/ })).toBeVisible();
  await page.getByRole("button", { name: "Customize" }).click();
  await page.getByRole("button", { name: "Reset to default" }).click();
  await page.getByRole("button", { name: "Save layout" }).click();
  await page.reload();
  await expect(page.getByRole("link", { name: /Plex users\s*5/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Link coverage" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /^Connections - (Next Sync in \d+ minutes?|Syncing now|Sync queued)$/ })).toBeVisible();
});

test("new widgets: open requests and roles that grant libraries; libraries page sync", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Customize" }).click();
  const more = page.getByRole("region", { name: "More widgets" });
  for (const name of ["Open request list", "Roles that grant libraries", "Custom emoji", "Animated emoji"]) {
    await more.getByRole("listitem").filter({ hasText: new RegExp(`^${name}`) }).getByRole("button", { name: "Add" }).click();
  }
  await page.getByRole("button", { name: "Save layout" }).click();
  await expect(page.getByRole("heading", { name: "Roles that grant libraries" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Open request list" })).toBeVisible();
  // Demo server: 50 slots each, none used yet.
  await expect(page.getByRole("link", { name: /Custom emoji\s*0\/50\s*50 free/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Animated emoji\s*0\/50/ })).toBeVisible();
  await page.getByRole("button", { name: "Customize" }).click();
  await page.getByRole("button", { name: "Reset to default" }).click();
  await page.getByRole("button", { name: "Save layout" }).click();

  // Blank space: as many as wanted, empty outside Customize, removed rather than hidden.
  await page.getByRole("button", { name: "Customize" }).click();
  const blank = more.getByRole("listitem").filter({ hasText: /^Blank space/ }).getByRole("button", { name: "Add" });
  await blank.click();
  await blank.click();
  await expect(page.getByRole("region", { name: "Blank space 2" })).toBeVisible();
  await page.getByRole("button", { name: "Save layout" }).click();
  await page.reload();
  await expect(page.locator(".dash-spacer")).toHaveCount(2);
  await expect(page.locator(".dash-spacer").first()).toBeEmpty();
  await page.getByRole("button", { name: "Customize" }).click();
  await page.getByRole("region", { name: "Blank space 1" }).getByRole("button", { name: "Remove" }).click();
  await page.getByRole("button", { name: "Save layout" }).click();
  await page.reload();
  await expect(page.locator(".dash-spacer")).toHaveCount(1);
  await page.getByRole("button", { name: "Customize" }).click();
  await page.getByRole("button", { name: "Reset to default" }).click();
  await page.getByRole("button", { name: "Save layout" }).click();

  await page.goto("/libraries");
  await page.getByRole("button", { name: "Sync now" }).click();
  await expect(page.getByText("Full sync queued.")).toBeVisible();
});

test("users page has a Linked tab between Unverified and Unlinked", async ({ page }) => {
  await login(page);
  await page.goto("/users");
  const tabs = page.getByRole("group", { name: "Link filter" }).getByRole("button");
  await expect(tabs).toHaveText(["All", "Verified", "Unverified", "Linked", "Unlinked"]);
  await tabs.filter({ hasText: /^Linked$/ }).click();
  await expect(tabs.filter({ hasText: /^Linked$/ })).toHaveAttribute("aria-pressed", "true");
});

test("users page Discord tab: members, filters and a role change preview", async ({ page }) => {
  await login(page);
  await page.goto("/users");
  await page.getByRole("tab", { name: "Discord" }).click();
  await expect(page).toHaveURL(/tab=discord/);
  const table = page.getByRole("region", { name: "Discord members" });
  await expect(table.getByRole("row", { name: /Frank/ })).toBeVisible();
  const filters = page.getByRole("group", { name: "Link filter" }).getByRole("button");
  await expect(filters).toHaveText(["All", "Verified", "Unverified", "Linked", "Unlinked"]);
  await filters.filter({ hasText: /^Unlinked$/ }).click();
  await expect(filters.filter({ hasText: /^Unlinked$/ })).toHaveAttribute("aria-pressed", "true");
  await filters.filter({ hasText: /^All$/ }).click();

  await page.getByLabel("Select Frank").check();
  await page.getByRole("button", { name: /Add roles \(1\)/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add Discord roles" });
  await dialog.getByLabel("Supporters").check();
  await expect(dialog.getByRole("heading", { name: "Plex impact" })).toBeVisible();
  // The preview really ran (not stuck calculating): Frank isn't linked, so nothing changes.
  await expect(dialog.getByText("No linked Plex account's libraries change.")).toBeVisible();
  // Demo mode has no bot: the impact shows, but the change itself is blocked with the reason.
  await expect(dialog.getByText(/demo mode/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Add 1 role$/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("tab", { name: "Plex" }).click();
  await expect(page.getByRole("region", { name: "Plex users" })).toBeVisible();
});

test("roles deleted in Discord can be removed from WooSarr", async ({ page }) => {
  await login(page);
  await page.goto("/roles");
  const row = page.getByRole("row", { name: /Old Movie Night/ });
  await expect(row.getByText("Deleted in Discord")).toBeVisible();
  await expect(page.getByRole("row", { name: /Movie Night/ }).filter({ hasNotText: "Old" }).getByRole("button", { name: /Remove/ })).toHaveCount(0);
  await row.getByRole("button", { name: "Remove Old Movie Night from WooSarr" }).click();
  await expect(page.getByRole("dialog")).toContainText("no longer exists in Discord");
  await expect(page.getByRole("dialog")).toContainText("nothing else refers to it");
  await page.getByRole("dialog").getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("Removed 1 role.")).toBeVisible();
  await expect(page.getByRole("row", { name: /Old Movie Night/ })).toHaveCount(0);
});

test("libraries: create role lets the admin set the name", async ({ page }) => {
  await login(page);
  // Demo mode has no real bot: report role creation as available and capture the create call.
  await page.route("**/api/v1/discord/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.features = (body.features ?? []).map((f: { key: string }) => (f.key === "create_roles" ? { ...f, available: true, notes: [] } : f));
    await route.fulfill({ response: res, json: body });
  });
  const sent: Record<string, unknown>[] = [];
  await page.route("**/api/v1/discord/roles", async (route) => {
    sent.push(route.request().postDataJSON());
    await route.fulfill({ status: 201, json: { role_id: "123456789012345678", name: "Film Club" } });
  });
  await page.goto("/libraries");
  await page.getByRole("row", { name: /^Movies/ }).getByRole("button", { name: "Create role" }).click();
  const dialog = page.getByRole("dialog", { name: "Create a role for Movies" });
  const name = dialog.getByLabel("Name");
  await expect(name).toHaveValue("Plex · Movies");
  await expect(dialog.getByRole("checkbox", { name: /^Movies/ })).toBeChecked();
  await name.fill("Film Club");
  await dialog.getByRole("button", { name: "Create role" }).click();
  await expect(page.getByText(/Created Film Club in Discord and mapped its libraries/)).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0].name).toBe("Film Club");
  expect(sent[0].purpose).toBe("plain");
  expect((sent[0].library_ids as string[]).length).toBe(1);
});

test("mobile layout uses a drawer for navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await login(page);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Libraries" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Libraries" })).toBeVisible();
});
