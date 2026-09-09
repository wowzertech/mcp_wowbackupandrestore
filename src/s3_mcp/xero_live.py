"""Live Xero access — read an organisation's data from Xero's own API, as
opposed to the S3 backups the rest of this server serves.

Why this is more than "just call the API": Xero access tokens live 30 minutes,
and the token the backup engine leaves in the `xero` table is a backup-cycle
token — stale the vast majority of the time (measured: ~5% valid at any given
moment). So every live call may need a refresh, and a refresh needs the Xero app
credentials and the organisation's refresh token.

Xero *rotates* the refresh token on every refresh: the response carries a new
refresh token and the old one dies (after a short grace window). Whoever
refreshes must persist the new token or the next refresh fails. Today we do not
have write access to the portal database, so we persist the rotated token in our
own store (see TokenStore). The consequence — and it must be understood before
this points at production — is that once we refresh an org, the copy still in
`xero.refresh_token` is orphaned, so the backup engine reading that copy will
fail for that org until the planned DB write-back replaces this store.

The token source is deliberately pluggable so that write-back is a store swap,
not a rewrite of this module.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .entitlements import fetch_xero_seed
from .settings import get_settings

# Read endpoints we expose, mapped to their Xero path. An allow-list, so a caller
# can never point the tool at an arbitrary or write endpoint.
RESOURCES: dict[str, str] = {
    "organisation": "Organisations",
    "invoices": "Invoices",
    "contacts": "Contacts",
    "accounts": "Accounts",
    "bank_transactions": "BankTransactions",
    "credit_notes": "CreditNotes",
    "payments": "Payments",
    "items": "Items",
    "journals": "Journals",
    "profit_and_loss": "Reports/ProfitAndLoss",
    "balance_sheet": "Reports/BalanceSheet",
    "trial_balance": "Reports/TrialBalance",
    "aged_receivables": "Reports/AgedReceivablesByContact",
}


class XeroLiveError(RuntimeError):
    """A live-Xero call could not be completed. Message is safe to show a user."""


@dataclass
class TokenRecord:
    refresh_token: str
    access_token: str
    expires_at: float      # unix seconds; when the access token stops working
    tenant_id: str


# --------------------------------------------------------------------------- #
# Token store — where the rotated refresh token lives between calls
# --------------------------------------------------------------------------- #

class _FileStore:
    """One JSON file per organisation under a local directory.

    Development / single-host only: it has no cross-process locking and writes
    the refresh token in plaintext, so it is unfit for the Lambda. The deployed
    server uses _S3Store (encrypted at rest); production ultimately uses the
    database once write-back exists."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, org_id: int) -> Path:
        return self._dir / f"{org_id}.json"

    def get(self, org_id: int) -> TokenRecord | None:
        p = self._path(org_id)
        if not p.exists():
            return None
        return TokenRecord(**json.loads(p.read_text()))

    def put(self, org_id: int, rec: TokenRecord) -> None:
        # Write-then-rename so a crash mid-write cannot leave a half token.
        tmp = self._path(org_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(rec)))
        tmp.replace(self._path(org_id))


class _S3Store:
    """One object per organisation. What the Lambda uses.

    Server-side encryption (AES256) covers at-rest; the object body is still the
    refresh token, so the bucket must be private and access-logged. This is an
    interim home until DB write-back — noted so it is not mistaken for the final
    design."""

    def __init__(self, bucket: str, prefix: str, region: str) -> None:
        import boto3

        self._bucket = bucket
        self._prefix = prefix
        self._s3 = boto3.client("s3", region_name=region or None)

    def _key(self, org_id: int) -> str:
        return f"{self._prefix}{org_id}.json"

    def get(self, org_id: int) -> TokenRecord | None:
        import botocore.exceptions

        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=self._key(org_id))
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return TokenRecord(**json.loads(obj["Body"].read()))

    def put(self, org_id: int, rec: TokenRecord) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(org_id),
            Body=json.dumps(asdict(rec)).encode(),
            ServerSideEncryption="AES256",
            ContentType="application/json",
        )


_store = None


def _get_store():
    global _store
    if _store is not None:
        return _store
    s = get_settings()
    if s.xero_token_store == "s3":
        if not s.xero_token_bucket:
            raise XeroLiveError("xero_token_store=s3 but xero_token_bucket is unset")
        _store = _S3Store(s.xero_token_bucket, s.xero_token_prefix, s.aws_region)
    else:
        _store = _FileStore(s.xero_token_store_dir)
    return _store


