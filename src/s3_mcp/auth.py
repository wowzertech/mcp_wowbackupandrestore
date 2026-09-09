"""OAuth 2.0 Resource Server behaviour.

The 2026-07-28 authorization model is ordinary production OAuth: the MCP server
is a *resource server*, Auth0 is the *authorization server*, and the MCP client
(Claude, Cursor) is the *client*. We never see a credential — only a bearer JWT
that we validate offline against Auth0's JWKS.

Two pieces make discovery work without the user pasting anything:

1. `/.well-known/oauth-protected-resource` (RFC 9728) tells the client which
   authorization server to talk to and what audience to request.
2. A `WWW-Authenticate` header on every 401 points at that document, so a client
   that hits the server cold knows exactly where to go.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .entitlements import Entitlement, SubscriptionInactive, resolve_entitlement
from .ratelimit import check_address, check_subject
from .settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Who is making this call, and what they are allowed to touch.

    `subject` is the Auth0 `sub` — stable, user-immutable, and the only thing the
    token is trusted for. `entitlement` comes from the database and carries the
    S3 prefix; the token has no authority over paths."""

    subject: str
    email: str | None
    scopes: frozenset[str]
    entitlement: "Entitlement"

    @property
    def s3_prefix(self) -> str:
        return self.entitlement.s3_prefix

    @property
    def account_id(self) -> str:
        return self.entitlement.account_id

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionError(f"token is missing required scope: {scope}")


# Stateless core means no session to hang this off, so we carry the verified
# principal for the lifetime of the single request via a ContextVar. Set by the
# middleware, read by the tools.
_principal: ContextVar[Principal | None] = ContextVar("principal", default=None)


def current_principal() -> Principal:
    p = _principal.get()
    if p is None:
        raise PermissionError("no authenticated principal on this request")
    return p


# --------------------------------------------------------------------------- #
# Token verification
# --------------------------------------------------------------------------- #

_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    """PyJWKClient caches keys in-process and refreshes on unknown `kid`, which
    is what you want across Auth0 key rotation. On a cold serverless start this
    costs one extra outbound call; keep the function warm or move to a
    provisioned container if that latency shows up in traces."""
    global _jwk_client
    if _jwk_client is None:
        s = get_settings()
        _jwk_client = PyJWKClient(s.jwks_url, cache_keys=True, lifespan=3600)
    return _jwk_client


def verify_token(token: str) -> tuple[str, str | None, frozenset[str], int]:
    """Validate the JWT and return (subject, email, scopes, issued_at).

    Note what this does NOT return: any notion of which data to serve. That is
    the database's job."""
    s = get_settings()

    if s.oauth_proxy_enabled:
        # The proxy is the authorization server: tokens are our own, signed with
        # our key and issued under our own issuer. Validate against those, never
        # against Auth0 — an Auth0 token reaching /mcp directly is exactly the
        # token-passthrough the MCP spec forbids, so it must fail here.
        from .oauth_proxy import issuer as proxy_issuer, public_jwks

        key = jwt.PyJWK(public_jwks()["keys"][0]).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            audience=s.auth0_audience,
            issuer=proxy_issuer() + "/",
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )
    else:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=s.auth0_audience,   # rejects tokens minted for other APIs
            issuer=s.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )

    # Defence in depth. The prefix comes from the database now, so an unverified
    # address can no longer reach anyone else's data — but a verified address is
    # still the cheapest signal that this is a real person's account.
    email = claims.get("email")
    if s.require_email_verified and email and claims.get("email_verified") is not True:
        raise PermissionError("email address is not verified")

    scopes = frozenset(str(claims.get("scope", "")).split())
    return claims["sub"], email, scopes, int(claims["iat"])


# --------------------------------------------------------------------------- #
# Signing out
# --------------------------------------------------------------------------- #

# Access tokens are validated offline, so revoking a session at Auth0 does not
# stop the token already in a client's hands — it stays good until it expires,
# which is a day. Signing out has to mean something sooner than that, so this
# records when a subject asked to leave and tokens issued before that moment are
# refused.
#
# This is deliberately the one piece of state in the server, and it is kept
# cheap: an entry matters only until the tokens that predate it would have
# expired anyway. Per-instance, so with several instances behind a balancer a
# sign-out binds on the instance that served it and the others catch up when
# their copies of the token expire. Move it to the database if that window
# matters to you.
_signed_out: dict[str, int] = {}


