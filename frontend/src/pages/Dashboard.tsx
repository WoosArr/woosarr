import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type PlanOut, type StatusOut } from "../api";
import type { components } from "../generated/api";
import { Icon, type IconName } from "../icons";
import { Badge, Empty, ErrorBanner, PageHead, Spinner, ago, useLoad, useToast, type Tone } from "../ui";

type ErrorGroup = components["schemas"]["ErrorGroupOut"];
type Insights = components["schemas"]["InsightsOut"];
type Widget = components["schemas"]["WidgetIn"];
type Layout = components["schemas"]["LayoutOut"];
type WidgetId = Widget["id"];
type Size = NonNullable<Widget["size"]>;
type View = NonNullable<Widget["view"]>;

type LastRun = {
  reason: string;
  full: boolean;
  dry_run: boolean;
  started_at: string;
  finished_at: string | null;
  users_checked: number;
  users_changed: number;
  writes: number;
  skipped_drift: number;
  errors: number;
  skipped: string | null;
  error_messages: string[];
};

interface Ctx {
  status: StatusOut;
  insights: Insights | null;
  errors: ErrorGroup[] | null;
  preview: PlanOut[] | null;
  reloadErrors: () => void;
}

const TITLES: Record<WidgetId, string> = {
  integrations: "Connections",
  stat_plex_users: "Plex users",
  stat_linked: "Linked users",
  stat_libraries: "Libraries",
  stat_roles: "Discord roles",
  stat_requests: "Open requests",
  stat_invites: "Pending invites",
  stat_invites_accepted: "Invites accepted this week",
  errors: "Alerts",
  last_sync: "Last sync",
  pending: "Pending changes",
  links: "Link coverage",
  access_chart: "Access changes (14 days)",
  activity: "Recent activity",
  libraries: "Library access",
  roles: "Discord role members",
  open_requests: "Open request list",
  role_libraries: "Roles that grant libraries",
  stat_emoji: "Custom emoji",
  stat_emoji_animated: "Animated emoji",
  requests: "Requests",
  invites: "Plex invites",
  setup: "Setup checklist",
  security: "Security",
};
const isSpacer = (id: WidgetId) => id.startsWith("spacer-");
/** A widget's name; blank widgets are numbered so their controls can be told apart. */
const titleOf = (id: WidgetId) => (isSpacer(id) ? `Blank space ${id.slice(7)}` : TITLES[id] ?? id);
const SIZE_LABEL: Record<Size, string> = { xs: "Eighth width", s: "Quarter width", m: "Half width", l: "Full width" };
const SIZES: Size[] = ["xs", "s", "m", "l"];

// Widgets whose data can also be drawn as a circle or bars; the first entry names the usual form.
const VIEWS: Partial<Record<WidgetId, string>> = {
  stat_plex_users: "Number", stat_linked: "Number", stat_libraries: "Number", stat_roles: "Number",
  stat_requests: "Number", errors: "List", links: "Stacked bar", role_libraries: "List",
  stat_emoji: "Number", stat_emoji_animated: "Number",
  access_chart: "Columns", activity: "List", libraries: "List", roles: "List", requests: "List", security: "Numbers",
};
const VIEW_LABEL: Record<Exclude<View, "default">, string> = { donut: "Circle", bar: "Bars" };

