"""get_live_data: read an organisation live from its accounting platform."""
from __future__ import annotations

import base64
import contextvars
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import duckdb

from .. import xero_live
from ..auth import current_principal, note_signed_out
from ..entitlements import resolve_entitlement, revoke_auth0_session
from ..settings import get_settings
from ..storage import UserStore
from .._core import *  # noqa: F401,F403  — mcp instance and the shared engine
from .._core import (  # noqa: F401  — explicit for names starting with underscore
    _server_icons,
    SERVER_VERSION,
    SERVER_CHANGELOG,
    mcp,
    _changelog,
    _store,
    _jsonl_sibling,
    _denoise,
    _OPERATIONS,
    _COMPARISONS,
    _CONDITION,
    _quote,
    _resolve_column,
    _PATH_SEG,
    _DATE_VALUE,
    _NUMERIC_VALUE,
    _PERIODS,
    _as_date,
    _is_list_type,
    _resolve_ref,
    _WHERE_KEYWORD,
    _tokenize_where,
    _compile_condition,
    _build_where,
    _leaf_conditions,
    _explain_empty,
    _aggregate,
    _workspace,
    _dataset,
)



# --------------------------------------------------------------------------- #
# Live Xero
# --------------------------------------------------------------------------- #

def _compact_xero(body: dict, page: int) -> str:
    """Turn a Xero response into a compact, size-bounded result.

    List responses (Invoices, Contacts, …) are capped to max_preview_rows with
    the true total noted; single-object responses (Reports, Organisation) are
    serialised whole but truncated if they would flood the conversation."""
    settings = get_settings()
    array_key = next(
        (k for k, v in body.items()
         if isinstance(v, list) and k not in ("ValidationErrors",)),
        None,
    )
    if array_key is not None:
        rows = body[array_key]
        cap = settings.max_preview_rows
        shown = rows[:cap]
        head = (
            f"{array_key}: {len(rows)} record(s) on page {page}"
            + (f", showing first {cap}" if len(rows) > cap else "")
            + (". Ask for the next page for more." if len(rows) == 100 else "")
            + "\n"
        )
        return head + json.dumps(shown, indent=2, default=str)

    text = json.dumps(body, indent=2, default=str)
    limit = 20_000
    if len(text) > limit:
        return text[:limit] + f"\n… truncated ({len(text):,} chars total)."
    return text


@mcp.tool()
async def get_live_data(organisation: str, resource: str = "organisation",
                        where: str = "", page: int = 1, source: str = "xero") -> str:
    """Read an organisation's data LIVE from its accounting platform — only when
    the server has a live connection configured.

    Live access is OPTIONAL and may be OFF on this server. When it is, this tool
    returns a plain "not enabled" message and you should use the backup tools
    (browse / preview_file / aggregate_file), which are always available and are
    the reliable path for a point-in-time snapshot, a period statement, or
    history. When enabled, live reads are subject to the platform's own rate
    limits. Don't assume this returns data — check its response.

    Args:
        organisation: Name as returned by `browse`.
        resource: One of organisation, invoices, contacts, accounts,
            bank_transactions, credit_notes, payments, items, journals,
            profit_and_loss, balance_sheet, trial_balance, aged_receivables.
        where: Optional platform filter, e.g. Status=="AUTHORISED" (list
            resources only).
        page: Page number for list resources (100 records per page).
        source: Platform to read live from. Xero is the only platform with a live
            path built, and only when it is enabled on the server; "quickbooks"
            live isn't available (its backups are).
    """
    principal = current_principal()
    principal.require("backups:read")

    if source.strip().lower() not in ("", "xero"):
        return (f"Live data for {source!r} isn't available yet — only Xero is "
                "wired up for live reads. The backup tools cover QuickBooks and "
                "Xero alike.")

    settings = get_settings()
    if not settings.xero_live_enabled:
        return ("Live Xero is not enabled on this server yet. The backup tools "
                "still work; live data needs the Xero connection configured.")

    if resource not in xero_live.RESOURCES:
        return ("Unknown resource. Choose one of: "
                + ", ".join(sorted(xero_live.RESOURCES)) + ".")

    ent = await resolve_entitlement(principal.subject, principal.email)
    ent.check()
    if not ent.permits_organisation(organisation):
        return (f"{organisation!r} is not an organisation on your account. "
                "Run `browse` with no arguments to see the ones you can read.")

    loc = ent.location_for(organisation)
    if not loc.xero_org_id:
        return (f"{organisation!r} has no live Xero connection on record, so only "
                "its backups are available.")

    try:
        body = await xero_live.fetch(
            loc.xero_org_id, loc.xero_tenant_id, resource, where=where, page=page
        )
    except xero_live.XeroLiveError as exc:
        return f"Could not read live Xero data: {exc}"

    return f"Live {resource} for {organisation}:\n" + _compact_xero(body, page)