def note_signed_out(subject: str, at: int) -> None:
    _signed_out[subject] = at
    now = int(time.time())
    if len(_signed_out) > 5_000:
        cutoff = now - 86_400 * 2
        for key in [k for k, ts in _signed_out.items() if ts < cutoff]:
            _signed_out.pop(key, None)


def _is_signed_out(subject: str, issued_at: int) -> bool:
    at = _signed_out.get(subject)
    # Tokens minted in the same second as the sign-out are treated as older, so
    # the request that asked to sign out cannot keep working afterwards.
    return at is not None and issued_at <= at


# --------------------------------------------------------------------------- #
# ASGI middleware
# --------------------------------------------------------------------------- #

_UNPROTECTED = {
    # NB: the landing page at "/" is public too, but only for GET/HEAD — when the
    # MCP path is the root, POST "/" is the authenticated protocol. That method-
    # aware exemption is handled in __call__, not by this whole-path set.
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/jwks.json",
    "/healthz",
    # The site icon, fetched by clients that show the connector's mark. Public
    # so it is not 401'd into a fallback to a stale cached icon. Apple/WebKit
    # clients prefer the apple-touch paths and request them first, so those must
    # be public too — a 401 there is exactly what sent them to the stale icon.
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    # OAuth proxy endpoints: these ARE the login, so they cannot require a token.
    # Each does its own validation (client lookup, PKCE, upstream exchange).
    "/authorize",
    "/callback",
    "/token",
    "/register",
}


class Auth0Middleware:
    """Validates the bearer token on every request and publishes a Principal.

    Deliberately implemented as plain ASGI rather than against an SDK auth hook,
    so it survives SDK churn while v2 settles."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        send = _hardened(send)

        if _declared_length(scope) > get_settings().max_request_bytes:
            await _too_large()(scope, receive, send)
            return

        # Anonymous traffic is limited by address, because there is nothing else
        # to key on. Note what this deliberately does NOT cover: a request that
        # arrives with a good token. Remote MCP clients reach this server
        # through their vendor's infrastructure rather than from the customer's
        # own network, so many unrelated customers share a handful of source
        # addresses — limiting authenticated calls by address would have them
        # throttling each other for traffic none of them sent. Once the caller
        # is named, the account is the honest key, and that check happens below.
        async def anonymous(response: JSONResponse) -> None:
            """Answer an unidentified caller, spending one of its address's tokens."""
            retry_after = check_address(_client_address(scope))
            await (_too_many(retry_after) if retry_after else response)(
                scope, receive, send
            )

        async def public() -> None:
            """Let an unauthenticated request through, spending an address token."""
            retry_after = check_address(_client_address(scope))
            if retry_after:
                await _too_many(retry_after)(scope, receive, send)
                return
            await self.app(scope, receive, send)

        if scope["path"] in _UNPROTECTED or scope["path"].startswith("/static/"):
            await public()
            return

        # The landing page is public, but as a GET only: when the MCP path is the
        # root, POST "/" is the authenticated protocol and must reach the bearer
        # check below. Exempt the method, not the path.
        if scope["path"] == "/" and scope["method"] in ("GET", "HEAD"):
            await public()
            return

        s = get_settings()
        request = Request(scope, receive)

        # Someone pasting a non-root endpoint URL into a browser sends `GET <path>`
        # asking for HTML. That is a person, not a client — send them to the
        # landing page instead of the 401 an MCP call would get. Clients negotiate
        # JSON or an event-stream, never text/html. (A root endpoint needs no
        # redirect: GET "/" is already the page, handled just above.)
        if (
            s.mcp_path != "/"
            and scope["method"] == "GET"
            and scope["path"] == s.mcp_path
            and "text/html" in request.headers.get("accept", "")
        ):
            retry_after = check_address(_client_address(scope))
            if retry_after:
                await _too_many(retry_after)(scope, receive, send)
                return
            await RedirectResponse("/", status_code=303)(scope, receive, send)
            return
        header = request.headers.get("authorization", "")

        if not header.lower().startswith("bearer "):
            await anonymous(_challenge("missing bearer token"))
            return

        # Step 1 — who is this? Offline JWT check against Auth0's JWKS.
        try:
            subject, email, scopes, issued_at = verify_token(header[7:].strip())
        except jwt.ExpiredSignatureError:
            await anonymous(_challenge("token expired", error="invalid_token"))
            return
        except (jwt.InvalidTokenError, PermissionError) as exc:
            await anonymous(_challenge(str(exc), error="invalid_token"))
            return

        # A 401 here is deliberate: it tells the client its credentials are no
        # longer good and sends it back through the login flow, which is exactly
        # what someone who signed out to switch accounts wants next.
        if _is_signed_out(subject, issued_at):
            await _challenge(
                "you signed out of this server; sign in again to continue",
                error="invalid_token",
            )(scope, receive, send)
            return

        # Now that the caller is named, limit them by identity rather than by
        # address — an account is harder to rotate than an IP, and the calls
        # past this point are the expensive ones.
        retry_after = check_subject(subject)
        if retry_after:
            await _too_many(retry_after)(scope, receive, send)
            return

        # Step 2 — are they entitled, and to what? Database is the system of
        # record. Runs before any tool does, so no handler has to remember.
        try:
            entitlement = await resolve_entitlement(subject, email)
            entitlement.check()
        except SubscriptionInactive as exc:
            # 403, not 401. A 401 sends the client back through the login loop,
            # which cannot fix a lapsed subscription and just confuses the user.
            await _forbidden(str(exc))(scope, receive, send)
            return
        except Exception:
            # Could not reach the system of record, so we do not know whether
            # this subscription is live. Fail closed and say so plainly.
            logger.exception("entitlement lookup failed for %s", subject)
            await _unavailable()(scope, receive, send)
            return

        principal = Principal(
            subject=subject, email=email, scopes=scopes, entitlement=entitlement
        )

        token = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token)


