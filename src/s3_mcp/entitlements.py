"""Entitlement resolution.

Auth0 answers one question: *who is this*. It does so with `sub`, which is stable
and cannot be changed by the user. Everything else — is the subscription live,
which S3 prefix belongs to them, what tier are they on — comes from your database.

This split matters. In the earlier design the S3 prefix came from the token's
email claim, which meant a user changing their address silently lost their data,
and an unverified address was a path into someone else's. Here the token carries
no authority over paths at all: it names a subject, the database returns the
prefix. The email claim becomes a convenience for logging.

Three properties this module has to hold:

1. **Fail closed.** If the database is unreachable we do not know whether the
   subscription is live, so we refuse. An outage that serves data to lapsed
   accounts is worse than an outage that serves nothing.
2. **Stateless-compatible.** The cache is per-instance and short-lived. Any
   instance can answer any call; a warm cache is an optimisation, never a
   correctness requirement.
3. **Bounded revocation lag.** A cancelled subscription keeps working until its
   cache entry expires. `ENTITLEMENT_CACHE_TTL` is that exposure window — set it
   deliberately rather than by accident.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Mapping, Protocol

import httpx

from .settings import get_settings


class SubscriptionInactive(PermissionError):
    """Authenticated, but not entitled. Distinct from 'not authenticated' so the
    transport can return 403 rather than 401 — a 401 makes the client retry the
    login loop, which will not help someone whose plan has lapsed."""


@dataclass(frozen=True)
class OrgLocation:
    """Where one organisation's backups actually are.

    Three coordinates, all from the database: the prefix (the account that
    connected the organisation), and the bucket and region for the country it is
    stored in. None of them is ever taken from the caller.

    `xero_org_id` and `xero_tenant_id` are the two stable Xero identifiers for
    the same organisation, carried here so a live-Xero call can find the right
    tenant without a second query. Both are stable (an org's tenant GUID and the
    portal's row id do not change), so they are safe to hold in the cached
    entitlement — unlike the Xero *refresh token*, which rotates on every use and
    is therefore read fresh from the database at call time, never cached."""

    prefix: str
    bucket: str
    region: str
    # "xero" | "qbo" — which product this organisation's backups come from. The
    # backup files differ (Xero entities vs QuickBooks entities) but are read the
    # same way; the source is carried for labelling and for the live API path.
    source: str = "xero"
    # Some products nest the backup files one level below the date folder. Xero
    # writes them directly under {date}/; QuickBooks writes them under
    # {date}/data/. Empty means "files sit directly in the date folder".
    backup_subdir: str = ""
    xero_org_id: int | None = None
    xero_tenant_id: str | None = None
    qbo_org_id: int | None = None
    qbo_realm_id: str | None = None
    # Backup dates whose most recent run did not finish — 'running', 'paused',
    # 'failed', or an empty status in the backup queue. Those folders can hold
    # partial data, so the reader hides them from listings and refuses to read
    # them: only a completed backup is ever served. Empty means "nothing known
    # to be in progress" — the safe default for backends without this signal.
    incomplete_dates: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Entitlement:
    """What this subject may read, according to the system of record.

    `org_prefixes` exists because organisations are shared. A Xero organisation
    is backed up once, under the prefix of the account that connected it, and
    other members are granted access to that same copy. So the prefix is a
    property of the *organisation*, not of the caller — a member reading a
    colleague's organisation reads it under the owner's prefix. Where the map is
    present it is authoritative: an organisation absent from it cannot be named,
    and the prefix for one present in it is never taken from the caller.

    `s3_prefix` remains the caller's own root. It is the fallback for backends
    that do not supply a map (portal_api, postgres), and what a caller's own
    organisations resolve under."""

    account_id: str
    s3_prefix: str          # e.g. "aksh29@yopmail.com/" — canonical, from the DB
    plan: str               # "free" | "pro" | ... whatever your billing uses
    active: bool
    organisations: tuple[str, ...] | None = None   # None = all under the prefix
    org_locations: Mapping[str, OrgLocation] | None = None

    def check(self) -> None:
        if not self.active:
            raise SubscriptionInactive(
                "This account does not have an active subscription. "
                "Renew in the portal and reconnect."
            )

    def permits_organisation(self, name: str) -> bool:
        if self.org_locations is not None:
            return name in self.org_locations
        if self.organisations is None:
            return True
        return name in self.organisations

    def location_for(self, organisation: str) -> OrgLocation:
        """Where to read this organisation's backups from.

        Raises rather than falling back to the caller's own prefix: a missing
        entry means the organisation is not one this account may read, and
        quietly substituting a location would turn that into an empty listing
        instead of a refusal."""
        if self.org_locations is None:
            s = get_settings()
            bucket, region = s.location_for(None)
            return OrgLocation(prefix=self.s3_prefix, bucket=bucket, region=region)
        try:
            return self.org_locations[organisation]
        except KeyError:
            raise PermissionError(
                f"your subscription does not include the organisation {organisation!r}"
            ) from None

    def prefix_for(self, organisation: str) -> str:
        return self.location_for(organisation).prefix


# --------------------------------------------------------------------------- #
# Resolvers
# --------------------------------------------------------------------------- #

class EntitlementResolver(Protocol):
    async def resolve(self, subject: str, email: str | None) -> Entitlement: ...


class PortalApiResolver:
    """Ask the existing portal backend.

    Preferred over talking to the database directly: the portal already owns the
    subscription logic, and duplicating "what counts as active" across two
    codebases is how the two answers drift apart. It also keeps this server free
    of database credentials and connection-pool problems on serverless.

    Expects an internal endpoint returning:
        {"account_id": "...", "s3_prefix": "...", "plan": "pro", "active": true,
         "organisations": ["Test1"]}          # organisations optional
    """

    def __init__(self) -> None:
        s = get_settings()
        self._url = s.entitlement_api_url.rstrip("/")
        self._token = s.entitlement_api_token
        self._timeout = s.entitlement_api_timeout

    async def resolve(self, subject: str, email: str | None) -> Entitlement:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._url}/entitlements",
                params={"sub": subject},
                headers=headers,
            )

        if response.status_code == 404:
            raise SubscriptionInactive(
                "No account found for this login. If you have a subscription, "
                "sign in with the same account you use on the portal."
            )
        response.raise_for_status()
        payload = response.json()

        prefix = payload["s3_prefix"]
        if not prefix.endswith("/"):
            prefix += "/"

        orgs = payload.get("organisations")
        return Entitlement(
            account_id=str(payload["account_id"]),
            s3_prefix=prefix,
            plan=payload.get("plan", "unknown"),
            active=bool(payload.get("active", False)),
            organisations=tuple(orgs) if orgs is not None else None,
        )


class PostgresResolver:
    """Direct database access, for when there is no portal API to call.

    Serverless note: open one pool per process, never per request. On Lambda,
    put RDS Proxy in front or you will exhaust connections under any real
    concurrency — a stateless protocol still means many concurrent instances.
    """

    _pool = None

    async def _get_pool(self):
        if PostgresResolver._pool is None:
            import asyncpg

            PostgresResolver._pool = await asyncpg.create_pool(
                get_settings().database_url, min_size=1, max_size=4
            )
        return PostgresResolver._pool

    async def resolve(self, subject: str, email: str | None) -> Entitlement:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT a.id            AS account_id,
                       a.s3_prefix     AS s3_prefix,
                       s.plan          AS plan,
                       (s.status = 'active' AND s.current_period_end > now())
                                       AS active
                  FROM accounts a
                  LEFT JOIN subscriptions s ON s.account_id = a.id
                 WHERE a.auth0_sub = $1
                 ORDER BY s.current_period_end DESC NULLS LAST
                 LIMIT 1
                """,
                subject,
            )

        if row is None:
            raise SubscriptionInactive("No account found for this login.")

        prefix = row["s3_prefix"]
        if not prefix.endswith("/"):
            prefix += "/"

        return Entitlement(
            account_id=str(row["account_id"]),
            s3_prefix=prefix,
            plan=row["plan"] or "none",
            active=bool(row["active"]),
        )


