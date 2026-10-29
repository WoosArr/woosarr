import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AppSettings, type ChannelOut, type RequestOut, type RoleOut, type SeerrStatus, type SeerrTest } from "../api";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, Field, PageHead, Spinner, Switch, ago, useConfirm, useLoad, useToast, type Tone } from "../ui";

const STATE: Record<string, [Tone, string]> = {
  pending: ["warn", "Waiting for approval"],
  approved: ["info", "Approved"],
  declined: ["danger", "Declined"],
  failed: ["danger", "Failed"],
  partially_available: ["accent", "Partly available"],
  available: ["ok", "Available"],
  deleted: ["neutral", "Removed in Seerr"],
};

// "in 3h": when an automatic retry is due (the next check after it, every 30 minutes).
function due(iso: string): string {
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (s < 60) return "at the next check";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  return `in ${Math.round(s / 3600)}h`;
}

export default function RequestsPage() {
  const seerr = useLoad(() => api.get<SeerrStatus>("/api/v1/requests/seerr"));
  const list = useLoad(() => api.get<RequestOut[]>("/api/v1/requests"));
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const pollNow = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ notified: number }>("/api/v1/requests/poll");
      await list.reload();
      toast.ok(r.notified ? `Checked with Seerr; ${r.notified} member${r.notified === 1 ? "" : "s"} notified.` : "Checked with Seerr.");
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const [retrying, setRetrying] = useState<number | null>(null);
  const retry = async (r: RequestOut) => {
    setRetrying(r.id);
    try {
      const next = await api.post<RequestOut>(`/api/v1/requests/${r.id}/retry`);
      list.setData((list.data ?? []).map((x) => (x.id === r.id ? next : x)));
      toast.ok(`Seerr is sending ${r.title} to ${r.media_type === "movie" ? "Radarr" : "Sonarr"} again.`);
    } catch (e) {
      toast.error(e);
      await list.reload();
    } finally {
      setRetrying(null);
    }
  };
  const name = seerr.data?.command_registered;
  return (
    <>
      <PageHead title="Requests" sub="Movies and shows members requested in Discord, made through Seerr as their own Seerr user.">
        {seerr.data?.configured ? (
          <button className="btn" disabled={busy} onClick={() => void pollNow()}><Icon name="refresh" size={15} /> Check status now</button>
        ) : null}
      </PageHead>
      <ErrorBanner error={seerr.error ?? list.error} />
      {seerr.data && !seerr.data.configured ? (
        <div className="banner info" role="status">
          <Icon name="info" />
          <div>Connect Seerr on the <Link to="/setup">Integrations</Link> page to turn on requests in Discord.</div>
        </div>
      ) : seerr.data ? (
        <p className="small muted">
          {name ? <>Members request with <code>/{name}</code>. </> : <>The request command registers when the bot is connected. </>}
          Who may request and the command's name are in <Link to="/settings?tab=requests">Settings → Requests</Link>.
          Open requests are checked every 30 minutes, however old; failed and declined ones for 30 days, in case they're retried or approved in Seerr.
          Failed requests are retried automatically after 1, 6 and 24 hours (<Link to="/settings?tab=requests">setting</Link>), or retry one now with <strong>Retry</strong>.
          Members are told by DM, or by a reply in the requests channel for public requests.
          Available and removed requests are listed for 7 days.
        </p>
      ) : null}
      {!list.data ? <Spinner /> : list.data.length === 0 ? (
        <Empty title="No requests to show">Requests made with the Discord command show up here: open ones until they're done, failed and declined ones for 30 days, the rest for 7 days.</Empty>
      ) : (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Requests">
          <table>
            <thead><tr><th>Title</th><th>Member</th><th>Status</th><th>Requested</th><th>Checked</th></tr></thead>
            <tbody>
              {list.data.map((r) => {
                const [tone, label] = STATE[r.status] ?? ["neutral", r.status];
                return (
                  <tr key={r.id}>
                    <td>
                      <strong>{r.title}</strong>{r.year ? <span className="muted"> ({r.year})</span> : null}
                      <div className="small muted">
                        {r.media_type === "movie" ? "Movie" : "TV show"}
                        {r.media_type === "tv" ? (r.seasons.length ? ` · seasons ${r.seasons.join(", ")}` : " · all missing seasons") : ""}
                        {" · "}Seerr #{r.seerr_request_id}
                      </div>
                    </td>
                    <td>
                      {r.discord_name ?? r.discord_user_id}
                      {r.plex_username ? <div className="small muted">Plex: {r.plex_username}</div> : null}
                    </td>
                    <td>
                      <Badge tone={tone}>{label}</Badge>
                      {r.status === "failed" ? (
                        <div className="small muted">
                          {r.next_retry_at ? `Retrying ${due(r.next_retry_at)}` : r.retries ? "Not retrying again" : "Not retried automatically"}
                          {r.retries ? ` · retried ${r.retries}×` : ""}
                        </div>
                      ) : null}
                      {r.public ? <div className="small muted">Posted publicly</div> : null}
                      {r.status === "failed" ? (
                        <button className="btn sm" style={{ marginTop: 4 }} disabled={retrying !== null} onClick={() => void retry(r)} aria-label={`Retry ${r.title}`}>
                          <Icon name="refresh" size={14} /> {retrying === r.id ? "Retrying…" : "Retry"}
                        </button>
                      ) : null}
                    </td>
                    <td className="small">{ago(r.created_at)}</td>
                    <td className="small">{ago(r.checked_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Setup card

export function SeerrCard() {
  const st = useLoad(() => api.get<SeerrStatus>("/api/v1/requests/seerr"));
  const [editing, setEditing] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();
  const s = st.data;
  return (
    <section className="card stack" aria-labelledby="seerr-setup">
      <div className="row spread">
        <h2 id="seerr-setup" className="row"><Icon name="download" /> Seerr (requests)</h2>
        {!s ? <Badge tone="neutral">{st.error ? "Couldn't check" : "Checking…"}</Badge> : s.configured ? (s.connected ? <Badge tone="ok">Connected</Badge> : <Badge tone="warn">Problem</Badge>) : <Badge tone="neutral">Optional</Badge>}
      </div>
      <ErrorBanner error={st.error} />
      {!s ? <Spinner /> : !s.configured || editing ? (
        <SeerrForm current={s} onDone={(next) => { st.setData(next); setEditing(false); }} onCancel={s.configured ? () => setEditing(false) : undefined} />
      ) : (
        <>
          <dl className="small" style={{ margin: 0 }}>
            <dt className="muted">Address</dt><dd style={{ margin: "0 0 6px" }}><code>{s.url}</code></dd>
            <dt className="muted">Version</dt><dd style={{ margin: "0 0 6px" }}>{s.version || "?"}</dd>
            <dt className="muted">API key belongs to</dt><dd style={{ margin: 0 }}>{s.admin}</dd>
          </dl>
          {s.detail ? <div className="banner warn small" role="status"><Icon name="alert" /><div>{s.detail}</div></div> : null}
          <p className="small muted" style={{ margin: 0 }}>
            {s.command_registered ? <>Members can request with <code>/{s.command_registered}</code>. </> : null}
            Settings for requests are in <Link to="/settings?tab=requests">Settings → Requests</Link>; requests are listed on the <Link to="/requests">Requests</Link> page.
          </p>
          <div className="row">
            <button className="btn sm" onClick={() => setEditing(true)}><Icon name="key" size={14} /> Change address or key</button>
            <button className="btn sm danger" onClick={async () => {
              if (!(await confirm({ title: "Disconnect Seerr?", body: <p>The request command is removed from Discord and the API key is deleted. Past requests stay in Seerr.</p>, confirmLabel: "Disconnect", danger: true }))) return;
              try {
                st.setData(await api.del<SeerrStatus>("/api/v1/requests/seerr"));
                toast.ok("Seerr disconnected.");
              } catch (e) {
                toast.error(e);
              }
            }}>Disconnect</button>
          </div>
        </>
      )}
    </section>
  );
}

function SeerrForm({ current, onDone, onCancel }: { current: SeerrStatus; onDone: (s: SeerrStatus) => void; onCancel?: () => void }) {
  const [url, setUrl] = useState(current.url ?? "");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"" | "test" | "save">("");
  const [error, setError] = useState<unknown>(null);
  const [tested, setTested] = useState<SeerrTest | null>(null);
  const body = () => ({ url: url.trim(), api_key: key.trim() || null });
  const run = async (kind: "test" | "save") => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "test") setTested(await api.post<SeerrTest>("/api/v1/requests/seerr/test", body()));
      else {
        onDone(await api.put<SeerrStatus>("/api/v1/requests/seerr", body()));
        setKey("");
      }
    } catch (e) {
      setTested(null);
      setError(e instanceof ApiError ? new Error(e.message) : e);
    } finally {
      setBusy("");
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run("save");
  };
  const needKey = !current.configured;
  return (
    <form className="stack" onSubmit={submit}>
      <p className="small muted" style={{ margin: 0 }}>
        Members request movies and shows from Discord; WooSarr sends them to Seerr (Overseerr and Jellyseerr work too) as the member's own Seerr user, so Seerr's permissions, quotas and auto-approval apply.
      </p>
      <ol className="steps small">
        <li>In Seerr, open <strong>Settings → General</strong> and copy the <strong>API Key</strong>.</li>
        <li>Enter the address WooSarr can reach Seerr on (usually <code>http://&lt;server-ip&gt;:5055</code>), not a login portal.</li>
      </ol>
      <ErrorBanner error={error} />
      <Field label="Seerr address">
        <input type="url" inputMode="url" placeholder="http://192.168.1.10:5055" value={url} onChange={(e) => { setUrl(e.target.value); setTested(null); }} required />
      </Field>
      <Field label="API key" hint={needKey ? "Checked with Seerr, then stored encrypted. It's never shown again." : "Leave empty to keep the stored key."}>
        <input type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => { setKey(e.target.value); setTested(null); }} required={needKey} minLength={16} />
      </Field>
      {tested ? (
        <div className="banner ok small" role="status">
          <Icon name="check" />
          <div>Seerr {tested.version} answered; the key belongs to <strong>{tested.admin}</strong>. {tested.users} Seerr users, {tested.plex_users} from Plex.</div>
        </div>
      ) : null}
      <div className="row">
        <button type="button" className="btn" disabled={!!busy || !url.trim() || (needKey && !key.trim())} onClick={() => void run("test")}>{busy === "test" ? "Testing…" : "Test"}</button>
        <button type="submit" className="btn primary" disabled={!!busy || !url.trim() || (needKey && !key.trim())}>{busy === "save" ? "Saving…" : "Save"}</button>
        {onCancel ? <button type="button" className="btn" onClick={onCancel}>Cancel</button> : null}
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- Settings tab

const NAME = /^[a-z0-9_-]{1,32}$/;
const RESERVED = ["link", "unlink", "invite", "help"];

export function RequestSettings() {
  const s = useLoad(() => api.get<AppSettings>("/api/v1/settings"));
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const channels = useLoad(() => api.get<ChannelOut[]>("/api/v1/discord/channels"));
  const [draft, setDraft] = useState<Partial<AppSettings>>({});
  const toast = useToast();
  const confirm = useConfirm();
  if (s.error) return <ErrorBanner error={s.error} />;
  if (!s.data) return <Spinner />;
  const v = { ...s.data, ...draft } as AppSettings;
  const set = (p: Partial<AppSettings>) => setDraft({ ...draft, ...p });
  const name = v.request_command_name ?? "request";
  const nameError = !NAME.test(name)
    ? "1–32 characters: lowercase letters, numbers, - and _ (no spaces or /)."
    : RESERVED.includes(name) ? `/${name} is already one of WooSarr's commands.` : null;
  const isPublic = v.request_visibility === "public";
  const channelError = isPublic && !v.request_channel_id ? "Choose the channel for public requests." : null;
  const renamed = draft.request_command_name !== undefined && draft.request_command_name !== s.data.request_command_name;
  const save = async () => {
    if (renamed && !(await confirm({
      title: `Rename the command to /${name}?`,
      body: (
        <p>
          Discord treats a renamed command as a new one: <code>/{s.data!.request_command_name}</code> disappears and any
          permissions set for it in <strong>Server Settings → Integrations → WooSarr</strong> have to be set again for <code>/{name}</code>.
        </p>
      ),
      confirmLabel: "Rename",
    }))) return;
    try {
      s.setData(await api.patch<AppSettings>("/api/v1/settings", draft));
      setDraft({});
      toast.ok(renamed ? `Saved. Discord shows /${name} within a few seconds.` : "Settings saved.");
    } catch (e) {
      toast.error(e);
    }
  };
  const current = v.request_role_ids ?? [];
  return (
    <div className="card settings" style={{ maxWidth: 720 }}>
      <p className="small muted" style={{ margin: 0 }}>
        Requests need Seerr on the <Link to="/setup">Integrations</Link> page. Seerr's own permissions, quotas and
        auto-approval still apply to every request; these settings decide who may use the command at all.
      </p>
      <Field label="Command name" hint={<>Without the slash. Change it if another bot in your server already has <code>/request</code>. Members type <code>/{name || "…"}</code>.</>} error={nameError ?? undefined}>
        <input type="text" value={name} onChange={(e) => set({ request_command_name: e.target.value.trim().toLowerCase() })} maxLength={32} spellCheck={false} style={{ maxWidth: 240 }} />
      </Field>
      <fieldset className="plain">
        <legend className="small" style={{ fontWeight: 600 }}>Who may request</legend>
        <span className="hint small muted" style={{ display: "block", marginBottom: 6 }}>
          Only members with a linked Plex account can request, so every request belongs to their Seerr user.
          Discord admins (Manage Server) can always request.
        </span>
        <label className="check" style={{ marginBottom: 6 }}>
          <input type="checkbox" checked={!!v.request_all_members} onChange={(e) => set({ request_all_members: e.target.checked })} />
          <span><strong>All linked members</strong></span>
        </label>
        <div className="chooser" style={v.request_all_members ? { opacity: 0.5 } : undefined} aria-disabled={!!v.request_all_members}>
          {(roles.data ?? []).filter((r) => !r.managed && !r.deleted).map((r) => {
            const on = current.includes(r.role_id);
            return (
              <label key={r.role_id} className="check">
                <input type="checkbox" disabled={!!v.request_all_members} checked={on} onChange={() => set({ request_role_ids: on ? current.filter((x) => x !== r.role_id) : [...current, r.role_id] })} />
                <span>{r.name}</span>
              </label>
            );
          })}
        </div>
      </fieldset>
      <fieldset className="plain">
        <legend className="small" style={{ fontWeight: 600 }}>Visibility</legend>
        <label className="check" style={{ marginBottom: 6 }}>
          <input type="radio" name="request-visibility" checked={!isPublic} onChange={() => set({ request_visibility: "private" })} />
          <span><strong>Private</strong> <span className="muted small">— only the member sees their request; status updates come by DM</span></span>
        </label>
        <label className="check">
          <input type="radio" name="request-visibility" checked={isPublic} onChange={() => set({ request_visibility: "public" })} />
          <span><strong>Public</strong> <span className="muted small">— each request is posted in a channel mentioning the member; status updates are replies there</span></span>
        </label>
      </fieldset>
      {isPublic ? (
        <>
          <Field label="Requests channel" hint="The bot needs View Channels, Send Messages, Embed Links and Read Message History there. Searching and picking a movie or show stay private to the member." error={channelError ?? undefined}>
            <select value={v.request_channel_id ?? ""} onChange={(e) => set({ request_channel_id: e.target.value || null })} style={{ maxWidth: 420 }}>
              <option value="">Choose a channel…</option>
              {(channels.data ?? []).map((c) => (
                <option key={c.id} value={c.id} disabled={!c.can_post_requests && c.id !== v.request_channel_id}>
                  #{c.name}{c.category ? ` (${c.category})` : ""}{c.can_post_requests ? "" : " — bot can't post here"}
                </option>
              ))}
            </select>
          </Field>
          {channels.error ? <ErrorBanner error={channels.error} /> : null}
          <Switch checked={!!v.request_channel_only} onChange={(x) => set({ request_channel_only: x })} label={<><strong>Only in this channel</strong> <span className="muted small">— <code>/{name}</code> used anywhere else tells the member to use the requests channel</span></>} />
        </>
      ) : null}
      <Switch checked={!!v.request_require_verified} onChange={(x) => set({ request_require_verified: x })} label={<><strong>Verified links only</strong> <span className="muted small">— members who linked by typing a username can't request as that Plex account</span></>} />
      <Switch checked={!!v.request_import_users} onChange={(x) => set({ request_import_users: x })} label={<><strong>Create missing Seerr users</strong> <span className="muted small">— import the member's Plex account into Seerr on their first request (Seerr's default permissions)</span></>} />
      <Switch checked={!!v.request_notify} onChange={(x) => set({ request_notify: x })} label={<><strong>Notify members</strong> <span className="muted small">— {isPublic ? "reply to the request's post, mentioning them," : "DM them"} when a request is approved, declined or available</span></>} />
      <Switch checked={!!v.request_auto_retry} onChange={(x) => set({ request_auto_retry: x })} label={<><strong>Retry failed requests</strong> <span className="muted small">— when Seerr couldn't hand an approved request to Radarr or Sonarr, ask it again after 1, 6 and 24 hours; the member hears of the failure only if every try fails</span></>} />
      <div className="row">
        <button className="btn primary" disabled={!Object.keys(draft).length || !!nameError || !!channelError} onClick={() => void save()}>Save</button>
        <button className="btn" disabled={!Object.keys(draft).length} onClick={() => setDraft({})}>Discard</button>
      </div>
    </div>
  );
}
