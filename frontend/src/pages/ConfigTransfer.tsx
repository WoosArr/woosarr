import { useState, type FormEvent } from "react";
import { api, type ExportParts } from "../api";
import { Icon } from "../icons";
import { ErrorBanner, Field, Spinner, Switch, useLoad, useToast } from "../ui";

// Settings → Export: the file is built by the server and handed straight to the browser as a
// download; nothing is kept on the server (D23).
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
