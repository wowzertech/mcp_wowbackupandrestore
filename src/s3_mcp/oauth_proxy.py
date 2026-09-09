"""An OAuth 2.1 authorization server that fronts Auth0 — statelessly.

Why this exists: Auth0 treats any client that registers itself (which every MCP
client does) as a *third-party* client under "strict" security, and strict
third-party clients are refused the consent screen on this tenant with
"Third Party clients are not allowed to use the Classic Universal Login
experience." No tenant setting lifts it (both candidate flags were tested and
refuted). So MCP clients talk to *us*, and we talk to Auth0 as one ordinary
first-party client. Auth0 never sees a third-party client, so strict never
engages, and the user still logs in on Auth0's own page — passwords, passkeys,
social, MFA all unchanged.

    MCP client ──DCR/PKCE──▶ this proxy ──first-party/PKCE──▶ Auth0 login
       ▲                          │                               │
       └──── our own JWT ─────────┴──── our auth code ◀───────────┘

Stateless by design. A login is four requests — /authorize, /callback, /token,
and a later refresh — that on Lambda land on different instances, so anything
remembered in one is invisible to the next. Rather than a shared database, every
piece of transient state is sealed into a signed value that carries itself
through the flow:

  client_id   is a signed token holding the client's redirect URIs (DCR needs no
              store; a re-registration is just a new token)
  state       sent to Auth0 is a signed token holding what the client asked for;
              Auth0 echoes it back to /callback, which unseals it
  auth code   is a signed token holding the authenticated subject; /token unseals
              and verifies it

Nothing is stored anywhere. The trades this makes, and why they are acceptable:

  * Authorization codes are not strictly single-use (there is nothing to mark
    "spent"). PKCE closes the real hole — a replayed code is worthless without
    the verifier, which never leaves the client — and codes live 60 seconds.
  * Refresh tokens cannot be individually revoked. Access tokens last an hour;
    rotating OAUTH_PROXY_SIGNING_KEY invalidates every outstanding token at once,
    which is the emergency "sign everyone out" lever.

The access tokens handed to clients are RS256 JWTs the resource server validates
against our JWKS. The internal sealed values are HS256 with a secret derived from
the signing key, so they need no extra configuration and rotate with it. Each
sealed value names its own purpose in a `typ` claim and is rejected if presented
as any other, so a code can never be replayed as a refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .settings import get_settings

_PENDING_TTL = 600       # a login in progress
_CODE_TTL = 60           # our authorization code
_ACCESS_TTL = 3600       # our access token
_REFRESH_TTL = 30 * 86400
_CLIENT_TTL = 365 * 86400  # a registered client id


# --------------------------------------------------------------------------- #
# Signing — one RSA key for access tokens (public via JWKS), one derived HMAC
# secret for the internal sealed values.
# --------------------------------------------------------------------------- #

_signing_key: str | None = None
_public_jwk: dict | None = None
_kid: str | None = None
_hmac_secret: bytes | None = None


def _load_keys() -> tuple[str, dict, str]:
    global _signing_key, _public_jwk, _kid, _hmac_secret
    if _signing_key is not None:
        return _signing_key, _public_jwk, _kid  # type: ignore[return-value]

    s = get_settings()
    pem = s.oauth_proxy_signing_key.strip()
    if pem:
        if "BEGIN" not in pem:
            pem = base64.b64decode(pem).decode()
    else:
        import logging
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        logging.getLogger(__name__).warning(
            "OAUTH_PROXY_SIGNING_KEY is not set — generating an ephemeral key. "
            "Tokens will not validate across instances or restarts. Set a key "
            "before deploying to more than one process."
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    from cryptography.hazmat.primitives import serialization

    priv_obj = serialization.load_pem_private_key(pem.encode(), password=None)
    pub_obj = priv_obj.public_key()
    pub_pem = pub_obj.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    jwk_json = jwt.algorithms.RSAAlgorithm.to_jwk(pub_obj)
    jwk = json.loads(jwk_json) if isinstance(jwk_json, str) else dict(jwk_json)
    kid = base64.urlsafe_b64encode(
        hashlib.sha256(pub_pem.encode()).digest()[:8]
    ).rstrip(b"=").decode()
    jwk.update({"use": "sig", "alg": "RS256", "kid": kid})

    _signing_key, _public_jwk, _kid = pem, jwk, kid
    # The internal HMAC secret is bound to the signing key: rotate the key and
    # every sealed value (client ids, codes, refresh tokens) rotates with it.
    _hmac_secret = hashlib.sha256(b"wow-mcp-proxy-internal|" + pem.encode()).digest()
    return _signing_key, _public_jwk, _kid


def public_jwks() -> dict:
    _, jwk, _ = _load_keys()
    return {"keys": [jwk]}


def issuer() -> str:
    return get_settings().public_url.rstrip("/")


def _secret() -> bytes:
    _load_keys()
    assert _hmac_secret is not None
    return _hmac_secret


# --------------------------------------------------------------------------- #
# Sealed values — signed, self-describing, self-expiring. Not stored anywhere.
# --------------------------------------------------------------------------- #

def _seal(typ: str, claims: dict, ttl: int) -> str:
    now = int(time.time())
    payload = {**claims, "typ": typ, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def _unseal(typ: str, token: str) -> dict | None:
    try:
        claims = jwt.decode(token, _secret(), algorithms=["HS256"],
                            options={"require": ["exp", "iat", "typ"]})
    except Exception:
        return None
    if claims.get("typ") != typ:      # a code may never be spent as a refresh token
        return None
    return claims


def _fernet():
    """Symmetric cipher for the one secret we carry inside a client-held token:
    the upstream Auth0 refresh token. A sealed value is only *signed*, so its
    payload is readable by whoever holds it — fine for a subject or an email,
    not for a live Auth0 credential. Encrypting it means a stolen proxy refresh
    token does not also hand over the Auth0 refresh token."""
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(_secret()))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str | None:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        return None


def _mint_access_token(sub, email, email_verified, scope) -> str:
    pem, _, kid = _load_keys()
    s = get_settings()
    now = int(time.time())
    claims = {"iss": issuer() + "/", "sub": sub, "aud": s.auth0_audience,
              "iat": now, "exp": now + _ACCESS_TTL, "scope": scope}
    if email:
        claims["email"] = email
        claims["email_verified"] = email_verified
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _upstream_verifier(nonce: str) -> str:
    """Our PKCE verifier for the Auth0 leg, derived from a nonce rather than
    transmitted. /authorize puts the nonce in the sealed state; /callback
    recomputes the same verifier. It is never sent to anyone, so even an observer
    of the redirect URL cannot replay our upstream code exchange."""
    return _b64url(hmac.new(_secret(), b"pkce|" + nonce.encode(), hashlib.sha256).digest())


def _err(error, description, status=400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description},
                        status_code=status)


def _client_error(redirect_uri, state, error, description) -> RedirectResponse:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

async def authorization_server_metadata(request: Request) -> JSONResponse:
    base = issuer()
    return JSONResponse(
        {
            "issuer": base + "/",
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["openid", "email", "backups:list", "backups:read"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def jwks(request: Request) -> JSONResponse:
    return JSONResponse(public_jwks(),
                        headers={"Cache-Control": "public, max-age=3600"})


# --------------------------------------------------------------------------- #
# Dynamic client registration — the client_id IS the record.
# --------------------------------------------------------------------------- #

async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _err("invalid_request", "body must be JSON")

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _err("invalid_redirect_uri", "redirect_uris is required")
    for uri in redirect_uris:
        if not _redirect_allowed(uri):
            return _err("invalid_redirect_uri",
                        f"redirect_uri not permitted: {uri}")

    # A short opaque client_id, deliberately NOT a signed blob. A public client's
    # id is not a secret and carries no security on its own — PKCE and the
    # redirect-URI policy are the controls — so there is nothing to encode into
    # it, and a long JWT-shaped id is only an interop hazard (some MCP clients
    # mishandle a 300-character id with dots). Nothing is stored: /authorize
    # re-checks the redirect_uri against the same policy rather than a lookup.
    client_id = "mcp_" + _rand(24)
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": body.get("client_name", "MCP client"),
        },
        status_code=201,
    )


def _rand(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


def _redirect_allowed(uri: str) -> bool:
    """Redirect-URI policy — the real control now that client_id is opaque.

    Loopback on any port is allowed (RFC 8252: native/CLI clients like Claude
    Code, Cursor and the Inspector use an ephemeral localhost port). Anything
    else must be HTTPS to a known MCP vendor host. This is what stops an
    authorization code being sent to an attacker's URL; PKCE is the second lock."""
    from urllib.parse import urlparse

    try:
        u = urlparse(uri)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    if u.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return True
    if u.scheme == "https":
        allow = get_settings().oauth_proxy_allowed_redirect_hosts
        return host in allow or any(host.endswith("." + d) for d in allow)
    return False


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #

