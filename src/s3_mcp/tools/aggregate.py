"""aggregate_file and its portfolio sweep, reconciliation, and measures."""
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



def _parse_measures(measures: str) -> list[tuple[str, str]]:
    """Parse a `measures` string into (operation, column) pairs. Each spec is
    `op:column` (sum:Total) or a bare `count`, comma-separated:
    "sum:Total, avg:Total, count" — several figures in one call."""
    specs: list[tuple[str, str]] = []
    for part in measures.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            op, col = part.split(":", 1)
            specs.append((op.strip(), col.strip()))
        else:
            specs.append((part, ""))
    return specs


@mcp.tool()
def aggregate_file(
    organisation: str = "",
    filename: str = "",
    operation: str = "count",
    measure: str = "",
    measures: str = "",
    group_by: str = "",
    where: str = "",
    join_file: str = "",
    join_key: str = "",
    join_on: str = "",
    join: str = "matched",
    organisations: str = "",
    backup_date: str = "latest",
    limit: int = 50,
) -> str:
    """Summarise a whole backup file — totals, counts, averages, breakdowns.

    Use this instead of preview_file whenever the question is about the data as
    a whole rather than a few example rows. It reads every row, so the answer is
    complete; preview_file only ever shows the first handful.

    Examples:
      invoices by status        operation="count", group_by="Status"
      total invoiced by month   operation="sum", measure="Total", group_by="Type"
      largest single invoice    operation="max", measure="Total"
      paid invoices over 1000   operation="count", where="Status=PAID AND Total>1000"
      purchases since 1 Nov      operation="sum", measure="TotalAmt", where="TxnDate>=2025-11-01"
      spend by expense category operation="sum", measure="Line.Amount",
                                group_by="Line.AccountBasedExpenseLineDetail.AccountRef.name"
      revenue by month (trend)  operation="sum", measure="Total", group_by="Date:month"
      summary stats in one call measures="sum:Total, avg:Total, count"
      one total across all orgs operation="sum", measure="Total", organisations="*"
      revenue by month per org  measure="Total", group_by="Date:month", organisations="*"
      payments with no invoice  filename="payments.jsonl", join_file="invoices.jsonl",
                                join_key="Invoice.InvoiceID", join_on="InvoiceID", join="unmatched"
      report figure by section  filename="balance_sheet.jsonl", operation="sum",
                                measure="<value column>", group_by="section", where="row_type=account"
      ledger postings in a period filename="general_ledger.jsonl", operation="sum",
                                measure="amount", where="row_type=posting AND account_path~Sales"

    Column names may be dotted paths into nested data. A list field — a
    QuickBooks transaction's Line[] — is expanded so its per-line account and
    amount can be grouped and summed, which is how a by-category profit & loss
    is built from transaction records. Preview a file first to see its shape.

    Report files (trial_balance, balance_sheet, profit_and_loss, cash_flow,
    general_ledger) arrive as flat rows — statements carry section, account,
    account_id, row_type (account|summary) and one column per report figure;
    the ledger carries a row per posting with row_type=posting. Filter
    row_type=account (or =posting) so section totals aren't double-counted.

    For a stated total the report already gives — Total Income, Gross Profit,
    Net Profit — read the report's own summary row (where="row_type=summary AND
    account~Total Income") rather than re-summing account rows: it is the exact
    figure the platform reports, and it avoids an over-broad section filter (a
    contains match like section~Income also catches "Plus Other Income", silently
    combining two sections). '~' means contains and is case-insensitive; '=' is an
    exact, case-sensitive match.

    For a period profit & loss or balance sheet, prefer the statement report
    (profit_and_loss.jsonl / balance_sheet.jsonl) over summing general_ledger:
    the statements are captured with the rest of the backup, whereas the ledger
    can lag (browse flags a mixed-date backup), so a figure derived from the
    ledger may understate a still-open month.

    Add a period suffix to a date in group_by for a trend — "Date:month" (also
    :year, :quarter, :week, :day), returned oldest-first.

    Reconcile two files with join_file: keep only the rows of `filename` whose
    join_key is present in (join="matched") — or absent from (join="unmatched")
    — join_file under join_on, then apply the usual summary. Both files are
    matched on the server, so a large one never enters the conversation. This
    answers "which payments have no invoice", "which invoices have nothing
    posted against them", and the like.

    Args:
        organisation: Organisation name — for a single company. Omit when using
            `organisations`.
        filename: File name as shown by `browse`, e.g. "invoices.jsonl".
        operation: count, count_distinct, sum, avg, min or max. Comma-separate
            for several at once against `measure` — "count,sum,avg".
        measure: Column (or dotted path) to aggregate. Required except for count.
        measures: Several figures in one call instead of operation/measure —
            comma-separated "op:column" specs (bare "count" needs no column),
            e.g. "sum:Total, avg:Total, count". Overrides operation/measure.
        group_by: Column or dotted path to break the answer down by; add a
            ":month"/":quarter"/":year" suffix to bucket a date. Comma-separate
            for more than one; omit for a single total.
        where: Optional filter, e.g. "Status=PAID AND Total>1000". Use ~ for
            contains ("Name~acme"); >, <, >=, <= compare numbers and dates
            ("TxnDate>=2025-11-01"); quote a value to keep spaces or force an
            exact match ("Account=\"Rent\""). Combine conditions with AND, OR
            and NOT, grouped with parentheses —
            "(Status=PAID OR Status=AUTHORISED) AND Total>1000".
        join_file: A second file to reconcile against — match rows of `filename`
            by key, then summarise the ones that do (or don't) match.
        join_key: Column or dotted path in `filename` to match on.
        join_on: The matching key in join_file (defaults to join_key).
        join: "matched" (rows with a match) or "unmatched" (rows without).
        organisations: Compare across several organisations instead of one — a
            comma-separated list, or "*" for every organisation on the account.
            Ranks one figure per organisation; add group_by (or measures) to get
            each organisation's breakdown instead (e.g. revenue by month per org).
        backup_date: Backup date, or "latest".
        limit: Maximum groups to return.
    """
    current_principal().require("backups:read")
    if not filename.strip():
        raise ValueError('filename is required, e.g. "invoices.jsonl"')
    if join_file.strip() and organisations.strip():
        raise ValueError("join_file reconciles within one organisation; drop `organisations`")
    specs = _parse_measures(measures)
    # A comma in `operation` ("count,sum,avg") is the natural way to ask for
    # several figures — expand it into specs against `measure` when no explicit
    # `measures` was given.
    if not specs and "," in operation:
        specs = [(op.strip(), "" if op.strip().lower() == "count" else measure)
                 for op in operation.split(",") if op.strip()]
    if join_file.strip() and specs:
        raise ValueError("measures isn't supported with join_file; use operation/measure")
    # Accept a portfolio request in the singular organisation field too — "*",
    # "all", or a comma-separated list — not only in the plural organisations.
    org_single = organisation.strip()
    to_sweep = org_single.lower() in ("*", "all") or "," in org_single
    sweep_spec = organisations.strip() or (org_single if to_sweep else "")
    if sweep_spec:
        return _sweep(filename, operation, measure, group_by, where,
                      sweep_spec, backup_date, specs or None, limit)
    if not organisation.strip():
        raise ValueError(
            "give an organisation, or name several in `organisations` (or \"*\") "
            "to compare across the portfolio"
        )
    if join_file.strip():
        return _aggregate_join(
            organisation, filename, join_file, join_key, join_on, join,
            operation, measure, group_by, where, backup_date, limit,
        )
    n = min(max(limit, 1), get_settings().max_result_rows)

    with _dataset(organisation, filename, backup_date) as (con, date):
        columns, rows = _aggregate(
            con, "data", operation, measure, group_by, where, n, specs or None)
        diag = _explain_empty(con, "data", where) if (not rows and where.strip()) else ""

    if not rows:
        msg = (
            f"{organisation} / {date} / {filename}\n"
            f"No rows matched" + (f" the filter {where!r}." if where else ".")
        )
        return msg + (f"\n\n{diag}" if diag else "")

    header = f"{organisation} / {date} / {filename}"
    if where:
        header += f"\nfiltered: {where}"
    body = _as_table(columns, rows)
    if len(rows) == n:
        body += f"\n\n[showing the top {n} — raise limit or narrow with where]"
    return f"{header}\n\n{body}"


