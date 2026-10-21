import { useEffect, useRef } from "react";

// Shared by Users → Plex and Users → Discord.

export type Filter = "" | "verified" | "unverified" | "linked" | "unlinked";

export function LinkFilter({ value, onChange }: { value: Filter; onChange: (f: Filter) => void }) {
  return (
    <div className="segmented" role="group" aria-label="Link filter">
      {(["", "verified", "unverified", "linked", "unlinked"] as Filter[]).map((f) => (
        <button key={f || "all"} aria-pressed={value === f} onClick={() => onChange(f)}>
          {f ? f[0].toUpperCase() + f.slice(1) : "All"}
        </button>
      ))}
    </div>
  );
}

export function TriStateBox({ state, marked, label, onToggle }: { state: "all" | "some" | "none"; marked: boolean; label: string; onToggle: () => void }) {
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
