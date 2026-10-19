import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type AdminOut, type ApiKeyCreated, type ApiKeyOut, type AppSettings, type LibraryOut, type RoleOut } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../icons";
import { useTheme, type Mode, type Palette } from "../theme";
import { Badge, ErrorBanner, Field, Modal, PageHead, Spinner, Switch, ago, useConfirm, useLoad, useToast } from "../ui";
import { useDiscordFeatures } from "../features";
import SsoSettings from "./Sso";
import { RequestSettings } from "./Requests";
import { ImpactList, type UserChange } from "../Impact";

const TABS = [
  ["general", "General"],
  ["linking", "Linking"],
  ["requests", "Requests"],
  ["appearance", "Appearance"],
  ["account", "My account"],
  ["admins", "Admins"],
  ["sso", "Single sign-on"],
  ["keys", "API keys"],
] as const;
// Tabs that were merged into others; old links still land in the right place.
const MOVED: Record<string, string> = { invites: "linking", logging: "general" };

export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") ?? "";
  const tab = TABS.some(([k]) => k === asked) ? asked : MOVED[asked] ?? "general";
  return (
    <>
      <PageHead title="Settings" />
      <div className="segmented" role="tablist" aria-label="Settings sections" style={{ marginBottom: 16, flexWrap: "wrap" }}>
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setParams({ tab: k })}>{label}</button>
        ))}
      </div>
      {tab === "general" ? <General /> : null}
      {tab === "linking" ? <Linking /> : null}
      {tab === "requests" ? <RequestSettings /> : null}
      {tab === "appearance" ? <Appearance /> : null}
      {tab === "account" ? <Account /> : null}
      {tab === "admins" ? <Admins /> : null}
      {tab === "sso" ? <SsoSettings /> : null}
      {tab === "keys" ? <ApiKeys /> : null}
    </>
  );
}

const ACCESS_SETTINGS = ["dry_run", "leave_policy", "ignore_unverified_links"] as const;

interface SettingsImpact {
  users_gaining: number;
  users_losing: number;
  users: UserChange[];
}

/** Loads the app settings and keeps unsaved changes; each settings tab edits its own fields. */
function useSettingsDraft() {
  const s = useLoad(() => api.get<AppSettings>("/api/v1/settings"));
  const [draft, setDraft] = useState<Partial<AppSettings>>({});
  const toast = useToast();
  const v = (s.data ? { ...s.data, ...draft } : null) as AppSettings | null;
  const set = (p: Partial<AppSettings>) => setDraft({ ...draft, ...p });
  const save = async () => {
    try {
      s.setData(await api.patch<AppSettings>("/api/v1/settings", draft));
      setDraft({});
      toast.ok("Settings saved.");
    } catch (e) {
      toast.error(e);
    }
  };
  return { s, v, draft, set, save, discard: () => setDraft({}), dirty: Object.keys(draft).length > 0 };
}

function SaveRow({ dirty, onSave, onDiscard }: { dirty: boolean; onSave: () => void; onDiscard: () => void }) {
  return (
    <div className="row">
      <button className="btn primary" disabled={!dirty} onClick={onSave}>Save</button>
      <button className="btn" disabled={!dirty} onClick={onDiscard}>Discard</button>
    </div>
  );
}

