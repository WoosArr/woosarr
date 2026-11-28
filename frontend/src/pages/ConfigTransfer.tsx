import { useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type ExportParts, type ImportPreview, type ImportResult } from "../api";
import { Icon } from "../icons";
import { ErrorBanner, Field, Spinner, Switch, useConfirm, useLoad, useToast } from "../ui";

// Settings → Export / Import (D23). Export: the server builds the file and hands it straight to the
// browser as a download; nothing is kept on the server. Import: the browser reads the file, the server
// previews what it would change, then applies the chosen parts.
const PART_INFO: Record<string, [string, string]> = {
  settings: ["Settings", "Everything under Settings, and the Seerr address."],
  libraries: ["Libraries and role mappings", "Which Discord roles give which libraries, and exempt libraries."],
  manual_grants: ["Manual grants", "Libraries given to Plex accounts by hand."],
  reaction_posts: ["Reaction posts", "Text, emoji, roles and buttons, and where each post is."],
  sso: ["Single sign-on", "Providers, access rules, and which admin each provider account signs in as."],
  links: ["Member links", "Which Discord member uses which Plex account."],
  admins: ["Admin accounts", "Usernames and how they sign in. Passwords only travel with the secrets."],
  requests: ["Request history", "Requests made from Discord and their Seerr status."],
  audit: ["Audit history", "Every entry on Logs → Audit trail."],
};

export function saveFile(name: string, text: string, type = "application/json") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export default function ConfigTransfer() {
  return (
    <div className="stack" style={{ gap: 16 }}>
      <ExportCard />
      <ImportCard />
    </div>
  );
}

function ExportCard() {
  const info = useLoad(() => api.get<ExportParts>("/api/v1/config/export/parts"));
  const [off, setOff] = useState<Set<string>>(new Set());
  const [secrets, setSecrets] = useState(false);
  const [pass, setPass] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  if (info.error) return <ErrorBanner error={info.error} />;
  if (!info.data) return <Spinner />;
  const min = info.data.passphrase_min;
  const chosen = info.data.parts.map((p) => p.name).filter((n) => !off.has(n));
  const passError = !secrets ? null : pass.length < min ? `At least ${min} characters.` : pass !== again ? "The two don't match." : null;
  const toggle = (name: string) => setOff((s) => {
    const next = new Set(s);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  const download = async (e: FormEvent) => {
    e.preventDefault();
    if (!chosen.length || passError) return;
    setBusy(true);
    try {
      const doc = await api.post<unknown>("/api/v1/config/export",
        { parts: chosen, include_secrets: secrets, ...(secrets ? { passphrase: pass } : {}) });
      saveFile(`woosarr-config-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(doc, null, 2));
      setPass("");
      setAgain("");
      toast.ok("Export downloaded.");
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card stack" style={{ maxWidth: 720 }} onSubmit={download}>
      <h2 style={{ margin: 0 }}>Export</h2>
      <p className="small">
        Download this server's setup as a file, to import into another WooSarr server. Choose as much or as little as you
        want. The file goes straight to your browser; nothing is kept on the server.
      </p>
      <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="small"><strong>What to include</strong></legend>
        {info.data.parts.map((p) => {
          const [label, hint] = PART_INFO[p.name] ?? [p.name, ""];
          return (
            <label key={p.name} className="row" style={{ alignItems: "flex-start", gap: 8 }}>
              <input type="checkbox" checked={!off.has(p.name)} onChange={() => toggle(p.name)} style={{ marginTop: 3 }} />
              <span>
                <strong>{label}</strong>{p.name !== "settings" ? <span className="muted small"> ({p.count})</span> : null}
                <br /><span className="muted small">{hint}</span>
              </span>
            </label>
          );
        })}
      </fieldset>
      <Switch checked={secrets} onChange={setSecrets} label={<><strong>Include tokens and secrets</strong> <span className="muted small">— Plex and Discord tokens, Seerr key{off.has("sso") ? "" : ", single sign-on client secrets"}{off.has("admins") ? "" : ", admin passwords"}, encrypted with a passphrase</span></>} />
      {secrets ? (
        <>
          <Field label="Passphrase" hint={`At least ${min} characters. You need it to import the secrets; it isn't stored anywhere.`}>
            <input type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} style={{ maxWidth: 360 }} />
          </Field>
          <Field label="Passphrase again" error={pass && again ? passError : null}>
            <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} style={{ maxWidth: 360 }} />
          </Field>
        </>
      ) : (
        <p className="small muted" style={{ margin: 0 }}>Without them, enter the Plex and Discord tokens again on the new server.</p>
      )}
      <div className="row">
        <button className="btn primary" type="submit" disabled={busy || !chosen.length || Boolean(passError)}>
          <Icon name="download" /> {busy ? "Preparing…" : "Download export"}
        </button>
        {!chosen.length ? <span className="small muted">Choose at least one part.</span> : null}
      </div>
    </form>
  );
}

const SECRETS_INFO: [string, string] = ["Tokens and secrets", "Plex and Discord tokens, Seerr key, single sign-on client secrets and admin passwords from the file."];
const MAX_IMPORT_BYTES = 25 * 1024 * 1024;

