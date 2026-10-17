import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  type DiscordStatus,
  type GuildOut,
  type PlexLoginPoll,
  type PlexLoginStart,
  type PlexServerOut,
  type PlexStatus,
  type UserOut,
} from "../api";
import { Icon } from "../icons";
import { Badge, ErrorBanner, Field, Modal, PageHead, Spinner, useConfirm, useLoad, useToast } from "../ui";

export default function SetupPage() {
  const [params] = useSearchParams();
  // Plex sends its sign-in popup back to /setup?plex=signed-in.
  if (params.get("plex") === "signed-in") return <PlexReturned />;
  return (
    <>
      <PageHead title="Setup" sub="Connect WooSarr to your Plex server and your Discord server." />
      <div className="grid cols-2">
        <PlexCard />
        <DiscordCard />
      </div>
      <p className="small muted" style={{ marginTop: 16 }}>
        WooSarr stays in dry-run until you turn it off in <Link to="/settings">Settings</Link>, so you can check the
        dashboard's pending changes first.
      </p>
    </>
  );
}

function PlexReturned() {
  useEffect(() => {
    const t = setTimeout(() => window.close(), 800);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="card" style={{ maxWidth: 480 }}>
      <h1 className="row"><Icon name="check" /> Plex sign-in complete</h1>
      <p>You can close this window and go back to the WooSarr tab.</p>
    </div>
  );
}

function Detail({ text }: { text?: string | null }) {
  return text ? (
    <div className="banner info small" role="status"><Icon name="info" /><div>{text}</div></div>
  ) : null;
}

// ---------------------------------------------------------------- Plex

function PlexCard() {
  const st = useLoad(() => api.get<PlexStatus>("/api/v1/plex/status"));
  const [changing, setChanging] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();
  const s = st.data;

  const signOut = async () => {
    const ok = await confirm({
      title: "Sign out of Plex?",
      body: <p>WooSarr signs its Plex token out and forgets the server. Syncing stops until you sign in again. Mappings and links are kept.</p>,
      confirmLabel: "Sign out",
      danger: true,
    });
    if (!ok) return;
    try {
      st.setData(await api.del<PlexStatus>("/api/v1/plex"));
      toast.ok("Signed out of Plex.");
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <section className="card stack" aria-labelledby="plex-setup">
      <div className="row spread">
        <h2 id="plex-setup" className="row"><Icon name="plex" /> Plex</h2>
        {s?.connected ? <Badge tone="ok">Connected</Badge> : s?.signed_in ? <Badge tone="warn">Needs a server</Badge> : <Badge tone="neutral">Not set up</Badge>}
      </div>
      <ErrorBanner error={st.error} />
      {!s ? <Spinner /> : !s.signed_in ? (
        <PlexSignIn onDone={() => void st.reload()} />
      ) : !s.server || changing ? (
        <PlexServerPicker current={s.server?.machine_id as string | undefined} onDone={(next) => { st.setData(next); setChanging(false); }} />
      ) : (
        <>
          <dl className="small" style={{ margin: 0 }}>
            <dt className="muted">Signed in as</dt><dd style={{ margin: "0 0 6px" }}><strong>{String(s.account?.username ?? "")}</strong></dd>
            <dt className="muted">Server</dt><dd style={{ margin: 0 }}><strong>{String(s.server.name)}</strong> <code className="small">{String(s.server.machine_id)}</code></dd>
          </dl>
          <Detail text={s.detail} />
          <PlexChecks />
          <div className="row">
            <button className="btn sm" onClick={() => setChanging(true)}>Change server</button>
            <button className="btn sm danger" onClick={() => void signOut()}><Icon name="logout" size={14} /> Sign out of Plex</button>
          </div>
        </>
      )}
    </section>
  );
}

function PlexSignIn({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const stop = useRef(false);
  const toast = useToast();
  useEffect(() => () => { stop.current = true; }, []);

  const signIn = async () => {
    setError(null);
    // Open the window during the click so popup blockers allow it.
    const popup = window.open("about:blank", "woosarr-plex", "width=800,height=760");
    setBusy(true);
    try {
      const start = await api.post<PlexLoginStart>("/api/v1/plex/login");
      if (popup) popup.location.href = start.auth_url;
      else window.open(start.auth_url, "_blank", "noopener");
      stop.current = false;
      for (let i = 0; i < 300 && !stop.current; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const poll = await api.post<PlexLoginPoll>("/api/v1/plex/login/poll", { pin_id: start.pin_id });
        if (poll.state === "done") {
          toast.ok(`Signed in to Plex as ${String(poll.account?.username ?? "")}.`);
          onDone();
          return;
        }
        if (poll.state === "expired") throw new Error("The Plex sign-in expired. Try again.");
      }
    } catch (e) {
      setError(e);
      popup?.close();
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="small">
        Sign in with the Plex account that <strong>owns</strong> the server. You approve access on plex.tv; WooSarr never
        sees your password. The sign-in is stored encrypted.
      </p>
      <ErrorBanner error={error} />
      <div className="row">
        <button className="btn primary" onClick={() => void signIn()} disabled={busy}>
          <Icon name="plex" /> {busy ? "Waiting for Plex…" : "Sign in with Plex"}
        </button>
        {busy ? <span className="small muted">Finish signing in in the Plex window.</span> : null}
      </div>
    </>
  );
}

function PlexServerPicker({ current, onDone }: { current?: string; onDone: (s: PlexStatus) => void }) {
  const servers = useLoad(() => api.get<PlexServerOut[]>("/api/v1/plex/servers"));
  const [pick, setPick] = useState<string | undefined>(current);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const save = async () => {
    if (!pick) return;
    setBusy(true);
    try {
      onDone(await api.put<PlexStatus>("/api/v1/plex/server", { machine_id: pick }));
      toast.ok("Server saved. WooSarr is reading its libraries and users.");
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0 }} className="stack">
      <legend className="small"><strong>Choose your server</strong></legend>
      <ErrorBanner error={servers.error} />
      {servers.loading && !servers.data ? <Spinner label="Finding servers" /> : null}
      {servers.data?.length === 0 ? <p className="small muted">No Plex Media Server owned by this account. Sign in with the account that owns the server.</p> : null}
      {servers.data?.map((s) => (
        <label key={s.machine_id} className="check">
          <input type="radio" name="plex-server" value={s.machine_id} checked={pick === s.machine_id} onChange={() => setPick(s.machine_id)} />
          <span>
            <strong>{s.name}</strong> <span className="small muted">{s.platform} · {s.version}</span>{" "}
            {s.online ? <Badge tone="ok">Online</Badge> : <Badge tone="neutral">Offline</Badge>}{" "}
          </span>
        </label>
      ))}
      <div><button className="btn primary" disabled={!pick || busy} onClick={() => void save()}>Use this server</button></div>
    </fieldset>
  );
}

interface ReadCheck {
  libraries: { library_id: string; title: string }[];
  users: number;
  shares: { username: string; libraries: string[] }[];
  unknown_library_keys: string[];
  errors: string[];
}

interface WriteCheck {
  passed: boolean;
  steps: { step: string; ok: boolean; settings_unchanged: boolean; expected: string[]; got: string[] }[];
}

function PlexChecks() {
  const [read, setRead] = useState<ReadCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [writing, setWriting] = useState(false);
  const toast = useToast();
  const runRead = async () => {
    setBusy(true);
    try {
      setRead(await api.post<ReadCheck>("/api/v1/plex/check/read"));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <div className="row">
        <button className="btn sm" onClick={() => void runRead()} disabled={busy}><Icon name="eye" size={14} /> {busy ? "Checking…" : "Run read check"}</button>
        <button className="btn sm" onClick={() => setWriting(true)}><Icon name="shieldCheck" size={14} /> Verify sharing on a test user…</button>
      </div>
      {read ? (
        <div className="small" role="status">
          {read.errors.length || read.unknown_library_keys.length ? <Badge tone="warn">Check the details</Badge> : <Badge tone="ok">Read check passed</Badge>}{" "}
          {read.libraries.length} libraries, {read.users} users, {read.shares.filter((s) => s.libraries.length).length} with shares.
          {read.errors.map((e) => <div key={e}><code>{e}</code></div>)}
          {read.unknown_library_keys.length ? <div>Shared library keys not on the server: <code>{read.unknown_library_keys.join(", ")}</code></div> : null}
        </div>
      ) : null}
      {writing ? <WriteCheckModal onClose={() => setWriting(false)} /> : null}
    </div>
  );
}

function WriteCheckModal({ onClose }: { onClose: () => void }) {
  const users = useLoad(() => api.get<UserOut[]>("/api/v1/users"));
  const [uid, setUid] = useState("");
  const noUsersYet = users.data !== null && users.data.length === 0;
  // Right after choosing a server the first sync is still reading users: keep checking.
  useEffect(() => {
    if (!noUsersYet) return;
    const t = setInterval(() => void users.reload(), 2000);
    return () => clearInterval(t);
  }, [noUsersYet, users]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<WriteCheck | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.post<WriteCheck>("/api/v1/plex/check/write", { plex_user_id: uid, confirm: true }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Verify sharing on a test user" onClose={onClose} actions={
      result ? <button className="btn primary" onClick={onClose}>Done</button> : (
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!uid || busy} onClick={() => void run()}>{busy ? "Running…" : "Run check"}</button>
        </>
      )
    }>
      <p className="small">
        WooSarr rewrites this user's current libraries, briefly adds one more, then puts everything back exactly as it
        was, checking after each step that Plex kept the rest of their access and settings. Pick a friend who won't mind
        a library appearing for a few seconds.
      </p>
      <ErrorBanner error={error} />
      {!result && noUsersYet ? <Spinner label="WooSarr is still reading your Plex users" /> : null}
      {!result ? (
        <Field label="Test user">
          <select value={uid} onChange={(e) => setUid(e.target.value)}>
            <option value="">Choose…</option>
            {users.data?.filter((u) => !u.removed).map((u) => <option key={u.plex_user_id} value={u.plex_user_id}>{u.username}</option>)}
          </select>
        </Field>
      ) : (
        <div className="stack small" role="status">
          {result.passed ? <Badge tone="ok">Sharing behaves as expected</Badge> : <Badge tone="danger">Unexpected result: keep dry-run on and check the logs</Badge>}
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {result.steps.map((s) => (
              <li key={s.step}>
                {s.ok && s.settings_unchanged ? "✓" : "✗"} {s.step}
                {!s.ok ? <> (expected {s.expected.join(", ") || "none"}, got {s.got.join(", ") || "none"})</> : null}
                {!s.settings_unchanged ? <> (sync/filter settings changed)</> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- Discord

function DiscordCard() {
  const st = useLoad(() => api.get<DiscordStatus>("/api/v1/discord/status"));
  const [replacing, setReplacing] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();
  const s = st.data;

  useEffect(() => {
    if (!s || s.connected || !s.configured) return;
    const t = setInterval(() => void st.reload(), 4000);
    return () => clearInterval(t);
  }, [s, st]);

  const act = async (fn: () => Promise<DiscordStatus>, msg: string) => {
    try {
      st.setData(await fn());
      toast.ok(msg);
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <section className="card stack" aria-labelledby="discord-setup">
      <div className="row spread">
        <h2 id="discord-setup" className="row"><Icon name="discord" /> Discord</h2>
        {s?.connected ? <Badge tone="ok">Connected</Badge> : s?.configured ? <Badge tone="warn">Not connected</Badge> : <Badge tone="neutral">Not set up</Badge>}
      </div>
      <ErrorBanner error={st.error} />
      {!s ? <Spinner /> : !s.bot || replacing ? (
        <DiscordToken onDone={(next) => { st.setData(next); setReplacing(false); }} onCancel={s.bot ? () => setReplacing(false) : undefined} />
      ) : !s.guild ? (
        <DiscordGuild status={s} onDone={(next) => st.setData(next)} />
      ) : (
        <>
          <dl className="small" style={{ margin: 0 }}>
            <dt className="muted">Bot</dt><dd style={{ margin: "0 0 6px" }}><strong>{String(s.bot.username)}</strong></dd>
            <dt className="muted">Server</dt><dd style={{ margin: 0 }}><strong>{String(s.guild.name ?? s.guild.id)}</strong></dd>
          </dl>
          <Detail text={s.detail} />
          <p className="small muted" style={{ margin: 0 }}>Members can now run <code>/link</code> in your server.</p>
          <div className="row">
            <button className="btn sm" onClick={() => void act(() => api.post<DiscordStatus>("/api/v1/discord/restart"), "Bot restarted.")}><Icon name="refresh" size={14} /> Restart bot</button>
            <button className="btn sm" onClick={() => setReplacing(true)}><Icon name="key" size={14} /> Replace token</button>
            {s.invite_url ? <a className="btn sm" href={s.invite_url} target="_blank" rel="noreferrer">Invite link</a> : null}
            <button className="btn sm danger" onClick={async () => {
              if (await confirm({ title: "Disconnect Discord?", body: <p>The bot stops and its token is deleted. Role changes won't sync until you set it up again.</p>, confirmLabel: "Disconnect", danger: true })) {
                await act(() => api.del<DiscordStatus>("/api/v1/discord"), "Discord disconnected.");
              }
            }}>Disconnect</button>
          </div>
        </>
      )}
    </section>
  );
}

function DiscordToken({ onDone, onCancel }: { onDone: (s: DiscordStatus) => void; onCancel?: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await api.put<DiscordStatus>("/api/v1/discord/token", { token: token.trim() });
      setToken("");
      onDone(next);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="stack" onSubmit={submit}>
      <ol className="steps small">
        <li>Open the <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Discord Developer Portal</a>, create an application and open <strong>Bot</strong>.</li>
        <li>Under <strong>Privileged Gateway Intents</strong>, turn on <strong>Server Members Intent</strong> and save.</li>
        <li>Press <strong>Reset Token</strong>, copy the token and paste it below.</li>
      </ol>
      <ErrorBanner error={error instanceof ApiError ? new Error(error.message) : error} />
      <Field label="Bot token" hint="Checked with Discord, then stored encrypted. It's never shown again.">
        <input type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} required minLength={20} />
      </Field>
      <div className="row">
        <button className="btn primary" type="submit" disabled={busy || !token.trim()}>{busy ? "Checking…" : "Save token"}</button>
        {onCancel ? <button type="button" className="btn" onClick={onCancel}>Cancel</button> : null}
      </div>
    </form>
  );
}

function DiscordGuild({ status, onDone }: { status: DiscordStatus; onDone: (s: DiscordStatus) => void }) {
  const [guilds, setGuilds] = useState<GuildOut[] | null>(null);
  const [pick, setPick] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setError(null);
    try {
      const g = await api.get<GuildOut[]>("/api/v1/discord/guilds");
      setGuilds(g);
      if (g.length === 1) setPick(g[0].id);
    } catch (e) {
      setError(e);
    }
  };
  useEffect(() => void load(), []);
  const save = async () => {
    setBusy(true);
    try {
      onDone(await api.put<DiscordStatus>("/api/v1/discord/guild", { guild_id: pick }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <p className="small">Bot <strong>{String(status.bot?.username ?? "")}</strong> is ready. Invite it to your server, then pick the server.</p>
      {status.invite_url ? <div><a className="btn" href={status.invite_url} target="_blank" rel="noreferrer"><Icon name="link" /> Invite the bot</a></div> : null}
      <ErrorBanner error={error} />
      {guilds === null ? <Spinner label="Looking for servers" /> : guilds.length === 0 ? (
        <p className="small muted">The bot isn't in any server yet. Invite it, then <button className="btn sm" onClick={() => void load()}>check again</button></p>
      ) : (
        <>
          <Field label="Discord server">
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Choose…</option>
              {guilds.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
          </Field>
          <div className="row">
            <button className="btn primary" disabled={!pick || busy} onClick={() => void save()}>Use this server</button>
            <button className="btn sm" onClick={() => void load()}>Refresh list</button>
          </div>
        </>
      )}
    </div>
  );
}
