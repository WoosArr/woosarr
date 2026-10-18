"""Make untrusted text (TMDB titles/overviews via Seerr, Seerr error messages,
what members typed) safe to put into Discord messages.

discord.utils.escape_markdown misses fragments such as `](https://x) [`, which
turn part of a line into a masked link once the text sits inside our own
`[label](url)`. Mentions are escaped too, so `<@&role>` / `@everyone` show as
text even where allowed_mentions would already stop the ping.
"""

from __future__ import annotations

import re

import discord

_EXTRA = re.compile(r"(?<!\\)([\[\]()<])")  # skip what escape_markdown already escaped
_SPACE = re.compile(r"\s+")


def safe(text: str, limit: int | None = None) -> str:
    """Plain, single-line text that renders exactly as written."""
    text = _SPACE.sub(" ", str(text)).strip()
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = discord.utils.escape_markdown(text, as_needed=False)
    text = _EXTRA.sub(r"\\\1", text)
    # Zero-width joiner after @ so @everyone / @here never form a mention.
    return text.replace("@everyone", "@‍everyone").replace("@here", "@‍here")