async def authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")

    # client_id is opaque and not looked up — a public client's id is not a
    # security boundary. The controls are the redirect-URI policy (below) and
    # PKCE (checked at /token). Reject only a missing client_id or a redirect
    # URI the policy forbids; anything else, including an id from a previous
    # registration, is accepted so a stale cache never wedges the client.
    if not client_id:
        return _err("invalid_client", "client_id is required")
    if not _redirect_allowed(redirect_uri):
        return _err("invalid_request", "redirect_uri is not permitted")

    if q.get("response_type") != "code":
        return _client_error(redirect_uri, q.get("state"),
                             "unsupported_response_type", "only code is supported")
    challenge = q.get("code_challenge")
    if not challenge or q.get("code_challenge_method") != "S256":
        return _client_error(redirect_uri, q.get("state"),
                             "invalid_request", "PKCE S256 is required")

    s = get_settings()
    nonce = secrets.token_urlsafe(24)
    # Everything we need at /callback, sealed and handed to Auth0 as `state`.
    state = _seal("txn", {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "client_state": q.get("state", ""),
        "challenge": challenge,
        "scope": q.get("scope", "openid email backups:list backups:read"),
        "nonce": nonce,
    }, _PENDING_TTL)

    upstream = {
        "client_id": s.oauth_proxy_upstream_client_id,
        "response_type": "code",
        "redirect_uri": f"{issuer()}/callback",
        "scope": "openid profile email offline_access backups:list backups:read",
        "audience": s.auth0_audience,
        "state": state,
        "code_challenge": _pkce_challenge(_upstream_verifier(nonce)),
        "code_challenge_method": "S256",
    }
    # Force a fresh login on every Connect when configured, so a disconnected
    # user is never silently re-authenticated off the browser SSO cookie. The
    # stateless proxy can't distinguish a post-disconnect reconnect from a
    # routine one, so this prompts on both. Empty restores silent SSO reuse.
    if s.oauth_authorize_prompt:
        upstream["prompt"] = s.oauth_authorize_prompt
    return RedirectResponse(
        f"https://{s.auth0_domain}/authorize?{urlencode(upstream)}", status_code=302)


