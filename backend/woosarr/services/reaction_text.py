"""Text of a reaction post: the admin's intro plus a legend line per reaction.

Lines come from a template with a fixed set of placeholders; only these are
substituted (no str.format, so templates can't reach object attributes):

  {emoji}      the reaction's emoji
  {role}       the role as a Discord mention (shows name + colour; nobody is pinged)
  {role_name}  the role's name as plain text
  {libraries}  the Plex libraries the role unlocks ("no libraries yet" if none)
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

DEFAULT_LINE = "{emoji} {role} — {libraries}"
DEFAULT_HEADER = ""
MAX_MESSAGE = 2000  # Discord's limit
PLACEHOLDERS = ("emoji", "role", "role_name", "libraries")
_TOKEN = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def render_line(template: str, values: Mapping[str, str]) -> str:
    return _TOKEN.sub(lambda m: values.get(m.group(1), ""), template)


def render(
    content: str,
    options: Sequence[Mapping],
    *,
    legend: bool,
    line_template: str,
    header: str,
    role_names: Mapping[str, str],
    role_libraries: Mapping[str, Sequence[str]],
) -> str:
    """The full message text for a post."""
    parts = [content.rstrip()]
    if legend and options:
        lines = []
        for o in options:
            role_id = str(o.get("role_id") or "")
            libs = list(role_libraries.get(role_id, ()))
            values = {
                "emoji": str(o.get("emoji", "")),
                "role": f"<@&{role_id}>" if role_id else "",
                "role_name": role_names.get(role_id, ""),
                "libraries": ", ".join(libs) if libs else "no libraries yet",
            }
            lines.append(render_line((o.get("text") or "").strip() or line_template, values))
        block = "\n".join(lines)
        parts.append(f"{header.strip()}\n{block}" if header.strip() else block)
    return "\n\n".join(p for p in parts if p)


def render_post(session, post) -> str:
    """Render a ReactionPost (or anything with the same fields) from the DB caches."""
    from collections import defaultdict

    from sqlalchemy import select

    from woosarr.db.models import DiscordRole, Library, RoleLibrary

    titles = dict(session.execute(select(Library.library_id, Library.title).where(Library.removed.is_(False))).all())
    role_libraries: dict[str, list[str]] = defaultdict(list)
    for row in session.scalars(select(RoleLibrary)):
        if row.library_id in titles:
            role_libraries[row.role_id].append(titles[row.library_id])
    for libs in role_libraries.values():
        libs.sort(key=str.lower)
    role_names = dict(session.execute(select(DiscordRole.role_id, DiscordRole.name)).all())
    return render(post.content, list(post.options or []), legend=post.legend, line_template=post.legend_template,
                  header=post.legend_header, role_names=role_names, role_libraries=role_libraries)
