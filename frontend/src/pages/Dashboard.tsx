import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, type PlanOut, type StatusOut } from "../api";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, PageHead, Spinner, ago, useLoad, useToast } from "../ui";

function Integration({ name, icon, s }: { name: string; icon: "plex" | "discord"; s: StatusOut["plex"] }) {
  return (
    <div className="card">
      <div className="row spread">
        <span className="row"><Icon name={icon} /> <strong>{name}</strong></span>
        {s.connected ? <Badge tone="ok">Connected</Badge> : s.configured ? <Badge tone="danger">Disconnected</Badge> : <Badge tone="neutral">Not set up</Badge>}
      </div>
      {s.detail ? <p className="small muted" style={{ marginTop: 8 }}>{s.detail}</p> : null}
      {!s.configured ? <Link to="/setup" className="small">Set up {name} →</Link> : null}
    </div>
  );
}

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

export default function DashboardPage() {
  const toast = useToast();
  const { data, error, reload } = useLoad(() => api.get<StatusOut>("/api/v1/status"));
  const preview = useLoad(() => api.get<PlanOut[]>("/api/v1/sync/preview"));

  useEffect(() => {
    const t = setInterval(() => void reload(), 10000);
    return () => clearInterval(t);
  }, [reload]);

  const syncNow = async () => {
    try {
      await api.post("/api/v1/sync", {});
      toast.ok("Full sync queued.");
      setTimeout(() => {
        void reload();
        void preview.reload();
      }, 2500);
    } catch (e) {
      toast.error(e);
    }
  };

  if (error) return <ErrorBanner error={error} />;
  if (!data) return <Spinner />;
  const last = data.sync.last_run as LastRun | null;
  const c = data.counts;

  return (
    <>
      <PageHead title="Dashboard" sub={`WooSarr ${data.version}`}>
        <button className="btn primary" onClick={() => void syncNow()} disabled={!data.plex.connected}>
          <Icon name="refresh" /> Sync now
        </button>
      </PageHead>

      {data.dry_run ? (
        <div className="banner warn" role="status">
          <Icon name="eye" />
          <div>
            <strong>Dry-run is on.</strong> WooSarr logs and audits what it <em>would</em> change but doesn't touch Plex.
            Turn it off in <Link to="/settings">Settings</Link> once the planned changes below look right.
          </div>
        </div>
      ) : null}
      {data.recent_errors > 0 ? (
        <div className="banner danger" role="alert">
          <Icon name="alert" />
          <div>
            {data.recent_errors} error{data.recent_errors === 1 ? "" : "s"} in the last 24 hours. <Link to="/logs?level=ERROR">View logs</Link>
          </div>
        </div>
      ) : null}

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Integration name="Plex" icon="plex" s={data.plex} />
        <Integration name="Discord" icon="discord" s={data.discord} />
      </div>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Plex users" value={c.plex_users} to="/users" />
        <Stat label="Linked" value={c.linked_users} sub={`${c.verified_links} verified`} to="/users" />
        <Stat label="Libraries" value={c.libraries} sub={`${c.exempt_libraries} exempt`} to="/libraries" />
        <Stat label="Discord roles" value={c.roles} sub={`${c.discord_members} members`} to="/roles" />
      </div>

      <div className="grid cols-2">
        <section className="card" aria-labelledby="sync-h">
          <h2 id="sync-h">Last sync</h2>
          {!last ? (
            <p className="muted">No sync has run yet{data.plex.connected ? "; the first one starts shortly after startup." : "."}</p>
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
            <Icon name="clock" size={14} /> Next full reconcile in {Math.round(Number(data.sync.next_full_reconcile_in_seconds) / 60)} min
            {data.sync.running ? " · running now" : ""}
          </p>
        </section>

        <section className="card" aria-labelledby="plan-h">
          <h2 id="plan-h">Pending changes</h2>
          {preview.loading && !preview.data ? <Spinner /> : null}
          {preview.data && preview.data.length === 0 ? (
            <Empty icon="check" title="Nothing to change">Every linked user already has the access their roles give.</Empty>
          ) : null}
          {preview.data && preview.data.length > 0 ? (
            <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
              {preview.data.slice(0, 8).map((p) => (
                <li key={p.plex_user_id}>
                  <strong>{p.username}</strong>
                  {p.add.length ? <> gains {p.add.length}</> : null}
                  {p.add.length && p.remove.length ? "," : null}
                  {p.remove.length ? <> loses {p.remove.length}</> : null}
                </li>
              ))}
              {preview.data.length > 8 ? <li className="muted">…and {preview.data.length - 8} more</li> : null}
            </ul>
          ) : null}
        </section>
      </div>
    </>
  );
}

function Stat({ label, value, sub, to }: { label: string; value: number; sub?: string; to: string }) {
  return (
    <Link to={to} className="card" style={{ textDecoration: "none", color: "inherit" }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="small muted">{sub}</div> : null}
    </Link>
  );
}