function RolePicker({ legend, hint, all, allHint, roles, value, onAll, onChange }: {
  legend: ReactNode; hint: ReactNode; all: boolean; allHint: string; roles: RoleOut[]; value: string[];
  onAll: (x: boolean) => void; onChange: (ids: string[]) => void;
}) {
  return (
    <fieldset className="plain">
      <legend className="small" style={{ fontWeight: 600 }}>{legend}</legend>
      <span className="hint small muted" style={{ display: "block", marginBottom: 6 }}>{hint}</span>
      <label className="check" style={{ marginBottom: 6 }}>
        <input type="checkbox" checked={all} onChange={(e) => onAll(e.target.checked)} />
        <span><strong>All members</strong> <span className="muted small">— {allHint}</span></span>
      </label>
      <div className="chooser" style={all ? { opacity: 0.5 } : undefined} aria-disabled={all}>
        {roles.filter((r) => !r.managed && !r.deleted).map((r) => {
          const on = value.includes(r.role_id);
          return (
            <label key={r.role_id} className="check">
              <input type="checkbox" checked={on} onChange={() => onChange(on ? value.filter((x) => x !== r.role_id) : [...value, r.role_id])} />
              <span>{r.name}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

/** Live preview of whose access the unsaved access-affecting settings would change. */
function useImpact(draft: Partial<AppSettings>) {
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const accessKey = JSON.stringify(Object.fromEntries(Object.entries(draft).filter(([k]) => (ACCESS_SETTINGS as readonly string[]).includes(k))));
  useEffect(() => {
    if (accessKey === "{}") {
      setImpact(null);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      api.post<SettingsImpact>("/api/v1/settings/preview", JSON.parse(accessKey)).then((r) => live && setImpact(r), () => live && setImpact(null));
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [accessKey]);
  return { impact, changed: accessKey !== "{}" };
}

function ImpactCard({ impact }: { impact: SettingsImpact | null }) {
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const titles = new Map((libraries.data ?? []).map((l) => [l.library_id, l.title]));
  return (
    <div className="card flat" aria-live="polite">
      <h3>Impact of these changes</h3>
      {!impact ? <Spinner label="Calculating" /> : (
        <ImpactList titles={titles} changes={impact.users} empty="No linked user's access changes." />
      )}
    </div>
  );
}

const POLICY_OPTIONS = (
  <>
    <option value="drop_role_access">Remove role-granted libraries (keep manual and exempt)</option>
    <option value="keep_all">Leave their access as it is</option>
  </>
);

function General() {
  const f = useSettingsDraft();
  const { impact, changed } = useImpact(f.draft);
  const confirm = useConfirm();
  if (f.s.error) return <ErrorBanner error={f.s.error} />;
  const v = f.v;
  if (!v) return <Spinner />;
  const set = f.set;

  const save = async () => {
    if (f.draft.dry_run === false && f.s.data!.dry_run) {
      const ok = await confirm({
        title: "Turn off dry-run?",
        body: (
          <p>
            WooSarr will start changing Plex library shares for linked users
            {impact ? <>: <strong>{impact.users_gaining}</strong> user{impact.users_gaining === 1 ? "" : "s"} gain and <strong>{impact.users_losing}</strong> lose access on the next sync</> : null}.
            Check the impact below the settings first.
          </p>
        ),
        confirmLabel: "Go live",
        danger: true,
      });
      if (!ok) return;
    }
    await f.save();
  };

  return (
    <div className="card settings" style={{ maxWidth: 720 }}>
      <Switch checked={v.dry_run} onChange={(x) => set({ dry_run: x })} label={<><strong>Dry-run</strong> <span className="muted small">— log and audit intended changes without touching Plex</span></>} />
      <Field label="Full reconcile every (minutes)" hint="Discord events trigger syncs right away; the reconcile catches anything missed. 5–1440.">
        <input type="number" min={5} max={1440} value={v.reconcile_interval_minutes} onChange={(e) => set({ reconcile_interval_minutes: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </Field>
      <Switch checked={v.ignore_unverified_links} onChange={(x) => set({ ignore_unverified_links: x })} label={<><strong>Ignore unverified links</strong> <span className="muted small">— only manage users who proved their Plex account</span></>} />
      <Field label="Log level" hint="DEBUG shows every Plex/Discord call. Applies immediately.">
        <select value={v.log_level ?? ""} onChange={(e) => set({ log_level: (e.target.value || null) as AppSettings["log_level"] })} style={{ maxWidth: 220 }}>
          <option value="">Default (container setting)</option>
          {["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
      </Field>
      <Field label="Keep audit entries for (days)" hint="Older audit entries are deleted. 7–3650.">
        <input type="number" min={7} max={3650} value={v.audit_retention_days} onChange={(e) => set({ audit_retention_days: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </Field>
      {changed ? <ImpactCard impact={impact} /> : null}
      <SaveRow dirty={f.dirty} onSave={() => void save()} onDiscard={f.discard} />
    </div>
  );
}

function Linking() {
  const f = useSettingsDraft();
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const { impact, changed } = useImpact(f.draft);
  if (f.s.error) return <ErrorBanner error={f.s.error} />;
  const v = f.v;
  if (!v) return <Spinner />;
  return (
    <div className="card settings" style={{ maxWidth: 720 }}>
      <RolePicker
        legend={<>Who may link by typing a Plex username (unverified)</>}
        hint={<>Members without one of these roles only see <strong>Sign in with Plex</strong> in <code>/link</code>, which verifies their account. None selected = nobody can link by username.</>}
        all={v.username_link_all_members ?? true} allHint="anyone in the Discord server" roles={roles.data ?? []}
        value={v.username_link_role_ids ?? []}
        onAll={(x) => f.set({ username_link_all_members: x })} onChange={(ids) => f.set({ username_link_role_ids: ids })} />
      <fieldset className="plain">
        <legend className="small" style={{ fontWeight: 600 }}>Shared Plex accounts (families)</legend>
        <span className="hint small muted" style={{ display: "block", marginBottom: 6 }}>
          Several Discord members can be linked to one Plex account; it gets the libraries all their roles give.
          Who may add a member to an account someone is already verified on:
        </span>
        <label className="check" style={{ marginBottom: 6 }}>
          <input type="radio" name="family-link-mode" checked={(v.family_link_mode ?? "admin") === "admin"} onChange={() => f.set({ family_link_mode: "admin" })} />
          <span><strong>Admins only</strong> <span className="muted small">— from the Users page (<em>Add member</em>)</span></span>
        </label>
        <label className="check">
          <input type="radio" name="family-link-mode" checked={v.family_link_mode === "verified"} onChange={() => f.set({ family_link_mode: "verified" })} />
          <span><strong>Admins, or members who sign in with that Plex account</strong> <span className="muted small">— in <code>/link</code>; typing the username is never enough</span></span>
        </label>
        <span className="hint small muted" style={{ display: "block", marginTop: 6 }}>
          Either way, signing in with Plex replaces links made by typing the username, and every new member shows up in Alerts.
        </span>
      </fieldset>
      <Field label={<><code>/link</code> cooldown (seconds)</>} hint="How long a member waits between link attempts. 0 = no wait.">
        <input type="number" min={0} max={86400} value={v.link_cooldown_seconds} onChange={(e) => f.set({ link_cooldown_seconds: Number(e.target.value) })} style={{ maxWidth: 160 }} />
      </Field>
      <label className="field">
        When a linked member leaves the Discord server
        <select value={v.leave_policy} onChange={(e) => f.set({ leave_policy: e.target.value as AppSettings["leave_policy"] })} style={{ maxWidth: 420 }}>{POLICY_OPTIONS}</select>
      </label>
      <label className="field">
        When a member unlinks themselves
        <select value={v.unlink_policy} onChange={(e) => f.set({ unlink_policy: e.target.value as AppSettings["unlink_policy"] })} style={{ maxWidth: 420 }}>{POLICY_OPTIONS}</select>
      </label>
      <LinkRoles roles={roles.data ?? []} value={v} set={f.set} onCreated={() => void roles.reload()} />
      <RolePicker
        legend="Plex Invites"
        hint={<>Members with one of these roles can <code>/invite</code> their own Plex account; their roles decide the libraries.
          Discord admins (Manage Server) can always invite for any member. None selected = admins only.</>}
        all={!!v.invite_all_members} allHint="anyone in the Discord server" roles={roles.data ?? []}
        value={v.invite_role_ids ?? []}
        onAll={(x) => f.set({ invite_all_members: x })} onChange={(ids) => f.set({ invite_role_ids: ids })} />
      {changed ? <ImpactCard impact={impact} /> : null}
      <SaveRow dirty={f.dirty} onSave={() => void f.save()} onDiscard={f.discard} />
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
    <div className="card settings" style={{ maxWidth: 720 }}>
      <div>
        <h3>Theme</h3>
        <div className="segmented" role="group" aria-label="Theme">
          {modes.map(([m, label]) => <button key={m} aria-pressed={prefs.mode === m} onClick={() => setPrefs({ mode: m })}>{label}</button>)}
        </div>
      </div>
      <fieldset className="plain">
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
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Admin accounts">
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
        <div className="table-wrap" tabIndex={0} role="region" aria-label="API keys">
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

const LINK_ROLES: { key: "verified_role_id" | "unverified_role_id" | "linked_role_id"; label: string; hint: string; name: string }[] = [
  { key: "verified_role_id", label: "Verified role", hint: "Members whose link is verified (Plex sign-in or admin)", name: "Plex Verified" },
  { key: "unverified_role_id", label: "Unverified role", hint: "Members linked by typed username", name: "Plex Unverified" },
  { key: "linked_role_id", label: "Linked role", hint: "Every linked member", name: "Plex Linked" },
];

function LinkRoles({ roles, value, set, onCreated }: {
  roles: RoleOut[]; value: AppSettings; set: (p: Partial<AppSettings>) => void; onCreated: () => void;
}) {
  const { available, why, pending } = useDiscordFeatures();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const create = async (key: (typeof LINK_ROLES)[number]["key"], name: string) => {
    setBusy(key);
    try {
      const r = await api.post<{ role_id: string; name: string }>("/api/v1/discord/roles", { name, purpose: key });
      set({ [key]: r.role_id } as Partial<AppSettings>);
      toast.ok(`Created role ${r.name}.`);
      onCreated();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const assignBlocked = why("link_roles");
  const createBlocked = why("create_roles");
  return (
    <fieldset className="plain stack">
      <legend className="small" style={{ fontWeight: 600 }}>Discord roles for Plex link status</legend>
      <span className="small muted">The bot gives and removes these roles to match each member's link. Leave on "Off" to skip.</span>
      {assignBlocked && (value.verified_role_id || value.unverified_role_id || value.linked_role_id) ? (
        <div className={`banner ${pending ? "info" : "warn"} small`} role={pending ? "status" : undefined}><span>{assignBlocked}</span></div>
      ) : null}
      {LINK_ROLES.map((r) => (
        <div key={r.key} className="row" style={{ alignItems: "flex-end" }}>
          <Field label={r.label} hint={r.hint}>
            <select value={value[r.key] ?? ""} onChange={(e) => set({ [r.key]: e.target.value || null } as Partial<AppSettings>)} style={{ maxWidth: 280 }}>
              <option value="">Off</option>
              {roles.filter((x) => !x.managed && !x.deleted).map((x) => <option key={x.role_id} value={x.role_id}>{x.name}</option>)}
            </select>
          </Field>
          <button type="button" className="btn sm" disabled={!available("create_roles") || busy !== null}
            title={createBlocked ?? `Create a role named "${r.name}"`} onClick={() => void create(r.key, r.name)}>
            {busy === r.key ? "Creating…" : `Create "${r.name}"`}
          </button>
        </div>
      ))}
    </fieldset>
  );
}
