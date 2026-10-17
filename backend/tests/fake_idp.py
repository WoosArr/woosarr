"""A fake OpenID Connect provider for tests: real RS256 signatures, discovery,
JWKS, a token endpoint that checks PKCE, and userinfo."""

import base64
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class FakeIdP:
    def __init__(self, issuer="https://idp.test", client_id="woosarr-client", client_secret="s3cret-" + "x" * 8,
                 discovered_issuer=None, auth_methods=("client_secret_basic", "client_secret_post"),
                 authorize_url=None):
        self.issuer, self.client_id, self.client_secret = issuer.rstrip("/"), client_id, client_secret
        self.discovered_issuer = discovered_issuer or self.issuer
        self.authorize_url = authorize_url  # e2e: a real page the browser can open
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.auth_methods = list(auth_methods)
        self.codes = {}
        self.claims = {"sub": "user-123", "email": "admin@example.com", "email_verified": True,
                       "preferred_username": "alice", "groups": ["WooSarr Admins"]}
        self.token_overrides = {}  # applied to the ID token
        self.sign_with = None  # (key, alg, kid) to sign differently
        self.userinfo = {}
        self.calls = []

    # requests.Session-like
    def get(self, url, timeout=None, headers=None):
        self.calls.append(("GET", url))
        base = self.issuer
        if url == f"{base}/.well-known/openid-configuration":
            return FakeResponse(200, {
                "issuer": self.discovered_issuer, "authorization_endpoint": self.authorize_url or f"{base}/authorize",
                "token_endpoint": f"{base}/token", "jwks_uri": f"{base}/jwks", "userinfo_endpoint": f"{base}/userinfo",
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": self.auth_methods,
            })
        if url == f"{base}/jwks":
            pub = self.key.public_key().public_numbers()
            return FakeResponse(200, {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                                                "n": _b64(pub.n), "e": _b64(pub.e)}]})
        if url == f"{base}/userinfo":
            return FakeResponse(200, {"sub": self.claims["sub"], **self.userinfo})
        return FakeResponse(404, {})

    def post(self, url, data=None, auth=None, timeout=None, headers=None):
        self.calls.append(("POST", url, dict(data or {}), auth))
        if url != f"{self.issuer}/token":
            return FakeResponse(404, {})
        cid, secret = auth if auth else (data.get("client_id"), data.get("client_secret"))
        if (cid, secret) != (self.client_id, self.client_secret):
            return FakeResponse(401, {"error": "invalid_client"})
        grant = self.codes.pop(data.get("code"), None)
        if grant is None:
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "unknown or used code"})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if challenge != grant["code_challenge"] or data["redirect_uri"] != grant["redirect_uri"]:
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "PKCE or redirect mismatch"})
        now = int(time.time())
        claims = {"iss": self.discovered_issuer, "aud": self.client_id, "iat": now, "exp": now + 300,
                  "nonce": grant["nonce"], **self.claims, **self.token_overrides}
        key, alg, kid = self.sign_with or (self.key, "RS256", self.kid)
        token = jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)
        return FakeResponse(200, {"id_token": token, "access_token": "at-" + uuid.uuid4().hex, "token_type": "Bearer"})

    # the browser's trip to the IdP
    def authorize(self, url: str) -> str:
        """Approve the request at `url`; returns the code."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["client_id"] == self.client_id and q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        code = uuid.uuid4().hex
        self.codes[code] = q
        return code


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def dump(x):
    return json.dumps(x)
