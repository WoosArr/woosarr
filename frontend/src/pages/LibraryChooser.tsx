import type { LibraryOut } from "../api";

export function LibraryChooser({
  libraries,
  selected,
  onChange,
  label,
}: {
  libraries: LibraryOut[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  label: string;
}) {
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
      <legend className="small muted" style={{ marginBottom: 6 }}>{label}</legend>
      <div className="chooser">
        {libraries.filter((l) => !l.removed).map((l) => (
          <label key={l.library_id} className="check">
            <input type="checkbox" checked={selected.has(l.library_id)} onChange={() => toggle(l.library_id)} />
            <span>
              {l.title}
              {l.is_exempt ? <span className="small muted"> (exempt)</span> : null}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
