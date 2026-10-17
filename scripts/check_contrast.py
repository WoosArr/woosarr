"""Check WCAG AA contrast (>= 4.5:1) of the UI theme tokens (plan P6-03, P11-05).

Parses frontend/src/styles.css, resolves each theme x palette x contrast
combination and checks every foreground/background pair the UI uses.
Run: python3 scripts/check_contrast.py   (exit 1 on any failure)
"""

import itertools
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "styles.css"
PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text-muted", "surface"), ("text-muted", "bg"),
    ("text-muted", "surface-2"), ("accent", "surface"), ("accent-strong", "accent-soft"), ("accent-text", "accent"),
    ("ok", "ok-soft"), ("warn", "warn-soft"), ("danger", "danger-soft"), ("info", "info-soft"),
    ("danger", "surface"), ("text", "warn-soft"), ("text", "danger-soft"), ("text", "info-soft"),
]


def blocks(css: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for sel, body in re.findall(r"(:root[^{]*)\{([^}]*)\}", css):
        out.setdefault(sel.strip(), {}).update(dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", body)))
    return out


def lum(hex_: str) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main() -> int:
    b = blocks(CSS.read_text())
    failures = 0
    for theme, palette, contrast in itertools.product(("light", "dark"), ("default", "redgreen", "blueyellow"), ("normal", "high")):
        tokens = dict(b[":root"])
        if theme == "dark":
            tokens.update(b.get(':root[data-theme="dark"]', {}))
        if palette != "default":
            tokens.update(b.get(f':root[data-palette="{palette}"]', {}))
            if theme == "dark":
                tokens.update(b.get(f':root[data-theme="dark"][data-palette="{palette}"]', {}))
        if contrast == "high":
            tokens.update(b.get(':root[data-contrast="high"]', {}))
            if theme == "dark":
                tokens.update(b.get(':root[data-theme="dark"][data-contrast="high"]', {}))
        for fg, bg in PAIRS:
            r = ratio(tokens[fg], tokens[bg])
            if r < 4.5:
                failures += 1
                print(f"FAIL {theme}/{palette}/{contrast}: --{fg} on --{bg} = {r:.2f}")
    print("contrast: all pairs >= 4.5:1" if not failures else f"contrast: {failures} failing pairs")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