def _resolve_filename(requested: str, present: set[str]) -> str | None:
    """Pick the file in `present` that answers `requested`, tolerating the
    Xero/QuickBooks naming split — Xero writes plural (invoices.jsonl),
    QuickBooks singular (invoice.jsonl) — so one filename can sweep a mixed
    portfolio. An exact name wins; otherwise match on the base name with a
    trailing 's' normalised off, within the same extension. Returns None when
    nothing matches, so the caller can skip that organisation and say why rather
    than translate by more than an 's' it can't be sure of."""
    if requested in present:
        return requested

    def norm(fn: str) -> str:
        base = fn.rsplit(".", 1)[0].lower()
        return base[:-1] if base.endswith("s") else base

    ext = requested.rsplit(".", 1)[-1]
    target = norm(requested)
    matches = [fn for fn in present
               if fn.rsplit(".", 1)[-1] == ext and norm(fn) == target]
    return matches[0] if len(matches) == 1 else None


def _sweep(
    filename: str,
    operation: str,
    measure: str,
    group_by: str,
    where: str,
    organisations: str,
    backup_date: str,
    measures: list[tuple[str, str]] | None = None,
    limit: int = 50,
) -> str:
    """Run the same summary across several organisations. With no group_by it
    ranks one figure per organisation; with group_by (e.g. Date:month) it returns
    each organisation's breakdown, labelled per organisation — so "revenue by
    month per org" is one call. Reads the newest backup of each, or the named
    date, and resolves each organisation's platform filename automatically."""
    settings = get_settings()
    store = _store()

    available = store.organisations()
    spec = organisations.strip()
    if spec and spec not in ("*", "all"):
        wanted = [name.strip() for name in spec.split(",") if name.strip()]
        unknown = [n for n in wanted if n not in available]
        if unknown:
            raise PermissionError(
                "not on this account: " + ", ".join(unknown)
            )
    else:
        wanted = available

    capped = wanted[: settings.max_organisations_per_sweep]
    per_org = min(max(limit, 1), get_settings().max_result_rows)
    grouped = bool(group_by.strip()) or bool(measures)

    def summarise(name: str):
        """One organisation's result (columns + rows). Runs on a worker thread."""
        try:
            store = _store()
            date = store.resolve_date(name, backup_date)
            present = {f.name for f in store.files(name, date)}
            actual = _resolve_filename(filename, present)
            if actual is None:
                return name, None, None, None, f"no {filename} in this backup"
            with _dataset(name, actual, date) as (con, resolved):
                columns, rows = _aggregate(
                    con, "data", operation, measure, group_by, where, per_org, measures
                )
            return name, resolved, columns, rows, None
        except Exception as exc:
            return name, None, None, None, _short_error(exc)

    # Concurrently, because each organisation means an S3 download and these do
    # not depend on each other. Sequentially a portfolio sweep takes minutes,
    # which is longer than an API gateway will wait for a response.
    #
    # copy_context() per task carries the authenticated principal onto the worker
    # thread — it lives in a ContextVar, which threads do not inherit on their
    # own, and without it every worker would fail as unauthenticated.
    collected = []
    workers = max(1, min(settings.sweep_concurrency, len(capped) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, summarise, name)
            for name in capped
        ]
        for future in futures:
            collected.append(future.result())

    if measures:
        figures = ", ".join(o if o.strip().lower() == "count" else f"{o}({m})"
                            for o, m in measures)
    else:
        figures = operation if operation == "count" else f"{operation}({measure})"

    lines = [
        f"{figures} of {filename} across {len(capped)} organisation(s)"
        + (f", by {group_by}" if group_by.strip() else "")
        + (f", filtered: {where}" if where else ""),
        "",
    ]
    skipped: list[str] = []

    if not grouped:
        # One figure per organisation, in listing order.
        rows_out: list[str] = []
        for name, date, _cols, rows, error in collected:
            if error:
                skipped.append((name, error))
            else:
                value = rows[0][0] if rows else None
                rows_out.append(f"  {name[:44]:46} {str(date):12} {_denoise(value)!s:>16}")
        lines.append(f"  {'organisation':46} {'backup':12} {figures:>16}")
        lines += rows_out or ["  (no organisation returned a value)"]
    else:
        # Each organisation's breakdown, labelled per organisation.
        any_rows = False
        for name, date, cols, rows, error in collected:
            if error:
                skipped.append((name, error))
                continue
            if not rows:
                continue
            any_rows = True
            lines.append(f"{name} ({date}):")
            for row in rows:
                lines.append("    " + " | ".join(
                    f"{c}={_denoise(v)}" for c, v in zip(cols, row)))
        if not any_rows:
            lines.append("  (no organisation returned rows)")

    if skipped:
        lines += ["", f"not included ({len(skipped)}):"] + _group_skips(skipped)
    if len(wanted) > len(capped):
        lines += [
            "",
            f"[covered {len(capped)} of {len(wanted)} organisations — name the "
            f"ones you want in `organisations` to look at the rest]",
        ]
    return "\n".join(lines)


