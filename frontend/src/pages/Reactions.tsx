import { useState } from "react";
import { api, type ChannelOut, type LibraryOut, type PublishOut, type ReactionPostIn, type ReactionPostOut } from "../api";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, Field, Modal, PageHead, Spinner, ago, useConfirm, useLoad, useToast } from "../ui";

type Option = ReactionPostIn["options"] extends (infer O)[] | undefined ? O : never;

const BLANK: ReactionPostIn = {
  title: "Plex libraries",
  content: "React below to get access to these Plex libraries. Remove your reaction to give it back.\nNot linked yet? Run /link first.",
  pin: true,
  enabled: true,
  options: [{ emoji: "🎬", label: "Movies", library_ids: [] }],
};

export default function ReactionsPage() {
  const posts = useLoad(() => api.get<ReactionPostOut[]>("/api/v1/reaction-posts"));
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const [editing, setEditing] = useState<ReactionPostOut | "new" | null>(null);
  const { why } = useDiscordFeatures();
  const titles = new Map((libraries.data ?? []).map((l) => [l.library_id, l.title]));
  const blocked = why("reaction_posts");

  return (
    <>
      <PageHead title="Reactions" sub="Pinned Discord posts where reacting grants libraries and removing the reaction takes them away.">
        <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" /> New post</button>
      </PageHead>
      {blocked ? <div className="banner warn"><Icon name="alert" /><div className="small">{blocked}. See Setup → Discord for the bot's permissions.</div></div> : null}
      <div className="banner info">
        <Icon name="info" />
        <div className="small">
          Reaction access works like a manual grant: it stays when roles change and is removed when the member removes the reaction.
          Only linked members get access; reactions from unlinked members apply once they <code>/link</code>.
        </div>
      </div>
      <ErrorBanner error={posts.error} />
      {posts.loading && !posts.data ? <Spinner /> : null}
      {posts.data?.length === 0 ? <div className="card"><Empty icon="roles" title="No reaction posts yet" /></div> : null}
      <div className="stack">
        {posts.data?.map((p) => (
          <section key={p.id} className="card stack" aria-labelledby={`post-${p.id}`}>
            <div className="row spread">
              <h2 id={`post-${p.id}`}>{p.title}</h2>
              <div className="row">
                {p.message_id ? <Badge tone="ok">Posted {ago(p.published_at)}</Badge> : <Badge tone="neutral">Draft</Badge>}
                {!p.enabled ? <Badge tone="warn">Disabled</Badge> : null}
                <button className="btn sm" onClick={() => setEditing(p)}>Edit</button>
              </div>
            </div>
            <div className="row">
              {(p.options ?? []).map((o) => (
                <span key={o.emoji} className="badge accent">
                  <span aria-hidden="true">{o.emoji.startsWith("<") ? "🔹" : o.emoji}</span>
                  {o.label || o.library_ids.map((id) => titles.get(id) ?? id).join(", ")}{" "}
                  <span className="muted">· {p.reactions[o.emoji] ?? 0}</span>
                </span>
              ))}
            </div>
          </section>
        ))}
      </div>
      {editing && libraries.data ? (
        <PostEditor post={editing === "new" ? null : editing} libraries={libraries.data}
          onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void posts.reload(); }} />
      ) : null}
    </>
  );
}