# --------------------------------------------------------------------------- #
# Request shaping
# --------------------------------------------------------------------------- #

def _client_address(scope: Scope) -> str:
    """The caller's address, from a source they cannot choose.

    Deliberately not `X-Forwarded-For`: a client sets that header itself, and a
    rate limit keyed on a value the caller controls is not a rate limit. Behind
    API Gateway, Mangum fills `scope["client"]` from the request context's
    `sourceIp`, which is observed by the gateway rather than sent by the client.
    """
    client = scope.get("client")
    return client[0] if client else ""


def _declared_length(scope: Scope) -> int:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


# Applied to every response. `Cache-Control` is only set where the handler has
# not already chosen one, so the discovery document keeps the long cache that
# stops clients refetching it — everything else defaults to not being stored.
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
)


def _hardened(send: Send) -> Send:
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            present = {name.lower() for name, _ in headers}
            headers.extend(h for h in _SECURITY_HEADERS if h[0] not in present)
            if b"cache-control" not in present:
                headers.append((b"cache-control", b"no-store"))
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


def _too_many(retry_after: float) -> JSONResponse:
    seconds = max(1, int(retry_after + 0.999))
    return JSONResponse(
        {
            "error": "rate_limited",
            "error_description": (
                f"Too many requests. Try again in {seconds} second"
                f"{'s' if seconds != 1 else ''}."
            ),
        },
        status_code=429,
        headers={"Retry-After": str(seconds)},
    )


def _too_large() -> JSONResponse:
    return JSONResponse(
        {
            "error": "payload_too_large",
            "error_description": "Request body exceeds the size this server accepts.",
        },
        status_code=413,
    )


def _challenge(detail: str, error: str = "invalid_request") -> JSONResponse:
    s = get_settings()
    metadata = f"{s.public_url.rstrip('/')}/.well-known/oauth-protected-resource"
    return JSONResponse(
        {"error": error, "error_description": detail},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="mcp", error="{error}", '
                f'error_description="{detail}", '
                f'resource_metadata="{metadata}"'
            )
        },
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728. This is how Claude/Cursor discover Auth0 with zero user input."""
    s = get_settings()
    return JSONResponse(
        {
            # `resource` must identify THIS server as the client reached it
            # (RFC 9728) — clients refuse on mismatch. It follows the MCP path, so
            # it is the bare host when MCP_PATH is "/". Keep AUTH0_AUDIENCE equal
            # to this value so validated tokens carry the same identifier.
            "resource": s.mcp_endpoint_url,
            # Point clients at whichever authorization server actually issues the
            # tokens we accept: ourselves when the proxy is on, Auth0 otherwise.
            "authorization_servers": [
                (s.public_url.rstrip("/") + "/") if s.oauth_proxy_enabled
                else s.issuer
            ],
            "scopes_supported": ["backups:list", "backups:read"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{s.public_url.rstrip('/')}/docs",
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "subscription_required", "error_description": detail},
        status_code=403,
    )


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {
            "error": "temporarily_unavailable",
            "error_description": (
                "Could not verify your subscription right now. "
                "This is a problem on our side — please try again shortly."
            ),
        },
        status_code=503,
        headers={"Retry-After": "10"},
    )


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "ts": int(time.time())})
