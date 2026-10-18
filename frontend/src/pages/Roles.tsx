import { useEffect, useState, type ReactNode } from "react";
import { api, type ImpactOut, type LibraryOut, type RoleCreated, type RoleOut } from "../api";
import type { components } from "../generated/api";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, Modal, PageHead, RoleChip, Spinner, useConfirm, useLoad, useToast } from "../ui";

type RoleReferences = components["schemas"]["RoleReferences"];
type PurgeOut = components["schemas"]["PurgeOut"];

const SETTING_NAMES: Record<string, string> = {
  verified_role_id: "Verified link role", unverified_role_id: "Unverified link role", linked_role_id: "Linked role",
  invite_role_ids: "Roles allowed to /invite", request_role_ids: "Roles allowed to request",
};

/** What removing these roles from WooSarr cleans up, as a list for the confirm dialog. */
function cleanup(refs: RoleReferences[], titles: Map<string, string>): ReactNode {
  return (
    <ul className="small" style={{ paddingLeft: 18, margin: "8px 0 0" }}>
      {refs.map((r) => {
        const parts = [
          r.library_ids.length ? `library mapping (${r.library_ids.map((id) => titles.get(id) ?? id).join(", ")})` : null,
          ...r.settings.map((k) => `setting "${SETTING_NAMES[k] ?? k}"`),
          ...r.reaction_posts.map((p) => `reaction ${String(p.emoji)} on "${String(p.title)}"`),
        ].filter(Boolean);
        return <li key={r.role_id}><strong>{r.name}</strong>: {parts.length ? parts.join("; ") : "nothing else refers to it"}</li>;
      })}
    </ul>
  );
}
import { LibraryChooser } from "./LibraryChooser";

