"""ASGI entrypoint.

  /mcp                                        the MCP endpoint (Streamable HTTP)
  /.well-known/oauth-protected-resource       RFC 9728 discovery, unauthenticated
  /healthz                                    load balancer probe
  /static/*                                   login-page branding assets

Because the core is stateless, this app holds nothing between requests. Scale it
with a plain round-robin load balancer — no sticky sessions, no shared store.
Route on the `Mcp-Method` and `Mcp-Name` headers if you want per-tool routing or
rate limits at the edge; do not route on session affinity, there isn't any.
"""

from pathlib import Path
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import Auth0Middleware, healthz, protected_resource_metadata
from .server import mcp
from .settings import get_settings

# Branding assets (login-page background). Served unauthenticated — the Auth0
# login page loads these before any user is signed in.
# Two layouts to satisfy: the repository, where the package sits under src/ and
# the assets are a sibling of it, and the deployment package, where they are
# shipped next to the module.
_ASSETS = next(
    (
        candidate
        for candidate in (
            Path(__file__).resolve().parent / "branding_assets",
            Path(__file__).resolve().parent.parent.parent / "branding" / "assets",
        )
        if candidate.is_dir()
    ),
    Path(__file__).resolve().parent / "branding_assets",
)

_settings = get_settings()
_public_host = urlparse(_settings.public_url).netloc


_LANDING = Path(__file__).resolve().parent / "landing.html"


async def _landing(request):
    """The human page a browser gets when it opens the endpoint.

    The same host answers two audiences (the pattern Railway's mcp. subdomain
    uses): a client speaks the protocol, a browser sees this. Public — it only
    says how to sign in. The endpoint shown is built from PUBLIC_URL plus the MCP
    path, so it is right whether that is the gateway host with /mcp today or a
    bare mcp.wowbackupandrestore.com once MCP_PATH is "/".

    When the MCP path is the root, this handler shares GET "/" with the protocol:
    a browser asks for text/html and gets the page; a client's stream request
    (any other Accept) is declined with 405, exactly as /mcp is."""
    if _settings.mcp_path == "/" and "text/html" not in request.headers.get("accept", ""):
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    try:
        html = _LANDING.read_text(encoding="utf-8")
    except OSError:
        return Response("WOW AISuite — MCP server", media_type="text/plain")
    return HTMLResponse(
        html.replace("{{ENDPOINT}}", _settings.mcp_endpoint_url)
            .replace("{{ENDPOINT_DISPLAY}}", _settings.mcp_endpoint_display),
        headers={"Cache-Control": "public, max-age=300"},
    )


async def _favicon(request):
    """The site icon, served at every default path a client probes.

    This covers /favicon.ico AND the Apple paths — /apple-touch-icon.png and
    /apple-touch-icon-precomposed.png — because Apple/WebKit clients (Claude's
    macOS webview among them) request those *first* and prefer them over the
    favicon. Leaving them to 401 made WebKit fall back to its own persistent
    favicon cache (the stale blue icon), so a fresh re-add still showed blue.
    Serve the navy brand tile on all of them — the same PNG as the serverInfo
    and .mcpb icon — so every surface resolves to one mark on both themes.

    max-age is deliberately short for now, to flush stale client caches quickly
    while the icon is being rolled out; raise it again once it has settled."""
    try:
        data = (_ASSETS / "wow-swirl.png").read_bytes()
    except OSError:
        return Response(status_code=404)
    return Response(
        data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=60"},
    )


async def _decline_sse_stream(request):
    """Decline the optional server->client SSE stream with 405.

    This server is stateless behind Lambda: it answers each call as JSON over
    POST and holds no session, so there is no server-initiated stream to open,
    and Lambda cannot hold one open regardless. The Streamable-HTTP spec says a
    server that does not offer the GET stream returns 405; without this the
    request reaches the SDK's stream handler, whose streaming response Mangum
    cannot produce, and the caller gets a 503 that clients (mcp-remote) treat as
    a dead connection instead of falling back to POST."""
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


