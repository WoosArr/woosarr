import { useEffect, useRef, useState } from "react";
import { api, type ChannelOut, type LibraryOut, type PublishOut, type ReactionPostIn, type ReactionPostOut, type RoleCreated, type RoleOut } from "../api";
import { EmojiDisplay, EmojiPicker } from "../EmojiPicker";
import { useDiscordFeatures } from "../features";
import { Icon } from "../icons";
import { Badge, Empty, ErrorBanner, Field, Modal, PageHead, Spinner, ago, useConfirm, useLoad, useToast } from "../ui";

type Option = ReactionPostIn["options"] extends (infer O)[] | undefined ? O : never;
const NEW_ROLE = "__new__";

const DEFAULT_LINE = "{emoji} {role} — {libraries}";

/** Discord-ish preview: role mentions as coloured @chips, line breaks kept. */
function MessagePreview({ text, roles }: { text: string; roles: Map<string, RoleOut> }) {
  const parts = text.split(/(<@&\d+>)/g);
  return (
    <div className="discord-preview" aria-label="Message preview">
      {parts.map((part, i) => {
        const m = /^<@&(\d+)>$/.exec(part);
        if (!m) return <span key={i}>{part}</span>;
        const role = roles.get(m[1]);
        const hex = role?.color ? `#${role.color.toString(16).padStart(6, "0")}` : "var(--accent)";
        // Text stays in the theme colour (role colours can be low-contrast); the role colour tints it.
        return <span key={i} className="mention" style={{ borderBottom: `2px solid ${hex}`, background: `color-mix(in srgb, ${hex} 16%, transparent)` }}>@{role?.name ?? "role"}</span>;
      })}
    </div>
  );
}

const BLANK: ReactionPostIn = {
  title: "Plex libraries",
  content: "React below to get these roles (and the Plex libraries they unlock). Remove your reaction to drop the role.\nNot linked yet? Run /link so your roles can update your Plex access.",
  pin: true,
  enabled: true,
  legend: true,
  legend_template: DEFAULT_LINE,
  legend_header: "",
  options: [{ emoji: "🎬", role_id: "", label: "", text: "" }],
};

