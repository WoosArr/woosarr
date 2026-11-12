import { useEffect, useState } from "react";
import { api, qs, type EmojiOut, type EmojiSearchOut, type EmojiSlotsOut, type EmojiSourceOut, type LibraryEmojiOut } from "../api";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Empty, ErrorBanner, Field, Modal, PageHead, Spinner, Switch, ago, useConfirm, useLoad, useToast } from "../ui";

type Source = EmojiSourceOut["key"];
const PAGE = 60;
const NAME_RE = /^[A-Za-z0-9_]{2,32}$/;
const nameError = (n: string) => (NAME_RE.test(n) ? null : "2 to 32 letters, digits or underscores");
const when = (t: number | null | undefined) => (t ? ago(new Date(t * 1000).toISOString()) : "never");

/** Custom Emoji: search emoji.gg, OpenMoji and slackmojis, add to the Discord server, manage the server's emoji. */
export default function EmojiPage() {
  const { why, pending } = useDiscordFeatures();
  const blocked = pending ? null : why("custom_emoji");
  const server = useLoad(() => api.get<EmojiOut[]>("/api/v1/discord/emojis"));
  const slots = useLoad(() => api.get<EmojiSlotsOut>("/api/v1/discord/emojis/slots"));
  const reloadServer = () => { void server.reload(); void slots.reload(); };

  return (
    <>
      <PageHead title="Custom Emoji" sub="Find emoji on emoji.gg, OpenMoji and slackmojis and add them to your Discord server. Added emoji show up in the reaction post picker." />
      {blocked ? (
        <div className="banner warn small"><Icon name="alert" /><div>{blocked}. You can browse the libraries; see Integrations → Discord → Bot features and permissions to let the bot add emoji.</div></div>
      ) : null}
      <ServerEmoji emoji={server.data} error={server.error} slots={slots.data} blocked={blocked} onChange={reloadServer} />
      <Library blocked={blocked} onAdded={reloadServer} />
    </>
  );
}

function ServerEmoji({ emoji, error, slots, blocked, onChange }: {
  emoji: EmojiOut[] | null; error: unknown; slots: EmojiSlotsOut | null; blocked: string | null; onChange: () => void;
}) {
  const [renaming, setRenaming] = useState<EmojiOut | null>(null);
  const confirm = useConfirm();
  const toast = useToast();
  const sorted = [...(emoji ?? [])].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

  const remove = async (e: EmojiOut) => {
    const ok = await confirm({
      title: `Delete :${e.name}:?`,
      body: <p>The emoji is removed from the Discord server, and from every message that uses it. This can't be undone.</p>,
      confirmLabel: "Delete", danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/v1/discord/emojis/${e.id}`);
      toast.ok(`:${e.name}: deleted.`);
      onChange();
    } catch (err) {
      toast.error(err);
    }
  };

  return (
    <section className="card" style={{ marginBottom: 16 }} aria-labelledby="server-emoji">
      <div className="row spread">
        <h2 id="server-emoji">On your server</h2>
        {slots ? (
          <span className="small muted">Static {slots.static} of {slots.limit} · Animated {slots.animated} of {slots.limit}</span>
        ) : null}
      </div>
      <ErrorBanner error={error} />
      {!emoji && !error ? <Spinner /> : null}
      {emoji && emoji.length === 0 ? <p className="small muted">No custom emoji yet. Add some from the libraries below.</p> : null}
      {sorted.length ? (
        <ul className="server-emoji" aria-label="Server emoji">
          {sorted.map((e) => (
            <li key={e.id}>
              <img src={e.url} alt="" width={32} height={32} loading="lazy" />
              <span className="small emoji-name" title={`:${e.name}:`}>:{e.name}:</span>
              {e.animated ? <span className="tag-gif" title="Animated">GIF</span> : null}
              <button className="btn sm ghost" disabled={!!blocked} title={blocked ?? undefined} onClick={() => setRenaming(e)} aria-label={`Rename :${e.name}:`}>Rename</button>
              <button className="btn sm ghost icon-btn" disabled={!!blocked} title={blocked ?? `Delete :${e.name}:`} onClick={() => void remove(e)} aria-label={`Delete :${e.name}:`}><Icon name="trash" size={15} /></button>
            </li>
          ))}
        </ul>
      ) : null}
      {renaming ? <RenameModal emoji={renaming} onClose={() => setRenaming(null)} onDone={() => { setRenaming(null); onChange(); }} /> : null}
    </section>
  );
}

function RenameModal({ emoji, onClose, onDone }: { emoji: EmojiOut; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(emoji.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const invalid = nameError(name);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/api/v1/discord/emojis/${emoji.id}`, { name });
      toast.ok(`Renamed to :${name}:. Reaction posts using it follow the new name.`);
      onDone();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal title={`Rename :${emoji.name}:`} onClose={onClose} actions={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!!invalid || busy || name === emoji.name} onClick={() => void submit()}>{busy ? "Renaming…" : "Rename"}</button>
      </>
    }>
      <ErrorBanner error={error} />
      <Field label="Name" error={invalid}>
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={32} />
      </Field>
    </Modal>
  );
}