export default function DashboardPage() {
  const toast = useToast();
  const status = useLoad(() => api.get<StatusOut>("/api/v1/status"));
  const preview = useLoad(() => api.get<PlanOut[]>("/api/v1/sync/preview"));
  const insights = useLoad(() => api.get<Insights>("/api/v1/dashboard/insights"));
  const errors = useLoad(() => api.get<ErrorGroup[]>("/api/v1/errors"));
  const layout = useLoad(() => api.get<Layout>("/api/v1/dashboard/layout"));
  const [editing, setEditing] = useState<Widget[] | null>(null);

  useEffect(() => {
    const fast = setInterval(() => { void status.reload(); void errors.reload(); }, 10000);
    const slow = setInterval(() => { void insights.reload(); void preview.reload(); }, 30000);
    return () => { clearInterval(fast); clearInterval(slow); };
  }, [status.reload, errors.reload, insights.reload, preview.reload]);

  const syncNow = async () => {
    try {
      await api.post("/api/v1/sync", {});
      toast.ok("Full sync queued.");
      setTimeout(() => { void status.reload(); void preview.reload(); void insights.reload(); }, 2500);
    } catch (e) {
      toast.error(e);
    }
  };
  const saveLayout = async () => {
    try {
      layout.setData(await api.put<Layout>("/api/v1/dashboard/layout", { widgets: editing }));
      setEditing(null);
      toast.ok("Dashboard saved.");
    } catch (e) {
      toast.error(e);
    }
  };
  const resetLayout = async () => {
    try {
      const fresh = await api.del<Layout>("/api/v1/dashboard/layout");
      layout.setData(fresh);
      setEditing(fresh.widgets);
      toast.ok("Back to the default dashboard.");
    } catch (e) {
      toast.error(e);
    }
  };

  if (status.error) return <ErrorBanner error={status.error} />;
  if (!status.data || !layout.data) return <Spinner />;
  const data = status.data;
  const ctx: Ctx = { status: data, insights: insights.data, errors: errors.data, preview: preview.data,
    reloadErrors: () => { void errors.reload(); void status.reload(); } };
  const widgets = editing ?? layout.data.widgets;
  const shown = widgets.filter((w) => !w.hidden);
  const hidden = widgets.filter((w) => w.hidden);
  const update = (id: WidgetId, patch: Partial<Widget>) => setEditing(widgets.map((w) => (w.id === id ? { ...w, ...patch } : w)));
  const move = (id: WidgetId, delta: number) => {
    const order = [...shown];
    const i = order.findIndex((w) => w.id === id);
    const j = i + delta;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    setEditing([...order, ...hidden]);
  };

  return (
    <>
      <PageHead title="Dashboard" sub={`WooSarr ${data.version}`}>
        {editing ? (
          <>
            <button className="btn" onClick={() => void resetLayout()}>Reset to default</button>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn primary" onClick={() => void saveLayout()}><Icon name="check" /> Save layout</button>
          </>
        ) : (
          <>
            <button className="btn" onClick={() => setEditing(layout.data!.widgets)}><Icon name="settings" /> Customize</button>
            <button className="btn primary" onClick={() => void syncNow()} disabled={!data.plex.connected}>
              <Icon name="refresh" /> Sync now
            </button>
          </>
        )}
      </PageHead>

      {data.dry_run ? (
        <div className="banner warn" role="status">
          <Icon name="eye" />
          <div>
            <strong>Dry-run is on.</strong> WooSarr logs and audits what it <em>would</em> change but doesn't touch Plex.
            Turn it off in <Link to="/settings">Settings</Link> once the planned changes look right.
          </div>
        </div>
      ) : null}
      {data.recent_errors > 0 ? (
        <div className="banner danger" role="alert">
          <Icon name="alert" />
          <div>
            {data.open_errors} unresolved error{data.open_errors === 1 ? "" : "s"}
            {data.recent_errors > data.open_errors ? ` (${data.recent_errors} times)` : ""}: see Alerts below to resolve.{" "}
            <Link to="/logs?level=ERROR">View logs</Link>
          </div>
        </div>
      ) : null}

      {editing ? (
        <p className="small muted">Move, resize or hide widgets, then <strong>Save layout</strong>. Hidden widgets and <strong>Blank space</strong> (for spacing) are under <a href="#hidden-widgets">More widgets</a> at the bottom. The layout is saved for your admin account.</p>
      ) : null}

      <div className="dash-grid">
        {shown.map((w, i) => (
          <section key={w.id} className={`${isSpacer(w.id) ? "dash-spacer" : "card"} dash-w w-${w.size ?? "m"}${editing ? " editing" : ""}`}
            aria-labelledby={isSpacer(w.id) ? undefined : `w-${w.id}`} aria-label={isSpacer(w.id) && editing ? titleOf(w.id) : undefined}
            aria-hidden={isSpacer(w.id) && !editing ? true : undefined}>
            {editing ? (
              <div className="row dash-tools">
                <strong className="small">{titleOf(w.id)}</strong>
                <span style={{ flex: 1 }} />
                <button className="btn ghost sm icon-btn" aria-label={`Move ${titleOf(w.id)} earlier`} disabled={i === 0} onClick={() => move(w.id, -1)}>↑</button>
                <button className="btn ghost sm icon-btn" aria-label={`Move ${titleOf(w.id)} later`} disabled={i === shown.length - 1} onClick={() => move(w.id, 1)}>↓</button>
                <select className="inline-input" aria-label={`Size of ${titleOf(w.id)}`} value={w.size ?? "m"} onChange={(e) => update(w.id, { size: e.target.value as Size })}>
                  {SIZES.map((s) => <option key={s} value={s}>{SIZE_LABEL[s]}</option>)}
                </select>
                {VIEWS[w.id] ? (
                  <select className="inline-input" aria-label={`Chart for ${titleOf(w.id)}`} value={w.view ?? "default"} onChange={(e) => update(w.id, { view: e.target.value as View })}>
                    <option value="default">{VIEWS[w.id]}</option>
                    <option value="donut">{VIEW_LABEL.donut}</option>
                    <option value="bar">{VIEW_LABEL.bar}</option>
                  </select>
                ) : null}
                {isSpacer(w.id)
                  ? <button className="btn ghost sm" onClick={() => setEditing(widgets.filter((x) => x.id !== w.id))}>Remove</button>
                  : <button className="btn ghost sm" onClick={() => update(w.id, { hidden: true })}>Hide</button>}
              </div>
            ) : null}
            {isSpacer(w.id) ? null : <WidgetBody id={w.id} ctx={ctx} view={w.view ?? "default"} />}
          </section>
        ))}
      </div>

      {editing ? (
        <section className="card" style={{ marginTop: 16 }} aria-labelledby="hidden-widgets">
          <h2 id="hidden-widgets">More widgets</h2>
          <ul className="dash-hidden">
            <li className="row spread">
              <span>Blank space <span className="small muted">— empty, for spacing (add as many as you like)</span></span>
              <button className="btn sm" onClick={() => setEditing([...shown, { id: nextSpacer(widgets), size: "s", view: "default", hidden: false }, ...hidden])}>
                <Icon name="plus" size={14} /> Add
              </button>
            </li>
            {hidden.map((w) => (
              <li key={w.id} className="row spread">
                <span>{titleOf(w.id)}</span>
                <button className="btn sm" onClick={() => setEditing([...shown, { ...w, hidden: false }, ...hidden.filter((h) => h.id !== w.id)])}>
                  <Icon name="plus" size={14} /> Add
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}

/** "Next Sync in 12 minutes" (the periodic full sync), or what the sync is doing right now. */
function nextSync(status: StatusOut): string {
  const s = status.sync as { running?: boolean; full_reconcile_pending?: boolean; plex_configured?: boolean; next_full_reconcile_in_seconds?: number };
  if (s.plex_configured === false) return "";
  if (s.running) return "Syncing now";
  if (s.full_reconcile_pending) return "Sync queued";
  const minutes = Math.ceil(Number(s.next_full_reconcile_in_seconds ?? 0) / 60);
  return minutes <= 1 ? "Next Sync in 1 minute" : `Next Sync in ${minutes} minutes`;
}

/** Used and total slots for static (stat_emoji) or animated emoji; null until the bot is connected. */
function emojiSlots(ins: Insights | null, id: WidgetId): { used: number; limit: number } | null {
  const e = ins?.emoji;
  if (!e) return null;
  return { used: id === "stat_emoji" ? e.static : e.animated, limit: e.limit };
}

function nextSpacer(widgets: Widget[]): WidgetId {
  const used = new Set(widgets.map((w) => w.id));
  let n = 1;
  while (used.has(`spacer-${n}`)) n++;
  return `spacer-${n}`;
}

function Title({ id, children, suffix }: { id: WidgetId; children?: ReactNode; suffix?: string }) {
  return (
    <div className="row spread" style={{ marginBottom: 8 }}>
      <h2 id={`w-${id}`} className={suffix ? "fit-title" : undefined} style={{ margin: 0 }}>{titleOf(id)}{suffix ? ` - ${suffix}` : ""}</h2>
      {children}
    </div>
  );
}

function WidgetBody({ id, ctx, view }: { id: WidgetId; ctx: Ctx; view: View }) {
  const c = ctx.status.counts;
  const ins = ctx.insights;
  if (view !== "default" && VIEWS[id]) {
    const data = seriesFor(id, ctx);
    return (
      <>
        <Title id={id} />
        {data ? <Chart view={view} {...data} />
          : ins ? <p className="small muted">{id.startsWith("stat_emoji") ? "Discord bot not connected." : "Nothing to show yet."}</p>
          : <Spinner />}
      </>
    );
  }
  switch (id) {
    case "integrations":
      return (
        <>
          <Title id={id} suffix={nextSync(ctx.status)} />
          <div className="grid cols-2">
            <Integration name="Plex" icon="plex" s={ctx.status.plex} />
            <Integration name="Discord" icon="discord" s={ctx.status.discord} />
            {ins ? (
              <div className="card flat">
                <div className="row spread">
                  <span className="row"><Icon name="download" /> <strong>Seerr</strong></span>
                  {ins.requests.configured ? <Badge tone="ok">Connected</Badge> : <Badge tone="neutral">Optional</Badge>}
                </div>
                {!ins.requests.configured ? <Link to="/setup" className="small">Set up requests →</Link> : null}
              </div>
            ) : null}
          </div>
        </>
      );
    case "stat_plex_users":
      return <Stat id={id} value={c.plex_users} to="/users" />;
    case "stat_emoji":
    case "stat_emoji_animated": {
      const e = emojiSlots(ins, id);
      return <Stat id={id} value={e ? `${e.used}/${e.limit}` : null} to="/emoji"
        sub={!ins ? undefined : e ? `${e.limit - e.used} free` : "Discord bot not connected"} />;
    }
    case "stat_linked":
      return <Stat id={id} value={c.linked_users} sub={`${c.verified_links} verified`} to="/users" />;
    case "stat_libraries":
      return <Stat id={id} value={c.libraries} sub={`${c.exempt_libraries} exempt`} to="/libraries" />;
    case "stat_roles":
      return <Stat id={id} value={c.roles} sub={`${c.discord_members} members`} to="/roles" />;
    case "stat_requests": {
      const by = (ins?.requests.by_status ?? {}) as Record<string, number>;
      const open = (by.pending ?? 0) + (by.approved ?? 0) + (by.partially_available ?? 0);
      return <Stat id={id} value={ins ? open : null} sub={ins ? `${ins.requests.last_7d as number} this week` : undefined} to="/requests" />;
    }
    case "stat_invites":
      return <Stat id={id} value={ins ? (ins.invites.pending as number) : null} to="/users" />;
    case "stat_invites_accepted":
      return <Stat id={id} value={ins ? (ins.invites.accepted_7d as number) : null} to="/users" />;
    case "errors":
      return <ErrorsWidget ctx={ctx} />;
    case "last_sync":
      return <LastSync status={ctx.status} />;
    case "pending":
      return <Pending preview={ctx.preview} />;
    case "links":
      return <Links ins={ins} />;
    case "access_chart":
      return <AccessChart ins={ins} />;
    case "activity":
      return <Activity ins={ins} />;
    case "libraries":
      return <Libraries ins={ins} />;
    case "roles":
      return <Roles ins={ins} />;
    case "requests":
      return <Requests ins={ins} />;
    case "open_requests":
      return <OpenRequests ins={ins} />;
    case "role_libraries":
      return <RoleLibraries ins={ins} />;
    case "invites":
      return <Invites ins={ins} />;
    case "setup":
      return <SetupChecklist ins={ins} status={ctx.status} />;
    case "security":
      return <Security ins={ins} />;
  }
}

function Integration({ name, icon, s }: { name: string; icon: "plex" | "discord"; s: StatusOut["plex"] }) {
  return (
    <div className="card flat">
      <div className="row spread">
        <span className="row"><Icon name={icon} /> <strong>{name}</strong></span>
        {s.connected ? <Badge tone="ok">Connected</Badge>
          : s.connecting ? <Badge tone="info" icon="refresh">Connecting…</Badge>
          : s.configured ? <Badge tone="danger">Disconnected</Badge> : <Badge tone="neutral">Not set up</Badge>}
      </div>
      {s.detail && !s.connecting ? <p className="small muted" style={{ marginTop: 8 }}>{s.detail}</p> : null}
      {!s.configured ? <Link to="/setup" className="small">Set up {name} →</Link> : null}
    </div>
  );
}

function Stat({ id, value, sub, to }: { id: WidgetId; value: number | string | null; sub?: string; to: string }) {
  return (
    <Link to={to} className="stat-link">
      <div className="stat-label" id={`w-${id}`}>{titleOf(id)}</div>
      <div className="stat-value">{value ?? "–"}</div>
      {sub ? <div className="small muted">{sub}</div> : null}
    </Link>
  );
}

function Loading({ ins }: { ins: Insights | null }) {
  return ins ? null : <Spinner />;
}

// Account-takeover alerts come from the audit log, not the app log.
const ACCOUNT_LOGGER = "woosarr.accounts";

function ErrorsWidget({ ctx }: { ctx: Ctx }) {
  const toast = useToast();
  const [showResolved, setShowResolved] = useState(false);
  const groups = ctx.errors ?? [];
  const open = groups.filter((g) => g.count > 0);
  const resolved = groups.filter((g) => g.count === 0 && g.resolved_until);
  const act = async (g: ErrorGroup, resolve: boolean) => {
    try {
      if (resolve) await api.put(`/api/v1/errors/${g.fingerprint}/resolved`);
      else await api.del(`/api/v1/errors/${g.fingerprint}/resolved`);
      toast.ok(resolve ? "Marked resolved." : "Shown again.");
      ctx.reloadErrors();
    } catch (e) {
      toast.error(e);
    }
  };
  return (
    <>
      <Title id="errors"><Link to="/logs?level=WARNING" className="small">All logs →</Link></Title>
      {!ctx.errors ? <Spinner /> : open.length === 0 ? (
        <p className="dash-ok small"><Icon name="check" size={16} /> No open alerts{resolved.length ? " (apart from resolved ones)" : ""}.</p>
      ) : (
        <ul className="dash-list">
          {open.slice(0, 8).map((g) => (
            <li key={g.fingerprint}>
              <div className="row" style={{ alignItems: "flex-start" }}>
                <Badge tone={g.level === "ERROR" ? "danger" : "warn"}>{g.logger === ACCOUNT_LOGGER ? "Account" : g.level === "ERROR" ? "Error" : "Warning"}</Badge>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="dash-msg">{g.message}</div>
                  <div className="small muted">
                    {g.count}× · last {ago(g.last_ts)} · {g.logger === ACCOUNT_LOGGER ? <Link to="/logs?tab=audit">audit log</Link> : <code>{g.logger}</code>}{g.exc ? <> · {g.exc}</> : null}
                  </div>
                </div>
                <button className="btn sm" onClick={() => void act(g, true)} aria-label={`Mark resolved: ${g.message.slice(0, 80)}`}>Resolve</button>
              </div>
            </li>
          ))}
          {open.length > 8 ? <li className="muted small">…and {open.length - 8} more on the <Link to="/logs?level=WARNING">Logs</Link> page</li> : null}
        </ul>
      )}
      {resolved.length ? (
        <div style={{ marginTop: 8 }}>
          <button className="btn ghost sm" aria-expanded={showResolved} onClick={() => setShowResolved(!showResolved)}>
            {showResolved ? "Hide" : "Show"} {resolved.length} resolved
          </button>
          {showResolved ? (
            <ul className="dash-list">
              {resolved.map((g) => (
                <li key={g.fingerprint} className="row">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="dash-msg muted">{g.message}</div>
                    <div className="small muted">Resolved by {g.resolved_by?.replace(/^admin:/, "")} on {new Date(g.resolved_until!).toLocaleString()}</div>
                  </div>
                  <button className="btn ghost sm" onClick={() => void act(g, false)}>Unresolve</button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function LastSync({ status }: { status: StatusOut }) {
  const last = status.sync.last_run as LastRun | null;
  return (
    <>
      <Title id="last_sync" />
      {!last ? (
        <p className="muted">No sync has run yet{status.plex.connected ? "; the first one starts shortly after startup." : "."}</p>
      ) : (
        <div className="stack small">
          <div className="row">
            {last.skipped ? <Badge tone="neutral">Skipped</Badge> : last.errors ? <Badge tone="danger">{last.errors} errors</Badge> : <Badge tone="ok">OK</Badge>}
            {last.dry_run ? <Badge tone="warn" icon="eye">Dry run</Badge> : null}
            <span className="muted">{ago(last.finished_at ?? last.started_at)} · {last.full ? "full" : "partial"} · {last.reason}</span>
          </div>
          {last.skipped ? <p>{last.skipped}</p> : (
            <p>
              Checked {last.users_checked} linked users, {last.users_changed} needed changes, {last.writes} written
              {last.skipped_drift ? `, ${last.skipped_drift} skipped (changed in Plex meanwhile)` : ""}.
            </p>
          )}
          {last.error_messages.slice(0, 3).map((m) => <code key={m} className="small" style={{ display: "block" }}>{m}</code>)}
        </div>
      )}
      <p className="small muted" style={{ marginTop: 8 }}>
        <Icon name="clock" size={14} /> Next full reconcile in {Math.round(Number(status.sync.next_full_reconcile_in_seconds) / 60)} min
        {status.sync.running ? " · running now" : ""}
      </p>
    </>
  );
}

function Pending({ preview }: { preview: PlanOut[] | null }) {
  return (
    <>
      <Title id="pending" />
      {!preview ? <Spinner /> : preview.length === 0 ? (
        <Empty icon="check" title="Nothing to change">Every linked user already has the access their roles give.</Empty>
      ) : (
        <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
          {preview.slice(0, 8).map((p) => (
            <li key={p.plex_user_id}>
              <strong>{p.username}</strong>
              {p.add.length ? <> gains {p.add.length}</> : null}
              {p.add.length && p.remove.length ? "," : null}
              {p.remove.length ? <> loses {p.remove.length}</> : null}
            </li>
          ))}
          {preview.length > 8 ? <li className="muted">…and {preview.length - 8} more</li> : null}
        </ul>
      )}
    </>
  );
}

function Bar({ parts, label }: { parts: [number, string, string][]; label: string }) {
  const total = parts.reduce((a, [n]) => a + n, 0) || 1;
  return (
    <div className="dash-bar" role="img" aria-label={label}>
      {parts.map(([n, color, name]) => n > 0 ? <span key={name} style={{ width: `${(n / total) * 100}%`, background: color }} title={`${name}: ${n}`} /> : null)}
    </div>
  );
}

function Links({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="links" /><Loading ins={ins} /></>;
  const l = ins.links;
  const pct = l.plex_users ? Math.round((l.linked / l.plex_users) * 100) : 0;
  return (
    <>
      <Title id="links"><span className="small muted">{pct}% linked</span></Title>
      <Bar label={`${l.verified} verified, ${l.unverified} unverified, ${l.unlinked} unlinked`}
        parts={[[l.verified, "var(--ok)", "Verified"], [l.unverified, "var(--warn)", "Unverified"], [l.unlinked, "var(--border)", "Unlinked"]]} />
      <ul className="dash-legend small">
        <li><span className="dot" style={{ background: "var(--ok)" }} /> <Link to="/users">{l.verified} verified</Link></li>
        <li><span className="dot" style={{ background: "var(--warn)" }} /> {l.unverified} unverified</li>
        <li><span className="dot" style={{ background: "var(--border)" }} /> {l.unlinked} Plex users not linked</li>
      </ul>
      <p className="small muted" style={{ margin: "8px 0 0" }}>
        {l.members_unlinked} of {l.members} Discord members haven't linked a Plex account (they can run <code>/link</code>).
      </p>
    </>
  );
}

function AccessChart({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="access_chart" /><Loading ins={ins} /></>;
  const days = ins.access_by_day as { date: string; changed: number; planned: number; users: number }[];
  const max = Math.max(1, ...days.map((d) => d.changed + d.planned));
  const total = days.reduce((a, d) => a + d.changed, 0);
  const planned = days.reduce((a, d) => a + d.planned, 0);
  const w = 280, h = 90, bw = w / days.length;
  return (
    <>
      <Title id="access_chart"><span className="small muted">{total} applied{planned ? `, ${planned} planned (dry-run)` : ""}</span></Title>
      <svg viewBox={`0 0 ${w} ${h + 14}`} className="dash-chart" role="img" aria-label={`Access changes per day over 14 days: ${total} applied, ${planned} planned`}>
        {days.map((d, i) => {
          const hc = (d.changed / max) * h;
          const hp = (d.planned / max) * h;
          return (
            <g key={d.date}>
              <title>{`${d.date}: ${d.changed} applied, ${d.planned} planned, ${d.users} users`}</title>
              <rect x={i * bw + 2} y={h - hc} width={bw - 4} height={hc} fill="var(--accent)" rx="2" />
              <rect x={i * bw + 2} y={h - hc - hp} width={bw - 4} height={hp} fill="var(--warn)" opacity="0.7" rx="2" />
            </g>
          );
        })}
        <text x="0" y={h + 12} className="dash-axis">{days[0]?.date.slice(5)}</text>
        <text x={w} y={h + 12} textAnchor="end" className="dash-axis">today</text>
      </svg>
      <ul className="dash-legend small">
        <li><span className="dot" style={{ background: "var(--accent)" }} /> applied</li>
        <li><span className="dot" style={{ background: "var(--warn)" }} /> planned in dry-run</li>
      </ul>
    </>
  );
}

const ACTION_TEXT: Record<string, string> = {
  link_self: "linked their Plex account", link_admin: "linked a user", unlink_self: "unlinked themselves",
  unlink_admin: "unlinked a user", invite_sent: "sent a Plex invite", invite_accepted: "invite accepted",
  role_mapping_changed: "changed role libraries", settings_changed: "changed settings", library_exempt: "changed an exemption",
  request_created: "requested a title", reaction_added: "reacted on a post", reaction_removed: "removed a reaction",
  login_failed: "failed to sign in", error_resolved: "resolved an error", discord_avatar_set: "changed the bot avatar",
  reaction_post_published: "published a reaction post", seerr_connected: "connected Seerr",
};

function actor(a: string): string {
  if (a === "system") return "WooSarr";
  if (a.startsWith("admin:")) return a.slice(6);
  if (a.startsWith("discord:")) return "A member";
  if (a.startsWith("ip:")) return a.slice(3);
  return a;
}

function Activity({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="activity" /><Loading ins={ins} /></>;
  const rows = ins.activity as { ts: string; actor: string; action: string; target_id: string | null }[];
  return (
    <>
      <Title id="activity"><Link to="/logs?tab=audit" className="small">Audit trail →</Link></Title>
      {rows.length === 0 ? <p className="muted small">Nothing yet.</p> : (
        <ul className="dash-list small">
          {rows.map((r, i) => (
            <li key={i} className="row spread">
              <span><strong>{actor(r.actor)}</strong> {ACTION_TEXT[r.action] ?? r.action.replace(/_/g, " ")}</span>
              <span className="muted">{ago(r.ts)}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Libraries({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="libraries" /><Loading ins={ins} /></>;
  const libs = ins.libraries as { library_id: string; title: string; users: number; exempt: boolean; managed: boolean }[];
  const max = Math.max(1, ...libs.map((l) => l.users));
  return (
    <>
      <Title id="libraries"><Link to="/libraries" className="small">Libraries →</Link></Title>
      {libs.length === 0 ? <p className="muted small">No libraries yet.</p> : (
        <ul className="dash-list small">
          {libs.slice(0, 8).map((l) => (
            <li key={l.library_id}>
              <div className="row spread">
                <span>{l.title} {l.exempt ? <Badge tone="info">Exempt</Badge> : !l.managed ? <Badge tone="neutral">No role</Badge> : null}</span>
                <span className="muted">{l.users} user{l.users === 1 ? "" : "s"}</span>
              </div>
              <div className="dash-bar thin" aria-hidden="true"><span style={{ width: `${(l.users / max) * 100}%`, background: "var(--accent)" }} /></div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

type RoleRow = { role_id: string; name: string; color: number; members: number; linked: number; libraries: number };

function Roles({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="roles" /><Loading ins={ins} /></>;
  const roles = ins.roles as RoleRow[];
  const max = Math.max(1, ...roles.map((r) => r.members));
  return (
    <>
      <Title id="roles"><Link to="/roles" className="small">Roles →</Link></Title>
      {roles.length === 0 ? <p className="muted small">No Discord roles yet.</p> : (
        <ul className="dash-list small">
          {roles.slice(0, 8).map((r) => (
            <li key={r.role_id}>
              <div className="row spread">
                <span className="row" style={{ gap: 6 }}>
                  <span className="role-dot" aria-hidden="true" style={r.color ? { background: `#${r.color.toString(16).padStart(6, "0")}` } : undefined} />
                  {r.name} {r.libraries ? null : <Badge tone="neutral">No libraries</Badge>}
                </span>
                <span className="muted">{r.members} member{r.members === 1 ? "" : "s"} · {r.linked} linked</span>
              </div>
              <div className="dash-bar thin" aria-hidden="true"><span style={{ width: `${(r.members / max) * 100}%`, background: "var(--accent)" }} /></div>
            </li>
          ))}
          {roles.length > 8 ? <li className="muted small">…and {roles.length - 8} more on the <Link to="/roles">Roles</Link> page</li> : null}
        </ul>
      )}
    </>
  );
}

type RoleLibRow = { role_id: string; name: string; color: number; members: number; linked: number; libraries: string[] };

/** Like Discord role members, for the roles that grant libraries (with the libraries' names). */
function RoleLibraries({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="role_libraries" /><Loading ins={ins} /></>;
  const roles = ins.role_libraries as RoleLibRow[];
  const max = Math.max(1, ...roles.map((r) => r.members));
  return (
    <>
      <Title id="role_libraries"><Link to="/roles" className="small">Roles →</Link></Title>
      {roles.length === 0 ? <p className="muted small">No role grants libraries yet. Map roles to libraries on the <Link to="/roles">Roles</Link> page.</p> : (
        <ul className="dash-list small">
          {roles.slice(0, 8).map((r) => (
            <li key={r.role_id}>
              <div className="row spread">
                <span className="row" style={{ gap: 6 }}>
                  <span className="role-dot" aria-hidden="true" style={r.color ? { background: `#${r.color.toString(16).padStart(6, "0")}` } : undefined} />
                  {r.name}
                </span>
                <span className="muted">{r.members} member{r.members === 1 ? "" : "s"} · {r.linked} linked</span>
              </div>
              <div className="muted">{r.libraries.join(", ")}</div>
              <div className="dash-bar thin" aria-hidden="true"><span style={{ width: `${(r.members / max) * 100}%`, background: "var(--accent)" }} /></div>
            </li>
          ))}
          {roles.length > 8 ? <li className="muted small">…and {roles.length - 8} more on the <Link to="/roles">Roles</Link> page</li> : null}
        </ul>
      )}
    </>
  );
}

type RequestRow = { title: string; year: string; media_type: string; status: string; created_at: string; plex_username: string | null; discord_name: string | null };

function RequestItem({ x }: { x: RequestRow }) {
  return (
    <li className="row spread" style={{ alignItems: "flex-start" }}>
      <span>
        {x.media_type === "movie" ? "🍿" : "📺"} {x.title}{x.year ? ` (${x.year})` : ""}
        {x.plex_username || x.discord_name ? (
          <span className="muted" style={{ display: "block" }}>
            by {x.plex_username ?? x.discord_name}{x.plex_username ? "" : " (Discord)"}
          </span>
        ) : null}
      </span>
      <span className="muted">{(REQ_STATE[x.status] ?? [, x.status])[1]} · {ago(x.created_at)}</span>
    </li>
  );
}

function OpenRequests({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="open_requests" /><Loading ins={ins} /></>;
  const r = ins.requests as { configured: boolean; open: RequestRow[]; open_count: number };
  return (
    <>
      <Title id="open_requests"><Link to="/requests" className="small">Requests →</Link></Title>
      {!r.configured && r.open_count === 0 ? (
        <p className="small muted">Connect Seerr on the <Link to="/setup">Integrations</Link> page so members can request movies and shows from Discord.</p>
      ) : r.open_count === 0 ? <p className="small muted">No open requests: everything requested is available, declined or done.</p> : (
        <ul className="dash-list small">
          {r.open.map((x, i) => <RequestItem key={i} x={x} />)}
          {r.open_count > r.open.length ? <li className="muted small">…and {r.open_count - r.open.length} more on the <Link to="/requests">Requests</Link> page</li> : null}
        </ul>
      )}
    </>
  );
}

const REQ_STATE: Record<string, [Tone, string]> = {
  pending: ["warn", "Waiting"], approved: ["info", "Approved"], declined: ["danger", "Declined"], failed: ["danger", "Failed"],
  partially_available: ["accent", "Partly available"], available: ["ok", "Available"], deleted: ["neutral", "Removed"],
};

function Requests({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="requests" /><Loading ins={ins} /></>;
  const r = ins.requests as { configured: boolean; by_status: Record<string, number>; last_7d: number; recent: RequestRow[] };
  return (
    <>
      <Title id="requests"><Link to="/requests" className="small">Requests →</Link></Title>
      {!r.configured && r.recent.length === 0 ? (
        <p className="small muted">Connect Seerr on the <Link to="/setup">Integrations</Link> page so members can request movies and shows from Discord.</p>
      ) : (
        <>
          <div className="row" style={{ flexWrap: "wrap", marginBottom: 8 }}>
            {Object.entries(r.by_status).map(([s, n]) => <Badge key={s} tone={(REQ_STATE[s] ?? ["neutral"])[0]}>{(REQ_STATE[s] ?? [, s])[1]}: {n}</Badge>)}
            <span className="small muted">{r.last_7d} this week</span>
          </div>
          <ul className="dash-list small">
            {r.recent.map((x, i) => <RequestItem key={i} x={x} />)}
          </ul>
        </>
      )}
    </>
  );
}

function Invites({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="invites" /><Loading ins={ins} /></>;
  const v = ins.invites as { pending: number; accepted_7d: number; recent: { identifier: string; created_at: string }[] };
  return (
    <>
      <Title id="invites"><Link to="/users" className="small">Users →</Link></Title>
      <p className="small" style={{ margin: "0 0 8px" }}><strong>{v.pending}</strong> pending · {v.accepted_7d} accepted this week</p>
      {v.recent.length ? (
        <ul className="dash-list small">
          {v.recent.map((i) => <li key={i.identifier + i.created_at} className="row spread"><span>{i.identifier}</span><span className="muted">{ago(i.created_at)}</span></li>)}
        </ul>
      ) : <p className="small muted">No pending invites.</p>}
    </>
  );
}

function Check({ ok, children, to }: { ok: boolean; children: ReactNode; to?: string }) {
  const icon: IconName = ok ? "check" : "alert";
  return (
    <li className="row">
      <span style={{ color: ok ? "var(--ok)" : "var(--warn)" }}><Icon name={icon} size={16} /></span>
      <span>{children} {to && !ok ? <Link to={to}>Fix →</Link> : null}</span>
    </li>
  );
}

function SetupChecklist({ ins, status }: { ins: Insights | null; status: StatusOut }) {
  if (!ins) return <><Title id="setup" /><Loading ins={ins} /></>;
  const s = ins.setup as { unmanaged_libraries: number; exempt_libraries: number; roles_mapped: number; roles_unmapped: number };
  return (
    <>
      <Title id="setup" />
      <ul className="dash-list small">
        <Check ok={status.plex.connected} to="/setup">Plex connected</Check>
        <Check ok={status.discord.connected} to="/setup">Discord bot connected</Check>
        <Check ok={s.roles_mapped > 0} to="/roles">{s.roles_mapped} role{s.roles_mapped === 1 ? "" : "s"} give libraries ({s.roles_unmapped} without)</Check>
        <Check ok={s.unmanaged_libraries === 0} to="/roles">{s.unmanaged_libraries === 0 ? "Every library is managed by a role or exempt" : `${s.unmanaged_libraries} librar${s.unmanaged_libraries === 1 ? "y has" : "ies have"} no role (left as shared in Plex)`}</Check>
        <Check ok={!status.dry_run} to="/settings">{status.dry_run ? "Dry-run is on (nothing is written to Plex)" : "Live: changes are written to Plex"}</Check>
      </ul>
    </>
  );
}

function Security({ ins }: { ins: Insights | null }) {
  if (!ins) return <><Title id="security" /><Loading ins={ins} /></>;
  const s = ins.security;
  return (
    <>
      <Title id="security"><Link to="/settings?tab=admins" className="small">Admins →</Link></Title>
      <div className="grid cols-3-tight">
        <div><div className="stat-value" style={{ color: s.failed_logins_24h ? "var(--warn)" : undefined }}>{s.failed_logins_24h}</div><div className="small muted">failed sign-ins (24 h)</div></div>
        <div><div className="stat-value">{s.sessions}</div><div className="small muted">active sessions</div></div>
        <div><div className="stat-value">{s.api_keys}</div><div className="small muted"><Link to="/settings?tab=keys">API keys</Link></div></div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- charts

type Seg = { label: string; value: number; color: string };
interface Series { segs: Seg[]; label: string; center?: string; unit?: string }

const C = {
  ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)", info: "var(--info)", accent: "var(--accent)",
  muted: "var(--border)", strong: "var(--accent-strong)",
};
const CYCLE = [C.accent, C.info, C.ok, C.warn, C.danger, C.strong, "var(--text-muted)", C.muted];
const REQ_COLOR: Record<string, string> = {
  pending: C.warn, approved: C.info, declined: C.danger, failed: C.danger, partially_available: C.accent,
  available: C.ok, deleted: C.muted,
};

/** The numbers behind a widget, for its circle / bar views (null while loading). */
function seriesFor(id: WidgetId, ctx: Ctx): Series | null {
  const ins = ctx.insights;
  const c = ctx.status.counts;
  const need = (x: unknown) => x === null || x === undefined;
  switch (id) {
    case "stat_plex_users":
    case "links": {
      if (need(ins)) return null;
      const l = ins!.links;
      return { label: "Plex users by link status", center: String(l.plex_users), unit: "users",
        segs: [{ label: "Verified", value: l.verified, color: C.ok }, { label: "Unverified", value: l.unverified, color: C.warn },
          { label: "Not linked", value: l.unlinked, color: C.muted }] };
    }
    case "stat_emoji":
    case "stat_emoji_animated": {
      const e = emojiSlots(ins, id);
      if (!e) return null;
      return { label: id === "stat_emoji" ? "Custom emoji slots" : "Animated emoji slots", center: `${e.used}/${e.limit}`,
        segs: [{ label: "Used", value: e.used, color: C.accent }, { label: "Free", value: Math.max(0, e.limit - e.used), color: C.muted }] };
    }
    case "stat_linked": {
      if (need(ins)) return null;
      const l = ins!.links;
      return { label: "Linked Plex users", center: `${l.plex_users ? Math.round((l.linked / l.plex_users) * 100) : 0}%`,
        segs: [{ label: "Linked", value: l.linked, color: C.accent }, { label: "Not linked", value: l.unlinked, color: C.muted }] };
    }
    case "stat_libraries": {
      if (need(ins)) return null;
      const libs = ins!.libraries as { exempt: boolean; managed: boolean }[];
      return { label: "Libraries by how they're managed", center: String(c.libraries), unit: "libraries",
        segs: [{ label: "Managed by roles", value: libs.filter((l) => l.managed && !l.exempt).length, color: C.accent },
          { label: "Exempt", value: libs.filter((l) => l.exempt).length, color: C.info },
          { label: "No role", value: libs.filter((l) => !l.managed && !l.exempt).length, color: C.muted }] };
    }
    case "stat_roles": {
      if (need(ins)) return null;
      const s = ins!.setup as { roles_mapped: number; roles_unmapped: number };
      return { label: "Discord roles that give libraries", center: String(c.roles), unit: "roles",
        segs: [{ label: "Give libraries", value: s.roles_mapped, color: C.accent }, { label: "Give none", value: s.roles_unmapped, color: C.muted }] };
    }
    case "stat_requests":
    case "requests": {
      if (need(ins)) return null;
      const by = ins!.requests.by_status as Record<string, number>;
      const total = Object.values(by).reduce((a, n) => a + n, 0);
      return { label: "Requests by status", center: String(total), unit: "requests",
        segs: Object.entries(by).map(([k, n]) => ({ label: (REQ_STATE[k] ?? [, k])[1] as string, value: n, color: REQ_COLOR[k] ?? C.muted })) };
    }
    case "errors": {
      if (need(ctx.errors)) return null;
      const g = ctx.errors!;
      const sum = (f: (x: ErrorGroup) => boolean, k: "count" | "hidden") => g.filter(f).reduce((a, x) => a + x[k], 0);
      const errors = sum((x) => x.level === "ERROR", "count");
      const warnings = sum((x) => x.level === "WARNING", "count");
      return { label: "Open alerts", center: String(errors + warnings), unit: "open",
        segs: [{ label: "Errors", value: errors, color: C.danger }, { label: "Warnings", value: warnings, color: C.warn },
          { label: "Resolved", value: g.reduce((a, x) => a + x.hidden, 0), color: C.muted }] };
    }
    case "access_chart": {
      if (need(ins)) return null;
      const days = ins!.access_by_day as { changed: number; planned: number }[];
      const applied = days.reduce((a, d) => a + d.changed, 0);
      const planned = days.reduce((a, d) => a + d.planned, 0);
      return { label: "Access changes in the last 14 days", center: String(applied + planned), unit: "changes",
        segs: [{ label: "Applied", value: applied, color: C.accent }, { label: "Planned in dry-run", value: planned, color: C.warn }] };
    }
    case "activity": {
      if (need(ins)) return null;
      const counts = new Map<string, number>();
      for (const a of ins!.activity as { action: string }[]) {
        const name = ACTION_TEXT[a.action] ?? a.action.replace(/_/g, " ");
        counts.set(name, (counts.get(name) ?? 0) + 1);
      }
      const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
      return { label: "Recent activity by kind", center: String(top.reduce((a, [, n]) => a + n, 0)), unit: "events",
        segs: top.map(([label, value], i) => ({ label, value, color: CYCLE[i % CYCLE.length] })) };
    }
    case "libraries": {
      if (need(ins)) return null;
      const libs = (ins!.libraries as { title: string; users: number }[]).slice(0, 8);
      return { label: "Users with access per library",
        segs: libs.map((l, i) => ({ label: l.title, value: l.users, color: CYCLE[i % CYCLE.length] })) };
    }
    case "roles":
    case "role_libraries": {
      if (need(ins)) return null;
      const roles = ((id === "roles" ? ins!.roles : ins!.role_libraries) as { name: string; members: number }[]).slice(0, 8);
      return { label: id === "roles" ? "Members per Discord role" : "Members per role that grants libraries", unit: "memberships",
        segs: roles.map((r, i) => ({ label: r.name, value: r.members, color: CYCLE[i % CYCLE.length] })) };
    }
    case "security": {
      if (need(ins)) return null;
      const s = ins!.security;
      return { label: "Security", segs: [{ label: "Failed sign-ins (24 h)", value: s.failed_logins_24h, color: C.warn },
        { label: "Active sessions", value: s.sessions, color: C.accent }, { label: "API keys", value: s.api_keys, color: C.info }] };
    }
    default:
      return null;
  }
}

function Chart({ view, segs, label, center, unit }: Series & { view: View }) {
  const total = segs.reduce((a, s) => a + s.value, 0);
  const summary = `${label}: ${segs.map((s) => `${s.label} ${s.value}`).join(", ")}`;
  if (total === 0) return <p className="small muted" style={{ margin: 0 }}>Nothing to show yet.</p>;
  return view === "donut" ? <Donut segs={segs} summary={summary} center={center ?? String(total)} unit={unit} total={total} />
    : <BarList segs={segs} summary={summary} />;
}

function Donut({ segs, summary, center, unit, total }: { segs: Seg[]; summary: string; center: string; unit?: string; total: number }) {
  const r = 40, len = 2 * Math.PI * r;
  let offset = 0;
  return (
    <div className="donut-wrap">
      <svg viewBox="0 0 100 100" className="donut" role="img" aria-label={summary}>
        <circle cx="50" cy="50" r={r} fill="none" stroke="var(--border)" strokeWidth="14" opacity="0.35" />
        {segs.filter((s) => s.value > 0).map((s) => {
          const part = (s.value / total) * len;
          const el = (
            <circle key={s.label} cx="50" cy="50" r={r} fill="none" stroke={s.color} strokeWidth="14"
              strokeDasharray={`${part} ${len - part}`} strokeDashoffset={-offset} transform="rotate(-90 50 50)">
              <title>{`${s.label}: ${s.value}`}</title>
            </circle>
          );
          offset += part;
          return el;
        })}
        <text x="50" y={unit ? 50 : 55} textAnchor="middle" className="donut-value">{center}</text>
        {unit ? <text x="50" y="64" textAnchor="middle" className="donut-unit">{unit}</text> : null}
      </svg>
      <ul className="dash-legend small donut-legend">
        {segs.map((s) => (
          <li key={s.label}><span className="dot" style={{ background: s.color }} /> {s.label} <strong>{s.value}</strong></li>
        ))}
      </ul>
    </div>
  );
}

function BarList({ segs, summary }: { segs: Seg[]; summary: string }) {
  const max = Math.max(1, ...segs.map((s) => s.value));
  return (
    <ul className="dash-list small" aria-label={summary}>
      {segs.map((s) => (
        <li key={s.label}>
          <div className="row spread"><span>{s.label}</span><strong>{s.value}</strong></div>
          <div className="dash-bar thin" aria-hidden="true"><span style={{ width: `${(s.value / max) * 100}%`, background: s.color }} /></div>
        </li>
      ))}
    </ul>
  );
}