function PostEditor({ post, libraries, onClose, onSaved }: {
  post: ReactionPostOut | null; libraries: LibraryOut[]; onClose: () => void; onSaved: () => void;
}) {
  const [draft, setDraft] = useState<ReactionPostIn>(post ? {
    title: post.title, content: post.content, channel_id: post.channel_id, pin: post.pin, enabled: post.enabled,
    options: post.options,
  } : BLANK);
  const [newChannel, setNewChannel] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const channels = useLoad(() => api.get<ChannelOut[]>("/api/v1/discord/channels"));
  const { available, why } = useDiscordFeatures();
  const toast = useToast();
  const confirm = useConfirm();
  const options = draft.options ?? [];
  const setOpt = (i: number, patch: Partial<Option>) =>
    setDraft({ ...draft, options: options.map((o, j) => (j === i ? { ...o, ...patch } : o)) });

  const save = async (): Promise<ReactionPostOut | null> => {
    setError(null);
    try {
      return post
        ? await api.put<ReactionPostOut>(`/api/v1/reaction-posts/${post.id}`, draft)
        : await api.post<ReactionPostOut>("/api/v1/reaction-posts", draft);
    } catch (e) {
      setError(e);
      return null;
    }
  };

  const saveAndPublish = async () => {
    setBusy(true);
    const saved = await save();
    if (saved) {
      try {
        const out = await api.post<PublishOut>(`/api/v1/reaction-posts/${saved.id}/publish`,
          { new_channel_name: draft.channel_id ? null : newChannel || null });
        toast.ok(`Posted in #${out.channel}${out.pinned ? " and pinned" : ""}.`);
        onSaved();
      } catch (e) {
        setError(e);
      }
    }
    setBusy(false);
  };

  return (
    <Modal title={post ? `Edit "${post.title}"` : "New reaction post"} onClose={onClose} actions={
      <>
        {post ? <button className="btn danger" onClick={async () => {
          if (await confirm({ title: "Delete this post?", body: <p>The Discord message is deleted too, and access granted by its reactions is removed on the next sync.</p>, confirmLabel: "Delete", danger: true })) {
            try { await api.del(`/api/v1/reaction-posts/${post.id}`); onSaved(); } catch (e) { setError(e); }
          }
        }}>Delete</button> : null}
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn" disabled={busy} onClick={async () => { if (await save()) { toast.ok("Saved."); onSaved(); } }}>Save draft</button>
        <button className="btn primary" disabled={busy || !available("reaction_posts")} title={why("reaction_posts") ?? undefined}
          onClick={() => void saveAndPublish()}>{post?.message_id ? "Save & update post" : "Save & post"}</button>
      </>
    }>
      <div className="stack">
        <ErrorBanner error={error} />
        <Field label="Name (only shown in WooSarr)"><input type="text" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></Field>
        <Field label="Message" hint="What members see in Discord (max 2000 characters).">
          <textarea rows={4} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} maxLength={2000} />
        </Field>
        <Field label="Channel" hint={channels.error ? "Connect the Discord bot to list channels." : undefined}>
          <select value={draft.channel_id ?? ""} onChange={(e) => setDraft({ ...draft, channel_id: e.target.value || null })}>
            <option value="">{available("create_channel") ? "New channel…" : "Choose…"}</option>
            {channels.data?.map((c) => <option key={c.id} value={c.id} disabled={!c.can_post}>#{c.name}{c.category ? ` (${c.category})` : ""}{c.can_post ? "" : " — bot can't post here"}</option>)}
          </select>
        </Field>
        {!draft.channel_id ? (
          <Field label="New channel name" hint={why("create_channel") ?? "Created if it doesn't exist (an existing channel with this name is reused)."}>
            <input type="text" value={newChannel} placeholder="plex-access" disabled={!available("create_channel")} onChange={(e) => setNewChannel(e.target.value)} />
          </Field>
        ) : null}
        <label className="check"><input type="checkbox" checked={!!draft.pin} onChange={(e) => setDraft({ ...draft, pin: e.target.checked })} />
          <span>Pin the post {!available("pin_posts") ? <span className="small muted">({why("pin_posts")})</span> : null}</span></label>
        <label className="check"><input type="checkbox" checked={!!draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          <span>Enabled <span className="small muted">— off = reactions grant nothing</span></span></label>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }} className="stack">
          <legend className="small" style={{ fontWeight: 600 }}>Reactions (up to 20)</legend>
          {options.map((o, i) => (
            <div key={i} className="card" style={{ boxShadow: "none", padding: 12 }}>
              <div className="row" style={{ alignItems: "flex-end" }}>
                <Field label="Emoji" hint="🎬 or a server emoji like <:plex:123…>">
                  <input type="text" value={o.emoji} onChange={(e) => setOpt(i, { emoji: e.target.value })} style={{ width: 160 }} />
                </Field>
                <Field label="Label">
                  <input type="text" value={o.label ?? ""} onChange={(e) => setOpt(i, { label: e.target.value })} style={{ width: 200 }} />
                </Field>
                <button className="btn sm danger" onClick={() => setDraft({ ...draft, options: options.filter((_, j) => j !== i) })} aria-label={`Remove reaction ${i + 1}`}>
                  <Icon name="trash" size={14} />
                </button>
              </div>
              <div className="chooser" style={{ marginTop: 8 }} role="group" aria-label={`Libraries for reaction ${i + 1}`}>
                {libraries.filter((l) => !l.removed).map((l) => {
                  const on = o.library_ids.includes(l.library_id);
                  return (
                    <label key={l.library_id} className="check">
                      <input type="checkbox" checked={on} onChange={() => setOpt(i, { library_ids: on ? o.library_ids.filter((x) => x !== l.library_id) : [...o.library_ids, l.library_id] })} />
                      <span>{l.title}</span>
                    </label>
                  );
                })}
              </div>
            </div>
          ))}
          <div><button className="btn sm" disabled={options.length >= 20} onClick={() => setDraft({ ...draft, options: [...options, { emoji: "", label: "", library_ids: [] }] })}><Icon name="plus" size={14} /> Add reaction</button></div>
        </fieldset>
      </div>
    </Modal>
  );
}
