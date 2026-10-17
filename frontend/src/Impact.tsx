// Shared "who gains / loses what" summary (Roles editor, Settings, Remove libraries).
import { Badge } from "./ui";

export interface UserChange {
  plex_user_id: string;
  username: string;
  gain?: string[];
  lose?: string[];
  kept?: string[];
}

export function ImpactList({ changes, titles, empty }: { changes: UserChange[]; titles: Map<string, string>; empty: string }) {
  const names = (ids: string[]) => ids.map((id) => titles.get(id) ?? id).join(", ");
  const gaining = changes.filter((c) => c.gain?.length).length;
  const losing = changes.filter((c) => c.lose?.length).length;
  if (!changes.some((c) => c.gain?.length || c.lose?.length || c.kept?.length)) {
    return <p className="small muted" style={{ margin: 0 }}>{empty}</p>;
  }
  return (
    <>
      <p className="small" style={{ marginBottom: 6 }}>
        <Badge tone="ok" icon="plus">{gaining} gain</Badge> <Badge tone="danger" icon="minus">{losing} lose</Badge>
      </p>
      <ul className="small" style={{ margin: 0, paddingLeft: 18, maxHeight: 200, overflow: "auto" }}>
        {changes.filter((c) => c.gain?.length || c.lose?.length || c.kept?.length).map((c) => (
          <li key={c.plex_user_id}>
            <strong>{c.username}</strong>
            {c.gain?.length ? <> gains {names(c.gain)}</> : null}
            {c.gain?.length && c.lose?.length ? ";" : null}
            {c.lose?.length ? <> loses {names(c.lose)}</> : null}
            {c.kept?.length ? <span className="muted"> — keeps {names(c.kept)} (Discord roles grant it)</span> : null}
          </li>
        ))}
      </ul>
    </>
  );
}