# --------------------------------------------------------------------------- #
# Email lookup
# --------------------------------------------------------------------------- #

_mgmt: tuple[float, str] | None = None
_emails: dict[str, tuple[float, tuple[str | None, bool]]] = {}


async def _management_token() -> str:
    """A Management API token, reused until shortly before it expires."""
    global _mgmt
    now = time.monotonic()
    if _mgmt and _mgmt[0] > now:
        return _mgmt[1]

    s = get_settings()
    domain = s.management_domain
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"https://{domain}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": s.auth0_client_id,
                "client_secret": s.auth0_client_secret,
                "audience": f"https://{domain}/api/v2/",
            },
        )
    response.raise_for_status()
    body = response.json()
    _mgmt = (now + int(body.get("expires_in", 3600)) - 60, body["access_token"])
    return _mgmt[1]


async def revoke_auth0_session(subject: str) -> list[str]:
    """End a subject's Auth0 session and take away what could rebuild it.

    Three things, because any one alone leaves a way back in without a password:
    the login session, the refresh tokens a client renews with silently, and the
    recorded consent that lets the next authorization complete without asking.

    Returns a list of what was revoked, for the message shown to the user."""
    s = get_settings()
    if not (s.auth0_client_id and s.auth0_client_secret):
        return []

    token = await _management_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://{s.management_domain}/api/v2"
    done: list[str] = []

    async with httpx.AsyncClient(timeout=10) as client:
        for path, label in (
            (f"/users/{subject}/sessions", "session"),
            (f"/users/{subject}/refresh-tokens", "refresh tokens"),
        ):
            response = await client.delete(f"{base}{path}", headers=headers)
            if response.status_code in (200, 202, 204):
                done.append(label)

        grants = await client.get(
            f"{base}/grants", headers=headers, params={"user_id": subject}
        )
        if grants.status_code == 200:
            rows = grants.json()
            for grant in rows if isinstance(rows, list) else []:
                await client.delete(f"{base}/grants/{grant['id']}", headers=headers)
            if rows:
                done.append(f"{len(rows)} consent grant(s)")

    # Nothing about this subject should survive into the next session: a stale
    # entitlement or email would be answered from cache for up to the TTL.
    _cache.pop(subject, None)
    _emails.pop(subject, None)
    return done