function ImportCard() {
  const [doc, setDoc] = useState<unknown>(null);
  const [fileName, setFileName] = useState("");
  const [fileError, setFileError] = useState<string | null>(null);
  const [pass, setPass] = useState("");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [off, setOff] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ImportResult | null>(null);
  const toast = useToast();
  const confirm = useConfirm();

  const pick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    setDoc(null); setPreview(null); setDone(null); setFileError(null); setOff(new Set());
    if (!file) return;
    setFileName(file.name);
    if (file.size > MAX_IMPORT_BYTES) { setFileError("The file is larger than 25 MB."); return; }
    try {
      setDoc(JSON.parse(await file.text()));
    } catch {
      setFileError("That file isn't valid JSON. Choose a file downloaded with Export.");
    }
  };

  const check = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!doc) return;
    setBusy(true);
    try {
      const p = await api.post<ImportPreview>("/api/v1/config/import/preview", { file: doc, passphrase: pass || null });
      setPreview(p);
      setDone(null);
      // Secrets only when the passphrase opened them.
      setOff((s) => { const next = new Set(s); if (!p.secrets_readable) next.add("secrets"); else next.delete("secrets"); return next; });
    } catch (err) {
      setPreview(null);
      toast.error(err);
    } finally {
      setBusy(false);
    }
  };

  const chosen = preview ? preview.available.filter((p) => !off.has(p)) : [];
  const run = async () => {
    if (!preview || !chosen.length) return;
    const names = chosen.map((p) => (p === "secrets" ? SECRETS_INFO : PART_INFO[p] ?? [p, ""])[0]).join(", ");
    const ok = await confirm({
      title: "Import into this server?",
      body: <><p>This replaces what this server has for: <strong>{names}</strong>.</p><p>Dry-run is turned on afterwards, so nothing changes on Plex until you've looked at the planned changes and turned it off.</p></>,
      confirmLabel: "Import",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api.post<ImportResult>("/api/v1/config/import", { file: doc, passphrase: pass || null, parts: chosen });
      setDone(r);
      setPass("");
      toast.ok("Configuration imported. Dry-run is on.");
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(false);
    }
  };

  const summary = new Map((done ?? preview)?.parts.map((p) => [p.name, p.summary]) ?? []);
  const src = preview?.source as { plex_server?: string | null; discord_guild?: string | null } | undefined;
  return (
    <form className="card stack" style={{ maxWidth: 720 }} onSubmit={check}>
      <h2 style={{ margin: 0 }}>Import</h2>
      <p className="small">
        Bring in a file made with Export on this or another WooSarr server. You'll see what it would change before anything is
        written, and choose which parts to import.
      </p>
      <Field label="Export file" error={fileError}>
        <input type="file" accept=".json,application/json" onChange={pick} />
      </Field>
      {doc ? (
        <>
          <Field label="Passphrase" hint="Only needed to import the tokens and secrets in the file.">
            <input type="password" autoComplete="off" value={pass} onChange={(e) => setPass(e.target.value)} style={{ maxWidth: 360 }} />
          </Field>
          <div className="row">
            <button className="btn" type="submit" disabled={busy}><Icon name="search" /> {preview ? "Check again" : "Check file"}</button>
            <span className="small muted">{fileName}</span>
          </div>
        </>
      ) : null}
      {preview ? (
        <>
          <p className="small" style={{ margin: 0 }}>
            Exported {preview.exported_at ? new Date(preview.exported_at).toLocaleString() : "at an unknown time"}
            {src?.plex_server || src?.discord_guild ? <> from <strong>{[src.plex_server, src.discord_guild].filter(Boolean).join(" / ")}</strong></> : null}
            {preview.woosarr ? <> (WooSarr {preview.woosarr})</> : null}.
          </p>
          {preview.warnings.length ? (
            <div className="banner warn small" role="status">
              <Icon name="alert" />
              <ul style={{ margin: 0, paddingLeft: 18 }}>{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </div>
          ) : null}
          <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="small"><strong>What to import</strong></legend>
            {preview.available.map((name) => {
              const [label, hint] = name === "secrets" ? SECRETS_INFO : PART_INFO[name] ?? [name, ""];
              const locked = name === "secrets" && !preview.secrets_readable;
              return (
                <label key={name} className="row" style={{ alignItems: "flex-start", gap: 8 }}>
                  <input type="checkbox" checked={!off.has(name)} disabled={locked || Boolean(done)} style={{ marginTop: 3 }}
                    onChange={() => setOff((s) => { const next = new Set(s); if (next.has(name)) next.delete(name); else next.add(name); return next; })} />
                  <span>
                    <strong>{label}</strong>
                    {summary.get(name) ? <span className="small"> — {summary.get(name)}</span> : null}
                    <br /><span className="muted small">{locked ? "Enter the passphrase and check again to import these." : hint}</span>
                  </span>
                </label>
              );
            })}
          </fieldset>
          {done ? (
            <div className="banner info small" role="status">
              <Icon name="check" />
              <div>
                Imported. Dry-run is on: look at the planned changes on the <Link to="/">Dashboard</Link>, then turn dry-run off
                under <Link to="/settings?tab=general">Settings → General</Link>.
                {done.reconnecting.length ? <> Connecting with the imported {done.reconnecting.map((r) => (r === "plex" ? "Plex token" : "Discord bot token")).join(" and ")}.</> : null}
              </div>
            </div>
          ) : (
            <div className="row">
              <button className="btn primary" type="button" onClick={run} disabled={busy || !chosen.length}>
                <Icon name="upload" /> Import
              </button>
              {!chosen.length ? <span className="small muted">Choose at least one part.</span> : null}
            </div>
          )}
        </>
      ) : busy ? <Spinner /> : null}
    </form>
  );
}
