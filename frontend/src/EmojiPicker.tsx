// Emoji picker for reaction posts: server emoji + common emoji, searchable.
// Typing/pasting any emoji (or <:name:id>) still works via the text field.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { COMMON_EMOJI } from "./emojiData";
import { Icon } from "./icons";

interface ServerEmoji {
  id: string;
  name: string;
  animated: boolean;
  text: string;
  url: string;
  available: boolean;
}

// Fetched each time the picker opens, so emoji added on the Custom Emoji page (or in
// Discord) show up without reloading the page.
function loadServerEmoji(): Promise<ServerEmoji[]> {
  return api.get<ServerEmoji[]>("/api/v1/discord/emojis").catch(() => []);
}

export function EmojiDisplay({ value, size = 20 }: { value: string; size?: number }) {
  const m = /^<(a?):(\w+):(\d+)>$/.exec(value);
  if (m) {
    return <img src={`https://cdn.discordapp.com/emojis/${m[3]}.${m[1] ? "gif" : "png"}?size=48`} alt={`:${m[2]}:`} width={size} height={size} style={{ verticalAlign: "middle" }} />;
  }
  return <span style={{ fontSize: size * 0.9, lineHeight: 1 }}>{value || "?"}</span>;
}

export function EmojiPicker({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [server, setServer] = useState<ServerEmoji[]>([]);
  // Animated emoji only work for members with Nitro: hidden unless asked for.
  const [hideAnimated, setHideAnimated] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    let live = true;
    void loadServerEmoji().then((list) => live && setServer(list));
    search.current?.focus();
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", onDoc);
    box.current?.addEventListener("keydown", onKey);
    const el = box.current;
    return () => { live = false; document.removeEventListener("mousedown", onDoc); el?.removeEventListener("keydown", onKey); };
  }, [open]);

  const needle = q.trim().toLowerCase();
  // Match the start of any keyword ("mov" finds movie, not remove).
  const common = useMemo(
    () => COMMON_EMOJI.filter(([e, k]) => !needle || e === needle || k.split(" ").some((w) => w.startsWith(needle))),
    [needle],
  );
  const custom = server.filter((e) => e.available && !(hideAnimated && e.animated) && (!needle || e.name.toLowerCase().includes(needle)));
  const animatedCount = server.filter((e) => e.available && e.animated).length;
  const pick = (v: string) => { onChange(v); setOpen(false); setQ(""); };

  return (
    <div className="emoji-picker" ref={box}>
      <div className="row" style={{ gap: 4 }}>
        <button type="button" className="btn emoji-btn" onClick={() => setOpen(!open)} aria-haspopup="dialog" aria-expanded={open} aria-label={`${label}: choose emoji`}>
          <EmojiDisplay value={value} /> <Icon name="menu" size={12} />
        </button>
        <input type="text" className="inline-input" value={value} onChange={(e) => onChange(e.target.value)} aria-label={label} style={{ width: 120 }} placeholder="or paste" />
      </div>
      {open ? (
        <div className="emoji-popover" role="dialog" aria-label="Choose an emoji">
          <input ref={search} type="search" placeholder="Search emoji (e.g. movie, tv, anime)…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search emoji" />
          {animatedCount ? (
            <label className="check small" style={{ marginTop: 6 }}>
              <input type="checkbox" checked={hideAnimated} onChange={(e) => setHideAnimated(e.target.checked)} />
              <span>Hide animated <span className="muted">({animatedCount}; members need Nitro to use them)</span></span>
            </label>
          ) : null}
          {custom.length ? (
            <>
              <div className="small muted emoji-section">This server</div>
              <div className="emoji-grid">
                {custom.map((e) => (
                  <button key={e.id} type="button" className="emoji-cell" title={`:${e.name}:`} aria-label={`:${e.name}:`} onClick={() => pick(e.text)}>
                    <img src={e.url} alt="" width={22} height={22} />
                  </button>
                ))}
              </div>
            </>
          ) : null}
          <div className="small muted emoji-section">Common</div>
          <div className="emoji-grid">
            {common.map(([e, k]) => (
              <button key={e} type="button" className="emoji-cell" title={k} aria-label={`${e} ${k.split(" ")[0]}`} onClick={() => pick(e)}>{e}</button>
            ))}
          </div>
          {!common.length && !custom.length ? <p className="small muted">No match. Paste any emoji into the field instead.</p> : null}
        </div>
      ) : null}
    </div>
  );
}
