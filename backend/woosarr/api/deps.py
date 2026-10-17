"""Shared route dependencies.

require_admin is the single gate for every non-public route (N10). Until local
admin login lands in P6-01 there is no session to check, so it lets requests
through; P6-01 replaces the body with a real session check. tests/test_app.py
fails if a route outside PUBLIC_PATHS is registered without this dependency.
"""

from __future__ import annotations

# Routes reachable without auth (N10). SAML and wizard routes join in P6.
PUBLIC_PATHS = frozenset({"/api/health", "/api/v1/health"})


def require_admin() -> None:
    # TODO(P6-01): reject requests without a valid admin session.
    return None
