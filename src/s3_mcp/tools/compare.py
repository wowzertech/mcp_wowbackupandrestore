"""compare_file_versions: what changed between two backups of one file."""
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

_MAX_SAMPLE_CHARS = 2000   # ceiling on any added/removed row dump
# Flattened-report as-of metadata: present on both sides but rolls forward each
# backup, so it isn't a content change — kept out of the differ.
#
# `running_balance` is deliberately NOT here. A backdated edit legitimately moves
# the running balance of every posting after it in that account, so the first
# such diff will show a handful of true added/removed postings plus a possibly
# large "changed: running_balance" fan-out downstream. That cascade is truthful —
# it is exactly the balance drift a bookkeeper wants after a backdated edit — so
# it stays visible. The expected shape of a general-ledger churn diff is:
# added/removed = the edited transaction's postings; changed = the downstream
# running_balances plus the affected summaries/openings. Anything OUTSIDE that
# shape (e.g. unrelated postings flipping) would indicate a real key regression.
_DIFF_IGNORE = {"period_start", "period_end"}


def _diff_snippet(before: object, after: object, width: int = 120) -> tuple[str, str]:
    """A window of each value centred on where they first differ, so the change
    is visible even when it sits deep inside a long nested value (truncating from
    the start would hide it). Returns (before_window, after_window)."""
    b, a = str(before), str(after)
    i = 0
    while i < min(len(b), len(a)) and b[i] == a[i]:
        i += 1
    start = max(0, i - width // 2)

    def window(s: str) -> str:
        seg = s[start:start + width]
        return ("…" if start else "") + seg + ("…" if start + width < len(s) else "")

    return window(b), window(a)


@mcp.tool(name="compare_file_versions")
def compare_backups(
    organisation: str,
    filename: str,
    from_date: str,
    to_date: str = "latest",
) -> str:
    """Compare the same file across two backups — what was added, removed and
    changed between the two dates.

    This is the question the data invites most: what happened to this company
    between one backup and the next.

    On a report file this is an account- or posting-level change journal. Note
    for the general ledger: a backdated edit legitimately shifts the running
    balance of every later posting in that account, so expect the edited
    transaction's postings as added/removed plus a downstream run of
    "changed: running_balance" — that cascade is real balance drift, not noise.

    Args:
        organisation: Organisation name.
        filename: File name as shown by `browse`, e.g. "invoices.jsonl".
        from_date: The earlier backup date.
        to_date: The later backup date, or "latest".
    """
    current_principal().require("backups:read")

    with _workspace() as ws:
        earlier = ws.load(organisation, filename, from_date, "before")
        later = ws.load(organisation, filename, to_date, "after")
        ws.seal()

        if earlier == later:
            return (
                f"{organisation} / {filename}\n"
                f"Both dates resolved to {earlier}, so there is nothing to compare."
            )

        before_cols = [name for name, _ in ws.columns("before")]
        after_cols = [name for name, _ in ws.columns("after")]
        before_n = ws.con.execute("SELECT count(*) FROM before").fetchone()[0]
        after_n = ws.con.execute("SELECT count(*) FROM after").fetchone()[0]

        lines = [
            f"{organisation} / {filename}",
            f"{earlier} -> {later}",
            "",
            f"rows: {before_n:,} -> {after_n:,} ({after_n - before_n:+,})",
        ]

        # A backwards range still runs, but say so: added/removed/changed read
        # from from_date to to_date, so a newer from_date reverses their sense.
        if earlier > later:
            lines.insert(2, "note: from_date is newer than to_date — the "
                            "direction is reversed from chronological order.")

        gone = [c for c in before_cols if c not in after_cols]
        new = [c for c in after_cols if c not in before_cols]
        if gone or new:
            lines.append(
                f"columns: {len(before_cols)} -> {len(after_cols)}"
                + (f", removed {', '.join(gone)}" if gone else "")
                + (f", added {', '.join(new)}" if new else "")
            )

        key = _identity_column(ws, before_cols, after_cols)
        matched_by = "id"
        # No entity id — try a label column (account name/code) so a per-row
        # report like trial_balance still diffs by value. Only for a genuinely
        # multi-row file: a one- or two-row file is a single nested report object,
        # and "matching" it dumps the whole structure as one added + one removed.
        if key is None and min(before_n, after_n) >= 4:
            key = _natural_key(ws, before_cols, after_cols)
            matched_by = "label"
        if key is None:
            lines += [
                "",
                "No per-row identifier common to both backups, so rows can't be "
                "matched one to one — the counts above are the whole story. Use "
                "aggregate_file on each date to compare specific figures.",
            ]
            return "\n".join(lines)

        # A flattened report's as-of columns (period_start/period_end) roll
        # forward each backup as metadata, not as a change — exclude them from the
        # change comparison so they don't flag every row as "changed".
        shared = [c for c in before_cols
                  if c in after_cols and c != key and c not in _DIFF_IGNORE]
        k = _quote(key)
        added = ws.con.execute(
            f"SELECT count(*) FROM after WHERE {k} NOT IN (SELECT {k} FROM before)"
        ).fetchone()[0]
        removed = ws.con.execute(
            f"SELECT count(*) FROM before WHERE {k} NOT IN (SELECT {k} FROM after)"
        ).fetchone()[0]

        differs = " OR ".join(
            f"CAST(a.{_quote(c)} AS VARCHAR) IS DISTINCT FROM CAST(b.{_quote(c)} AS VARCHAR)"
            for c in shared
        ) or "FALSE"
        changed = ws.con.execute(
            f"SELECT count(*) FROM after a JOIN before b ON a.{k} = b.{k} "
            f"WHERE {differs}"
        ).fetchone()[0]

        matched_note = "" if matched_by == "id" else \
            "  (no id column — matched on this label, report-style)"
        lines += [
            f"matched on {key}{matched_note}",
            "",
            f"added:   {added:,}",
            f"removed: {removed:,}",
            f"changed: {changed:,}",
        ]

        # "310 changed" on its own is not an answer — it could be one timestamp
        # column rewritten on every row, or it could be every figure in the
        # ledger. Counting per column says which.
        if changed and shared:
            per_column = ", ".join(
                f"sum(CASE WHEN CAST(a.{_quote(c)} AS VARCHAR) IS DISTINCT FROM "
                f"CAST(b.{_quote(c)} AS VARCHAR) THEN 1 ELSE 0 END) AS {_quote(c)}"
                for c in shared
            )
            row = ws.con.execute(
                f"SELECT {per_column} FROM after a JOIN before b ON a.{k} = b.{k}"
            )
            names = [d[0] for d in row.description]
            counts = sorted(
                ((n, v) for n, v in zip(names, row.fetchone()) if v),
                key=lambda pair: pair[1],
                reverse=True,
            )
            if counts:
                lines += ["", "columns that differ:"]
                lines += [f"  {name:32} {value:,}" for name, value in counts[:12]]
                if len(counts) > 12:
                    lines.append(f"  ... and {len(counts) - 12} more")

        def sample_rows(table: str, other: str, heading: str) -> None:
            if matched_by == "label":
                # Report rows are large nested structs — list the keys that were
                # added/removed rather than dumping whole structures (that was the
                # 210 KB blow-up). Value changes show under "changed" below.
                keys = [r[0] for r in ws.con.execute(
                    f"SELECT {k} FROM {table} WHERE {k} NOT IN "
                    f"(SELECT {k} FROM {other}) LIMIT 25"
                ).fetchall()]
                if keys:
                    shown = ", ".join(str(x) for x in keys[:25])
                    lines.extend(["", heading,
                                  f"  {shown}" + ("  …" if len(keys) > 25 else "")])
                return
            rows = ws.con.execute(
                f"SELECT * FROM {table} WHERE {k} NOT IN (SELECT {k} FROM {other}) "
                f"LIMIT 3"
            )
            names = [d[0] for d in rows.description]
            # Omit empty/null fields — a flattened posting has a dozen blank
            # columns (txn_id:"", doc_num:"", …) that are pure noise in an example.
            records = [{n: v for n, v in zip(names, row) if v not in (None, "")}
                       for row in rows.fetchall()]
            if records:
                dump = json.dumps(records, indent=2, default=str)
                if len(dump) > _MAX_SAMPLE_CHARS:
                    dump = dump[:_MAX_SAMPLE_CHARS] + "\n  … [truncated]"
                lines.extend(["", heading, dump])

        if added:
            sample_rows("after", "before", "examples of added rows:")
        if removed:
            sample_rows("before", "after", "examples of removed rows:")

        # For changed rows, the values themselves are the answer: pull a few and
        # show only the columns that moved, before -> after, so "changed: 12"
        # becomes "Status DRAFT -> AUTHORISED, Total 100 -> 120".
        if changed and shared:
            pairs = []
            for c in shared:
                pairs.append(f'b.{_quote(c)} AS {_quote("before::" + c)}')
                pairs.append(f'a.{_quote(c)} AS {_quote("after::" + c)}')
            detail = ws.con.execute(
                f"SELECT a.{k} AS {_quote('__key')}, {', '.join(pairs)} "
                f"FROM after a JOIN before b ON a.{k} = b.{k} WHERE {differs} LIMIT 3"
            )
            names = [d[0] for d in detail.description]

            example_lines: list[str] = []
            for row in detail.fetchall():
                rec = dict(zip(names, row))
                deltas = []
                for c in shared:
                    bv, av = rec["before::" + c], rec["after::" + c]
                    if bv != av:
                        # Window each value on the difference so a change buried
                        # deep in a long value is still visible.
                        bs, as_ = _diff_snippet(bv, av)
                        deltas.append(f"    {c}: {bs} -> {as_}")
                if deltas:
                    example_lines.append(f"  {key}={rec['__key']!s}")
                    example_lines.extend(deltas)
            if example_lines:
                lines += ["", "examples of changed rows (before -> after):"]
                lines += example_lines

        return "\n".join(lines)


def _identity_column(
    ws: "_workspace", before_cols: list[str], after_cols: list[str]
) -> str | None:
    """Pick the column that identifies a row across two backups.

    Xero entities carry their own ID — InvoiceID, ContactID, AccountID — so
    prefer one of those, and only accept it if it is actually unique on both
    sides. Without a stable key, added/removed/changed cannot be told apart from
    reordering."""
    shared = [c for c in before_cols if c in after_cols]
    candidates = [c for c in shared if c.lower().endswith("id")]
    # A column named exactly like the file's own entity sorts first: InvoiceID
    # beats ContactID inside invoices.jsonl.
    candidates.sort(key=lambda c: (len(c), c.lower()))

    for column in candidates:
        q = _quote(column)
        try:
            ok = ws.con.execute(
                f"SELECT (SELECT count(DISTINCT {q}) = count(*) FROM before) "
                f"   AND (SELECT count(DISTINCT {q}) = count(*) FROM after)"
            ).fetchone()[0]
        except Exception:
            continue
        if ok:
            return column
    return None


_KEYISH = ("account", "code", "name", "description", "reportcode",
           "label", "title", "line", "ref")


def _natural_key(
    ws: "_workspace", before_cols: list[str], after_cols: list[str]
) -> str | None:
    """A stand-in key for files with no entity id — a report like trial_balance,
    keyed by account name or code — so its rows can still be matched and diffed by
    value rather than dismissed as unmatchable. Prefer a label-like column that is
    unique (and non-empty) on both sides."""
    shared = [c for c in before_cols
              if c in after_cols and not c.lower().endswith("id")]

    def score(column: str) -> tuple:
        lc = column.lower()
        return (0 if any(k in lc for k in _KEYISH) else 1, len(column), lc)

    for column in sorted(shared, key=score):
        q = _quote(column)
        try:
            ok = ws.con.execute(
                f"SELECT (SELECT count(*) > 0 AND count(DISTINCT {q}) = count(*) FROM before) "
                f"   AND (SELECT count(*) > 0 AND count(DISTINCT {q}) = count(*) FROM after)"
            ).fetchone()[0]
        except Exception:
            continue
        if ok:
            return column
    return None