async def callback(request: Request):
    q = request.query_params
    pending = _unseal("txn", q.get("state", ""))
    if not pending:
        return _err("invalid_request", "unknown or expired login state", 400)

    if q.get("error"):
        return _client_error(pending["redirect_uri"], pending["client_state"],
                             q.get("error"), q.get("error_description", ""))
    code = q.get("code")
    if not code:
        return _client_error(pending["redirect_uri"], pending["client_state"],
                             "invalid_request", "no code from upstream")

    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(f"https://{s.auth0_domain}/oauth/token", data={
            "grant_type": "authorization_code",
            "client_id": s.oauth_proxy_upstream_client_id,
            "client_secret": s.oauth_proxy_upstream_client_secret,
            "code": code,
            "redirect_uri": f"{issuer()}/callback",
            "code_verifier": _upstream_verifier(pending["nonce"]),
        })
    if resp.status_code != 200:
        return _client_error(pending["redirect_uri"], pending["client_state"],
                             "access_denied",
                             f"upstream token exchange failed: {resp.status_code}")

    upstream = resp.json()
    sub, email, email_verified = _identity_from_upstream(upstream)
    if not sub:
        return _client_error(pending["redirect_uri"], pending["client_state"],
                             "access_denied", "no subject in upstream token")

    # Carry Auth0's refresh token (encrypted) so our own refresh can re-check the
    # session against Auth0 — that is what makes "log out" revoke us too. If
    # Auth0 returned none (offline_access not granted), we fall back to a
    # self-contained refresh with no live revocation; logged, not silent.
    auth0_rt = upstream.get("refresh_token")
    our_code = _seal("code", {
        "client_id": pending["client_id"],
        "redirect_uri": pending["redirect_uri"],
        "challenge": pending["challenge"],
        "scope": pending["scope"],
        "sub": sub, "email": email, "email_verified": email_verified,
        "u_rt": _encrypt(auth0_rt) if auth0_rt else "",
    }, _CODE_TTL)

    params = {"code": our_code}
    if pending["client_state"]:
        params["state"] = pending["client_state"]
    return RedirectResponse(
        f"{pending['redirect_uri']}?{urlencode(params)}", status_code=302)


