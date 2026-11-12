import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AppSettings } from "./api";

/** Settings → General → Hide deleted items (on by default; also while the setting loads, so nothing flickers). */
export function useHideDeleted(): boolean {
  const [hide, setHide] = useState(true);
  useEffect(() => {
    let live = true;
    api.get<AppSettings>("/api/v1/settings").then((s) => live && setHide(s.hide_deleted ?? true), () => undefined);
    return () => { live = false; };
  }, []);
  return hide;
}

/** "2 roles deleted in Discord are hidden": says where to show them. what = [one, several]. */
export function HiddenNote({ count, what }: { count: number; what: [string, string] }) {
  if (!count) return null;
  return (
    <p className="small muted">
      {count} {count === 1 ? `${what[0]} is` : `${what[1]} are`} hidden. <Link to="/settings">Settings → General → Hide deleted items</Link> shows them.
    </p>
  );
}
