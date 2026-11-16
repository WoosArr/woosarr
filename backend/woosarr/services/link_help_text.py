"""Editable texts for helping unlinked members (P14-09): the DM's parts, the button labels and colours.

Kept apart from link_help.py (which reads the settings) so core/app_settings.py can validate with it.
The DM is built from up to four parts (Settings → Linking → Unlinked members who react):

* main text: always.
* invite: the member may invite themselves (Plex Invites, or an admin), so the invite button is there.
* no_invite: they may not; e.g. ask an admin.
* closing: always, after the others.

An empty part is left out. Placeholders in braces are filled in (PLACEHOLDERS); role and server
names are escaped so they can't add Discord formatting or mentions.
"""

from __future__ import annotations

import re

DEFAULT_TEXT = ("You reacted to get {roles} in **{server}**. Roles only unlock Plex libraries once your Discord "
                "account is linked to your Plex account.\n"
                "• Already on the Plex server? Press **{link_button}**.")
DEFAULT_INVITE = "• New to the Plex server? Press **{invite_button}** and enter your Plex email or username."
DEFAULT_NO_INVITE = "• New to the Plex server? Ask a server admin for an invite."
DEFAULT_CLOSING = "You can also use `/link` and `/invite` in the server."

DEFAULT_LINK_LABEL = "Link my Plex account"
DEFAULT_INVITE_LABEL = "Get a Plex invite"
MAX_LABEL = 80  # Discord's limit for a button label

# Discord's button colours (ButtonStyle names). Grey is hard to see hovered in Discord's dark theme.
STYLES = ("primary", "secondary", "success", "danger")
STYLE_NAMES = {"primary": "Blurple", "secondary": "Grey", "success": "Green", "danger": "Red"}

PLACEHOLDERS = {
    "roles": "the role(s) they reacted for, in bold",
    "server": "the Discord server's name",
    "link_button": "the link button's label",
    "invite_button": "the invite button's label",
}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
MAX_PART = 1500
MAX_MESSAGE = 2000  # Discord's limit for one message


def unknown_placeholders(text: str) -> list[str]:
    return sorted({m for m in _PLACEHOLDER.findall(text) if m not in PLACEHOLDERS})


def render(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template).strip()