def _allowed_hosts() -> list[str]:
    """Hosts this server answers to.

    The SDK rejects a request whose Host header it does not recognise, which is
    DNS-rebinding protection worth keeping. Behind a tunnel or a load balancer
    the Host is the public name, not the bind address, so it has to be listed
    explicitly or every proxied request comes back 421."""
    hosts = {_public_host} if _public_host else set()
    for name in ("localhost", "127.0.0.1", "[::1]"):
        hosts.add(name)
        hosts.add(f"{name}:8000")
        hosts.add(f"{name}:8001")
    return sorted(h for h in hosts if h)

# The SDK hands back a complete Starlette application that already serves /mcp
# and, importantly, owns the lifespan that starts the session manager's task
# group. Our routes are added to *that* app rather than mounting it inside a new
# one: a mounted sub-application never has its lifespan run, and the transport
# then fails every request with "Task group is not initialized".
app = mcp.streamable_http_app(
    # Stateless is the whole point of this design: no session is held between
    # requests, so any instance can answer any call and a plain round-robin
    # balancer works. It also stops a session created on one process from being
    # unknown to the next one the client happens to reach.
    stateless_http=True,
    json_response=_settings.mcp_json_response,
    # Serve the protocol on the configured path — "/mcp" by default, or "/" so a
    # bare host is the whole endpoint. GET on that path is handled by our own
    # routes (landing page / SSE decline); POST falls through to the SDK here.
    streamable_http_path=_settings.mcp_path,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
        allowed_origins=[f"https://{_public_host}", f"http://{_public_host}"]
        if _public_host
        else [],
    ),
)

_routes = [
    # Root GET: the human landing page (and, when the MCP path is "/", the place
    # a client's stream GET is declined). POST "/" falls through to the SDK.
    Route("/", _landing, methods=["GET"]),
    # The brand tile at every default icon path a client probes. Apple/WebKit
    # clients request the apple-touch paths first and prefer them; leaving those
    # to 401 makes them fall back to a stale cached favicon.
    Route("/favicon.ico", _favicon, methods=["GET"]),
    Route("/apple-touch-icon.png", _favicon, methods=["GET"]),
    Route("/apple-touch-icon-precomposed.png", _favicon, methods=["GET"]),
    Route(
        "/.well-known/oauth-protected-resource",
        protected_resource_metadata,
        methods=["GET"],
    ),
    # RFC 9728 path-inserted form for the /mcp resource — some clients try this
    # first and only fall back to the root form on 404.
    Route(
        "/.well-known/oauth-protected-resource/mcp",
        protected_resource_metadata,
        methods=["GET"],
    ),
    Route("/healthz", healthz, methods=["GET"]),
]

# Intercept the SSE-stream GET before the SDK's handler (which 503s under
# Lambda); POST still falls through to the SDK. When the endpoint is the root,
# the landing handler already declines the stream GET, so no separate route.
if _settings.mcp_path != "/":
    _routes.append(Route(_settings.mcp_path, _decline_sse_stream, methods=["GET"]))

# OAuth proxy routes — only when enabled. This server then IS the authorization
# server: MCP clients register, log in and get tokens here, and we federate to
# Auth0 behind the scenes as one first-party client (see oauth_proxy.py).
if _settings.oauth_proxy_enabled:
    from . import oauth_proxy

    _routes += [
        Route("/.well-known/oauth-authorization-server",
              oauth_proxy.authorization_server_metadata, methods=["GET"]),
        Route("/.well-known/jwks.json", oauth_proxy.jwks, methods=["GET"]),
        Route("/register", oauth_proxy.register, methods=["POST"]),
        Route("/authorize", oauth_proxy.authorize, methods=["GET"]),
        Route("/callback", oauth_proxy.callback, methods=["GET"]),
        Route("/token", oauth_proxy.token, methods=["POST"]),
    ]

# Serving the login artwork is a convenience, not a dependency: StaticFiles
# raises at construction if the directory is absent, which would take the whole
# server down over a background image.
if _ASSETS.is_dir():
    _routes.append(
        Mount("/static", app=StaticFiles(directory=_ASSETS), name="static")
    )

app.router.routes[:0] = _routes

# Order matters: auth wraps everything, and exempts the well-known paths itself.
app = Auth0Middleware(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("s3_mcp.app:app", host="0.0.0.0", port=8000, reload=True)
