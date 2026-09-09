"""Configuration. Everything comes from the environment — nothing is baked in,
because the same image is deployed per-environment and per-region."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Public identity of this server -------------------------------------
    # Must match the Auth0 API Identifier exactly: it is the `aud` claim we
    # validate, and what stops a token minted for the portal being replayed here.
    public_url: str = "https://mcp.example.com"

    # --- Auth0 ---------------------------------------------------------------
    auth0_domain: str = "your-tenant.us.auth0.com"
    auth0_audience: str = "https://mcp.example.com"

    # The tenant's own Auth0 domain, which stays canonical even when logins run
    # on a custom domain. The Management API only exists under this name: its
    # audience is https://{tenant}/api/v2/, and asking a custom domain for that
    # audience is refused. Leave empty to reuse auth0_domain.
    auth0_tenant_domain: str = ""

    # Machine-to-machine credentials, used only to read a user's email address
    # when the access token does not carry it — see lookup_email(). Not needed
    # once the MCP and the portal share one Auth0 tenant, because then the
    # token's `sub` matches the database directly.
    auth0_client_id: str = ""
    auth0_client_secret: str = ""

    # With the database as the system of record for prefixes, the email claim no
    # longer decides which data is served, so this is defence in depth rather
    # than load-bearing. Keep it on anyway.
    require_email_verified: bool = True

    # --- Entitlements ---------------------------------------------------------
    # "portal_api" (recommended), "mysql" (the portal database), or "postgres".
    entitlement_backend: str = "portal_api"

    # mysql — the portal's own database, the system of record for who may read
    # which organisation.
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""

    # One deployment can resolve entitlements across several databases on the
    # same server — e.g. "app_live,app_staging" so both real
    # customers and test accounts work. Comma-separated; empty falls back to the
    # single mysql_database. Order matters only for which one supplies the
    # caller's own account_id/prefix when they exist in more than one.
    mysql_databases: str = ""

    @property
    def mysql_database_list(self) -> list[str]:
        names = [d.strip() for d in self.mysql_databases.split(",") if d.strip()]
        return names or ([self.mysql_database] if self.mysql_database else [])

    # The MCP runs on its own Auth0 tenant during development, so a token's `sub`
    # has no matching row in the portal database (whose subs come from the portal
    # tenant). Falling back to the verified email address bridges the two.
    # Set false once the MCP and portal share one tenant — then `sub` matches and
    # the token stops having any influence over which account is found.
    entitlement_match_verified_email: bool = True

    # portal_api
    entitlement_api_url: str = "https://api.example.com/internal"
    entitlement_api_token: str = ""
    entitlement_api_timeout: float = 3.0

    # postgres
    database_url: str = ""

    # How long a resolved entitlement is trusted, in seconds. This IS the
    # revocation lag: a subscription cancelled now keeps working for up to this
    # long. 300s is a reasonable default; drop it if billing wants tighter.
    entitlement_cache_ttl: int = 300

    # --- S3 ------------------------------------------------------------------
    # Layout:  s3://{bucket}/{email}/{organisation}/{backup_date}/{file}
    #
    # Backups are stored in the region the organisation belongs to, so the bucket
    # is chosen per organisation from its country code — not once for the whole
    # server. These defaults mirror the portal's own configuration.
    s3_bucket: str = "example-backups-beta"   # fallback for unknown codes
    aws_region: str = "us-east-1"

    s3_bucket_name_ca: str = "example-backups-beta"
    s3_bucket_name_au: str = "example-backups-au"
    s3_bucket_name_nz: str = "example-backups-nz"
    s3_bucket_name_us: str = "example-backups-us"
    s3_bucket_name_uk: str = "example-backups"
    s3_bucket_name_gb: str = "example-backups"

    aws_region_ca: str = "ca-central-1"
    aws_region_au: str = "ap-southeast-2"
    aws_region_nz: str = "ap-southeast-6"
    aws_region_us: str = "us-east-1"
    aws_region_uk: str = "eu-west-2"
    aws_region_gb: str = "eu-west-2"

    # QuickBooks Online backups live in their own per-country buckets
    # (example-backups-qbo-<cc>), in the same regions as the Xero ones. The
    # backup layout inside is identical — {prefix}/{org}/{date}/*.jsonl — so only
    # the bucket differs. Include QBO orgs in a user's entitlement when on.
    qbo_backups_enabled: bool = True
    s3_bucket_name_qbo_ca: str = "example-backups-qbo-ca"
    s3_bucket_name_qbo_us: str = "example-backups-qbo-us"
    s3_bucket_name_qbo_au: str = "example-backups-qbo-au"
    s3_bucket_name_qbo_nz: str = "example-backups-qbo-nz"
    s3_bucket_name_qbo_uk: str = "example-backups-qbo-uk"
    s3_bucket_name_qbo_gb: str = "example-backups-qbo-gb"

    def location_for(self, country_code: str | None,
                     source: str = "xero") -> tuple[str, str]:
        """Bucket and region holding an organisation's backups.

        `source` selects the product's bucket family: Xero backups and
        QuickBooks backups are stored in different buckets for the same country.
        An unrecognised code falls back to the default bucket rather than
        raising: the country column carries occasional oddities, and refusing to
        serve an organisation because of a data-entry quirk is worse than
        looking in the default place and finding nothing."""
        code = (country_code or "").strip().lower()
        region = (getattr(self, f"aws_region_{code}", "") if code else "") or self.aws_region
        if source == "qbo":
            bucket = getattr(self, f"s3_bucket_name_qbo_{code}", "") if code else ""
            return bucket or self.s3_bucket_name_qbo_ca, region
        bucket = getattr(self, f"s3_bucket_name_{code}", "") if code else ""
        return bucket or self.s3_bucket, region

    # Named profile from ~/.aws/credentials. Development convenience; in
    # production leave this empty and give the task an execution role instead,
    # so there is no long-lived key to rotate or leak.
    aws_profile: str = ""

    # Optional but recommended: role assumed per request with a session policy
    # scoped to the caller's own email prefix.
    s3_assume_role_arn: str = ""

    # Answer tool calls as plain JSON rather than a server-sent event stream.
    # Required behind anything that buffers a response — a Lambda invocation
    # returns one payload, so an SSE reply is held until the handler finishes
    # and the transport stalls. Nothing here streams, so there is nothing lost.
    mcp_json_response: bool = False

    # Path the MCP endpoint is served on. Default "/mcp"; set to "/" to serve the
    # protocol at the host root so clients paste a bare "mcp.example.com" with no
    # path. When "/", the root answers both audiences by method: GET (browser)
    # gets the landing page, POST is the protocol. Keep in step with AUTH0_AUDIENCE
    # — the resource clients validate is this same URL (see auth.protected_resource
    # _metadata), so a "/" path means the audience is the bare public_url.
    mcp_path: str = "/mcp"

    @property
    def mcp_endpoint_url(self) -> str:
        """The URL a client connects to — public_url plus the endpoint path."""
        base = self.public_url.rstrip("/")
        return base if self.mcp_path == "/" else base + self.mcp_path

    @property
    def mcp_endpoint_display(self) -> str:
        """The endpoint without its scheme, for showing on the landing page."""
        return self.mcp_endpoint_url.split("://", 1)[-1]

    # --- Guard rails ---------------------------------------------------------
    max_object_bytes: int = 200 * 1024 * 1024
    max_preview_rows: int = 100

    # Aggregation output: enough groups to be useful, few enough to stay legible
    # in a conversation.
    max_result_rows: int = 500

    # Backups an organisation exposes: the newest few, nothing older. This is a
    # visibility window for the whole server rather than a listing nicety —
    # everything addressable goes through backup_dates(), and resolve_date()
    # checks a caller's date against it, so a backup outside the window reads as
    # unavailable to every tool that takes a backup_date, not merely absent from
    # list_backups.
    max_backup_dates: int = 7

    # A backup older than this many days is flagged as stale in `browse`, so a
    # feed that has quietly stopped (an organisation whose bookkeeping ended) is
    # visible from the listing instead of only by drilling in.
    stale_backup_days: int = 14

    # `browse` counts rows for .jsonl files at or under this size (it downloads
    # them to count lines). Larger files show their byte size only, so listing a
    # backup stays a navigation call rather than a full download.
    max_rowcount_bytes: int = 12_000_000

    # A sweep across organisations downloads one file per organisation, so an
    # account with ninety of them is ninety round trips. Cap it, and say so in
    # the answer rather than pretending the sample was the whole portfolio.
    #
    # Twelve rather than twenty-five: behind an API gateway the whole response
    # has thirty seconds, and a sweep that times out returns nothing at all,
    # which is worse than a smaller sweep that answers.
    max_organisations_per_sweep: int = 12

    # Downloads run in parallel. Each worker holds one backup file in memory, so
    # this trades against the function's memory rather than being free.
    sweep_concurrency: int = 6

    # --- Rate limiting -------------------------------------------------------
    # Per-caller, on top of the gateway's stage-wide throttle. See ratelimit.py
    # for why both exist. Turn off only for local load testing.
    rate_limit_enabled: bool = True

    # By source address, applied before anyone is identified — so it also covers
    # the unauthenticated discovery documents and requests with a bad token.
    # The discovery response is cacheable for an hour, so a well-behaved client
    # fetches it once; anything near this ceiling is not a well-behaved client.
    rate_limit_anon_per_minute: int = 60
    rate_limit_anon_burst: int = 20

    # By authenticated subject. One analytics question can fan out into a dozen
    # tool calls, and a person may ask several in a row, so the burst is what
    # matters here rather than the sustained rate.
    rate_limit_user_per_minute: int = 120
    rate_limit_user_burst: int = 40

    # Largest request body accepted. A tool call is a small JSON document; the
    # only thing a large body can do here is occupy the function's memory before
    # it is rejected, so it is refused on the Content-Length rather than read.
    max_request_bytes: int = 1024 * 1024

    # --- OAuth proxy ---------------------------------------------------------
    # When on, this server is its own authorization server toward MCP clients and
    # federates to Auth0 as one first-party client (see oauth_proxy.py). Off, it
    # is a plain resource server validating Auth0 tokens directly. The switch
    # changes which issuer/keys tokens are validated against and which
    # authorization server the discovery document advertises.
    oauth_proxy_enabled: bool = False

    # The single first-party Auth0 application the proxy logs in through. Distinct
    # from auth0_client_id (the machine-to-machine app used for lookup_email):
    # this one is a Regular Web App with a client secret and a /callback URL.
    oauth_proxy_upstream_client_id: str = ""
    oauth_proxy_upstream_client_secret: str = ""

    # Auth0 `prompt` value sent on every /authorize. "login" forces a fresh
    # credential entry on each Connect — the browser SSO session is ignored, so a
    # user who disconnected can never be silently re-authenticated as the previous
    # account. The cost is that every Connect re-prompts, including routine
    # reconnects, since the stateless proxy can't tell "just disconnected" from
    # "normal reconnect". Set empty to restore silent SSO reuse, or
    # "select_account" for the lighter account-picker instead of a full re-login.
    oauth_authorize_prompt: str = "login"

    # RS256 private key (PEM, optionally base64) the proxy signs tokens with. Must
    # be the same across every instance or a token minted on one fails on the
    # next, and it doubles as the seed for the internal HMAC/encryption secret —
    # rotating it invalidates every outstanding proxy token at once (the
    # emergency "sign everyone out" lever). Empty generates an ephemeral key,
    # single-process only. In production source this from Secrets Manager / SSM.
    #
    # The proxy holds no database: every transient value (login state, auth code,
    # refresh token) is a signed/encrypted self-contained token, and revocation
    # is delegated to Auth0 via the refresh chain (see oauth_proxy.py).
    oauth_proxy_signing_key: str = ""

    # HTTPS redirect-URI hosts the proxy accepts, besides loopback (always
    # allowed for native/CLI clients per RFC 8252). Since the client_id is
    # opaque, this is the real control on where an authorization code may be
    # sent. Comma-separated; a listed host also matches its subdomains.
    oauth_proxy_allowed_redirect_hosts_csv: str = "claude.ai,cursor.com,anthropic.com"

    @property
    def oauth_proxy_allowed_redirect_hosts(self) -> set[str]:
        return {h.strip().lower() for h in
                self.oauth_proxy_allowed_redirect_hosts_csv.split(",") if h.strip()}

    # --- Live Xero -----------------------------------------------------------
    # A separate capability from the backups: read an organisation's data live
    # from Xero's own API. Off unless the app credentials are present.
    xero_live_enabled: bool = False

    # The Xero app (from the portal's own Xero integration). Needed to refresh a
    # stored refresh token into a 30-minute access token — the DB's stored access
    # token is a backup-cycle token and is stale most of the time.
    xero_client_id: str = ""
    xero_client_secret: str = ""

    xero_token_url: str = "https://identity.xero.com/connect/token"
    xero_api_base: str = "https://api.xero.com/api.xro/2.0"
    xero_http_timeout: float = 20.0

    # Where the *rotated* refresh token is persisted between calls. Xero rotates
    # it on every refresh, so it must survive across stateless invocations.
    #   "file" — a local directory. Development / single host only.
    #   "s3"   — an object per org. What the deployed Lambda uses.
    # Interim: we own this store because we do not yet have DB write access. When
    # write-back lands, this becomes the `xero` table and the backup engine and
    # this server share one rotating token again.
    xero_token_store: str = "file"
    xero_token_store_dir: str = ".xero-tokens"
    xero_token_bucket: str = ""
    xero_token_prefix: str = "xero-live-tokens/"

    @property
    def management_domain(self) -> str:
        """Where the Management API lives — never the custom domain."""
        return self.auth0_tenant_domain or self.auth0_domain

    @property
    def issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @property
    def jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