# --------------------------------------------------------------------------- #
# Refresh + call
# --------------------------------------------------------------------------- #

async def _refresh(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token (and a new refresh token).

    Xero authenticates the confidential client with HTTP Basic on the token
    endpoint; the body carries only the grant."""
    s = get_settings()
    if not (s.xero_client_id and s.xero_client_secret):
        raise XeroLiveError(
            "Live Xero is not configured on this server (missing app credentials)."
        )
    basic = base64.b64encode(
        f"{s.xero_client_id}:{s.xero_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=s.xero_http_timeout) as client:
        r = await client.post(
            s.xero_token_url,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if r.status_code != 200:
        # A 400 invalid_grant here means the refresh token was rotated out from
        # under us — most likely the backup engine refreshed it. This is the
        # two-writers hazard the module docstring warns about.
        raise XeroLiveError(
            f"Xero refused the token refresh ({r.status_code}). The connection may "
            f"need re-authorising in the portal."
        )
    return r.json()


async def _access_token(org_id: int, tenant_hint: str | None,
                        force: bool = False) -> tuple[str, str]:
    """Return a valid (access_token, tenant_id) for an organisation, refreshing
    if the stored access token is missing, expiring, or `force`d."""
    store = _get_store()
    rec = store.get(org_id)
    now = time.time()
    if rec and not force and rec.access_token and rec.expires_at > now + 60:
        return rec.access_token, rec.tenant_id or (tenant_hint or "")

    # Need to refresh. Prefer our own rotated token; fall back to the DB seed the
    # first time only (after which the DB copy is orphaned — see docstring).
    refresh_token = rec.refresh_token if rec else None
    tenant_id = (rec.tenant_id if rec else None) or tenant_hint
    if not refresh_token:
        seed = await fetch_xero_seed(org_id)
        if not seed or not seed.get("refresh_token"):
            raise XeroLiveError(
                "No Xero connection token is available for this organisation."
            )
        refresh_token = seed["refresh_token"]
        tenant_id = tenant_id or seed.get("tenant_id")

    body = await _refresh(refresh_token)
    rec = TokenRecord(
        refresh_token=body.get("refresh_token", refresh_token),
        access_token=body["access_token"],
        expires_at=now + int(body.get("expires_in", 1800)),
        tenant_id=tenant_id or "",
    )
    store.put(org_id, rec)
    return rec.access_token, rec.tenant_id


async def _get(tenant_id: str, access_token: str, path: str,
               params: dict | None) -> tuple[int, dict]:
    s = get_settings()
    url = f"{s.xero_api_base}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=s.xero_http_timeout) as client:
        r = await client.get(
            url,
            params={k: v for k, v in (params or {}).items() if v not in (None, "")},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-tenant-id": tenant_id,
                "Accept": "application/json",
            },
        )
    if r.status_code == 429:
        retry = r.headers.get("Retry-After", "?")
        raise XeroLiveError(f"Xero rate limit reached; retry after {retry}s.")
    return r.status_code, (r.json() if r.status_code == 200 else {"_raw": r.text[:300]})


async def fetch(org_id: int, tenant_hint: str | None, resource: str,
                where: str = "", page: int = 1) -> dict:
    """Fetch one read resource for an organisation, refreshing the token as
    needed and retrying once if a cached token is unexpectedly rejected."""
    if resource not in RESOURCES:
        raise XeroLiveError(
            f"Unknown resource {resource!r}. Available: {', '.join(sorted(RESOURCES))}."
        )
    path = RESOURCES[resource]
    params: dict = {}
    if not path.startswith("Reports/"):
        params["page"] = max(1, int(page))
        if where:
            params["where"] = where

    access_token, tenant_id = await _access_token(org_id, tenant_hint)
    status, body = await _get(tenant_id, access_token, path, params)
    if status == 401:
        # Stored token rejected — force one refresh and retry.
        access_token, tenant_id = await _access_token(org_id, tenant_hint, force=True)
        status, body = await _get(tenant_id, access_token, path, params)
    if status != 200:
        raise XeroLiveError(f"Xero returned {status} for {resource}.")
    return body
