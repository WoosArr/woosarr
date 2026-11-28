import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { api, type PublicProvider } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../icons";
import { ErrorBanner, Field } from "../ui";
import { ssoErrorMessage } from "../ssoErrors";
import { readExportFile, setPendingImport } from "./ConfigTransfer";

function Brand() {
  return (
    <div className="brand">
      <img src="/logo-192.webp" alt="" width={96} height={96} />
      WooSarr
    </div>
  );
}

export function LoginScreen() {
  const { login, status } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(() => {
    const sso = ssoErrorMessage(window.location.search);
    return sso ? new Error(sso) : null;
  });
  const [providers, setProviders] = useState<PublicProvider[]>([]);
  useEffect(() => {
    api.get<PublicProvider[]>("/api/v1/auth/oidc/providers").then(setProviders, () => setProviders([]));
    if (window.location.search.includes("sso_error")) window.history.replaceState(null, "", window.location.pathname);
  }, []);
  const localEnabled = status?.local_login_enabled ?? true;
  const next = encodeURIComponent(window.location.pathname === "/" ? "/" : window.location.pathname);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(err);
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth-wrap">
      <form className="card auth-card stack" onSubmit={submit} aria-labelledby="login-title">
        <Brand />
        <h1 id="login-title" className="visually-hidden">Sign in</h1>
        <ErrorBanner error={error} />
        {providers.map((p) => (
          <a key={p.slug} className="btn" style={{ width: "100%" }} href={`/api/v1/auth/oidc/${p.slug}/login?next=${next}`}>
            <Icon name="shieldCheck" /> Sign in with {p.name}
          </a>
        ))}
        {localEnabled ? (
          <>
            {providers.length ? <div className="row small muted" style={{ justifyContent: "center" }}>or</div> : null}
            <label className="field">
              Username
              <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </label>
            <label className="field">
              Password
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </label>
            <button className="btn primary" type="submit" disabled={busy} style={{ width: "100%" }}>
              <Icon name="lock" /> {busy ? "Signing in…" : "Sign in"}
            </button>
          </>
        ) : !providers.length ? (
          <div className="banner warn small"><Icon name="alert" /><div>Password login is off and no sign-in provider is available. Use the recovery command in the container console.</div></div>
        ) : null}
      </form>
    </main>
  );
}

export function SetupScreen() {
  const { setupAdmin, status } = useAuth();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [restore, setRestore] = useState<{ name: string; doc: unknown } | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const mismatch = confirm.length > 0 && confirm !== password;

  const pickRestore = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    setRestore(null);
    setRestoreError(null);
    if (!file) return;
    const read = await readExportFile(file);
    if ("error" in read) setRestoreError(read.error); else setRestore({ name: file.name, doc: read.doc });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (mismatch || restoreError) return;
    setBusy(true);
    setError(null);
    const from = window.location.pathname + window.location.search;
    if (restore) {
      // The router mounts once the admin exists and opens wherever the address points: review and
      // import the file on Settings → Export / Import.
      setPendingImport(restore);
      window.history.replaceState(null, "", "/settings?tab=transfer");
    }
    try {
      await setupAdmin(username, password, code);
    } catch (err) {
      if (restore) {
        setPendingImport(null);
        window.history.replaceState(null, "", from);
      }
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth-wrap">
      <form className="card auth-card stack" onSubmit={submit} aria-labelledby="setup-title">
        <Brand />
        <h1 id="setup-title">Welcome! Create the first admin</h1>
        <p className="muted small">
          This account manages WooSarr. Once it exists, this page is gone for good; more admins can be added under
          Settings.
        </p>
        <ErrorBanner error={error} />
        {status?.setup_code_required ? (
          <Field
            label="Setup code"
            hint={<>Printed in the container log at startup (Unraid: Docker tab → WooSarr icon → Logs). Looks like <code>1a2b-3c4d-5e6f</code>.</>}
          >
            <input type="text" value={code} onChange={(e) => setCode(e.target.value)} required autoComplete="one-time-code" spellCheck={false} />
          </Field>
        ) : null}
        <label className="field">
          Username
          <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} maxLength={64} pattern="[A-Za-z0-9._@\-]+" />
        </label>
        <Field label="Password" hint="At least 12 characters. A passphrase works well.">
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={12} />
        </Field>
        <Field label="Confirm password" error={mismatch ? "Passwords don't match." : null}>
          <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
        </Field>
        <details>
          <summary className="small">Moving from another WooSarr server? Restore from an export</summary>
          <div className="stack" style={{ marginTop: 8 }}>
            <p className="muted small" style={{ margin: 0 }}>
              Choose a file made with Settings → Export / Import. After the admin above is created you'll see what it would
              change, and choose what to import.
            </p>
            <Field label="Export file" error={restoreError}>
              <input type="file" accept=".json,application/json" onChange={pickRestore} />
            </Field>
          </div>
        </details>
        <button className="btn primary" type="submit" disabled={busy || mismatch || Boolean(restoreError)} style={{ width: "100%" }}>
          {busy ? "Creating…" : restore ? "Create admin and review the import" : "Create admin and sign in"}
        </button>
      </form>
    </main>
  );
}
