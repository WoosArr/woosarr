import { useEffect, useMemo, useState } from "react";
import { api, qs, type DiscordMemberRow, type LibraryOut, type MemberRolesResult, type RoleImpactOut, type RoleOut } from "../api";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { ImpactList } from "../Impact";
import { Badge, Empty, ErrorBanner, Modal, RoleChip, Spinner, VerifiedBadge, useLoad, useToast } from "../ui";
import { LinkFilter, TriStateBox, type Filter } from "./UserFilters";

/** Users → Discord: members of the server, their roles and Plex links, with bulk role changes. */
export default function DiscordUsers() {
  const [filter, setFilter] = useState<Filter>("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<null | "add" | "remove">(null);
  const members = useLoad(() => api.get<DiscordMemberRow[]>(`/api/v1/discord/member-list${qs({ link: filter })}`), [filter]);
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const { why, pending } = useDiscordFeatures();
  // Shown in the dialog: the impact can be previewed even when the bot can't make the change.
  const blocked = pending ? null : why("assign_roles");

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (members.data ?? []).filter((m) => !q || [m.username, m.display_name ?? "", m.link?.plex_username ?? "", m.discord_user_id]
      .some((x) => x.toLowerCase().includes(q)));
  }, [members.data, search]);
  const allSelected = shown.length > 0 && shown.every((m) => selected.has(m.discord_user_id));
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };
  const picked = (members.data ?? []).filter((m) => selected.has(m.discord_user_id));

  return (
    <>
      <div className="row spread" style={{ marginBottom: 12 }}>
        <div className="row">
          <input type="search" className="inline-input" placeholder="Search members…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search members" style={{ width: 260 }} />
          <LinkFilter value={filter} onChange={setFilter} />
        </div>
        <div className="row">
          <button className="btn" disabled={!selected.size} onClick={() => setBulk("add")}>
            <Icon name="plus" /> Add roles{selected.size ? ` (${selected.size})` : ""}
          </button>
          <button className="btn" disabled={!selected.size} onClick={() => setBulk("remove")}>
            <Icon name="minus" /> Remove roles{selected.size ? ` (${selected.size})` : ""}
          </button>
        </div>
      </div>

      <ErrorBanner error={members.error} />
      {members.loading && !members.data ? <Spinner /> : null}
      {members.data && shown.length === 0 ? (
        <div className="card">
          <Empty icon="users" title="No members here">
            {members.data.length === 0 && !filter ? "Members appear once the Discord bot has connected to your server." : "Try another filter."}
          </Empty>
        </div>
      ) : null}
      {shown.length > 0 ? (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Discord members">
          <table className="responsive">
            <thead>
              <tr>
                <th style={{ width: 32 }}>
                  <input type="checkbox" aria-label="Select all shown members" checked={allSelected}
                    onChange={() => setSelected(allSelected ? new Set() : new Set(shown.map((m) => m.discord_user_id)))} />
                </th>
                <th>Discord member</th>
                <th>Roles</th>
                <th>Plex account</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((m) => (
                <tr key={m.discord_user_id}>
                  <td>
                    <input type="checkbox" aria-label={`Select ${m.display_name ?? m.username}`} checked={selected.has(m.discord_user_id)} onChange={() => toggle(m.discord_user_id)} />
                  </td>
                  <td data-label="Discord member">
                    <strong>{m.display_name ?? m.username}</strong>
                    <div className="small muted">@{m.username}</div>
                  </td>
                  <td data-label="Roles">
                    <div className="row">
                      {m.roles.length ? m.roles.map((r) => <RoleChip key={r.role_id} name={r.name} color={r.color} />) : <span className="muted small">—</span>}
                    </div>
                  </td>
                  <td data-label="Plex account">
                    {m.link ? (
                      <span className="row">{m.link.plex_username ?? m.link.plex_user_id} <VerifiedBadge verified={m.link.verified} /></span>
                    ) : <Badge tone="neutral" icon="unlink">Not linked</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {bulk && roles.data ? (
        <RolesModal mode={bulk} members={picked} roles={roles.data} libraries={libraries.data ?? []} blocked={blocked}
          onClose={() => setBulk(null)}
          onDone={() => { setBulk(null); setSelected(new Set()); void members.reload(); }} />
      ) : null}
    </>
  );
}

function RolesModal({ mode, members, roles, libraries, blocked, onClose, onDone }: {
  mode: "add" | "remove"; members: DiscordMemberRow[]; roles: RoleOut[]; libraries: LibraryOut[];
  blocked: string | null; onClose: () => void; onDone: () => void;
}) {
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<RoleImpactOut[] | null>(null);
  const [previewError, setPreviewError] = useState<unknown>(null);
  const [results, setResults] = useState<MemberRolesResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const titles = new Map(libraries.map((l) => [l.library_id, l.title]));
  const roleName = new Map(roles.map((r) => [r.role_id, r.name]));
  const ids = members.map((m) => m.discord_user_id);
  const count = (rid: string) => members.filter((m) => m.roles.some((r) => r.role_id === rid)).length;
  const stateOf = (rid: string): "all" | "some" | "none" => {
    const n = count(rid);
    return n === 0 ? "none" : n === members.length ? "all" : "some";
  };
  // Bot and integration roles can't be given or taken; for removal, only roles someone selected has.
  const usable = roles.filter((r) => !r.managed && !r.deleted).sort((a, b) => b.position - a.position);
  const listed = mode === "add" ? usable : usable.filter((r) => stateOf(r.role_id) !== "none");
  const body = () => ({ discord_user_ids: ids, add_role_ids: mode === "add" ? [...chosen] : [], remove_role_ids: mode === "remove" ? [...chosen] : [] });

  const key = [...chosen].sort().join(",");
  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
    if (!chosen.size) return;
    let live = true;
    const t = setTimeout(() => {
      api.post<RoleImpactOut[]>("/api/v1/discord/members/roles/preview", body())
        .then((r) => live && setPreview(r), (e) => live && setPreviewError(e));
    }, 200);
    return () => { live = false; clearTimeout(t); };
  }, [key]);

  const toggle = (rid: string) => {
    const next = new Set(chosen);
    if (next.has(rid)) next.delete(rid); else next.add(rid);
    setChosen(next);
  };
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await api.post<MemberRolesResult[]>("/api/v1/discord/members/roles", body());
      setResults(out);
      const changed = out.filter((r) => r.added.length || r.removed.length).length;
      const failed = out.filter((r) => r.error).length;
      if (failed) toast.error(new Error(`${changed} member${changed === 1 ? "" : "s"} changed, ${failed} couldn't be.`));
      else toast.ok(`${changed} member${changed === 1 ? "" : "s"} changed. Linked Plex accounts sync next.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const nameOf = (id: string) => {
    const m = members.find((x) => x.discord_user_id === id);
    return m ? m.display_name ?? m.username : id;
  };
  const verb = mode === "add" ? "Add" : "Remove";

  if (results) {
    return (
      <Modal title={`${verb} roles: results`} onClose={onDone} actions={<button className="btn primary" onClick={onDone}>Done</button>}>
        <ul className="small" style={{ paddingLeft: 18 }}>
          {results.map((r) => (
            <li key={r.discord_user_id} style={{ marginBottom: 6 }}>
              <strong>{nameOf(r.discord_user_id)}</strong>
              {r.error ? <> — <span style={{ color: "var(--danger)" }}>{r.error}</span></> : null}
              {r.added.length ? <> — added {r.added.map((x) => roleName.get(x) ?? x).join(", ")}</> : null}
              {r.removed.length ? <> — removed {r.removed.map((x) => roleName.get(x) ?? x).join(", ")}</> : null}
              {!r.error && !r.added.length && !r.removed.length ? <> — nothing to change</> : null}
            </li>
          ))}
        </ul>
      </Modal>
    );
  }

  return (
    <Modal title={`${verb} Discord roles`} onClose={onClose} actions={
      <>
        <button className="btn" disabled={!chosen.size} onClick={() => setChosen(new Set())}>Reset</button>
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className={`btn ${mode === "add" ? "primary" : "danger"}`} disabled={!chosen.size || busy || !!blocked}
          title={blocked ?? undefined} onClick={() => void submit()}>
          {busy ? "Working…" : `${verb}${chosen.size ? ` ${chosen.size} role${chosen.size === 1 ? "" : "s"}` : ""}`}
        </button>
      </>
    }>
      <ErrorBanner error={error} />
      {blocked ? <div className="banner warn small"><Icon name="alert" /><div>{blocked}. You can preview the impact; see Integrations → Discord → Bot features and permissions to let the bot make the change.</div></div> : null}
      <p className="small">{members.length} member{members.length === 1 ? "" : "s"}: {members.map((m) => m.display_name ?? m.username).join(", ")}</p>
      <p className="small muted">
        {mode === "add"
          ? "Tick the roles to give everyone selected (members who already have one are skipped)."
          : "✓ all selected members have it · – some do. Untick a role to take it from everyone who has it."}{" "}
        The bot makes the change in Discord; linked Plex accounts then follow your role mappings.
      </p>
      <fieldset className="plain">
        <legend className="small muted">Roles</legend>
        {listed.length === 0 ? <p className="small muted">{mode === "add" ? "No roles the bot can give." : "The selected members have no roles the bot can take."}</p> : null}
        <div className="chooser">
          {listed.map((r) => {
            const st = stateOf(r.role_id);
            const on = chosen.has(r.role_id);
            return mode === "add" ? (
              <label key={r.role_id} className="check">
                <input type="checkbox" checked={on} onChange={() => toggle(r.role_id)} aria-label={r.name} />
                <span>{r.name}{st !== "none" ? <span className="small muted"> — {st === "all" ? "all have it" : `${count(r.role_id)} of ${members.length} have it`}</span> : null}</span>
              </label>
            ) : (
              <label key={r.role_id} className="check" style={on ? { borderColor: "var(--danger)", background: "var(--danger-soft)" } : undefined}>
                <TriStateBox state={st} marked={on} label={r.name} onToggle={() => toggle(r.role_id)} />
                <span>{r.name}<span className="small muted"> {on ? "— will be removed" : st === "some" ? `— ${count(r.role_id)} of ${members.length}` : ""}</span></span>
              </label>
            );
          })}
        </div>
      </fieldset>
      {chosen.size ? (
        <div className="card flat" style={{ marginTop: 14 }} aria-live="polite">
          <h3>Plex impact</h3>
          {previewError ? <ErrorBanner error={previewError} /> : !preview ? <Spinner label="Calculating" /> : (
            <ImpactList titles={titles} changes={preview} empty="No linked Plex account's libraries change." />
          )}
        </div>
      ) : null}
    </Modal>
  );
}
