import { useState } from "react";
import { api, type ChannelOut, type LibraryOut, type PublishOut, type ReactionPostIn, type ReactionPostOut, type RoleCreated, type RoleOut } from "../api";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, Field, Modal, PageHead, Spinner, ago, useConfirm, useLoad, useToast } from "../ui";

type Option = ReactionPostIn["options"] extends (infer O)[] | undefined ? O : never;

const BLANK: ReactionPostIn = {
  title: "Plex libraries",
  content: "React below to get these roles (and the Plex libraries they unlock). Remove your reaction to drop the role.\nNot linked yet? Run /link so your roles can update your Plex access.",
  pin: true,
  enabled: true,
  options: [{ emoji: "🎬", label: "Movies", role_ids: [] }],
};

export default function ReactionsPage() {
  const posts = useLoad(() => api.get<ReactionPostOut[]>("/api/v1/reaction-posts"));
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const [editing, setEditing] = useState<ReactionPostOut | "new" | null>(null);
  const { why } = useDiscordFeatures();
  const titles = new Map((libraries.data ?? []).map((l) => [l.library_id, l.title]));
  const roleById = new Map((roles.data ?? []).map((r) => [r.role_id, r]));
  const libsOf = (roleIds: string[]) =>
    [...new Set(roleIds.flatMap((id) => roleById.get(id)?.library_ids ?? []))].map((id) => titles.get(id) ?? id);
  const blocked = why("reaction_posts");

  return (
    <>
      <PageHead title="Reactions" sub="Pinned Discord posts where reacting gives members roles and removing the reaction takes them away.">
        <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" /> New post</button>
      </PageHead>
      {blocked ? <div className="banner warn"><Icon name="alert" /><div className="small">{blocked}. See Setup → Discord for the bot's permissions.</div></div> : null}
      <div className="banner info">
        <Icon name="info" />
        <div className="small">
          Reactions give and remove <strong>Discord roles</strong>. Plex libraries then follow from the role mappings on the Roles page,
          for linked members. Removing a reaction removes its roles unless another reaction the member kept gives the same role.
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
              {(p.options ?? []).map((o) => {
                const libs = libsOf(o.role_ids);
                return (
                  <span key={o.emoji} className="badge accent" title={libs.length ? `Libraries: ${libs.join(", ")}` : "These roles grant no libraries yet"}>
                    <span aria-hidden="true">{o.emoji.startsWith("<") ? "🔹" : o.emoji}</span>
                    {o.label || o.role_ids.map((id) => roleById.get(id)?.name ?? id).join(", ")}{" "}
                    <span className="muted">· {p.reactions[o.emoji] ?? 0}</span>
                  </span>
                );
              })}
            </div>
          </section>
        ))}
      </div>
      {editing && roles.data ? (
        <PostEditor post={editing === "new" ? null : editing} roles={roles.data} libraryTitles={titles}
          onRolesChanged={() => void roles.reload()}
          onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void posts.reload(); }} />
      ) : null}
    </>
  );
}

function PostEditor({ post, roles, libraryTitles, onRolesChanged, onClose, onSaved }: {
  post: ReactionPostOut | null; roles: RoleOut[]; libraryTitles: Map<string, string>;
  onRolesChanged: () => void; onClose: () => void; onSaved: () => void;
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
    setDraft((d) => ({ ...d, options: (d.options ?? []).map((o, j) => (j === i ? { ...o, ...patch } : o)) }));
  const assignable = roles.filter((r) => !r.managed && !r.deleted);

  const createRole = async (i: number) => {
    const name = options[i].label?.trim() || `Reaction ${options[i].emoji}`;
    try {
      const r = await api.post<RoleCreated>("/api/v1/discord/roles", { name, purpose: "reaction" });
      setOpt(i, { role_ids: [...options[i].role_ids, r.role_id] });
      toast.ok(`Created role ${r.name}. Map it to libraries on the Roles page.`);
      onRolesChanged();
    } catch (e) {
      toast.error(e);
    }
  };

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
              <div className="chooser" style={{ marginTop: 8 }} role="group" aria-label={`Roles for reaction ${i + 1}`}>
                {assignable.map((r) => {
                  const on = o.role_ids.includes(r.role_id);
                  const libs = r.library_ids.map((id) => libraryTitles.get(id) ?? id);
                  return (
                    <label key={r.role_id} className="check" title={libs.length ? `Grants: ${libs.join(", ")}` : "Grants no libraries yet"}>
                      <input type="checkbox" checked={on} onChange={() => setOpt(i, { role_ids: on ? o.role_ids.filter((x) => x !== r.role_id) : [...o.role_ids, r.role_id] })} />
                      <span>{r.name}{libs.length ? <span className="small muted"> · {libs.join(", ")}</span> : null}</span>
                    </label>
                  );
                })}
              </div>
              <div style={{ marginTop: 8 }}>
                <button className="btn sm" disabled={!available("create_roles")} title={why("create_roles") ?? "Create a new Discord role for this reaction"} onClick={() => void createRole(i)}>
                  <Icon name="plus" size={14} /> Create role{o.label ? ` "${o.label}"` : ""}
                </button>
              </div>
            </div>
          ))}
          <div><button className="btn sm" disabled={options.length >= 20} onClick={() => setDraft({ ...draft, options: [...options, { emoji: "", label: "", role_ids: [] }] })}><Icon name="plus" size={14} /> Add reaction</button></div>
        </fieldset>
      </div>
    </Modal>
  );
}
