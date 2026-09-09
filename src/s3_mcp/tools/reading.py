"""preview_file: schema plus a projected, filtered page of rows."""
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
# Reading
# --------------------------------------------------------------------------- #

def _page_status(off: int, shown: int, total: int, page: int,
                 filtered: bool = False) -> str:
    """One-line status under a preview page: which rows it covers, or a clear
    bounds message when the offset lands past the end (rather than a nonsensical
    'rows 200–200 of 87'). Offsets are 0-based, matching the offset argument."""
    if shown == 0:
        if total == 0:
            # A filtered zero isn't an empty file — say which it is.
            return "no rows match the filter" if filtered else "(this file has no rows)"
        return (f"offset {off} is past the end — this file has {total:,} rows "
                f"(offsets 0–{total - 1}). Call again with a smaller offset.")
    upto = off + shown
    line = f"rows {off}–{upto - 1} of {total:,}"
    if upto < total:
        line += f"  ·  more: call again with offset={off + page}"
    return line


@mcp.tool()
def preview_file(
    organisation: str, filename: str, backup_date: str = "latest",
    rows: int = 10, offset: int = 0, columns: str = "", where: str = "",
    raw: bool = False,
) -> str:
    """Inspect a backup file: its schema, and a page of rows.

    Shows the columns and types and the total row count, then up to `rows`
    records starting at `offset`. Set rows=0 for the schema alone, pulling no
    data — handy for a very large file. When more rows remain, the header gives
    the exact call to fetch the next page; some files hold one record per period
    (the general ledger is a month per row), so the row you want can sit past
    the first page.

    Wide, deeply nested files are expensive to pull whole. Name the fields you
    want in `columns` — dotted paths and all — to see just those, and narrow with
    `where` to the rows that matter, so a look at three fields costs three fields.

    For a period profit & loss or balance sheet, read the statement report
    (profit_and_loss.jsonl / balance_sheet.jsonl) rather than the general_ledger:
    the statements are captured with the rest of the backup, while the ledger can
    lag (browse flags a mixed-date backup).

    Args:
        organisation: Organisation name.
        filename: File name as shown by `browse`.
        backup_date: Backup date, or "latest".
        rows: How many records to show; 0 for the full schema (names + types)
            with no data. Capped by the server. When showing rows, the header
            gives the column count and the rows' own keys name every field.
        offset: Records to skip first. offset=100 shows the second hundred; page
            through a long file by raising it. Defaults to 0 (the start).
        columns: Comma-separated fields to return instead of the whole row —
            dotted paths into nested data are fine, e.g.
            "Contact.Name, Total, Status". Omit for every column; call rows=0 to
            see the full list of fields you can ask for.
        where: Optional row filter, same syntax as aggregate_file — e.g.
            "Status=AUTHORISED AND Total>1000", combine with AND/OR/NOT and
            parentheses. Filters and paginates over the matching rows.
        raw: For report files (which are normally flattened into rows), read the
            file exactly as stored instead — a debug view of its nested shape.
    """
    current_principal().require("backups:read")
    n = min(max(int(rows), 0), get_settings().max_preview_rows)
    off = max(int(offset), 0)

    ds = _dataset(organisation, filename, backup_date, raw=raw)
    with ds as (con, date):
        schema = con.execute("DESCRIBE SELECT * FROM data").fetchall()
        cols_types = [(name, dtype) for name, dtype, *_ in schema]

        unnest: set = set()
        select = "*"
        if columns.strip():
            paths = [p.strip() for p in columns.split(",") if p.strip()]
            picked = []
            for path in paths:
                expr, ucol = _resolve_ref(path, cols_types)
                if ucol:
                    unnest.add(ucol)
                picked.append(f"{expr} AS {_quote(path)}")
            select = ", ".join(picked)

        where_sql, params, where_unnest = _build_where(where, cols_types)
        unnest |= where_unnest
        if len(unnest) > 1:
            raise ValueError(
                "only one list field can be expanded per query; got "
                + ", ".join(sorted(unnest))
            )
        source = "data"
        if unnest:
            source = f'(SELECT unnest({_quote(next(iter(unnest)))}) AS _u, * FROM data)'

        total = con.execute(
            f"SELECT count(*) FROM {source}{where_sql}", params
        ).fetchone()[0]
        records: list = []
        if n:
            result = con.execute(
                f"SELECT {select} FROM {source}{where_sql} LIMIT {n} OFFSET {off}",
                params,
            )
            out_cols = [d[0] for d in result.description]
            records = [dict(zip(out_cols, r)) for r in result.fetchall()]

    header = f"{organisation} / {date} / {filename}\n"
    header += f"{total:,} rows" + (f" matching {where!r}" if where.strip() else "")
    header += f", {len(schema)} columns"
    if ds.report_legend:
        # A report was flattened to a table; name the value columns against their
        # original labels (e.g. c_31_dec_2026 = "31 Dec 2026") so filters are easy.
        header += "\nvalue columns: " + ", ".join(
            f"{slug}={label!r}" for slug, label in ds.report_legend.items())
    if columns.strip():
        # Projection asked for specific fields — don't reprint the whole schema
        # (that ate the token win). Name the fields, and how to see the rest.
        header += (f"\n\nshowing columns: {columns}"
                   "\n(call with rows=0 for the full schema)")
    elif not n:
        # Schema-only mode (rows=0): the full names and types. Since a rows>0
        # preview no longer prints the schema, this is now the ONLY place type
        # information is served — it must always stay complete, never truncated
        # or compacted, however large the schema.
        header += "\n\n" + "\n".join(
            f"  {name}: {dtype}" for name, dtype, *_ in schema)
    else:
        # Showing rows: their keys already name every column, so dumping the full
        # schema too is ~475 wasted tokens on the most common call. One line that
        # names the whole efficient path — schema, projection, and filter.
        header += ("\n\nrows=0 for schema (names + types) · "
                   "columns=\"a,b\" to project · where= to filter")
    if not n:
        return header

    status = _page_status(off, len(records), total, n, filtered=bool(where.strip()))
    if int(rows) > n:
        status += f"  ·  page size capped at {n} (asked {int(rows)})"
    if not records:
        return header + "\n\n" + status

    # Flat rows (a projection, or a flattened report) render as a compact table —
    # keys named once, not per row. Rows with nested values keep JSON, which the
    # table can't lay out.
    flat = all(not isinstance(v, (dict, list)) for r in records for v in r.values())
    if flat:
        body = _as_table(list(records[0].keys()),
                         [list(r.values()) for r in records])
    else:
        body = json.dumps(records, indent=2, default=str)
    return header + "\n\n" + status + "\n\n" + body
