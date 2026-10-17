import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  api,
  type AppSettings,
  type DiscoveryOut,
  type PresetOut,
  type PresetsOut,
  type ProviderIn,
  type ProviderOut,
  type TestResult,
} from "../api";
import { Icon } from "../icons";
import { Badge, ErrorBanner, Field, Modal, Spinner, Switch, ago, useConfirm, useLoad, useToast } from "../ui";

/** **bold**, `code`, {redirect_uri} -> elements (instructions from the presets). */
export function RichText({ text, redirectUri }: { text: string; redirectUri: string }) {
  const parts = text.replace(/\{redirect_uri\}/g, redirectUri).split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong>
          : p.startsWith("`") ? <code key={i}>{p.slice(1, -1)}</code>
            : <Fragment key={i}>{p}</Fragment>)}
    </>
  );
}

const KIND_ICON: Record<string, string> = { okta: "🔵", entra: "🟦", google: "🟩", adfs: "🏢", custom: "⚙️" };
const lines = (v: string) => v.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

export default function SsoSettings() {
  const presets = useLoad(() => api.get<PresetsOut>("/api/v1/oidc/presets"));
  const providers = useLoad(() => api.get<ProviderOut[]>("/api/v1/oidc/providers"));
  const settings = useLoad(() => api.get<AppSettings>("/api/v1/settings"));
  const [wizard, setWizard] = useState<{ provider: ProviderOut | null } | null>(null);
  const [publicUrl, setPublicUrl] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const testId = params.get("test");
  const toast = useToast();
  const confirm = useConfirm();

  const saveSetting = async (patch: Partial<AppSettings>, msg: string) => {
    try {
      settings.setData(await api.patch<AppSettings>("/api/v1/settings", patch));
      toast.ok(msg);
      void presets.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  if (presets.error) return <ErrorBanner error={presets.error} />;
  if (!presets.data || !settings.data) return <Spinner />;
  const p = presets.data;

  return (
    <div className="stack" style={{ maxWidth: 900 }}>
      {testId ? <TestResultCard id={testId} providers={providers.data ?? []} onClose={() => { params.delete("test"); setParams(params); }} /> : null}
      <section className="card stack" aria-labelledby="sso-h">
        <div className="row spread">
          <h2 id="sso-h">Single sign-on (OpenID Connect)</h2>
          <button className="btn primary" onClick={() => setWizard({ provider: null })}><Icon name="plus" /> Add provider</button>
        </div>
        <p className="small muted" style={{ margin: 0 }}>
          Let admins sign in with Okta, Microsoft Entra ID, Google, ADFS or any OpenID Connect provider. Admin accounts are created on
          first sign-in for people your access rules allow, and linked to the provider account (not the email).
        </p>
        {!p.https ? (
          <div className="banner warn small"><Icon name="alert" /><div>
            WooSarr isn't reached over HTTPS here. Okta, Entra, Google and ADFS only accept <code>https://</code> redirect URIs (except{" "}
            <code>localhost</code>). Put WooSarr behind an HTTPS reverse proxy and set the public URL below.
          </div></div>
        ) : null}
        <Field label="Public URL" hint="How browsers reach WooSarr, e.g. https://woosarr.example.com. Leave empty to use the current address. The redirect URI is built from it.">
          <div className="row">
            <input type="text" className="inline-input" style={{ width: 360 }} placeholder="https://woosarr.example.com"
              value={publicUrl ?? settings.data.public_url ?? ""} onChange={(e) => setPublicUrl(e.target.value)} />
            <button className="btn sm" disabled={publicUrl === null} onClick={() => void saveSetting({ public_url: (publicUrl ?? "").trim().replace(/\/$/, "") }, "Public URL saved.").then(() => setPublicUrl(null))}>Save</button>
          </div>
        </Field>
        <div className="small">Redirect URI for every provider: <CopyBox value={p.redirect_uri} /></div>
      </section>

      <section className="card stack" aria-labelledby="providers-h">
        <h2 id="providers-h">Providers</h2>
        <ErrorBanner error={providers.error} />
        {providers.data?.length === 0 ? <p className="muted small">No providers yet.</p> : null}
        {providers.data?.map((pr) => (
          <div key={pr.id} className="row spread" style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
            <div>
              <strong>{KIND_ICON[pr.kind]} {pr.name}</strong> <span className="small muted">{p.presets.find((x) => x.kind === pr.kind)?.label} · {pr.issuer}</span>
              <div className="row small" style={{ marginTop: 4 }}>
                {pr.enabled ? <Badge tone="ok">Enabled</Badge> : <Badge tone="neutral">Disabled</Badge>}
                {!pr.client_secret_set ? <Badge tone="warn">No client secret</Badge> : null}
                <span className="muted">{pr.admins} admin{pr.admins === 1 ? "" : "s"} · last sign-in {ago(pr.last_login_at)}</span>
              </div>
            </div>
            <div className="row">
              <button className="btn sm" onClick={async () => {
                try {
                  const r = await api.post<{ url: string }>(`/api/v1/oidc/providers/${pr.id}/test`);
                  window.location.href = r.url;
                } catch (e) { toast.error(e); }
              }}><Icon name="eye" size={14} /> Test sign-in</button>
              <button className="btn sm" onClick={() => setWizard({ provider: pr })}>Edit</button>
              <button className="btn sm danger" aria-label={`Delete ${pr.name}`} title={`Delete ${pr.name}`} onClick={async () => {
                if (await confirm({ title: `Delete ${pr.name}?`, body: <p>Admins who sign in only with {pr.name} won't be able to sign in any more (their accounts stay; reset a password to use them locally).</p>, confirmLabel: "Delete", danger: true })) {
                  try { await api.del(`/api/v1/oidc/providers/${pr.id}`); void providers.reload(); } catch (e) { toast.error(e); }
                }
              }}><Icon name="trash" size={14} /></button>
            </div>
          </div>
        ))}
      </section>

      <section className="card stack" aria-labelledby="local-h">
        <h2 id="local-h">Password login</h2>
        <Switch checked={settings.data.local_login_enabled ?? true} label="Allow signing in with a WooSarr username and password"
          onChange={(v) => void saveSetting({ local_login_enabled: v }, v ? "Password login enabled." : "Password login disabled; admins use single sign-on.")} />
        <p className="small muted" style={{ margin: 0 }}>
          Turning it off is only possible after an admin has signed in with single sign-on, and the last working provider can't be
          disabled while it's off. Locked out? Run <code>python -m woosarr.admin reset-password &lt;user&gt;</code> in the container console.
        </p>
      </section>

      {wizard ? (
        <ProviderWizard presets={p} provider={wizard.provider}
          onClose={() => setWizard(null)} onSaved={() => { setWizard(null); void providers.reload(); }} />
      ) : null}
    </div>
  );
}

function CopyBox({ value }: { value: string }) {
  const toast = useToast();
  return (
    <span className="row" style={{ display: "inline-flex", gap: 6 }}>
      <code className="secret-box" style={{ padding: "2px 8px", borderStyle: "solid" }}>{value}</code>
      <button type="button" className="btn sm" onClick={() => void navigator.clipboard?.writeText(value).then(() => toast.ok("Copied."))}>Copy</button>
    </span>
  );
}

function TestResultCard({ id, providers, onClose }: { id: string; providers: ProviderOut[]; onClose: () => void }) {
  const r = useLoad(() => api.get<TestResult>(`/api/v1/oidc/test-results/${id}`), [id]);
  const name = providers.find((p) => p.id === r.data?.provider_id)?.name ?? "the provider";
  return (
    <section className="card stack" aria-labelledby="test-h" style={{ borderColor: r.data?.ok ? "var(--ok)" : "var(--warn)" }}>
      <div className="row spread">
        <h2 id="test-h">Test sign-in with {name}</h2>
        <button className="btn sm" onClick={onClose}>Close</button>
      </div>
      <ErrorBanner error={r.error} />
      {r.data ? (
        <>
          {r.data.error ? <div className="banner danger small"><Icon name="alert" /><div>{r.data.error}</div></div> : (
            r.data.ok ? <Badge tone="ok">This account would be allowed in — {r.data.reason}</Badge>
              : <Badge tone="danger">This account would be refused — {r.data.reason}</Badge>
          )}
          {!r.data.error ? (
            <dl className="small" style={{ margin: 0, display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 12px" }}>
              <dt className="muted">Subject</dt><dd style={{ margin: 0 }}><code>{r.data.subject}</code></dd>
              <dt className="muted">Username</dt><dd style={{ margin: 0 }}>{r.data.username ?? "—"}</dd>
              <dt className="muted">Email</dt><dd style={{ margin: 0 }}>{r.data.email ?? "—"}</dd>
              <dt className="muted">Groups</dt><dd style={{ margin: 0 }}>{r.data.groups.length ? r.data.groups.join(", ") : "none received"}</dd>
            </dl>
          ) : null}
          {Object.keys(r.data.claims).length ? (
            <details className="small"><summary>All claims received</summary>
              <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{JSON.stringify(r.data.claims, null, 2)}</pre>
            </details>
          ) : null}
        </>
      ) : <Spinner />}
    </section>
  );
}

const STEPS = ["Provider", "Before you start", "Connection", "Who can sign in", "Test & enable"];

function ProviderWizard({ presets, provider, onClose, onSaved }: {
  presets: PresetsOut; provider: ProviderOut | null; onClose: () => void; onSaved: () => void;
}) {
  const [step, setStep] = useState(provider ? 2 : 0);
  const [kind, setKind] = useState<string>(provider?.kind ?? "okta");
  const preset = presets.presets.find((p) => p.kind === kind) as PresetOut;
  const [saved, setSaved] = useState<ProviderOut | null>(provider);
  const [form, setForm] = useState(() => initial(provider, preset));
  const [discovery, setDiscovery] = useState<DiscoveryOut | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (!provider) setForm(initial(null, preset));
  }, [kind]); // a new provider takes the preset's defaults

  const set = (patch: Partial<typeof form>) => setForm({ ...form, ...patch });
  const body = (enabled: boolean): ProviderIn => ({
    name: form.name, kind: kind as ProviderIn["kind"], settings: form.settings, client_id: form.client_id,
    client_secret: form.client_secret || null, scopes: form.scopes, username_claim: form.username_claim,
    email_claim: form.email_claim, groups_claim: form.groups_claim, allowed_groups: lines(form.groups),
    allowed_emails: lines(form.emails), allowed_domains: lines(form.domains),
    require_email_verified: form.require_email_verified, extra_params: form.extra_params, enabled,
  });

  const save = async (enabled: boolean): Promise<ProviderOut | null> => {
    setError(null);
    setBusy(true);
    try {
      const out = saved
        ? await api.put<ProviderOut>(`/api/v1/oidc/providers/${saved.id}`, body(enabled))
        : await api.post<ProviderOut>("/api/v1/oidc/providers", body(enabled));
      setSaved(out);
      setForm((f) => ({ ...f, client_secret: "" }));
      return out;
    } catch (e) {
      setError(e);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    const out = await save(saved?.enabled ?? false);
    if (!out) return;
    setDiscovery(await api.post<DiscoveryOut>(`/api/v1/oidc/providers/${out.id}/discover`));
  };

  const test = async () => {
    const out = await save(saved?.enabled ?? false);
    if (!out) return;
    try {
      const r = await api.post<{ url: string }>(`/api/v1/oidc/providers/${out.id}/test`);
      window.location.href = r.url;
    } catch (e) {
      setError(e);
    }
  };

  const content: ReactNode[] = [
    // 0: provider
    <div key="0" className="chooser" role="radiogroup" aria-label="Identity provider">
      {presets.presets.map((p) => (
        <label key={p.kind} className="check">
          <input type="radio" name="kind" checked={kind === p.kind} onChange={() => setKind(p.kind)} />
          <span>{KIND_ICON[p.kind]} {p.label}</span>
        </label>
      ))}
    </div>,
    // 1: instructions
    <div key="1" className="stack small">
      <h3>Requirements</h3>
      <ul style={{ margin: 0, paddingLeft: 18 }}>{preset.requirements.map((r) => <li key={r}><RichText text={r} redirectUri={presets.redirect_uri} /></li>)}</ul>
      {!presets.https ? <div className="banner warn"><Icon name="alert" /><div>This WooSarr address isn't HTTPS; {preset.label} will likely reject the redirect URI. Set the public URL first.</div></div> : null}
      <h3>Set up {preset.label}</h3>
      <div>Redirect URI: <CopyBox value={presets.redirect_uri} /></div>
      <ol className="steps">{preset.steps.map((s) => <li key={s}><RichText text={s} redirectUri={presets.redirect_uri} /></li>)}</ol>
      {preset.notes.length ? <><h3>Good to know</h3><ul style={{ margin: 0, paddingLeft: 18 }}>{preset.notes.map((n) => <li key={n}><RichText text={n} redirectUri={presets.redirect_uri} /></li>)}</ul></> : null}
    </div>,
    // 2: connection
    <div key="2" className="stack">
      <Field label="Name" hint={`Shown on the login button: "Sign in with ${form.name || preset.label}".`}>
        <input type="text" value={form.name} maxLength={100} onChange={(e) => set({ name: e.target.value })} />
      </Field>
      {preset.fields.map((f) => (
        <Field key={f.key} label={f.label} hint={f.hint}>
          <input type="text" value={form.settings[f.key] ?? ""} placeholder={f.placeholder}
            onChange={(e) => set({ settings: { ...form.settings, [f.key]: e.target.value } })} />
        </Field>
      ))}
      <Field label="Client ID">
        <input type="text" value={form.client_id} onChange={(e) => set({ client_id: e.target.value })} autoComplete="off" spellCheck={false} />
      </Field>
      <Field label="Client secret" hint={saved?.client_secret_set ? "Stored encrypted. Leave empty to keep it, or paste a new one to replace it." : "Stored encrypted; never shown again."}>
        <input type="password" value={form.client_secret} onChange={(e) => set({ client_secret: e.target.value })} autoComplete="off" spellCheck={false} />
      </Field>
      <div className="row">
        <button className="btn sm" disabled={busy || !form.client_id} onClick={() => void check()}><Icon name="refresh" size={14} /> Save & check connection</button>
        {discovery ? (discovery.ok
          ? <Badge tone="ok">Found {discovery.issuer}</Badge>
          : <Badge tone="danger">{discovery.error}</Badge>) : null}
      </div>
    </div>,
    // 3: access rules
    <div key="3" className="stack">
      <p className="small">{preset.rule_hint} Anyone matching <strong>any</strong> rule becomes an admin; nobody else can sign in.</p>
      {preset.groups_supported ? (
        <Field label={`Allowed ${form.groups_claim === "roles" ? "app roles" : "groups"}`} hint={`One per line. Matched against the "${form.groups_claim || "groups"}" claim.`}>
          <textarea rows={2} value={form.groups} onChange={(e) => set({ groups: e.target.value })} />
        </Field>
      ) : null}
      <Field label="Allowed email addresses" hint="One per line.">
        <textarea rows={2} value={form.emails} onChange={(e) => set({ emails: e.target.value })} />
      </Field>
      <Field label={kind === "google" ? "Allowed Google Workspace domains" : "Allowed email domains"} hint={kind === "google" ? "Checked against Google's verified hosted-domain (hd) claim." : "e.g. example.com"}>
        <textarea rows={1} value={form.domains} onChange={(e) => set({ domains: e.target.value })} />
      </Field>
      <details className="small">
        <summary>Advanced: claims and scopes</summary>
        <div className="stack" style={{ marginTop: 8 }}>
          <Field label="Scopes"><input type="text" value={form.scopes} onChange={(e) => set({ scopes: e.target.value })} /></Field>
          <Field label="Username claim" hint="Used for the admin's WooSarr username."><input type="text" value={form.username_claim} onChange={(e) => set({ username_claim: e.target.value })} /></Field>
          <Field label="Email claim"><input type="text" value={form.email_claim} onChange={(e) => set({ email_claim: e.target.value })} /></Field>
          {preset.groups_supported ? <Field label="Groups claim" hint="Dotted paths work, e.g. realm_access.roles"><input type="text" value={form.groups_claim} onChange={(e) => set({ groups_claim: e.target.value })} /></Field> : null}
          <label className="check"><input type="checkbox" checked={form.require_email_verified} onChange={(e) => set({ require_email_verified: e.target.checked })} />
            <span>Require <code>email_verified</code> for email and domain rules</span></label>
        </div>
      </details>
    </div>,
    // 4: test & enable
    <div key="4" className="stack small">
      <p>
        <strong>Test sign-in</strong> sends you to {preset.label} and back, then shows the claims WooSarr received and whether this account
        would be allowed in. It doesn't sign you in or create an admin.
      </p>
      <div className="row">
        <button className="btn" disabled={busy} onClick={() => void test()}><Icon name="eye" /> Save & test sign-in</button>
      </div>
      <p className="muted">When the test looks right, enable the provider: the login page then shows "Sign in with {form.name || preset.label}".</p>
    </div>,
  ];

  return (
    <Modal title={provider ? `Edit ${provider.name}` : `Add ${step === 0 ? "a provider" : preset.label}`} onClose={onClose} actions={
      <>
        {step > (provider ? 1 : 0) ? <button className="btn" onClick={() => setStep(step - 1)}>Back</button> : null}
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onClose}>Close</button>
        {step < STEPS.length - 1 ? (
          <button className="btn primary" disabled={step === 2 && (!form.name || !form.client_id)} onClick={async () => {
            if (step >= 2 && !(await save(saved?.enabled ?? false))) return;
            setStep(step + 1);
          }}>Next</button>
        ) : (
          <>
            {saved?.enabled ? <button className="btn" disabled={busy} onClick={async () => { if (await save(false)) { toast.ok("Disabled."); onSaved(); } }}>Disable</button> : null}
            <button className="btn primary" disabled={busy} onClick={async () => { if (await save(true)) { toast.ok("Enabled: the login page offers it now."); onSaved(); } }}>{saved?.enabled ? "Save" : "Enable"}</button>
          </>
        )}
      </>
    }>
      <ol className="wizard-steps small" aria-label="Steps">
        {STEPS.map((s, i) => <li key={s} aria-current={i === step ? "step" : undefined} className={i === step ? "on" : i < step ? "done" : ""}>{s}</li>)}
      </ol>
      <ErrorBanner error={error} />
      {content[step]}
    </Modal>
  );
}

function initial(p: ProviderOut | null, preset: PresetOut) {
  return {
    name: p?.name ?? preset.label,
    settings: (p?.settings as Record<string, string>) ?? (preset.kind === "okta" ? { auth_server: "default" } : {}),
    client_id: p?.client_id ?? "",
    client_secret: "",
    scopes: p?.scopes ?? preset.scopes,
    username_claim: p?.username_claim ?? preset.username_claim,
    email_claim: p?.email_claim ?? preset.email_claim,
    groups_claim: p?.groups_claim ?? preset.groups_claim,
    groups: (p?.allowed_groups ?? []).join("\n"),
    emails: (p?.allowed_emails ?? []).join("\n"),
    domains: (p?.allowed_domains ?? []).join("\n"),
    require_email_verified: p?.require_email_verified ?? preset.require_email_verified,
    extra_params: (p?.extra_params as Record<string, string>) ?? {},
  };
}