def _aggregate_join(
    organisation: str, filename: str, join_file: str, join_key: str, join_on: str,
    join: str, operation: str, measure: str, group_by: str, where: str,
    backup_date: str, limit: int,
) -> str:
    """Reconcile `filename` against `join_file` by key, then aggregate the rows
    that match (or don't). Both files are read into one DuckDB and matched by a
    key-membership test — no column-merging join — so a large file never enters
    the conversation, and the ordinary summary then runs over the kept rows."""
    if not join_key.strip():
        raise ValueError("join_key is required when join_file is set")
    mode = join.strip().lower()
    if mode not in ("matched", "unmatched"):
        raise ValueError('join must be "matched" or "unmatched"')
    rkey = join_on.strip() or join_key.strip()
    negate = "NOT " if mode == "unmatched" else ""
    n = min(max(limit, 1), get_settings().max_result_rows)

    with _workspace() as ws:
        date = ws.load(organisation, filename, backup_date, "left_rows")
        ws.load(organisation, join_file, backup_date, "right_rows")
        ws.seal()
        lexpr, lu = _resolve_ref(join_key.strip(), ws.columns("left_rows"))
        rexpr, ru = _resolve_ref(rkey, ws.columns("right_rows"))
        if lu or ru:
            raise ValueError(
                "a join key can't be a list field — use a scalar column or a "
                "dotted path to one, e.g. join_key=\"Invoice.InvoiceID\""
            )
        ws.con.execute(
            f'CREATE VIEW data AS SELECT * FROM "left_rows" WHERE '
            f'CAST({lexpr} AS VARCHAR) {negate}IN '
            f'(SELECT CAST({rexpr} AS VARCHAR) FROM "right_rows" WHERE {rexpr} IS NOT NULL)'
        )
        columns, rows = _aggregate(ws.con, "data", operation, measure, group_by, where, n)

    relation = "with no match in" if negate else "matched to"
    head = (f"{organisation} / {date} / {filename} {relation} {join_file}"
            f"  (on {join_key.strip()} = {rkey})")
    if where:
        head += f"\nfiltered: {where}"
    if not rows:
        return head + "\n\nNo rows."
    body = _as_table(columns, rows)
    if len(rows) == n:
        body += f"\n\n[showing the top {n} — raise limit or narrow with where]"
    return f"{head}\n\n{body}"