function Library({ blocked, onAdded }: { blocked: string | null; onAdded: () => void }) {
  const sources = useLoad(() => api.get<EmojiSourceOut[]>("/api/v1/emoji/sources"));
  const [source, setSource] = useState<Source>("emojigg");
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [hideAnimated, setHideAnimated] = useState(true);
  const [items, setItems] = useState<LibraryEmojiOut[]>([]);
  const [total, setTotal] = useState(0);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [adding, setAdding] = useState<LibraryEmojiOut | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 350);
    return () => clearTimeout(t);
  }, [query]);

  const load = (offset: number, live: () => boolean) => {
    setLoading(true);
    setError(null);
    return api.get<EmojiSearchOut>(`/api/v1/emoji/search${qs({ source, q, hide_animated: String(hideAnimated), offset: String(offset), limit: String(PAGE) })}`)
      .then((r) => {
        if (!live()) return;
        setItems((prev) => (offset ? [...prev, ...r.items] : r.items));
        setTotal(r.total);
        setMore(r.more);
      }, (e) => live() && setError(e))
      .finally(() => live() && setLoading(false));
  };
  useEffect(() => {
    let live = true;
    setItems([]);
    setMore(false);
    setTotal(0);
    if (source === "slackmojis" && !q) {
      setLoading(false);
      return;
    }
    void load(0, () => live);
    return () => { live = false; };
  }, [source, q, hideAnimated]);

  const info = sources.data?.find((s) => s.key === source);
  const needsQuery = source === "slackmojis" && !q;

  return (
    <section className="card" aria-labelledby="emoji-library">
      <h2 id="emoji-library">Libraries</h2>
      <div className="row spread" style={{ marginBottom: 10 }}>
        <div className="row">
          <div className="segmented" role="tablist" aria-label="Emoji library">
            {(sources.data ?? []).map((s) => (
              <button key={s.key} role="tab" aria-selected={source === s.key} onClick={() => setSource(s.key)}>{s.label}</button>
            ))}
          </div>
          <input type="search" className="inline-input" placeholder={source === "slackmojis" ? "Search slackmojis…" : "Search…"} value={query}
            onChange={(e) => setQuery(e.target.value)} aria-label="Search emoji" style={{ width: 240 }} />
        </div>
        <Switch checked={hideAnimated} onChange={setHideAnimated} label="Hide animated" />
      </div>
      {info ? (
        <p className="small muted">
          {info.live ? "Searched on slackmojis.com as you type." : <>{info.count?.toLocaleString() ?? 0} emoji{info.version ? ` (version ${info.version})` : ""}, checked for updates {when(info.checked_at)}; updated daily.</>}
          {" "}{info.license}{hideAnimated ? " Animated emoji are hidden: members need Discord Nitro to use them." : ""}
        </p>
      ) : null}
      {info?.error ? <div className="banner warn small"><Icon name="alert" /><div>Last update failed: {info.error}. {info.count ? "Showing the copy downloaded earlier." : ""}</div></div> : null}
      <ErrorBanner error={sources.error ?? error} />
      {needsQuery ? <Empty icon="smile" title="Search slackmojis">slackmojis has no full list to browse; type what you're looking for.</Empty> : null}
      {!needsQuery && !loading && !error && items.length === 0 ? <Empty icon="smile" title="No emoji found">Try another word{hideAnimated ? ", or show animated emoji" : ""}.</Empty> : null}
      {items.length ? (
        <>
          <p className="small muted" aria-live="polite">{more && source === "slackmojis" ? `${items.length}+` : total.toLocaleString()} found</p>
          <ul className="library-grid" aria-label="Emoji results">
            {items.map((e) => (
              <li key={`${e.source}-${e.id}`}>
                <button type="button" onClick={() => setAdding(e)} title={`${e.title}${e.category ? ` · ${e.category}` : ""}`} aria-label={`${e.title}: add to server`}>
                  <img src={`/api/v1/emoji/preview/${e.source}/${encodeURIComponent(e.id)}`} alt="" width={48} height={48} loading="lazy"
                    onError={(ev) => { ev.currentTarget.style.visibility = "hidden"; }} />
                  <span className="small">{e.name}</span>
                  {e.animated ? <span className="tag-gif corner" title="Animated">GIF</span> : null}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {loading ? <Spinner label="Searching" /> : null}
      {more && !loading ? (
        <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
          <button className="btn" onClick={() => void load(items.length, () => true)}>Show more</button>
        </div>
      ) : null}
      {adding ? <AddModal item={adding} blocked={blocked} onClose={() => setAdding(null)} onDone={() => { setAdding(null); onAdded(); }} /> : null}
    </section>
  );
}

function AddModal({ item, blocked, onClose, onDone }: { item: LibraryEmojiOut; blocked: string | null; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(item.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const invalid = nameError(name);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const added = await api.post<EmojiOut>("/api/v1/emoji/add", { source: item.source, id: item.id, name });
      toast.ok(`:${added.name}: added to the server.`);
      onDone();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal title="Add to Discord" onClose={onClose} actions={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!!invalid || busy || !!blocked} title={blocked ?? undefined} onClick={() => void submit()}>
          {busy ? "Adding…" : "Add to server"}
        </button>
      </>
    }>
      <ErrorBanner error={error} />
      {blocked ? <div className="banner warn small"><Icon name="alert" /><div>{blocked}</div></div> : null}
      <div className="row" style={{ gap: 16, alignItems: "flex-start" }}>
        <img src={`/api/v1/emoji/preview/${item.source}/${encodeURIComponent(item.id)}`} alt={item.title} width={96} height={96} className="emoji-large" />
        <div style={{ flex: 1 }}>
          <Field label="Name" hint="What members type, as :name:" error={invalid}>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={32} />
          </Field>
          <p className="small muted">
            {item.title}{item.category ? ` · ${item.category}` : ""}{item.animated ? " · animated (uses an animated slot; members need Nitro)" : ""}
            {item.credit ? <><br />By {item.credit}</> : null}
            <br /><a href={item.page} target="_blank" rel="noopener noreferrer">View on {item.source === "emojigg" ? "emoji.gg" : item.source === "openmoji" ? "OpenMoji" : "slackmojis"}</a>
          </p>
          <p className="small muted">WooSarr resizes it to fit Discord (128 px, 256 KB).</p>
        </div>
      </div>
    </Modal>
  );
}
