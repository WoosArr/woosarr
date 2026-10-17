import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type AdminOut, type ApiKeyCreated, type ApiKeyOut, type AppSettings } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../icons";
import { useTheme, type Mode, type Palette } from "../theme";
import { Badge, ErrorBanner, Field, Modal, PageHead, Spinner, Switch, ago, useConfirm, useLoad, useToast } from "../ui";

const TABS = [
  ["general", "General"],
  ["appearance", "Appearance"],
  ["account", "My account"],
  ["admins", "Admins"],
  ["keys", "API keys"],
] as const;

export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const tab = TABS.some(([k]) => k === params.get("tab")) ? params.get("tab")! : "general";
  return (
    <>
      <PageHead title="Settings" />
      <div className="segmented" role="tablist" aria-label="Settings sections" style={{ marginBottom: 16, flexWrap: "wrap" }}>
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} aria-pressed={tab === k} onClick={() => setParams({ tab: k })}>{label}</button>
        ))}
      </div>
      {tab === "general" ? <General /> : null}
      {tab === "appearance" ? <Appearance /> : null}
      {tab === "account" ? <Account /> : null}
      {tab === "admins" ? <Admins /> : null}
      {tab === "keys" ? <ApiKeys /> : null}
    </>
  );
}

function General() {
  const s = useLoad(() => api.get<AppSettings>("/api/v1/settings"));
  const [draft, setDraft] = useState<Partial<AppSettings>>({});
  const toast = useToast();
  const confirm = useConfirm();
  if (s.error) return <ErrorBanner error={s.error} />;
  if (!s.data) return <Spinner />;
  const v = { ...s.data, ...draft } as AppSettings;
  const dirty = Object.keys(draft).length > 0;
  const set = (p: Partial<AppSettings>) => setDraft({ ...draft, ...p });

  const save = async () => {
    if (draft.dry_run === false && s.data!.dry_run) {
      const ok = await confirm({
        title: "Turn off dry-run?",
        body: <p>WooSarr will start changing Plex library shares for linked users. Check the dashboard's pending changes first.</p>,
        confirmLabel: "Go live",
        danger: true,
      });
      if (!ok) return;
    }
    try {
      s.setData(await api.patch<AppSettings>("/api/v1/settings", draft));
      setDraft({});
      toast.ok("Settings saved.");
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <div className="card stack" style={{ maxWidth: 720 }}>
      <Switch checked={v.dry_run} onChange={(x) => set({ dry_run: x })} label={<><strong>Dry-run</strong> <span className="muted small">— log and audit intended changes without touching Plex</span></>} />
      <Field label="Full reconcile every (minutes)" hint="Discord events trigger syncs right away; the reconcile catches anything missed. 5–1440.">
        <input type="number" min={5} max={1440} value={v.reconcile_interval_minutes} onChange={(e) => set({ reconcile_interval_minutes: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </Field>
      <label className="field">
        When a linked member leaves the Discord server
        <select value={v.leave_policy} onChange={(e) => set({ leave_policy: e.target.value as AppSettings["leave_policy"] })} style={{ maxWidth: 420 }}>
          <option value="drop_role_access">Remove role-granted libraries (keep manual and exempt)</option>
          <option value="keep_all">Leave their access as it is</option>
        </select>
      </label>
      <Switch checked={v.ignore_unverified_links} onChange={(x) => set({ ignore_unverified_links: x })} label={<><strong>Ignore unverified links</strong> <span className="muted small">— only manage users who proved their Plex account</span></>} />
      <label className="field">
        <code>/link</code> cooldown (seconds)
        <input type="number" min={0} max={86400} value={v.link_cooldown_seconds} onChange={(e) => set({ link_cooldown_seconds: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </label>
      <Field label="Log level" hint="DEBUG shows every Plex/Discord call. Applies immediately.">
        <select value={v.log_level ?? ""} onChange={(e) => set({ log_level: (e.target.value || null) as AppSettings["log_level"] })} style={{ maxWidth: 220 }}>
          <option value="">Default (container setting)</option>
          {["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
      </Field>
      <label className="field">
        Keep audit entries for (days)
        <input type="number" min={7} max={3650} value={v.audit_retention_days} onChange={(e) => set({ audit_retention_days: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </label>
      <div className="row">
        <button className="btn primary" disabled={!dirty} onClick={() => void save()}>Save</button>
        <button className="btn" disabled={!dirty} onClick={() => setDraft({})}>Discard</button>
      </div>
    </div>
  );
}

function Appearance() {
  const { prefs, setPrefs } = useTheme();
  const modes: [Mode, string][] = [["system", "Match system"], ["light", "Light"], ["dark", "Dark"]];
  const palettes: [Palette, string, string][] = [
    ["default", "Standard", "Green / amber / red status colours"],
    ["redgreen", "Red-green friendly", "Blue / orange (deuteranopia, protanopia)"],
    ["blueyellow", "Blue-yellow friendly", "Teal / magenta (tritanopia)"],
  ];
  return (
    <div className="card stack" style={{ maxWidth: 720 }}>
      <div>
        <h3>Theme</h3>
        <div className="segmented" role="group" aria-label="Theme">
          {modes.map(([m, label]) => <button key={m} aria-pressed={prefs.mode === m} onClick={() => setPrefs({ mode: m })}>{label}</button>)}
        </div>
      </div>
      <fieldset style={{ border: 0, padding: 0 }}>
        <legend><h3>Colour vision</h3></legend>
        {palettes.map(([p, label, hint]) => (
          <label key={p} className="check" style={{ marginBottom: 6 }}>
            <input type="radio" name="palette" checked={prefs.palette === p} onChange={() => setPrefs({ palette: p })} />
            <span>{label} <span className="muted small">— {hint}</span></span>
          </label>
        ))}
      </fieldset>
      <Switch checked={prefs.highContrast} onChange={(x) => setPrefs({ highContrast: x })} label="Extra contrast" />
      <div>
        <h3>Preview</h3>
        <div className="row">
          <Badge tone="ok">Verified</Badge><Badge tone="warn">Unverified</Badge><Badge tone="danger">Error</Badge>
          <Badge tone="info">Pending</Badge><Badge tone="accent" icon="roles">Role</Badge>
        </div>
        <p className="small muted" style={{ marginTop: 8 }}>Statuses always show an icon and a word as well as a colour. Saved in this browser.</p>
      </div>
    </div>
  );
}

function Account() {
  const { status } = useAuth();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== again) return setError(new Error("New passwords don't match."));
    try {
      await api.post("/api/v1/auth/password", { current_password: cur, new_password: next });
      setCur(""); setNext(""); setAgain(""); setError(null);
      toast.ok("Password changed. Other sessions were signed out.");
    } catch (err) {
      setError(err);
    }
  };
  return (
    <form className="card stack" style={{ maxWidth: 480 }} onSubmit={submit}>
      <p>Signed in as <strong>{status?.user?.username}</strong>.</p>
      <ErrorBanner error={error} />
      <label className="field">Current password<input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required /></label>
      <Field label="New password" hint="At least 12 characters.">
        <input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required />
      </Field>
      <label className="field">Repeat new password<input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></label>
      <div><button className="btn primary" type="submit">Change password</button></div>
    </form>
  );
}

function Admins() {
  const admins = useLoad(() => api.get<AdminOut[]>("/api/v1/admins"));
  const { status } = useAuth();
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<AdminOut | null>(null);
  const confirm = useConfirm();
  const toast = useToast();

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try {
      await fn();
      toast.ok(msg);
      void admins.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="btn primary" onClick={() => setAdding(true)}><Icon name="plus" /> Add admin</button>
      </div>
      <ErrorBanner error={admins.error} />
      {admins.data ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Username</th><th>Status</th><th>Last sign-in</th><th aria-label="Actions" /></tr></thead>
            <tbody>
              {admins.data.map((a) => (
                <tr key={a.id}>
                  <td><strong>{a.username}</strong>{a.id === status?.user?.id ? <span className="muted small"> (you)</span> : null}<div className="small muted">{a.auth_source}</div></td>
                  <td>
                    <div className="row">
                      {a.is_active ? <Badge tone="ok">Active</Badge> : <Badge tone="neutral">Disabled</Badge>}
                      {a.locked ? <Badge tone="danger" icon="lock">Locked</Badge> : null}
                    </div>
                  </td>
                  <td className="small">{ago(a.last_login_at)}</td>
                  <td>
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      {a.locked ? <button className="btn sm" onClick={() => void act(() => api.patch(`/api/v1/admins/${a.id}`, { unlock: true }), `${a.username} unlocked.`)}>Unlock</button> : null}
                      <button className="btn sm" onClick={() => setResetting(a)}>Reset password</button>
                      <button className="btn sm" onClick={() => void act(() => api.patch(`/api/v1/admins/${a.id}`, { is_active: !a.is_active }), `${a.username} ${a.is_active ? "disabled" : "enabled"}.`)}>
                        {a.is_active ? "Disable" : "Enable"}
                      </button>
                      <button
                        className="btn sm danger"
                        onClick={async () => {
                          if (await confirm({ title: `Delete ${a.username}?`, body: <p>This admin account and its API keys are removed.</p>, confirmLabel: "Delete", danger: true })) {
                            await act(() => api.del(`/api/v1/admins/${a.id}`), `${a.username} deleted.`);
                          }
                        }}
                      >
                        <Icon name="trash" size={14} /> Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <Spinner />}
      {adding ? <PasswordModal title="Add admin" withUsername onClose={() => setAdding(false)} onSubmit={async (username, password) => { await api.post("/api/v1/admins", { username, password }); setAdding(false); toast.ok(`${username} added.`); void admins.reload(); }} /> : null}
      {resetting ? <PasswordModal title={`New password for ${resetting.username}`} onClose={() => setResetting(null)} onSubmit={async (_u, password) => { await api.patch(`/api/v1/admins/${resetting.id}`, { password }); setResetting(null); toast.ok("Password reset; their sessions were signed out."); }} /> : null}
    </>
  );
}

function PasswordModal({ title, withUsername, onClose, onSubmit }: { title: string; withUsername?: boolean; onClose: () => void; onSubmit: (u: string, p: string) => Promise<void> }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await onSubmit(u, p);
    } catch (err) {
      setError(err);
    }
  };
  return (
    <Modal title={title} onClose={onClose}>
      <form className="stack" onSubmit={submit}>
        <ErrorBanner error={error} />
        {withUsername ? <label className="field">Username<input type="text" value={u} onChange={(e) => setU(e.target.value)} required minLength={3} /></label> : null}
        <Field label="Password" hint="At least 12 characters.">
          <input type="password" autoComplete="new-password" minLength={12} value={p} onChange={(e) => setP(e.target.value)} required />
      </Field>
        <div className="modal-actions"><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" type="submit">Save</button></div>
      </form>
    </Modal>
  );
}

function ApiKeys() {
  const keys = useLoad(() => api.get<ApiKeyOut[]>("/api/v1/api-keys"));
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"read" | "admin">("read");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const toast = useToast();
  const confirm = useConfirm();

  const create = async (e: FormEvent) => {
    e.preventDefault();
    try {
      setCreated(await api.post<ApiKeyCreated>("/api/v1/api-keys", { name, scope }));
      setName("");
      void keys.reload();
    } catch (err) {
      toast.error(err);
    }
  };

  return (
    <>
      <div className="card stack" style={{ maxWidth: 720, marginBottom: 16 }}>
        <p className="small">
          API keys let scripts call WooSarr with <code>Authorization: Bearer wsk_…</code>. A <strong>read</strong> key can only
          read (for example <code>GET /api/v1/logs</code> for remote troubleshooting); an <strong>admin</strong> key can change things.
        </p>
        <form className="row" onSubmit={create}>
          <input type="text" className="inline-input" placeholder="Name, e.g. troubleshooting" value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} aria-label="Key name" style={{ width: 260 }} />
          <select className="inline-input" value={scope} onChange={(e) => setScope(e.target.value as "read" | "admin")} aria-label="Scope">
            <option value="read">read</option>
            <option value="admin">admin</option>
          </select>
          <button className="btn primary" type="submit"><Icon name="key" /> Create key</button>
        </form>
      </div>
      <ErrorBanner error={keys.error} />
      {keys.data?.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Key</th><th>Scope</th><th>Owner</th><th>Last used</th><th aria-label="Actions" /></tr></thead>
            <tbody>
              {keys.data.map((k) => (
                <tr key={k.id}>
                  <td>{k.name}</td>
                  <td><code>wsk_{k.prefix}_…</code></td>
                  <td><Badge tone={k.scope === "admin" ? "warn" : "info"}>{k.scope}</Badge></td>
                  <td className="small">{k.owner}</td>
                  <td className="small">{ago(k.last_used_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    {k.revoked ? <Badge tone="neutral">Revoked</Badge> : (
                      <button className="btn sm danger" onClick={async () => {
                        if (await confirm({ title: `Revoke ${k.name}?`, body: <p>Anything using this key stops working immediately.</p>, confirmLabel: "Revoke", danger: true })) {
                          try { await api.del(`/api/v1/api-keys/${k.id}`); void keys.reload(); } catch (e) { toast.error(e); }
                        }
                      }}>Revoke</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : keys.data ? <p className="muted">No API keys yet.</p> : <Spinner />}
      {created ? (
        <Modal title="Copy your new key" onClose={() => setCreated(null)} actions={<button className="btn primary" onClick={() => setCreated(null)}>Done</button>}>
          <p className="small">This is the only time the key is shown. Store it somewhere safe.</p>
          <div className="secret-box" data-testid="new-api-key">{created.key}</div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn sm" onClick={() => void navigator.clipboard?.writeText(created.key).then(() => toast.ok("Copied."))}>Copy</button>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
