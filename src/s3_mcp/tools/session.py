"""logout: end the session and revoke it upstream."""
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



@mcp.tool(name="logout", title="WOW AI Suite — log out")
async def wow_aisuite_logout() -> str:
    """Sign out of WOW AI Suite — end the session so a different account can be
    used, or so a shared machine is left clean.

    Args: none. It signs out whoever is currently connected.
    """
    principal = current_principal()
    settings = get_settings()

    revoked = await revoke_auth0_session(principal.subject)
    note_signed_out(principal.subject, int(time.time()))

    who = principal.email or principal.subject
    logout_url = f"https://{settings.auth0_domain}/v2/logout"

    lines = [
        f"Signed out {who}.",
        "",
        "This connection will stop working on the next call, and no new access "
        "can be granted without signing in again.",
    ]
    if revoked:
        lines.append("Revoked at the identity provider: " + ", ".join(revoked) + ".")

    lines += [
        "",
        "Two things this server cannot do for you:",
        f"  1. Your browser may still hold a login session. Open {logout_url} "
        f"to clear it — otherwise the next sign-in goes straight through as "
        f"{who} without asking.",
        "  2. Your MCP client is still holding its copy of the credentials. "
        "Disconnect this server and reconnect to sign in as someone else.",
    ]
    return "\n".join(lines)