export default function RolesPage() {
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const deleted = useLoad(() => api.get<RoleReferences[]>("/api/v1/roles/deleted"));
  const confirm = useConfirm();
  const toast = useToast();
  const [editing, setEditing] = useState<RoleOut | null>(null);
  const [showManaged, setShowManaged] = useState(false);
  const [creating, setCreating] = useState(false);
  const { available, why } = useDiscordFeatures();
  const titles = new Map((libraries.data ?? []).map((l) => [l.library_id, l.title]));
  const shown = (roles.data ?? []).filter((r) => showManaged || !r.managed || r.library_ids.length);
  const gone = deleted.data ?? [];

  const remove = async (refs: RoleReferences[]) => {
    const one = refs.length === 1;
    const ok = await confirm({
      title: one ? `Remove ${refs[0].name}?` : `Remove ${refs.length} deleted roles?`,
      body: (
        <>
          <p>{one ? "This role no longer exists in Discord." : "These roles no longer exist in Discord."} WooSarr forgets {one ? "it" : "them"} and cleans up:</p>
          {cleanup(refs, titles)}
          <p className="small muted" style={{ marginTop: 8 }}>Nobody can hold a deleted role, so no one's Plex access changes; a sync runs to confirm.</p>
        </>
      ),
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    try {
      const r = one ? await api.del<PurgeOut>(`/api/v1/roles/${refs[0].role_id}`) : await api.post<PurgeOut>("/api/v1/roles/remove-deleted");
      toast.ok(`Removed ${r.removed.length} role${r.removed.length === 1 ? "" : "s"}.`);
      void roles.reload();
      void deleted.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <>
      <PageHead title="Roles" sub="Give Discord roles access to Plex libraries. Access is additive: a user gets the union of all their roles.">
        <label className="check small">
          <input type="checkbox" checked={showManaged} onChange={(e) => setShowManaged(e.target.checked)} />
          Show bot/integration roles
        </label>
        {gone.length > 1 ? (
          <button className="btn danger" onClick={() => void remove(gone)}><Icon name="trash" /> Remove {gone.length} deleted roles</button>
        ) : null}
        <button className="btn primary" disabled={!available("create_roles") || !libraries.data} title={why("create_roles") ?? "Create a new Discord role"} onClick={() => setCreating(true)}>
          <Icon name="plus" /> Create role
        </button>
      </PageHead>
      <ErrorBanner error={roles.error ?? libraries.error} />
      {roles.loading && !roles.data ? <Spinner /> : null}
      {roles.data && shown.length === 0 ? (
        <div className="card"><Empty icon="roles" title="No roles yet">Roles appear once the Discord bot is connected.</Empty></div>
      ) : null}
      {shown.length > 0 ? (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Discord roles">
          <table className="responsive">
            <thead>
              <tr><th>Role</th><th>Libraries</th><th>Linked members</th><th aria-label="Actions" /></tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.role_id}>
                  <td data-label="Role">
                    <RoleChip name={r.name} color={r.color} />
                    {r.deleted ? <> <Badge tone="danger">Deleted in Discord</Badge></> : null}
                    {r.managed ? <> <Badge tone="neutral">Bot role</Badge></> : null}
                  </td>
                  <td data-label="Libraries">
                    <div className="row">
                      {r.library_ids.length ? r.library_ids.map((id) => <Badge key={id} tone="accent" icon="library">{titles.get(id) ?? id}</Badge>) : <span className="muted small">None</span>}
                    </div>
                  </td>
                  <td data-label="Linked members">{r.linked_holders}</td>
                  <td style={{ textAlign: "right" }}>
                    {r.deleted ? (
                      <button className="btn sm danger" disabled={!gone.some((g) => g.role_id === r.role_id)}
                        onClick={() => void remove(gone.filter((g) => g.role_id === r.role_id))} aria-label={`Remove ${r.name} from WooSarr`}>
                        <Icon name="trash" size={14} /> Remove
                      </button>
                    ) : (
                      <button className="btn sm" onClick={() => setEditing(r)} disabled={!libraries.data}>
                        <Icon name="library" size={14} /> Edit libraries
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {creating && libraries.data ? (
        <CreateRoleModal libraries={libraries.data} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); void roles.reload(); }} />
      ) : null}
      {editing && libraries.data ? (
        <RoleEditor
          role={editing}
          libraries={libraries.data}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void roles.reload();
          }}
        />
      ) : null}
    </>
  );
}

function RoleEditor({ role, libraries, onClose, onSaved }: { role: RoleOut; libraries: LibraryOut[]; onClose: () => void; onSaved: () => void }) {
  const [libs, setLibs] = useState<Set<string>>(new Set(role.library_ids));
  const [impact, setImpact] = useState<ImpactOut | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const titles = new Map(libraries.map((l) => [l.library_id, l.title]));
  const key = [...libs].sort().join(",");

  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      api.post<ImpactOut>(`/api/v1/roles/${role.role_id}/preview`, { library_ids: [...libs] })
        .then((r) => live && setImpact(r))
        .catch(() => live && setImpact(null));
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [key, role.role_id]);

  const save = async () => {
    setBusy(true);
    try {
      await api.put(`/api/v1/roles/${role.role_id}/libraries`, { library_ids: [...libs] });
      toast.ok(`Saved ${role.name}. A sync has been queued.`);
      onSaved();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Libraries for ${role.name}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={() => void save()}>Save</button>
        </>
      }
    >
      <LibraryChooser libraries={libraries} selected={libs} onChange={setLibs} label="Members with this role get:" />
      <div className="card" style={{ marginTop: 14, boxShadow: "none" }} aria-live="polite">
        <h3>Impact</h3>
        {!impact ? <Spinner label="Calculating" /> : impact.users.length === 0 ? (
          <p className="small muted" style={{ margin: 0 }}>No linked user's access changes.</p>
        ) : (
          <>
            <p className="small" style={{ marginBottom: 6 }}>
              <Badge tone="ok" icon="plus">{impact.users_gaining} gain</Badge>{" "}
              <Badge tone="danger" icon="minus">{impact.users_losing} lose</Badge>
            </p>
            <ul className="small" style={{ margin: 0, paddingLeft: 18, maxHeight: 180, overflow: "auto" }}>
              {impact.users.map((u) => (
                <li key={u.plex_user_id}>
                  <strong>{u.username}</strong>
                  {u.gain.length ? <> gains {u.gain.map((id) => titles.get(id) ?? id).join(", ")}</> : null}
                  {u.gain.length && u.lose.length ? ";" : null}
                  {u.lose.length ? <> loses {u.lose.map((id) => titles.get(id) ?? id).join(", ")}</> : null}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </Modal>
  );
}

export function CreateRoleModal({ libraries, onClose, onCreated, initialName = "", initialLibraries = [], title = "Create a Discord role" }: {
  libraries: LibraryOut[];
  onClose: () => void;
  onCreated: (role: RoleCreated) => void;
  initialName?: string;
  initialLibraries?: string[];
  title?: string;
}) {
  const [name, setName] = useState(initialName);
  const [color, setColor] = useState("#7c3aed");
  const [libs, setLibs] = useState<Set<string>>(new Set(initialLibraries));
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const create = async () => {
    setBusy(true);
    try {
      const r = await api.post<RoleCreated>("/api/v1/discord/roles", {
        name: name.trim(), purpose: "plain", library_ids: [...libs], color: parseInt(color.slice(1), 16),
      });
      toast.ok(`Created ${r.name} in Discord${libs.size ? " and mapped its libraries. A sync has been queued." : "."}`);
      onCreated(r);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={title} onClose={onClose} actions={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!name.trim() || busy} onClick={() => void create()}>{busy ? "Creating…" : "Create role"}</button>
      </>
    }>
      <div className="stack">
        <label className="field">Name<input type="text" value={name} maxLength={100} autoFocus onChange={(e) => setName(e.target.value)} placeholder="e.g. Movie Night" /></label>
        <label className="field">Colour
          <input type="color" value={color} onChange={(e) => setColor(e.target.value)} style={{ width: 64, height: 36, padding: 2 }} />
        </label>
        <LibraryChooser libraries={libraries} selected={libs} onChange={setLibs} label="Members with this role get (optional, can be changed later):" />
        <p className="small muted" style={{ margin: 0 }}>The role is created at the bottom of the role list, below the bot's role, so WooSarr can assign it.</p>
      </div>
    </Modal>
  );
}
