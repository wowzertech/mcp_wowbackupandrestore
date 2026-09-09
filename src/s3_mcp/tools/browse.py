"""Browse tool: organisations, backup dates, and files, with staleness."""
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
# Navigation
# --------------------------------------------------------------------------- #

def _backup_age_days(latest: str) -> int | None:
    """Days since a backup date (newest-first, so pass dates[0]); None if the
    string isn't an ISO date."""
    try:
        return (date.today() - date.fromisoformat(latest)).days
    except (ValueError, TypeError):
        return None


@mcp.tool(name="browse")
def browse(organisation: str = "", backup_date: str = "") -> str:
    """Browse your backups, one level at a time — the starting point.

      list()                      -> the organisations you have backups for
      list("Acme")                -> Acme's backup dates, newest first
      list("Acme", "latest")      -> the files in Acme's newest backup
      list("Acme", "2026-06-30")  -> the files in that dated backup

    Start with no arguments; pass an organisation to see its backups, then a
    date (or "latest") to see that backup's files.

    Args:
        organisation: Organisation name. Omit to list organisations.
        backup_date: A date from a prior listing, or "latest". Only used with an
            organisation; omit it to list that organisation's backup dates.
    """
    principal = current_principal()
    principal.require("backups:list")
    store = _store()

    # Level 1 — organisations.
    if not organisation.strip():
        orgs = store.organisations()
        if not orgs:
            return (
                "No organisations found for your account. If you expect data here, "
                "check that you signed in with the same email you use on the portal."
            )
        locs = principal.entitlement.org_locations or {}
        names = {"xero": "Xero", "qbo": "QuickBooks"}
        threshold = get_settings().stale_backup_days

        # Each organisation's newest backup age, looked up concurrently — one S3
        # list apiece — so a feed that has stopped is flagged here, on the list,
        # not only after drilling in. A fresh store per worker (boto3 clients are
        # not shared across threads); the principal rides along via copy_context.
        def newest(name: str) -> tuple[str, str | None, int | None]:
            try:
                dates = _store().backup_dates(name)
                latest = dates[0] if dates else None
                return name, latest, (_backup_age_days(latest) if latest else None)
            except Exception:
                return name, None, None

        newest_by: dict[str, tuple[str | None, int | None]] = {}
        workers = max(1, min(get_settings().sweep_concurrency, len(orgs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # copy_context() is evaluated here on the main thread, so each worker
            # inherits the authenticated principal (a ContextVar threads don't
            # share). Calling it inside the worker would capture an empty context.
            futures = {
                name: pool.submit(contextvars.copy_context().run, newest, name)
                for name in orgs
            }
        for name, future in futures.items():
            _n, latest, age = future.result()
            newest_by[name] = (latest, age)

        def line(name: str) -> str:
            src = names.get(getattr(locs.get(name), "source", ""), "")
            tag = f"  [{src}]" if src else ""
            latest, age = newest_by.get(name, (None, None))
            if age is None:
                note = ""
            elif age > threshold:
                note = f"  ⚠️ last backup {latest}"
            else:
                note = f"  (latest {latest})"
            return f"  {name}{tag}{note}"

        return (f"WOW AISuite MCP · {SERVER_VERSION}\n\n"
                + "Organisations:\n" + "\n".join(line(o) for o in orgs)
                + "\n\nPass an organisation to see its backups."
                + "\n⚠️ marks a feed with no backup in over "
                + f"{threshold} days — worth checking.")

    # Level 2 — backup dates for one organisation.
    if not backup_date.strip():
        dates = store.backup_dates(organisation)
        if not dates:
            return f"No backups found for {organisation}."
        age = _backup_age_days(dates[0])
        threshold = get_settings().stale_backup_days
        stale = ""
        if age is not None:
            flag = f"  ⚠️ stale — over {threshold} days old" if age > threshold else ""
            stale = f"\n\nnewest backup: {dates[0]}{flag}"
        return (
            f"{len(dates)} backup(s) for {organisation}, newest first:\n"
            + "\n".join(f"  {d}" for d in dates)
            + stale
            + '\n\nPass a date (or "latest") to see that backup\'s files.'
        )

    # Level 3 — files in one backup.
    date = store.resolve_date(organisation, backup_date)
    files = store.files(organisation, date)
    if not files:
        return f"Backup {date} for {organisation} is empty."

    # Row counts for readable files under a size guard, fetched concurrently — a
    # navigation call shouldn't become a full backup download, so oversized or
    # non-jsonl files show their byte size only.
    count_cap = get_settings().max_rowcount_bytes

    def row_count(f) -> tuple[str, int | None]:
        try:
            body = store.fetch(store.object_for(organisation, date, f.name))
        except Exception:
            return f.name, None
        if _report_kind(f.name):
            # A report is one nested object — count the rows it flattens to.
            try:
                for line in body.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        _legend, flat = flatten_report(json.loads(line))
                        return f.name, len(flat)
                return f.name, 0
            except Exception:
                return f.name, None
        if _is_general_ledger(f.name):
            # The ledger is monthly report-structs; count the postings it flattens
            # to (its queryable rows), not the handful of structs.
            try:
                objs = [json.loads(line) for line
                        in body.decode("utf-8", "replace").splitlines() if line.strip()]
                return f.name, len(flatten_general_ledger(objs))
            except Exception:
                return f.name, None
        rows = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
        return f.name, rows

    countable = [f for f in files
                 if f.name.endswith((".jsonl", ".ndjson")) and f.size_bytes <= count_cap]
    counts: dict[str, int | None] = {}
    if countable:
        workers = max(1, min(get_settings().sweep_concurrency, len(countable)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, row_count, f)
                       for f in countable]
            for future in futures:
                name, rc = future.result()
                counts[name] = rc

    # Each file's real capture time is its S3 last-modified date. A backup folder
    # is labelled with one date, but the capture engine can write files at
    # different times (the general ledger has been seen days behind the rest) — so
    # surface each file's date and flag the stragglers. Without this a report built
    # from more than one file silently mixes as-of dates and reads as stale.
    cap = {f.name: (f.last_modified or "")[:10] for f in files}
    distinct = {d for d in cap.values() if d}
    mixed = len(distinct) > 1
    newest = max(distinct, default="")

    def _days_before(d: str) -> int | None:
        if not d or not newest or d >= newest:
            return None
        try:
            return (date.fromisoformat(newest) - date.fromisoformat(d)).days
        except (ValueError, TypeError):
            return None

    head = f"{organisation} / {date} — {len(files)} file(s)"
    head += f", all captured {next(iter(distinct))}:" if len(distinct) == 1 else ":"
    lines = [head]
    stale: list[str] = []
    for f in files:
        rc = counts.get(f.name)
        rows = f", {rc:,} rows" if rc is not None else ""
        extra = ""
        if mixed:                                   # only clutter when it matters
            cd = cap.get(f.name) or "?"
            extra = f", captured {cd}"
            older = _days_before(cd)
            if older:
                extra += f"  ⚠️ {older}d older than the rest"
                stale.append(f.name)
        lines.append(f"  {f.name}  ({f.size_bytes:,} bytes{rows}{extra})")
    lines.append(
        "\nWhere a name appears as both .jsonl and .xlsx, they hold the same "
        "data; the .jsonl reads more reliably for analysis."
    )
    if stale:
        lines.append(
            f"\n⚠️ mixed capture dates — {', '.join(stale)} predate the rest of "
            f"this backup. A figure built from more than one file may combine "
            f"different as-of dates; check the per-file 'captured' dates above."
        )
    return "\n".join(lines)
