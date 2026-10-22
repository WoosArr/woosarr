import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Documentation screenshots (docs/images). Everything shown comes from the demo
// backend (WOOSARR_DEMO: fake Plex server and Discord guild) plus the fake data
// seeded below through the API. Run with `make screenshots`.

const OUT = "../docs/images";
const PASSWORD = "shots-" + "long-password-1";

async function setupAdmin(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: /create admin/i }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  // The startup sync loads the demo inventory about a second after boot.
  await expect(page.getByRole("link", { name: /Plex users\s*5/ })).toBeVisible({ timeout: 20_000 });
}

async function client(page: Page): Promise<(method: string, path: string, data?: unknown) => Promise<unknown>> {
  const status = await (await page.request.get("/api/v1/auth/status")).json();
  const headers = { "X-CSRF-Token": status.csrf_token as string };
  const req: APIRequestContext = page.request;
  return async (method, path, data) => {
    const r = await req.fetch(path, { method, data, headers });
    expect(r.ok(), `${method} ${path}: ${r.status()} ${await r.text()}`).toBeTruthy();
    return r.status() === 204 ? null : r.json();
  };
}

async function seed(api: Awaited<ReturnType<typeof client>>) {
  // The demo's own startup notices aren't interesting in a screenshot; resolve them.
  for (const g of (await api("GET", "/api/v1/errors")) as { fingerprint: string; logger: string }[]) {
    if (g.logger !== "woosarr.accounts") await api("PUT", `/api/v1/errors/${g.fingerprint}/resolved`);
  }
  // Role → library mappings (libraries 1-6). Demo role ids are looked up by name.
  const roleId = Object.fromEntries(((await api("GET", "/api/v1/roles")) as { role_id: string; name: string }[])
    .map((r) => [r.name, r.role_id]));
  for (const [role, libs] of [["Movie Night", ["1", "4"]], ["Series Fans", ["2"]], ["Anime Club", ["3"]], ["Supporters", ["4", "5"]]] as const) {
    await api("PUT", `/api/v1/roles/${roleId[role]}/libraries`, { library_ids: libs });
  }
  await api("PATCH", "/api/v1/libraries/6", { is_exempt: true }); // Family Videos
  // Links: alice and bob on their own accounts, carol's account shared by a family (Carol + Frank).
  const memberId = Object.fromEntries(((await api("GET", "/api/v1/discord/member-list")) as { discord_user_id: string; display_name: string }[])
    .map((m) => [m.display_name, m.discord_user_id]));
  for (const [plex, discord] of [["1001", "Alice"], ["1002", "Bob"], ["1003", "Carol"], ["1003", "Frank"]]) {
    await api("PUT", `/api/v1/users/${plex}/link`, { discord_user_id: memberId[discord] });
  }
  await api("POST", "/api/v1/reaction-posts", {
    title: "Library roles",
    content: "React below to pick the Plex libraries you'd like. Remove your reaction to give one back.",
    pin: true,
    options: [
      { emoji: "🎬", role_id: roleId["Movie Night"] },
      { emoji: "📺", role_id: roleId["Series Fans"] },
      { emoji: "🍙", role_id: roleId["Anime Club"] },
    ],
    legend: true,
    legend_template: "{emoji} {role} — {libraries}",
    legend_header: "**Pick your libraries:**",
  });
  await api("POST", "/api/v1/sync", {});
}

async function shot(page: Page, name: string, opts: { fullPage?: boolean } = {}) {
  await page.waitForTimeout(700); // charts and polled data settle
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: opts.fullPage ?? false });
}

async function theme(page: Page, mode: "light" | "dark") {
  await page.evaluate((m) => localStorage.setItem("woosarr.theme", JSON.stringify({ mode: m, palette: "default", highContrast: false })), mode);
}

test("documentation screenshots", async ({ page }) => {
  await setupAdmin(page);
  const api = await client(page);
  await seed(api);
  await page.waitForTimeout(2500); // the queued sync runs

  await page.setViewportSize({ width: 1440, height: 1720 }); // the whole default dashboard
  for (const mode of ["dark", "light"] as const) {
    await theme(page, mode);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Library access" })).toBeVisible();
    await shot(page, `dashboard-${mode}`);
  }
  await page.setViewportSize({ width: 1440, height: 900 });

  await page.goto("/users");
  await expect(page.getByText("Shared by 2")).toBeVisible();
  await shot(page, "users");

  await page.goto("/users?tab=discord");
  await expect(page.getByRole("region", { name: "Discord members" })).toBeVisible();
  await shot(page, "users-discord");
  await page.getByLabel("Select Frank").check();
  await page.getByLabel("Select Bob").check();
  await page.getByRole("button", { name: /Add roles/ }).click();
  await page.getByRole("dialog").getByLabel("Anime Club").check();
  await expect(page.getByRole("dialog").getByText(/gains/).first()).toBeVisible();
  await page.waitForTimeout(400);
  await page.getByRole("dialog").screenshot({ path: `${OUT}/discord-add-roles.png` });
  await page.keyboard.press("Escape");

  await page.goto("/roles");
  await shot(page, "roles");

  await page.goto("/libraries");
  await shot(page, "libraries");

  await page.goto("/reactions");
  await page.getByRole("button", { name: "Edit" }).first().click();
  const preview = page.getByLabel("Message preview");
  await expect(preview).toContainText("Pick your libraries");
  await preview.scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  await page.getByRole("dialog").screenshot({ path: `${OUT}/reaction-post-editor.png` });
  await page.keyboard.press("Escape");

  for (const [source, name] of [["openmoji", "popcorn"], ["emojigg", "blob_wave"], ["emojigg", "cat_vibe"]]) {
    const found = (await api("GET", `/api/v1/emoji/search?source=${source}&q=${name}&hide_animated=false`)) as { items: { id: string }[] };
    await api("POST", "/api/v1/emoji/add", { source, id: found.items[0].id, name });
  }
  await page.goto("/emoji");
  await page.getByRole("switch", { name: "Hide animated" }).uncheck();
  await expect(page.getByRole("button", { name: /party_parrot/ })).toBeVisible();
  await shot(page, "custom-emoji");

  await page.goto("/settings?tab=linking");
  await shot(page, "settings-linking");

  await page.goto("/setup");
  await shot(page, "integrations");

  await page.goto("/logs?tab=audit");
  await shot(page, "audit-trail");
});
