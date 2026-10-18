import { useEffect, useMemo, useRef, useState } from "react";
import { api, qs, type InviteOut, type LibraryOut, type MemberOut, type UserOut } from "../api";
import { Icon } from "../icons";
import {
  Badge,
  Empty,
  ErrorBanner,
  Modal,
  PageHead,
  RoleChip,
  SourceBadge,
  Spinner,
  VerifiedBadge,
  ago,
  useConfirm,
  useLoad,
  useToast,
} from "../ui";
import { ImpactList } from "../Impact";
import { LibraryChooser } from "./LibraryChooser";

type Filter = "" | "verified" | "unverified" | "linked" | "unlinked";

export default function UsersPage() {
  const [filter, setFilter] = useState<Filter>("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<null | "add" | "remove">(null);
  const [linking, setLinking] = useState<UserOut | null>(null);
  const users = useLoad(() => api.get<UserOut[]>(`/api/v1/users${qs({ link: filter })}`), [filter]);
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const confirm = useConfirm();
  const toast = useToast();

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (users.data ?? []).filter(
      (u) =>
        !q ||
        u.username.toLowerCase().includes(q) ||
        (u.email ?? "").toLowerCase().includes(q) ||
        (u.link?.discord_username ?? "").toLowerCase().includes(q),
    );
  }, [users.data, search]);

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };
  const selectable = shown.filter((u) => !u.is_owner);
  const allSelected = selectable.length > 0 && selectable.every((u) => selected.has(u.plex_user_id));

  const unlink = async (u: UserOut) => {
    const ok = await confirm({
      title: `Unlink ${u.username}?`,
      body: (
        <p>
          WooSarr stops managing <strong>{u.username}</strong>'s library access. Their current Plex shares stay as they
          are. They can link again with <code>/link</code>.
        </p>
      ),
      confirmLabel: "Unlink",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/v1/users/${u.plex_user_id}/link`);
      toast.ok(`${u.username} unlinked.`);
      void users.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <>
      <PageHead title="Users" sub="Plex users, their linked Discord accounts, roles and library access.">
        <button className="btn" disabled={!selected.size} onClick={() => setBulk("add")}>
          <Icon name="plus" /> Add libraries{selected.size ? ` (${selected.size})` : ""}
        </button>
        <button className="btn" disabled={!selected.size} onClick={() => setBulk("remove")}>
          <Icon name="minus" /> Remove libraries{selected.size ? ` (${selected.size})` : ""}
        </button>
      </PageHead>

      <div className="row" style={{ marginBottom: 12 }}>
        <input type="search" className="inline-input" placeholder="Search users…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search users" style={{ width: 260 }} />
        <div className="segmented" role="group" aria-label="Link filter">
          {(["", "verified", "unverified", "linked", "unlinked"] as Filter[]).map((f) => (
            <button key={f || "all"} aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f ? f[0].toUpperCase() + f.slice(1) : "All"}
            </button>
          ))}
        </div>
      </div>

      <ErrorBanner error={users.error} />
      {users.loading && !users.data ? <Spinner /> : null}
      {users.data && shown.length === 0 ? (
        <div className="card">
          <Empty icon="users" title="No users here">
            {users.data.length === 0 && !filter ? "Users appear after WooSarr reads them from your Plex server." : "Try another filter."}
          </Empty>
        </div>
      ) : null}
      {shown.length > 0 ? (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Plex users">
          <table className="responsive">
            <thead>
              <tr>
                <th style={{ width: 32 }}>
                  <input
                    type="checkbox"
                    aria-label="Select all shown users"
                    checked={allSelected}
                    onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((u) => u.plex_user_id)))}
                  />
                </th>
                <th>Plex user</th>
                <th>Discord</th>
                <th>Roles</th>
                <th>Libraries</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {shown.map((u) => (
                <tr key={u.plex_user_id}>
                  <td>
                    {u.is_owner ? null : (
                      <input type="checkbox" aria-label={`Select ${u.username}`} checked={selected.has(u.plex_user_id)} onChange={() => toggle(u.plex_user_id)} />
                    )}
                  </td>
                  <td data-label="Plex user">
                    <strong>{u.username}</strong>
                    {u.is_owner ? <> <Badge tone="accent" icon="shieldCheck" title="Owns the Plex server">Owner</Badge></> : null}
                    <div className="small muted">{u.email ?? (u.is_managed ? "Managed (Home) user" : "")}</div>
                    {u.removed ? <Badge tone="neutral">No longer shared</Badge> : null}
                  </td>
                  <td data-label="Discord">
                    {u.link ? (
                      <div className="stack" style={{ gap: 4 }}>
                        <span>{u.link.discord_display_name ?? u.link.discord_username ?? u.link.discord_user_id}</span>
                        <span className="row">
                          <VerifiedBadge verified={u.link.verified} />
                          {!u.link.in_guild ? <Badge tone="warn">Not in server</Badge> : null}
                        </span>
                        <span className="small muted">{u.link.method} · {ago(u.link.linked_at)}</span>
                      </div>
                    ) : (
                      <Badge tone="neutral" icon="unlink">Not linked</Badge>
                    )}
                  </td>
                  <td data-label="Roles">
                    <div className="row">
                      {u.roles.length ? u.roles.map((r) => <RoleChip key={r.role_id} name={r.name} color={r.color} />) : <span className="muted small">—</span>}
                    </div>
                  </td>
                  <td data-label="Libraries">
                    <div className="stack" style={{ gap: 4 }}>
                      {u.is_owner ? <span className="small">Every library (owner; never changed)</span> : null}
                      {!u.is_owner && u.libraries.length === 0 ? <span className="muted small">None</span> : null}
                      {u.libraries.map((l) => (
                        <div key={l.library_id} className="row">
                          <span style={{ minWidth: 90 }}>{l.title}</span>
                          {l.sources.map((s) => <SourceBadge key={s} source={s} />)}
                          {!l.shared ? <Badge tone="info" icon="clock" title="Will be shared on the next sync (or would be, in dry-run)">pending</Badge> : null}
                        </div>
                      ))}
                    </div>
                  </td>
                  <td>
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      {u.link ? (
                        <button className="btn sm danger" onClick={() => void unlink(u)}>
                          <Icon name="unlink" size={14} /> Unlink
                        </button>
                      ) : (
                        <button className="btn sm" onClick={() => setLinking(u)}>
                          <Icon name="link" size={14} /> Link
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <PendingInvites libraries={libraries.data ?? []} />

      {bulk && libraries.data ? (
        <BulkModal
          action={bulk}
          users={(users.data ?? []).filter((u) => selected.has(u.plex_user_id))}
          libraries={libraries.data}
          onClose={() => setBulk(null)}
          onDone={() => {
            setBulk(null);
            void users.reload();
          }}
        />
      ) : null}
      {linking ? <LinkModal user={linking} onClose={() => setLinking(null)} onDone={() => { setLinking(null); void users.reload(); }} /> : null}
    </>
  );
}

interface RemoveResult {
  plex_user_id: string;
  username: string;
  removed: string[];
  kept_by_roles: string[];
  manual_removed: string[];
  not_shared: string[];
  error: string | null;
  dry_run: boolean;
}

function BulkModal(props: {
  action: "add" | "remove";
  users: UserOut[];
  libraries: LibraryOut[];
  onClose: () => void;
  onDone: () => void;
}) {
  return props.action === "add" ? <AddModal {...props} /> : <RemoveModal {...props} />;
}

function AddModal({ users, libraries, onClose, onDone }: { users: UserOut[]; libraries: LibraryOut[]; onClose: () => void; onDone: () => void }) {
  const [libs, setLibs] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ changed: number }>("/api/v1/users/manual-grants", {
        plex_user_ids: users.map((u) => u.plex_user_id), library_ids: [...libs], action: "add",
      });
      toast.ok(`${r.changed} manual grant${r.changed === 1 ? "" : "s"} added. Syncing.`);
      onDone();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Add libraries manually" onClose={onClose} actions={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!libs.size || busy} onClick={() => void submit()}>Add</button>
      </>
    }>
      <p className="small">{users.length} user{users.length === 1 ? "" : "s"}: {users.map((u) => u.username).join(", ")}</p>
      <p className="small muted">Manual grants stay no matter which roles a user has.</p>
      <LibraryChooser libraries={libraries} selected={libs} onChange={setLibs} label="Libraries" />
    </Modal>
  );
}

interface RemoveResult {
  plex_user_id: string;
  username: string;
  removed: string[];
  kept_by_roles: string[];
  manual_removed: string[];
  not_shared: string[];
  error: string | null;
  dry_run: boolean;
}

interface PreviewRow {
  plex_user_id: string;
  username: string;
  removed: string[];
  kept_by_roles: string[];
}

function TriStateBox({ state, marked, label, onToggle }: { state: "all" | "some" | "none"; marked: boolean; label: string; onToggle: () => void }) {
  const ref = useRef<HTMLInputElement>(null);
  const checked = state === "all" && !marked;
  const mixed = state === "some" && !marked;
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = mixed;
  }, [mixed]);
  return (
    <input ref={ref} type="checkbox" checked={checked} disabled={state === "none"} onChange={onToggle} aria-label={label} />
  );
}

function RemoveModal({ users, libraries, onClose, onDone }: { users: UserOut[]; libraries: LibraryOut[]; onClose: () => void; onDone: () => void }) {
  const [marked, setMarked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<PreviewRow[] | null>(null);
  const [results, setResults] = useState<RemoveResult[] | null>(null);
  const [undone, setUndone] = useState(false);
  const toast = useToast();
  const titles = new Map(libraries.map((l) => [l.library_id, l.title]));
  const names = (ids: string[]) => ids.map((id) => titles.get(id) ?? id).join(", ");
  const ids = users.map((u) => u.plex_user_id);

  // How many selected users currently have each library (last sync).
  const has = (u: UserOut, lib: string) => u.libraries.some((l) => l.library_id === lib && l.shared);
  const stateOf = (lib: string): "all" | "some" | "none" => {
    const n = users.filter((u) => has(u, lib)).length;
    return n === 0 ? "none" : n === users.length ? "all" : "some";
  };
  const key = [...marked].sort().join(",");
  useEffect(() => {
    if (!marked.size) {
      setPreview(null);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      api.post<PreviewRow[]>("/api/v1/users/remove-access/preview", { plex_user_ids: ids, library_ids: [...marked] })
        .then((r) => live && setPreview(r), () => live && setPreview(null));
    }, 200);
    return () => { live = false; clearTimeout(t); };
  }, [key]);

  const toggle = (lib: string) => {
    const next = new Set(marked);
    if (next.has(lib)) next.delete(lib); else next.add(lib);
    setMarked(next);
  };

  const submit = async () => {
    setBusy(true);
    try {
      setResults(await api.post<RemoveResult[]>("/api/v1/users/remove-access", { plex_user_ids: ids, library_ids: [...marked] }));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!results) return;
    setBusy(true);
    try {
      const items = results.filter((r) => r.removed.length || r.manual_removed.length)
        .map((r) => ({ plex_user_id: r.plex_user_id, library_ids: r.removed, manual_library_ids: r.manual_removed }));
      const out = await api.post<{ plex_user_id: string; restored: string[]; error: string | null }[]>("/api/v1/users/restore-access", { items });
      const failed = out.filter((o) => o.error);
      if (failed.length) toast.error(new Error(`Undo failed for ${failed.length} user(s); see the logs.`));
      else toast.ok("Undone: the removed libraries were given back.");
      setUndone(true);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };

  if (results) {
    const anything = results.some((r) => r.removed.length || r.manual_removed.length);
    return (
      <Modal title={undone ? "Remove libraries: undone" : "Remove libraries: results"} onClose={onDone} actions={
        <>
          {anything && !undone ? <button className="btn" disabled={busy} onClick={() => void undo()}><Icon name="refresh" /> Undo</button> : null}
          <button className="btn primary" onClick={onDone}>Done</button>
        </>
      }>
        {results[0]?.dry_run ? <div className="banner warn small"><Icon name="eye" /><div>Dry-run is on: nothing was changed in Plex; the audit trail shows what would have been removed.</div></div> : null}
        {undone ? <div className="banner info small"><Icon name="check" /><div>Reverted: the libraries listed below were given back.</div></div> : null}
        <ul className="small" style={{ paddingLeft: 18 }}>
          {results.map((r) => (
            <li key={r.plex_user_id} style={{ marginBottom: 6 }}>
              <strong>{r.username}</strong>
              {r.error ? <> — <span style={{ color: "var(--danger)" }}>{r.error}</span></> : null}
              {r.removed.length ? <> — {undone ? "restored" : "removed"} {names(r.removed)}</> : null}
              {r.kept_by_roles.length ? <> — kept {names(r.kept_by_roles)} (their Discord roles grant it; change roles or the role mapping)</> : null}
              {!r.error && !r.removed.length && !r.kept_by_roles.length ? <> — nothing to remove</> : null}
            </li>
          ))}
        </ul>
      </Modal>
    );
  }

  const visible = libraries.filter((l) => !l.removed);
  return (
    <Modal title="Remove libraries" onClose={onClose} actions={
      <>
        <button className="btn" disabled={!marked.size} onClick={() => setMarked(new Set())}>Reset</button>
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn danger" disabled={!marked.size || busy} onClick={() => void submit()}>
          Remove{marked.size ? ` ${marked.size} librar${marked.size === 1 ? "y" : "ies"}` : ""}
        </button>
      </>
    }>
      <p className="small">{users.length} user{users.length === 1 ? "" : "s"}: {users.map((u) => u.username).join(", ")}</p>
      <p className="small muted">
        ✓ all selected users have it · – some do · empty: none do. Untick a library to remove it from everyone who has it.
        Libraries a linked user's Discord roles grant are kept (the next sync would add them back).
      </p>
      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="small muted" style={{ marginBottom: 6 }}>Libraries</legend>
        <div className="chooser">
          {visible.map((l) => {
            const st = stateOf(l.library_id);
            const n = users.filter((u) => has(u, l.library_id)).length;
            const isMarked = marked.has(l.library_id);
            return (
              <label key={l.library_id} className="check" style={isMarked ? { borderColor: "var(--danger)", background: "var(--danger-soft)" } : undefined}>
                <TriStateBox state={st} marked={isMarked} label={l.title} onToggle={() => toggle(l.library_id)} />
                <span>
                  {l.title}
                  <span className="small muted"> {isMarked ? "— will be removed" : st === "none" ? "— nobody" : st === "some" ? `— ${n} of ${users.length}` : ""}</span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>
      {marked.size ? (
        <div className="card" style={{ marginTop: 14, boxShadow: "none" }} aria-live="polite">
          <h3>Impact</h3>
          {!preview ? <Spinner label="Calculating" /> : (
            <ImpactList titles={titles} empty="Nobody would lose anything."
              changes={preview.map((p) => ({ plex_user_id: p.plex_user_id, username: p.username, lose: p.removed, kept: p.kept_by_roles }))} />
          )}
        </div>
      ) : null}
    </Modal>
  );
}

function LinkModal({ user, onClose, onDone }: { user: UserOut; onClose: () => void; onDone: () => void }) {
  const [q, setQ] = useState("");
  const members = useLoad(() => api.get<MemberOut[]>(`/api/v1/discord/members${qs({ q, limit: 25 })}`), [q]);
  const toast = useToast();
  const link = async (m: MemberOut) => {
    try {
      await api.put(`/api/v1/users/${user.plex_user_id}/link`, { discord_user_id: m.discord_user_id });
      toast.ok(`${user.username} linked to ${m.display_name ?? m.username}.`);
      onDone();
    } catch (e) {
      toast.error(e);
    }
  };
  return (
    <Modal title={`Link ${user.username} to a Discord member`} onClose={onClose}>
      <p className="small muted">Admin links count as verified.</p>
      <input type="search" placeholder="Search by name or id…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search Discord members" />
      <div style={{ marginTop: 10 }}>
        {members.loading && !members.data ? <Spinner /> : null}
        {members.data?.length === 0 ? <p className="muted small">No members found. The Discord bot fills this list.</p> : null}
        {members.data?.map((m) => (
          <div key={m.discord_user_id} className="row spread" style={{ padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
            <span>
              <strong>{m.display_name ?? m.username}</strong> <span className="small muted">@{m.username} · {m.discord_user_id}</span>
            </span>
            {m.linked_plex_user_id ? (
              <Badge tone="neutral">Already linked</Badge>
            ) : (
              <button className="btn sm primary" onClick={() => void link(m)}>Link</button>
            )}
          </div>
        ))}
      </div>
    </Modal>
  );
}

function PendingInvites({ libraries }: { libraries: LibraryOut[] }) {
  const inv = useLoad(() => api.get<InviteOut[]>("/api/v1/invites?status=pending"));
  const confirm = useConfirm();
  const toast = useToast();
  const titles = new Map(libraries.map((l) => [l.library_id, l.title]));
  if (!inv.data?.length) return null;
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="invites-h">
      <h2 id="invites-h">Pending invites</h2>
      <p className="small muted">Sent with <code>/invite</code>. WooSarr links each one when the person accepts on Plex.</p>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Pending invites">
        <table className="responsive">
          <thead><tr><th>Invited</th><th>Discord member</th><th>Libraries</th><th>Sent</th><th aria-label="Actions" /></tr></thead>
          <tbody>
            {inv.data.map((i) => (
              <tr key={i.id}>
                <td data-label="Invited"><strong>{i.identifier}</strong></td>
                <td data-label="Discord member">
                  {i.discord_username ?? i.discord_user_id} {i.verified ? <VerifiedBadge verified /> : null}
                  <div className="small muted">{i.invited_by.startsWith("discord-admin") ? "by an admin" : "self-invite"}</div>
                </td>
                <td data-label="Libraries"><div className="row">{i.library_ids.map((id) => <Badge key={id} tone="accent" icon="library">{titles.get(id) ?? id}</Badge>)}</div></td>
                <td data-label="Sent" className="small">{ago(i.created_at)}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn sm" onClick={async () => {
                    if (await confirm({ title: `Stop tracking the invite for ${i.identifier}?`, body: <p>WooSarr won't link it automatically. The invite itself stays on plex.tv; cancel it there if needed.</p>, confirmLabel: "Stop tracking" })) {
                      try { await api.del(`/api/v1/invites/${i.id}`); void inv.reload(); } catch (e) { toast.error(e); }
                    }
                  }}>Cancel</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