export default function ReactionsPage() {
  const posts = useLoad(() => api.get<ReactionPostOut[]>("/api/v1/reaction-posts"));
  const libraries = useLoad(() => api.get<LibraryOut[]>("/api/v1/libraries"));
  const roles = useLoad(() => api.get<RoleOut[]>("/api/v1/roles"));
  const [editing, setEditing] = useState<ReactionPostOut | "new" | null>(null);
  const { why, pending } = useDiscordFeatures();
  const titles = new Map((libraries.data ?? []).map((l) => [l.library_id, l.title]));
  const roleById = new Map((roles.data ?? []).map((r) => [r.role_id, r]));
  const libsOf = (roleIds: string[]) =>
    [...new Set(roleIds.flatMap((id) => roleById.get(id)?.library_ids ?? []))].map((id) => titles.get(id) ?? id);
  const blocked = why("reaction_posts");

  return (
    <>
      <PageHead title="Reaction Posts" sub="Pinned Discord posts where reacting gives members roles and removing the reaction takes them away.">
        <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" /> New post</button>
      </PageHead>
      {blocked ? (pending
        ? <div className="banner info" role="status"><Icon name="clock" /><div className="small">{blocked}</div></div>
        : <div className="banner warn"><Icon name="alert" /><div className="small">{blocked}. See Integrations → Discord for the bot's permissions.</div></div>) : null}
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
                {p.outdated ? <Badge tone="info" icon="refresh" title="The text changed (e.g. a role's libraries); the bot updates the post shortly">Update pending</Badge> : null}
                {!p.enabled ? <Badge tone="warn">Disabled</Badge> : null}
                <button className="btn sm" onClick={() => setEditing(p)}>Edit</button>
              </div>
            </div>
            <div className="row">
              {(p.options ?? []).map((o) => {
                const libs = libsOf(o.role_id ? [o.role_id] : []);
                return (
                  <span key={o.emoji} className="badge accent" title={libs.length ? `Libraries: ${libs.join(", ")}` : "These roles grant no libraries yet"}>
                    <EmojiDisplay value={o.emoji} size={16} />
                    {roleById.get(o.role_id)?.name ?? o.label ?? o.role_id}{" "}
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
    options: post.options, legend: post.legend, legend_template: post.legend_template, legend_header: post.legend_header,
  } : BLANK);
  const [preview, setPreview] = useState<{ text: string; length: number; max_length: number } | null>(null);
  const roleMap = new Map(roles.map((r) => [r.role_id, r]));
  const draftKey = JSON.stringify([draft.content, draft.legend, draft.legend_template, draft.legend_header,
    (draft.options ?? []).map((o) => [o.emoji, o.role_id, o.text])]);
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      const body = { ...draft, options: (draft.options ?? []).filter((o) => o.emoji && o.role_id) };
      api.post<{ text: string; length: number; max_length: number }>("/api/v1/reaction-posts/preview", body)
        .then((r) => live && setPreview(r), () => live && setPreview(null));
    }, 300);
    return () => { live = false; clearTimeout(t); };
  }, [draftKey]);
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

  const [newRoleFor, setNewRoleFor] = useState<number | null>(null);
  const [newRoleName, setNewRoleName] = useState("");
  const createRole = async (i: number) => {
    try {
      const r = await api.post<RoleCreated>("/api/v1/discord/roles", { name: newRoleName.trim(), purpose: "reaction" });
      setOpt(i, { role_id: r.role_id });
      setNewRoleFor(null);
      toast.ok(`Created role ${r.name}. Map it to libraries on the Roles page.`);
      onRolesChanged();
    } catch (e) {
      toast.error(e);
    }
  };

  const errorRef = useRef<HTMLDivElement>(null);
  const save = async (): Promise<ReactionPostOut | null> => {
    setError(null);
    try {
      return post
        ? await api.put<ReactionPostOut>(`/api/v1/reaction-posts/${post.id}`, draft)
        : await api.post<ReactionPostOut>("/api/v1/reaction-posts", draft);
    } catch (e) {
      setError(e);
      // The banner sits above a long form; bring it into view so the problem isn't missed.
      requestAnimationFrame(() => errorRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
      return null;
    }
  };

  const saveDraft = async () => {
    setBusy(true);
    const saved = await save();
    setBusy(false);
    if (saved) {
      toast.ok("Saved.");
      onSaved();
    }
  };

  const saveAndPublish = async () => {
    setBusy(true);
    const saved = await save();
    if (!saved) {
      setBusy(false);
      return;  // nothing saved: stay open with the error shown
    }
    try {
      const out = await api.post<PublishOut>(`/api/v1/reaction-posts/${saved.id}/publish`,
        { new_channel_name: draft.channel_id ? null : newChannel || null });
      toast.ok(`Posted in #${out.channel}${out.pinned ? " and pinned" : ""}.`);
    } catch (e) {
      // The changes are saved either way; say why Discord wasn't updated.
      toast.error(new Error(`Saved, but ${post?.message_id ? "the Discord post wasn't updated" : "not posted"}: `
        + (e instanceof Error ? e.message : String(e))));
    }
    setBusy(false);
    onSaved();
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
        <button className="btn" disabled={busy} onClick={() => void saveDraft()}>Save draft</button>
        <button className="btn primary" disabled={busy || !available("reaction_posts")} title={why("reaction_posts") ?? undefined}
          onClick={() => void saveAndPublish()}>{post?.message_id ? "Save & update post" : "Save & post"}</button>
      </>
    }>
      <div className="stack">
        <div ref={errorRef}><ErrorBanner error={error} /></div>
        <Field label="Name (only shown in WooSarr)"><input type="text" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></Field>
        <Field label="Message" hint="The intro members see in Discord. The legend below is added after it.">
          <textarea rows={4} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} maxLength={2000} />
        </Field>
        <label className="check"><input type="checkbox" checked={draft.legend ?? true} onChange={(e) => setDraft({ ...draft, legend: e.target.checked })} />
          <span>Show the roles and libraries under the message <span className="small muted">— updated automatically when a role's libraries change</span></span></label>
        {draft.legend ?? true ? (
          <>
            <Field label="Legend heading (optional)">
              <input type="text" value={draft.legend_header ?? ""} maxLength={200} placeholder="e.g. **React to get access:**" onChange={(e) => setDraft({ ...draft, legend_header: e.target.value })} />
            </Field>
            <Field label="Line for each reaction" hint="Placeholders: {emoji} {role} (mention) {role_name} {libraries}. Discord markdown works (**bold**, *italic*).">
              <input type="text" value={draft.legend_template ?? DEFAULT_LINE} maxLength={300} onChange={(e) => setDraft({ ...draft, legend_template: e.target.value || DEFAULT_LINE })} />
            </Field>
          </>
        ) : null}
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
                <div className="field">
                  <span style={{ display: "block", fontWeight: 600, fontSize: "0.88rem" }}>Emoji</span>
                  <EmojiPicker value={o.emoji} label={`Emoji ${i + 1}`} onChange={(v) => setOpt(i, { emoji: v })} />
                </div>
                <Field label="Gives role">
                  <select
                    value={newRoleFor === i ? NEW_ROLE : o.role_id}
                    onChange={(e) => {
                      if (e.target.value === NEW_ROLE) {
                        setNewRoleFor(i);
                        setNewRoleName("");
                      } else {
                        setNewRoleFor(null);
                        setOpt(i, { role_id: e.target.value });
                      }
                    }}
                    style={{ minWidth: 260 }}
                  >
                    <option value="">Choose a role…</option>
                    {assignable.map((r) => {
                      const libs = r.library_ids.map((id) => libraryTitles.get(id) ?? id);
                      return (
                        <option key={r.role_id} value={r.role_id}>
                          {r.name}{libs.length ? ` — ${libs.join(", ")}` : " — no libraries yet"}
                        </option>
                      );
                    })}
                    {available("create_roles") ? <option value={NEW_ROLE}>＋ Create new role…</option> : null}
                  </select>
                </Field>
                <button className="btn sm danger" onClick={() => setDraft({ ...draft, options: options.filter((_, j) => j !== i) })} aria-label={`Remove reaction ${i + 1}`}>
                  <Icon name="trash" size={14} />
                </button>
              </div>
              {draft.legend ?? true ? (
                <div style={{ marginTop: 8 }}>
                  <Field label="Custom line for this reaction (optional)" hint="Leave empty to use the line above. Same placeholders.">
                    <input type="text" value={o.text ?? ""} maxLength={300} placeholder={draft.legend_template ?? DEFAULT_LINE} onChange={(e) => setOpt(i, { text: e.target.value })} />
                  </Field>
                </div>
              ) : null}
              {newRoleFor === i ? (
                <div className="row" style={{ marginTop: 8, alignItems: "flex-end" }}>
                  <Field label="New role name">
                    <input type="text" value={newRoleName} maxLength={100} autoFocus onChange={(e) => setNewRoleName(e.target.value)} placeholder="e.g. Movie Night" style={{ width: 240 }} />
                  </Field>
                  <button className="btn sm primary" disabled={!newRoleName.trim()} onClick={() => void createRole(i)}>Create</button>
                  <button className="btn sm" onClick={() => setNewRoleFor(null)}>Cancel</button>
                </div>
              ) : null}
              {!available("create_roles") ? <p className="small muted" style={{ margin: "6px 0 0" }}>{why("create_roles")}</p> : null}
            </div>
          ))}
          <div><button className="btn sm" disabled={options.length >= 20} onClick={() => setDraft({ ...draft, options: [...options, { emoji: "", role_id: "", label: "", text: "" }] })}><Icon name="plus" size={14} /> Add reaction</button></div>
        </fieldset>
        <div>
          <div className="row spread">
            <h3 style={{ margin: 0 }}>Preview</h3>
            {preview ? (
              <span className="small" style={{ color: preview.length > preview.max_length ? "var(--danger)" : "var(--text-muted)" }}>
                {preview.length} / {preview.max_length} characters
              </span>
            ) : null}
          </div>
          {preview ? <MessagePreview text={preview.text} roles={roleMap} /> : <Spinner label="Rendering" />}
        </div>
      </div>
    </Modal>
  );
}