async def lookup_email(subject: str) -> tuple[str | None, bool]:
    """Read a subject's email address from Auth0.

    The access token carries `email` only on the token minted at login: Auth0
    does not re-run post-login Actions when a client exchanges a refresh token,
    and MCP clients refresh constantly. A resolver that trusts the claim
    therefore works for one request and then stops, which is indistinguishable
    from a lapsed subscription. Asking Auth0 makes the address available on
    every request instead of only the first.

    Cached per subject for the entitlement TTL; failures are not cached."""
    s = get_settings()
    if not (s.auth0_client_id and s.auth0_client_secret):
        return None, False

    now = time.monotonic()
    hit = _emails.get(subject)
    if hit and hit[0] > now:
        return hit[1]

    token = await _management_token()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"https://{s.management_domain}/api/v2/users/{subject}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "email,email_verified", "include_fields": "true"},
        )
    response.raise_for_status()
    body = response.json()
    result = (body.get("email"), bool(body.get("email_verified")))

    _emails[subject] = (now + s.entitlement_cache_ttl, result)
    if len(_emails) > 2_000:
        for key in [k for k, (exp, _) in _emails.items() if exp <= now]:
            _emails.pop(key, None)
    return result


class MySQLResolver:
    """The portal's own database.

    Schema, as it actually is:

        users                     one row per person; `root` is their S3 prefix
                                  (always their email), `auth0_user_id` the sub
        xero                      one row per connected organisation; `account`
                                  is its name, `email` the prefix its backups
                                  live under, `subscription_status` 1 when live
        xero_user_connections     which people may see which organisations
        blocked_users             suspended accounts

    An organisation is backed up once, under the connecting account's prefix,
    and shared with colleagues — so this returns a prefix per organisation
    rather than one per caller. Only organisations with a live subscription are
    included; an account with none is inactive and refused.
    """

    _pool = None

    async def _get_pool(self):
        if MySQLResolver._pool is None:
            import aiomysql

            s = get_settings()
            MySQLResolver._pool = await aiomysql.create_pool(
                host=s.mysql_host,
                port=s.mysql_port,
                user=s.mysql_user,
                password=s.mysql_password,
                db=s.mysql_database,
                minsize=1,
                maxsize=4,
                autocommit=True,
                connect_timeout=10,
            )
        return MySQLResolver._pool

    async def resolve(self, subject: str, email: str | None) -> Entitlement:
        s = get_settings()
        # The email arm is a bridge while the MCP runs on its own Auth0 tenant;
        # see entitlement_match_verified_email in settings. Tokens minted by a
        # refresh carry no email claim, so ask Auth0 rather than treating the
        # absence as "no such account".
        if s.entitlement_match_verified_email and not email:
            email, verified = await lookup_email(subject)
            if email and s.require_email_verified and not verified:
                raise SubscriptionInactive(
                    "Verify your email address before connecting, then reconnect."
                )
        match_email = email if (email and s.entitlement_match_verified_email) else ""

        # One deployment can serve more than one database on the same server —
        # the live customer database and a staging one for test accounts. The
        # caller is resolved against each in turn and the results merged: a login
        # present in either is served, and an organisation present in both is
        # kept once, tagged with its database so neither hides the other.
        pool = await self._get_pool()
        locations: dict[str, OrgLocation] = {}
        plans: set[str] = set()
        account_id: object | None = None
        own_root = ""

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for db in s.mysql_database_list:
                    found = await self._resolve_in_db(
                        cur, db, subject, match_email, s, locations, plans
                    )
                    if found and account_id is None:
                        account_id, own_root = found

        if account_id is None:
            raise SubscriptionInactive(
                "No account found for this login. If you have a subscription, "
                "sign in with the same address you use on the portal."
            )

        if not own_root.endswith("/"):
            own_root += "/"

        return Entitlement(
            account_id=str(account_id),
            s3_prefix=own_root,
            plan=", ".join(sorted(plans)) if plans else "none",
            active=bool(locations),
            organisations=tuple(sorted(locations)),
            org_locations=locations,
        )

    async def _resolve_in_db(self, cur, db, subject, match_email, s,
                             locations, plans):
        """Resolve one database and merge its organisations into `locations`.

        Returns (user_id, own_root) if the login exists in this database, else
        None. Table names are qualified with the database so a single pooled
        connection can read several. `db` is configuration, never a caller
        value, but is still constrained to an identifier as defence in depth."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", db or ""):
            raise RuntimeError(f"invalid database name: {db!r}")

        await cur.execute(
            f"""
            SELECT u.id, u.email, u.root,
                   x.account, x.email AS org_prefix,
                   x.subscription_status, x.subscription_type,
                   x.country_code, x.id AS xero_org_id, x.tenant_id,
                   (SELECT bq.s3_bucket_url FROM {db}.backup_queues bq
                     WHERE bq.xero_id = x.id
                       AND bq.s3_bucket_url IS NOT NULL AND bq.s3_bucket_url <> ''
                     ORDER BY bq.id DESC LIMIT 1) AS backup_url
              FROM {db}.users u
              LEFT JOIN {db}.xero_user_connections c ON c.user_id = u.id
              LEFT JOIN {db}.xero x
                     ON x.id = c.xero_org_id AND x.deleted_at IS NULL
             WHERE u.deleted_at IS NULL
               AND (u.auth0_user_id = %s OR (%s <> '' AND u.email = %s))
            """,
            (subject, match_email, match_email),
        )
        rows = await cur.fetchall()
        if not rows or rows[0][0] is None:
            return None

        user_id = rows[0][0]
        await cur.execute(
            f"SELECT is_blocked FROM {db}.blocked_users "
            f"WHERE user_id = %s ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        blocked = await cur.fetchone()
        if blocked and blocked[0]:
            raise SubscriptionInactive(
                "This account is suspended. Contact support to restore access."
            )

        qbo_rows: list = []
        if s.qbo_backups_enabled:
            await cur.execute(
                f"""
                SELECT q.account, q.email AS org_prefix,
                       q.subscription_status, q.subscription_type,
                       q.country_code, q.id AS qbo_org_id, q.realm_id,
                       (SELECT bq.s3_bucket_url FROM {db}.qbo_backup_queues bq
                         WHERE bq.realm_id = q.realm_id
                           AND bq.s3_bucket_url IS NOT NULL AND bq.s3_bucket_url <> ''
                         ORDER BY bq.id DESC LIMIT 1) AS backup_url
                  FROM {db}.qbo_user_connections c
                  JOIN {db}.qbo q ON q.id = c.qbo_org_id AND q.deleted_at IS NULL
                 WHERE c.user_id = %s
                """,
                (user_id,),
            )
            qbo_rows = await cur.fetchall()

        async def _incomplete(table: str, idcol: str, ids: list) -> dict:
            """Per organisation, the set of backup dates whose newest run did
            not complete. A folder is written while its run is in progress, so
            these dates may hold partial data and must not be served. Keyed on
            str(id) so an int xero_id and a string realm_id compare alike."""
            wanted = [i for i in {*ids} if i is not None]
            if not wanted:
                return {}
            placeholders = ",".join(["%s"] * len(wanted))
            await cur.execute(
                f"""
                SELECT {idcol}, DATE(backup_date) AS d, status
                  FROM {db}.{table}
                 WHERE {idcol} IN ({placeholders}) AND backup_date IS NOT NULL
                 ORDER BY id
                """,
                tuple(wanted),
            )
            # ORDER BY id ascending: the last write for each (org, date) wins,
            # so a completed run supersedes an earlier failure and vice versa.
            latest: dict = {}
            for oid, d, status in await cur.fetchall():
                if d is None:
                    continue
                latest[(str(oid), d.isoformat())] = (status or "").strip().lower()
            out: dict = {}
            for (oid, day), status in latest.items():
                if status != "completed":
                    out.setdefault(oid, set()).add(day)
            return out

        xero_incomplete = await _incomplete(
            "backup_queues", "xero_id", [r[8] for r in rows]
        )
        qbo_incomplete = (
            await _incomplete("qbo_backup_queues", "realm_id",
                              [r[6] for r in qbo_rows])
            if s.qbo_backups_enabled else {}
        )

        def put(name: str, loc: OrgLocation) -> None:
            # Same-name orgs across databases both survive, the later tagged.
            key = name if name not in locations else f"{name} [{db}]"
            locations[key] = loc

        for row in rows:
            (_uid, _uemail, _root, account, org_prefix, sub_status, sub_type,
             country, xero_org_id, tenant_id, backup_url) = row
            if not account or sub_status != 1:
                continue
            prefix = _backup_prefix(backup_url, org_prefix)
            if not prefix:
                continue
            bucket, region = s.location_for(country)
            put(account, OrgLocation(
                prefix=prefix if prefix.endswith("/") else prefix + "/",
                bucket=bucket, region=region, source="xero",
                xero_org_id=int(xero_org_id) if xero_org_id is not None else None,
                xero_tenant_id=(tenant_id or "").strip() or None,
                incomplete_dates=frozenset(xero_incomplete.get(str(xero_org_id), ())),
            ))
            if sub_type:
                plans.add(sub_type)

        for row in qbo_rows:
            (account, org_prefix, sub_status, sub_type, country, qbo_org_id,
             realm_id, backup_url) = row
            if not account or sub_status != 1:
                continue
            prefix = _backup_prefix(backup_url, org_prefix)
            if not prefix:
                continue
            bucket, region = s.location_for(country, source="qbo")
            # A user could hold a Xero and a QuickBooks org of the same name;
            # disambiguate the QBO one rather than clobbering.
            name = account if account not in locations else f"{account} (QuickBooks)"
            put(name, OrgLocation(
                prefix=prefix if prefix.endswith("/") else prefix + "/",
                bucket=bucket, region=region, source="qbo", backup_subdir="data",
                qbo_org_id=int(qbo_org_id) if qbo_org_id is not None else None,
                qbo_realm_id=(realm_id or "").strip() or None,
                incomplete_dates=frozenset(qbo_incomplete.get(str(realm_id), ())),
            ))
            if sub_type:
                plans.add(sub_type)

        own_root = (rows[0][2] or rows[0][1] or "").strip()
        return (user_id, own_root)


def _backup_prefix(recorded_url: str | None, registered: str | None) -> str:
    """The account prefix an organisation's backups actually live under.

    An org's `email` column is meant to be that prefix, but it can drift from
    where the backup engine writes — an org set up under one account and later
    owned by another keeps the original value while new backups land under the
    new owner. The backup queue records the true destination each run, so trust
    its leading path segment, and fall back to the column only when nothing has
    been backed up yet."""
    if recorded_url:
        seg = recorded_url.strip().split("/", 1)[0].strip()
        if seg:
            return seg
    return (registered or "").strip()


async def fetch_xero_seed(xero_org_id: int) -> dict | None:
    """Read one organisation's Xero token row from the portal database.

    This is the *seed* for live-Xero: the refresh token the backup engine last
    stored. It is used only when our own store has no rotated token for the org
    yet — after the first refresh we hold our own copy and stop reading this,
    because both we and the backup engine rotating the same DB token would
    invalidate each other's. mysql backend only; other backends have no direct
    row to read.

    Returns {refresh_token, access_token, tenant_id, expires} or None. No values
    are cached; the caller decides what to persist."""
    if get_settings().entitlement_backend != "mysql":
        return None
    pool = await MySQLResolver()._get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT refresh_token, token, tenant_id, expires "
                "FROM xero WHERE id = %s AND deleted_at IS NULL",
                (xero_org_id,),
            )
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "refresh_token": row[0],
        "access_token": row[1],
        "tenant_id": row[2],
        "expires": row[3],
    }


def get_resolver() -> EntitlementResolver:
    backend = get_settings().entitlement_backend
    if backend == "portal_api":
        return PortalApiResolver()
    if backend == "mysql":
        return MySQLResolver()
    if backend == "postgres":
        return PostgresResolver()
    raise RuntimeError(f"unknown entitlement backend: {backend}")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

_cache: dict[str, tuple[float, Entitlement]] = {}


async def resolve_entitlement(subject: str, email: str | None) -> Entitlement:
    """Resolve with a short per-instance cache.

    Deliberately caches only *successful* resolutions. A failure is never cached:
    a database blip must not lock a paying customer out for the whole TTL.

    Inactive results ARE cached, because a lapsed account hammering the DB on
    every tool call is the exact traffic pattern you do not want when billing is
    already unhappy. The user sees a clear message either way."""
    ttl = get_settings().entitlement_cache_ttl
    now = time.monotonic()

    hit = _cache.get(subject)
    if hit and hit[0] > now:
        return hit[1]

    entitlement = await get_resolver().resolve(subject, email)
    _cache[subject] = (now + ttl, entitlement)

    # Unbounded dicts in long-lived containers are a slow leak. Trim on write.
    if len(_cache) > 2_000:
        for key in [k for k, (exp, _) in _cache.items() if exp <= now]:
            _cache.pop(key, None)

    return entitlement
