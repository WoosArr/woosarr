"""HTTP middleware: request ids and security/cache headers (plan P10-03)."""

from __future__ import annotations

import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Client-supplied ids end up in logs; only accept short, harmless ones.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    # React sets style properties through the DOM (allowed); 'unsafe-inline'
    # only covers style attributes some components render server-side.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "font-src 'self' data:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self' https://app.plex.tv",
    "frame-ancestors 'none'",
)
CSP = "; ".join(_CSP_DIRECTIVES)

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def request_id_for(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex[:12]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = request_id_for(request)
        response = await call_next(request)
        path = request.url.path
        response.headers["X-Request-ID"] = request.state.request_id
        for name, value in STATIC_HEADERS.items():
            response.headers.setdefault(name, value)
    # Swagger UI loads its bundle from a CDN; it gets no CSP (and is admin-only).
        if not path.startswith("/api/docs"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith("/assets/"):
            # Vite fingerprints asset names, so they never change in place.
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            # index.html and friends: always revalidate so updates show up immediately.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response