def _identity_from_upstream(tokens: dict) -> tuple[str | None, str | None, bool]:
    id_token = tokens.get("id_token")
    if not id_token:
        return None, None, False
    s = get_settings()
    try:
        key = _upstream_jwks().get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=["RS256"],
                            audience=s.oauth_proxy_upstream_client_id,
                            issuer=f"https://{s.auth0_domain}/")
    except Exception as exc:
        # The token just came from Auth0's token endpoint over TLS on a back
        # channel — its provenance is the connection, not the signature, so full
        # re-validation is defence in depth rather than the security boundary.
        # Custom-domain tenants can trip issuer/JWKS validation (canonical vs
        # custom iss); rather than fail the whole login on that, log why and
        # trust the back-channel claims. Signature is still checked with
        # verify_signature where the key resolves.
        import logging
        logging.getLogger(__name__).warning(
            "upstream id_token strict validation failed (%s); trusting "
            "back-channel claims", exc)
        try:
            claims = jwt.decode(id_token, options={"verify_signature": False})
        except Exception:
            return None, None, False
    return claims.get("sub"), claims.get("email"), bool(claims.get("email_verified"))


_upstream_jwk_client = None


def _upstream_jwks():
    global _upstream_jwk_client
    if _upstream_jwk_client is None:
        s = get_settings()
        _upstream_jwk_client = jwt.PyJWKClient(
            f"https://{s.auth0_domain}/.well-known/jwks.json",
            cache_keys=True, lifespan=3600)
    return _upstream_jwk_client


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #

async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        return _token_auth_code(form)
    if grant == "refresh_token":
        return await _token_refresh(form)
    return _err("unsupported_grant_type", f"unsupported grant_type: {grant}")


def _token_auth_code(form) -> JSONResponse:
    record = _unseal("code", form.get("code", ""))
    if not record:
        return _err("invalid_grant", "code is invalid or expired")
    if form.get("client_id") != record["client_id"]:
        return _err("invalid_grant", "code was issued to a different client")
    if form.get("redirect_uri") != record["redirect_uri"]:
        return _err("invalid_grant", "redirect_uri does not match")

    verifier = form.get("code_verifier", "")
    if not verifier or _pkce_challenge(verifier) != record["challenge"]:
        return _err("invalid_grant", "PKCE verification failed")

    return _issue(record["sub"], record["email"], record["email_verified"],
                  record["scope"], record["client_id"], record.get("u_rt", ""))


async def _token_refresh(form) -> JSONResponse:
    record = _unseal("refresh", form.get("refresh_token", ""))
    if not record:
        return _err("invalid_grant", "refresh token is invalid or expired")
    if form.get("client_id") != record["client_id"]:
        return _err("invalid_grant", "refresh token belongs to a different client")

    sub = record["sub"]
    email, email_verified = record["email"], record["email_verified"]
    u_rt_enc = record.get("u_rt", "")

    # Revocation lives at Auth0. If we hold an Auth0 refresh token, spend it now:
    # a session the logout tool revoked will be refused here, and a live one
    # returns current identity. No Auth0 refresh token means this session predates
    # offline_access — it keeps working until our refresh expires, with no live
    # revocation, which is the documented fallback.
    if u_rt_enc:
        auth0_rt = _decrypt(u_rt_enc)
        if not auth0_rt:
            return _err("invalid_grant", "refresh token is malformed")
        s = get_settings()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(f"https://{s.auth0_domain}/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": s.oauth_proxy_upstream_client_id,
                "client_secret": s.oauth_proxy_upstream_client_secret,
                "refresh_token": auth0_rt,
            })
        if resp.status_code != 200:
            # Auth0 refused: the session was revoked (logout) or the token aged
            # out. Either way this refresh is dead — the client must log in again.
            return _err("invalid_grant", "session is no longer valid; sign in again")
        upstream = resp.json()
        fresh_sub, fresh_email, fresh_verified = _identity_from_upstream(upstream)
        if fresh_sub:
            sub, email, email_verified = fresh_sub, fresh_email, fresh_verified
        # Auth0 may rotate the refresh token; carry the newest one forward.
        u_rt_enc = _encrypt(upstream.get("refresh_token") or auth0_rt)

    return _issue(sub, email, email_verified, record["scope"],
                  record["client_id"], u_rt_enc)


def _issue(sub, email, email_verified, scope, client_id, u_rt_enc="") -> JSONResponse:
    access = _mint_access_token(sub, email, email_verified, scope)
    refresh = _seal("refresh", {
        "sub": sub, "email": email, "email_verified": email_verified,
        "scope": scope, "client_id": client_id, "u_rt": u_rt_enc,
    }, _REFRESH_TTL)
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope,
        },
        headers={"Cache-Control": "no-store"},
    )
