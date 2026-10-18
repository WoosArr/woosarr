import { useState } from "react";
import { api, type LibraryOut, type PlexServerOut } from "../api";
import { CreateRoleModal } from "./Roles";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, PageHead, Spinner, Switch, useConfirm, useLoad, useToast } from "../ui";

export default function LibrariesPage() {
  const libs = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const server = useLoad(() => api.get<PlexServerOut | null>("/api/v1/plex/server"));
  const confirm = useConfirm();
  const toast = useToast();
  const { available, why } = useDiscordFeatures();
  const [roleFor, setRoleFor] = useState<LibraryOut | null>(null);

  const setExempt = async (lib: LibraryOut, exempt: boolean) => {
    const ok = await confirm({
      title: exempt ? `Exempt ${lib.title}?` : `Manage ${lib.title} again?`,
      body: exempt ? (
        <p>Role changes will never add or remove <strong>{lib.title}</strong>. Everyone keeps exactly the access they have now; you can still grant it manually.</p>
      ) : (
        <p>Roles mapped to <strong>{lib.title}</strong> will add and remove it again on the next sync.</p>
      ),
      confirmLabel: exempt ? "Exempt" : "Manage",
    });
    if (!ok) return;
    try {
      const updated = await api.patch<LibraryOut>(`/api/v1/libraries/${lib.library_id}`, { is_exempt: exempt });
      libs.setData((libs.data ?? []).map((l) => (l.library_id === updated.library_id ? updated : l)));
      toast.ok(`${lib.title} ${exempt ? "is now exempt" : "is managed by roles again"}.`);
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <>
      <PageHead title="Libraries" sub="Libraries on your Plex server, which roles grant them, and which ones are exempt." />
      {server.data ? (
        <div className="card row spread" style={{ marginBottom: 16 }}>
          <span className="row">
            <Icon name="plex" /> <strong>{server.data.name}</strong>
            <span className="small muted">Plex Media Server {server.data.version ?? "?"} · {server.data.platform ?? "?"}</span>
          </span>
          {server.data.online ? <Badge tone="ok">Online</Badge> : <Badge tone="danger">Offline</Badge>}
        </div>
      ) : null}
      <div className="banner info">
        <Icon name="lock" />
        <div className="small">
          <strong>Exempt</strong> libraries are never added or removed by role logic; each user's current access is kept as it is.
          Libraries that no role maps to are also left alone.
        </div>
      </div>
      <ErrorBanner error={libs.error} />
      {libs.loading && !libs.data ? <Spinner /> : null}
      {libs.data?.length === 0 ? (
        <div className="card"><Empty icon="library" title="No libraries yet">They appear after WooSarr reads your Plex server.</Empty></div>
      ) : null}
      {libs.data && libs.data.length > 0 ? (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Plex libraries">
          <table className="responsive">
            <thead><tr><th>Library</th><th>Type</th><th>Granted by roles</th><th>Shared with</th><th>Exempt</th><th aria-label="Actions" /></tr></thead>
            <tbody>
              {libs.data.map((l) => (
                <tr key={l.library_id}>
                  <td data-label="Library">
                    <strong>{l.title}</strong> <span className="small muted">#{l.library_id}</span>
                    {l.removed ? <div><Badge tone="danger">Missing on server</Badge></div> : null}
                  </td>
                  <td data-label="Type" className="small">{l.type}</td>
                  <td data-label="Granted by roles">
                    <div className="row">
                      {l.roles.length ? l.roles.map((r) => <Badge key={r.role_id} tone="accent" icon="roles">{r.name}</Badge>) : <Badge tone="neutral">Unmanaged</Badge>}
                    </div>
                  </td>
                  <td data-label="Shared with">{l.shared_with} user{l.shared_with === 1 ? "" : "s"}</td>
                  <td data-label="Exempt">
                    <Switch checked={l.is_exempt} onChange={(v) => void setExempt(l, v)} label={l.is_exempt ? "Exempt" : "Managed"} />
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn sm" disabled={!available("create_roles") || l.removed} title={why("create_roles") ?? "Create a Discord role for this library"} onClick={() => setRoleFor(l)}>
                      <Icon name="roles" size={14} /> Create role
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {roleFor && libs.data ? (
        <CreateRoleModal
          title={`Create a role for ${roleFor.title}`}
          libraries={libs.data.filter((l) => !l.removed)}
          initialName={`Plex · ${roleFor.title}`}
          initialLibraries={[roleFor.library_id]}
          onClose={() => setRoleFor(null)}
          onCreated={() => { setRoleFor(null); void libs.reload(); }}
        />
      ) : null}
    </>
  );
}
