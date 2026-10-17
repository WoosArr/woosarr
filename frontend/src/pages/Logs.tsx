import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, qs, type AuditOut, type AuditPage, type LogEntry, type LogPage } from "../api";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, PageHead, Spinner, fmtLogTime, fmtTime, useToast } from "../ui";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
const MAX_LINES = 20000;

export default function LogsPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "audit" ? "audit" : "logs";
  return (
    <>
      <PageHead title="Logs" sub="Application logs for debugging, and the audit trail of every access and account change." />
      <div className="segmented" role="tablist" aria-label="Log type" style={{ marginBottom: 14 }}>
        <button role="tab" aria-selected={tab === "logs"} aria-pressed={tab === "logs"} onClick={() => setParams({})}>Application log</button>
        <button role="tab" aria-selected={tab === "audit"} aria-pressed={tab === "audit"} onClick={() => setParams({ tab: "audit" })}>Audit trail</button>
      </div>
      {tab === "logs" ? <AppLog initialLevel={params.get("level")} /> : <AuditTrail />}
    </>
  );
}

function AppLog({ initialLevel }: { initialLevel: string | null }) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState<string>(LEVELS.includes(initialLevel as never) ? initialLevel! : "INFO");
  const [q, setQ] = useState("");
  const [live, setLive] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const cursor = useRef(0);
  const view = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const poll = useCallback(async () => {
    try {
      const page = await api.get<LogPage>(`/api/v1/logs${qs({ since: cursor.current, level, limit: 5000 })}`);
      cursor.current = page.latest_seq;
      if (page.entries.length) setEntries((prev) => [...prev, ...page.entries].slice(-MAX_LINES));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [level]);

  useEffect(() => {
    cursor.current = 0;
    setEntries([]);
    void poll();
  }, [poll]);

  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => void poll(), 2000);
    return () => clearInterval(t);
  }, [live, poll]);

  useEffect(() => {
    if (stick.current && view.current) view.current.scrollTop = view.current.scrollHeight;
  }, [entries]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter((e) => e.message.toLowerCase().includes(needle) || e.logger.toLowerCase().includes(needle) || (e.exc ?? "").toLowerCase().includes(needle));
  }, [entries, q]);

  const exportShown = () => {
    const blob = new Blob([shown.map((e) => JSON.stringify(e)).join("\n") + "\n"], { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `woosarr-logs-${new Date().toISOString().slice(0, 19)}.jsonl`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <label className="row small">
          Level
          <select className="inline-input" value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => <option key={l} value={l}>{l}+</option>)}
          </select>
        </label>
        <input type="search" className="inline-input" placeholder="Filter (message, logger, request id)…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter logs" style={{ width: 300 }} />
        <button className="btn sm" onClick={() => setLive(!live)} aria-pressed={live}>
          <Icon name={live ? "pause" : "play"} size={14} /> {live ? "Pause" : "Live"}
        </button>
        <button className="btn sm" onClick={exportShown} disabled={!shown.length}><Icon name="download" size={14} /> Export shown</button>
        <a className="btn sm" href="/api/v1/logs/file" download><Icon name="download" size={14} /> Full log file</a>
        <span className="small muted" aria-live="polite">{shown.length} lines{live ? " · live" : ""}</span>
      </div>
      <ErrorBanner error={error} />
      <div
        className="log-view"
        ref={view}
        role="log"
        aria-label="Application log"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {shown.length === 0 ? <Empty icon="logs" title="No log lines match" /> : null}
        {shown.map((e) => (
          <div key={e.seq} className="log-line">
            <span className="muted" title={fmtTime(e.ts)}>{fmtLogTime(e.ts)}</span>
            <span className={`lvl-${e.level}`}>{e.level}</span>
            <span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{e.logger}</span>
            <span className="msg">
              {e.message}
              {e.exc ? <pre>{e.exc}</pre> : null}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

const ACTION_LABEL: Record<string, string> = {
  access_changed: "Access changed",
  access_planned: "Access change (dry run)",
  access_skipped: "Change skipped",
  role_mapping_changed: "Role mapping changed",
  manual_grant_add: "Manual grant added",
  manual_grant_remove: "Manual grant removed",
  library_exempt: "Library exempted",
  library_unexempt: "Library managed again",
  link_admin: "Linked by admin",
  unlink_admin: "Unlinked by admin",
  settings_changed: "Settings changed",
  login: "Signed in",
  login_failed: "Failed sign-in",
};

function AuditTrail() {
  const [entries, setEntries] = useState<AuditOut[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [action, setAction] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();

  const load = useCallback(
    async (before?: number) => {
      setLoading(true);
      try {
        const page = await api.get<AuditPage>(`/api/v1/audit${qs({ action, before_id: before, limit: 100 })}`);
        setEntries((prev) => (before ? [...prev, ...page.entries] : page.entries));
        setNext(page.next_before_id ?? null);
        setError(null);
      } catch (e) {
        setError(e);
        if (before) toast.error(e);
      } finally {
        setLoading(false);
      }
    },
    [action],
  );

  useEffect(() => void load(), [load]);

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <label className="row small">
          Action
          <select className="inline-input" value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">All</option>
            {Object.entries(ACTION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      <ErrorBanner error={error} />
      {loading && !entries.length ? <Spinner /> : null}
      {!loading && !entries.length ? <div className="card"><Empty icon="logs" title="Nothing audited yet" /></div> : null}
      {entries.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>When</th><th>Who</th><th>What</th><th>Target</th><th>Details</th></tr></thead>
            <tbody>
              {entries.map((a) => (
                <tr key={a.id}>
                  <td className="small" style={{ whiteSpace: "nowrap" }}>{fmtTime(a.ts)}</td>
                  <td className="small">{a.actor}</td>
                  <td>
                    {ACTION_LABEL[a.action] ?? a.action}
                    {a.dry_run ? <> <Badge tone="warn" icon="eye">dry run</Badge></> : null}
                  </td>
                  <td className="small">{a.target_type ? `${a.target_type}:${a.target_id}` : "—"}</td>
                  <td className="small"><AuditDetails d={a.details} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {next ? (
        <div style={{ marginTop: 12 }}>
          <button className="btn" disabled={loading} onClick={() => void load(next)}>Load older</button>
        </div>
      ) : null}
    </>
  );
}

function AuditDetails({ d }: { d: Record<string, unknown> }) {
  const add = d.add as string[] | undefined;
  const remove = d.remove as string[] | undefined;
  if (add || remove) {
    return (
      <span>
        {add?.length ? <span>+ {add.join(", ")} </span> : null}
        {remove?.length ? <span>− {remove.join(", ")} </span> : null}
        {d.trigger ? <span className="muted">({String(d.trigger)})</span> : null}
      </span>
    );
  }
  const text = JSON.stringify(d);
  return <code title={text}>{text.length > 160 ? `${text.slice(0, 160)}…` : text}</code>;
}
