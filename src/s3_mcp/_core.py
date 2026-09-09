"""Tool surface.

Shape of the data: these are Xero backups. One organisation has many dated
backups; each backup holds the same set of entities (accounts, balance_sheet,
trial_balance, profit_and_loss, users, tax_rates, currencies …), several of them
written in two formats — .jsonl for structure and .xlsx for humans.

Design rules that hold across every handler:

* Pure request/response. Nothing cached against "the current user", nothing that
  assumes two calls land on the same instance. Stateless core, any instance
  answers any call.
* The email segment of the S3 path comes from the token. Never an argument.
* Results are compact text. A 20k-row trial balance in a tool result is tokens
  spent to make the answer worse — aggregate, or hand back a download link.
"""

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
from mcp.server import MCPServer
from mcp.types import Icon

from . import xero_live
from ._reports import (
    _report_kind, flatten_report, _is_general_ledger, flatten_general_ledger,
)
from .auth import current_principal, note_signed_out
from .entitlements import resolve_entitlement, revoke_auth0_session
from .settings import get_settings
from .storage import UserStore


def _server_icons() -> list[Icon]:
    """The brand swirl, so clients show it instead of a generated 'W'.

    A PNG on a brand-navy tile, not a bare SVG: the swirl itself is navy, so on
    a transparent background it vanishes against a dark client surface. The tile
    gives it a solid, intentional frame that reads the same in light and dark.

    A data URI rather than a URL: the icon needs no fetch and no CSP allowance,
    and renders even before the caller is authenticated. Loaded from the asset
    shipped in the package, or its source location when running from the repo."""
    for cand in (
        Path(__file__).resolve().parent / "branding_assets" / "wow-swirl.png",
        Path(__file__).resolve().parent.parent.parent / "branding" / "assets" / "wow-swirl.png",
    ):
        if cand.is_file():
            uri = "data:image/png;base64," + base64.b64encode(cand.read_bytes()).decode()
            return [Icon(src=uri, mimeType="image/png", sizes=["512x512"])]
    return []


# Semver, bumped on every deploy. It rides in the MCP handshake
# (serverInfo.version) and is stamped on `browse`, so an agent can tell when the
# server changed under it — behaviour used to shift mid-session with no
# client-visible signal. The tail lists the last few notable changes so "what
# moved" is answerable without a tool.
SERVER_VERSION = "v1.1.17"
SERVER_CHANGELOG = [
    "landing page links the public source repo on GitHub",
    "landing page adds ChatGPT and Gemini setup, with a visible client-picker scrollbar",
    "docs: read a report's own summary row for stated totals, not a re-sum (avoids over-broad section filters)",
    "get_live_data no longer over-promises: docs say live is optional and may be off",
    "tool docs steer period P&L/balance-sheet to the statement reports over the ledger",
    "browse shows each file's capture date and flags mixed-date backups",
    "brand icon regenerated at 512x512 (handshake + .mcpb, the recommended size)",
    "preview: flat/projected pages render as a compact table (half the tokens)",
    "browse: general_ledger shows its flattened row count, not the struct count",
    "preview hint names the whole efficient path: rows=0 / columns= / where=",
    "preview: full schema is opt-in (rows=0), not dumped on every call",
    "aggregate returns a compact table; diff examples drop empty fields",
    "sweep skips group by reason even when a per-org tail differs",
    "general_ledger diff: as-of date no longer false-flags rows as removed+added",
    "general_ledger: cumulative open-month slices deduped to the latest snapshot",
    "general_ledger: columns matched by QBO ColKey, so doc_num populates ('#')",
    "general_ledger: Split account (+id) and Adj columns added; keys persist across calls",
    "general_ledger: unique posting keys for multi-line journals (compare works)",
    "preview_file raw=true bypasses the flattener to inspect a report's raw shape",
    "general_ledger: real txn/account ids, stable posting keys, label rows fixed",
    "Xero general_ledger unions all journal parts, not just the first 5000",
    "preview: a filtered zero says 'no rows match', not 'file has no rows'",
    "report files (trial_balance/balance_sheet/P&L/cash_flow) flatten to rows",
    "compare on a report now gives account-level deltas (closes IMP-9)",
    "general_ledger flattens to postings; on Xero it reads journals",
    "sum/avg on a text column errors instead of a silent null",
    "cross-platform column aliases (Total/TotalAmt, Date/TxnDate) for org sweeps",
    "organisation names match case-insensitively with a clearer not-found error",
    "browse shows the backup date, not an ambiguous days-ago count",
    "operation= accepts several ops (count,sum,avg); organisation=* also sweeps",
    "report files no longer dump whole structs; diff output is size-capped",
    "preview hides the full schema once you project columns",
    "code split into _core + tools/ modules (no behaviour change)",
    "boolean where (OR/NOT/parentheses); preview columns=/where=",
    "multi-measure and group-by-per-org aggregate; portfolio find_uncategorized",
    "compare shows before->after values; .xlsx reads its .jsonl twin",
    "browse flags stale feeds; cross-org resolves platform filenames",
]

mcp = MCPServer(
    "wow-aisuite",
    title="WOW AISuite",
    website_url="https://wowbackupandrestore.com",
    icons=_server_icons(),
    version=SERVER_VERSION,
)


@mcp.resource(
    "wow://changelog",
    name="Server changelog",
    description="Version and recent notable changes to the WOW AISuite MCP server.",
    mime_type="text/plain",
)
def _changelog() -> str:
    """Read to see what changed when the server version moves under you."""
    recent = "\n".join(f"  - {c}" for c in SERVER_CHANGELOG)
    return (f"WOW AISuite MCP server {SERVER_VERSION}\n\n"
            f"recent changes:\n{recent}")


def _store() -> UserStore:
    return UserStore(current_principal())


def _jsonl_sibling(filename: str) -> str:
    """The .jsonl counterpart of a spreadsheet backup name — invoices.xlsx ->
    invoices.jsonl. An .xlsx is a human-facing copy of the same-named .jsonl,
    which is the form the server reads."""
    return filename.rsplit(".", 1)[0] + ".jsonl"


def _denoise(value: object) -> object:
    """Strip binary float noise from a computed number for display — a summed
    33077.020000000004 becomes 33077.02 — and collapse a whole-number float to an
    int (5.0 -> 5). Rounds to four decimals, enough to keep cents on large sums
    without asserting the figure is money. Non-numbers pass straight through."""
    if isinstance(value, float):
        rounded = round(value, 4)
        return int(rounded) if rounded == int(rounded) else rounded
    return value


def _short_error(exc: Exception, width: int = 140) -> str:
    """A skip-message form of an exception: our ValueErrors are already written
    for a reader, so show the message alone; anything else keeps its type. Cut at
    a word boundary rather than mid-word so the line stays legible."""
    msg = str(exc) if isinstance(exc, (ValueError, PermissionError)) \
        else f"{type(exc).__name__}: {exc}"
    msg = " ".join(msg.split())
    if len(msg) <= width:
        return msg
    cut = msg[:width].rsplit(" ", 1)[0]
    return cut + " …"


def _group_skips(skipped: list[tuple[str, str]], max_reasons: int = 10) -> list[str]:
    """Collapse per-organisation skip reasons into one line each: a reason shared
    by several organisations (a missing file, say) is printed once with the names,
    not repeated verbatim per organisation. Group on the first sentence, so the
    same error carrying a per-organisation tail — e.g. a 'not a field' error whose
    column list differs by schema — still collapses to one line."""
    by_reason: dict[str, list[str]] = {}
    for name, reason in skipped:
        head = reason.split(". ")[0].strip().rstrip(".")   # first sentence, tail dropped
        by_reason.setdefault(head, []).append(name)
    out = []
    for reason, orgs in list(by_reason.items())[:max_reasons]:
        names = ", ".join(orgs[:8]) + (" …" if len(orgs) > 8 else "")
        out.append(f"  {names} — {reason}")
    if len(by_reason) > max_reasons:
        out.append(f"  ... and {len(by_reason) - max_reasons} more reasons")
    return out


def _as_table(columns: list[str], rows: list) -> str:
    """A compact aligned table — one header row, values named once — instead of
    pretty JSON that repeats every key on every row. Numbers are denoised for
    display; a nested value renders as compact JSON; a stray pipe is neutralised
    so it can't split a column."""
    def cell(v: object) -> str:
        if isinstance(v, (dict, list)):
            s = json.dumps(v, default=str, separators=(",", ":"))
        else:
            s = str(_denoise(v))
        return s.replace("|", "/")

    body = [[cell(v) for v in row] for row in rows]
    widths = [max(len(columns[i]), *(len(r[i]) for r in body)) if body else len(columns[i])
              for i in range(len(columns))]

    def line(cells: list) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    return "\n".join([line(columns)] + [line(r) for r in body])


# --------------------------------------------------------------------------- #
# Query building
#
# Callers name columns and operations; they never supply SQL. Column names are
# checked against the schema the file actually has, and values only ever reach
# DuckDB as bound parameters, so the text of a query is built entirely from
# things this module chose.
# --------------------------------------------------------------------------- #

_OPERATIONS = {
    "count": "count(*)",
    "count_distinct": "count(DISTINCT {col})",
    "sum": "sum(TRY_CAST({col} AS DOUBLE))",
    "avg": "avg(TRY_CAST({col} AS DOUBLE))",
    "min": "min({col})",
    "max": "max({col})",
}

_COMPARISONS = {
    "=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<=",
    "~": "ILIKE",          # contains
}

_CONDITION = re.compile(r"^\s*(.+?)\s*(>=|<=|!=|~|=|>|<)\s*(.*?)\s*$")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _resolve_column(name: str, columns: list[str], label: str) -> str:
    """Match a caller-supplied column name to a real one, case-insensitively."""
    wanted = name.strip()
    for column in columns:
        if column.lower() == wanted.lower():
            return column
    raise ValueError(
        f"{label} {name!r} is not a column in this file. Available columns: "
        + ", ".join(columns[:25])
        + (" ..." if len(columns) > 25 else "")
    )


_PATH_SEG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A date/datetime literal, so a ">" filter on a date column compares as a date
# rather than being forced to a number (which silently matched nothing).
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_NUMERIC_VALUE = re.compile(r"^-?\d+(\.\d+)?$")
# group_by="Date:month" buckets a date field into a period, for trends.
_PERIODS = {"day", "week", "month", "quarter", "year"}


def _as_date(expr: str) -> str:
    """A SQL expression that reads a value as a timestamp whether it is a normal
    date string or Xero's /Date(1699999999999+0000)/ epoch-millisecond form — so
    a date filter reaches the journal files, which store their dates that way."""
    digits = f"regexp_extract(CAST({expr} AS VARCHAR), '[0-9]{{10,13}}')"
    return (f"COALESCE(TRY_CAST({expr} AS TIMESTAMP), "
            f"epoch_ms(TRY_CAST({digits} AS BIGINT)))")


def _is_list_type(dtype: str) -> bool:
    """A DuckDB list/array column type — a JSON array read in — ends in '[]'."""
    return dtype.strip().endswith("[]")


# Fields that Xero and QuickBooks name differently but mean the same — so one
# portfolio query can span both platforms. Kept deliberately small and only for
# genuinely equivalent fields: Total/TotalAmt is a grand total on both, Date is
# the transaction date on both. Status is NOT aliased to QuickBooks' EmailStatus
# (delivery state, not the document's status) — that would sum apples to oranges.
_COLUMN_ALIASES = [
    {"total", "totalamt"},
    {"date", "txndate", "datestring"},
]


def _resolve_ref(path: str, cols_types: list[tuple[str, str]]) -> tuple[str, str | None]:
    """Resolve a column name or dotted path to a SQL expression.

    A plain name is a top-level column. A dotted path reaches into a nested
    value: struct fields join with '.', and a list column — a JSON array such
    as a QuickBooks transaction's Line[] — is expanded with UNNEST so its
    elements become rows and their fields can be grouped or summed. Returns
    (sql_expression, list_column_to_unnest_or_None). Every path segment past
    the first is checked against an identifier allowlist before it reaches SQL;
    the head is matched case-insensitively to a real column, then to a known
    cross-platform alias (Total/TotalAmt, Date/TxnDate)."""
    parts = path.split(".")
    head = parts[0].strip()
    col = ctype = None
    for name, dtype in cols_types:
        if name.lower() == head.lower():
            col, ctype = name, dtype
            break
    if col is None:
        # Cross-platform fallback: try the requested field's known equivalents
        # before giving up, so a Xero "Total" reaches QuickBooks' "TotalAmt".
        group = next((g for g in _COLUMN_ALIASES if head.lower() in g), None)
        if group:
            for name, dtype in cols_types:
                if name.lower() in group:
                    col, ctype = name, dtype
                    break
    if col is None:
        raise ValueError(
            f"{path!r} is not a field in this file. Top-level columns: "
            + ", ".join(n for n, _ in cols_types[:25])
            + (" ..." if len(cols_types) > 25 else "")
        )
    rest = [s.strip() for s in parts[1:]]
    for seg in rest:
        if not _PATH_SEG.match(seg):
            raise ValueError(f"invalid nested field {seg!r} in path {path!r}")
    if _is_list_type(ctype):
        base, unnest = "_u", col            # element alias; list to expand
    else:
        base, unnest = _quote(col), None
    return base + "".join(f".{seg}" for seg in rest), unnest


_WHERE_KEYWORD = re.compile(r"(AND|OR|NOT)", re.IGNORECASE)


def _tokenize_where(where: str) -> list[tuple[str, str]]:
    """Split a filter into structural tokens — '(' ')' AND OR NOT — and the raw
    `col OP value` chunks between them. Quotes hide parentheses and keywords, so
    a value that literally contains one is kept by quoting it: Name~"A (Pty)".
    A keyword only counts when bordered by whitespace, a paren, or the ends, so a
    column such as ORDERS or NOTES is never mistaken for OR / NOT."""
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(where)

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            tokens.append(("cond", text))

    while i < n:
        c = where[i]
        if c in "\"'":                          # copy a quoted span verbatim
            buf.append(c); i += 1
            while i < n and where[i] != c:
                buf.append(where[i]); i += 1
            if i < n:
                buf.append(where[i]); i += 1     # closing quote
            continue
        if c in "()":
            flush()
            tokens.append(("lp" if c == "(" else "rp", c))
            i += 1
            continue
        if not buf or buf[-1].isspace():
            m = _WHERE_KEYWORD.match(where, i)
            if m:
                j = i + len(m.group(0))
                if j >= n or where[j].isspace() or where[j] in "()":
                    flush()
                    kw = m.group(0).upper()
                    tokens.append((kw.lower(), kw))
                    i = j
                    continue
        buf.append(c); i += 1
    flush()
    return tokens


def _compile_condition(raw: str, cols_types: list[tuple[str, str]],
                       params: list, unnest: set) -> str:
    """Compile one `col OP value` leaf to a SQL boolean expression, appending its
    bound parameters and any list column it needs expanded. The column is always
    validated against the real schema and the value is always bound as a
    parameter — never interpolated — so a filter cannot inject SQL."""
    match = _CONDITION.match(raw)
    if not match:
        raise ValueError(
            f"could not read the condition {raw!r}. Use forms like Status=PAID, "
            f"Total>1000, Name~acme (~ means contains), combined with AND / OR / "
            f"NOT and parentheses."
        )
    name, operator, value = match.groups()
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]                       # unwrap a quoted value

    expr, ucol = _resolve_ref(name.strip(), cols_types)
    if ucol:
        unnest.add(ucol)
    sql_op = _COMPARISONS[operator]

    if operator == "~":
        params.append(f"%{value}%")
        return f"CAST({expr} AS VARCHAR) ILIKE ?"
    if _DATE_VALUE.match(value):
        # Any operator against a date value compares as a date, parsing Xero's
        # /Date(ms)/ strings too — so a period filter reaches the journals,
        # which is where the complete accrual ledger lives.
        params.append(value)
        return f"{_as_date(expr)} {sql_op} TRY_CAST(? AS TIMESTAMP)"
    if operator in {">", "<", ">=", "<="}:
        if not _NUMERIC_VALUE.match(value):
            # A magnitude comparison needs a number or date; a stray word here
            # would TRY_CAST to NULL and match nothing. Fail loud.
            raise ValueError(
                f"'{operator}' needs a number or date, but the value in {raw!r} "
                f"reads as {value!r}. Use '=' with a quoted value to match text."
            )
        params.append(value)
        return f"TRY_CAST({expr} AS DOUBLE) {sql_op} TRY_CAST(? AS DOUBLE)"
    if _NUMERIC_VALUE.match(value):
        # '=' / '!=' on a number: match whether the column reads as text or a
        # number, so Amount=100 finds a value stored as 100.0 (text '=' alone
        # compared "100.0" against "100" and silently missed it).
        params.append(value); params.append(value)
        if operator == "=":
            return (f"(CAST({expr} AS VARCHAR) = ? "
                    f"OR TRY_CAST({expr} AS DOUBLE) = TRY_CAST(? AS DOUBLE))")
        return (f"(CAST({expr} AS VARCHAR) != ? "
                f"AND TRY_CAST({expr} AS DOUBLE) IS DISTINCT FROM TRY_CAST(? AS DOUBLE))")
    params.append(value)
    return f"CAST({expr} AS VARCHAR) {sql_op} ?"


def _build_where(where: str, cols_types: list[tuple[str, str]]) -> tuple[str, list, set]:
    """Turn a filter like `Status=PAID AND (Total>1000 OR Balance>0)` into SQL,
    bound params, and any list columns the filter needs expanded.

    Conditions combine with AND, OR and NOT and group with parentheses, at SQL's
    own precedence (NOT before AND before OR). Each leaf is `col OP value` where
    OP is = != > < >= <= or ~ (contains). Values may be quoted to keep spaces or
    force an exact string; > < >= <= compare numerically unless the value is a
    date (YYYY-MM-DD, optionally with a time), where both sides compare as
    timestamps — without that a date filter cast to a number and matched nothing.
    Column names may be dotted paths into nested/list fields, as in group_by.
    Columns are validated and values are bound, so the filter is injection-safe."""
    if not where.strip():
        return "", [], set()

    tokens = _tokenize_where(where)
    if not tokens:
        return "", [], set()
    params: list = []
    unnest: set = set()
    pos = 0

    def peek():
        return tokens[pos][0] if pos < len(tokens) else None

    def parse_or():
        nonlocal pos
        node = parse_and()
        while peek() == "or":
            pos += 1
            node = f"({node} OR {parse_and()})"
        return node

    def parse_and():
        nonlocal pos
        node = parse_not()
        while peek() == "and":
            pos += 1
            node = f"({node} AND {parse_not()})"
        return node

    def parse_not():
        nonlocal pos
        if peek() == "not":
            pos += 1
            return f"(NOT {parse_not()})"
        return parse_atom()

    def parse_atom():
        nonlocal pos
        t = peek()
        if t == "lp":
            pos += 1
            inner = parse_or()
            if peek() != "rp":
                raise ValueError("unbalanced parentheses in the filter.")
            pos += 1
            return inner
        if t == "cond":
            raw = tokens[pos][1]
            pos += 1
            return _compile_condition(raw, cols_types, params, unnest)
        found = tokens[pos][1] if pos < len(tokens) else "the end of the filter"
        raise ValueError(
            f"expected a condition but found {found!r}. Combine conditions with "
            f"AND / OR / NOT and group with parentheses, e.g. (A=1 OR B=2) AND C>3."
        )

    sql = parse_or()
    if pos != len(tokens):
        raise ValueError(
            f"could not parse the whole filter — unexpected {tokens[pos][1]!r}. "
            f"Check the parentheses and that AND / OR / NOT sit between conditions."
        )
    return " WHERE " + sql, params, unnest


def _leaf_conditions(where: str) -> list[str]:
    """The `col OP value` leaves of a filter, ignoring AND/OR/NOT and parens —
    used to diagnose an empty result across a boolean filter, not just an AND."""
    return [text for kind, text in _tokenize_where(where) if kind == "cond"]


def _explain_empty(con: "duckdb.DuckDBPyConnection", table: str, where: str) -> str:
    """Say why a filtered query matched nothing — the review's top ask.

    A zero-row result is dangerous when it looks the same whether the filter
    genuinely matched nothing or could not apply at all (the column holds a
    different type or format than the value compared against). For each typed
    condition, check whether the column actually holds values readable as the
    kind being compared; if not, name it. Otherwise say the zero is genuine and
    point at the usual real cause — data split across chunked files."""
    try:
        cols_types = [(n, t) for n, t, *_ in
                      con.execute(f'DESCRIBE SELECT * FROM "{table}"').fetchall()]
    except Exception:
        return ""

    mismatches: list[str] = []
    for raw in _leaf_conditions(where):
        m = _CONDITION.match(raw)
        if not m:
            continue
        name, op, value = m.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        try:
            expr, ucol = _resolve_ref(name.strip(), cols_types)
        except ValueError:
            continue

        is_date = bool(_DATE_VALUE.match(value))
        is_num = bool(_NUMERIC_VALUE.match(value))
        # Only a typed comparison can silently fail on a type/format mismatch;
        # a text '=' or '~' that misses is a genuine miss.
        if op == "~" or not (is_date or (is_num and op in _COMPARISONS) or op in {">", "<", ">=", "<="}):
            continue
        kind = "dates" if is_date else "numbers"
        cast = _as_date(expr) if is_date else f"TRY_CAST({expr} AS DOUBLE)"
        src = (f'(SELECT unnest({_quote(ucol)}) AS _u, * FROM "{table}")'
               if ucol else f'"{table}"')
        try:
            nn, ok = con.execute(
                f"SELECT count(*) FILTER (WHERE {expr} IS NOT NULL), "
                f"count(*) FILTER (WHERE {cast} IS NOT NULL) FROM {src}"
            ).fetchone()
        except Exception:
            continue
        if nn and not ok:
            mismatches.append(
                f"'{name.strip()}' holds {nn:,} values but none read as {kind}, so the "
                f"'{op}' filter cannot match in this file (wrong type or format — e.g. a "
                f"Xero /Date(...)/ date, or an id kept as text)."
            )

    if mismatches:
        return "Why zero — " + " ".join(mismatches)
    return ("Why zero — the filter is valid for these columns and simply matched nothing in "
            "this file. If you expected matches, the rows may be genuinely absent, or in another "
            "file: some entities are split across chunks (journals, journals_1, journals_2 …).")


def _aggregate(
    con: duckdb.DuckDBPyConnection,
    table: str,
    operation: str,
    measure: str,
    group_by: str,
    where: str,
    limit: int,
    measures: list[tuple[str, str]] | None = None,
) -> tuple[list[str], list[tuple]]:
    """Run one grouped aggregation and hand back columns and rows.

    measure, group_by parts and filter columns may be dotted paths. When any of
    them reaches into a list column (a QuickBooks Line[] array), that column is
    UNNESTed once so line-level fields — the account each line is coded to, and
    its amount — become groupable. This is what lets a by-category total be
    built from transaction records, not only a total by bank account or payee."""
    cols_types = [(n, t) for n, t, *_ in
                  con.execute(f'DESCRIBE SELECT * FROM "{table}"').fetchall()]

    specs = measures if measures else [(operation, measure)]

    unnest: set = set()

    def ref(path: str) -> str:
        expr, ucol = _resolve_ref(path, cols_types)
        if ucol:
            unnest.add(ucol)
        return expr

    # One or several figures in a single pass (sum, avg and count together cost
    # one round-trip, not three). Each spec is (operation, measure).
    agg_columns: list[tuple[str, str]] = []
    numeric_checks: list[tuple[str, str, str]] = []   # (op, measure, expr) for sum/avg
    used: dict[str, int] = {}
    for spec_op, spec_measure in specs:
        o = spec_op.strip().lower()
        if o not in _OPERATIONS:
            raise ValueError(
                f"unknown operation {spec_op!r}. Use one of: " + ", ".join(_OPERATIONS)
            )
        if o != "count" and not spec_measure.strip():
            raise ValueError(f"{o} needs a measure column, e.g. 'sum:Total'")
        measure_sql = ref(spec_measure) if o != "count" else ""
        if o in ("sum", "avg"):
            numeric_checks.append((o, spec_measure.strip(), measure_sql))
        label = o if o == "count" else f"{o}_{spec_measure.strip()}"
        used[label] = used.get(label, 0) + 1
        if used[label] > 1:                       # keep distinct SQL aliases
            label = f"{label}_{used[label]}"
        agg_columns.append((label, _OPERATIONS[o].format(col=measure_sql)))

    group_parts = [p.strip() for p in group_by.split(",") if p.strip()]
    group_exprs: list[str] = []
    has_period = False
    for part in group_parts:
        base, period = part, None
        if ":" in part:
            head, suffix = part.rsplit(":", 1)
            if suffix.strip().lower() in _PERIODS:
                base, period = head.strip(), suffix.strip().lower()
        expr = ref(base)
        if period:
            # Bucket a date into a period (parsing epoch dates too) for trends.
            expr = f"CAST(date_trunc('{period}', {_as_date(expr)}) AS DATE)"
            has_period = True
        group_exprs.append(expr)
    where_sql, params, where_unnest = _build_where(where, cols_types)
    unnest |= where_unnest

    if len(unnest) > 1:
        raise ValueError(
            "only one list field can be expanded per query; got "
            + ", ".join(sorted(unnest))
        )
    source = f'"{table}"'
    if unnest:
        # Explode the list once; top-level columns stay reachable via the '*'.
        source = f'(SELECT unnest({_quote(next(iter(unnest)))}) AS _u, * FROM "{table}")'

    # sum/avg only mean something on numbers. If the measure holds values but
    # none read as numbers (a status, a name), TRY_CAST would silently sum to
    # NULL — a wrong "no data". Fail loud instead.
    for op, measure, expr in numeric_checks:
        nn, ok = con.execute(
            f"SELECT count(*) FILTER (WHERE {expr} IS NOT NULL), "
            f"count(*) FILTER (WHERE TRY_CAST({expr} AS DOUBLE) IS NOT NULL) "
            f"FROM {source}"
        ).fetchone()
        if nn and not ok:
            raise ValueError(
                f"cannot {op} {measure!r}: it holds text, not numbers, so the "
                f"result would be an empty NULL. Use count, or a numeric measure."
            )

    select_groups = [f'{e} AS {_quote(p)}' for e, p in zip(group_exprs, group_parts)]
    select_aggs = [f'{sql} AS {_quote(lbl)}' for lbl, sql in agg_columns]
    select = ", ".join(select_groups + select_aggs)
    grouping = f" GROUP BY {', '.join(group_exprs)}" if group_exprs else ""
    # A time bucket reads best chronologically; otherwise by the first measure.
    if not group_exprs:
        ordering = ""
    elif has_period:
        ordering = " ORDER BY " + ", ".join(group_exprs)
    else:
        ordering = f" ORDER BY {_quote(agg_columns[0][0])} DESC"

    sql = f'SELECT {select} FROM {source}{where_sql}{grouping}{ordering} LIMIT {limit}'
    result = con.execute(sql, params)
    return [d[0] for d in result.description], result.fetchall()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class _workspace:
    """A DuckDB connection holding one or more backup files as tables.

    Files are pulled to scratch, read into memory, and the connection is then
    sealed — no filesystem, no network — before anything derived from caller
    input runs against it. /tmp is per-instance and ephemeral, which suits a
    stateless request exactly; nothing is reused between calls, deliberately.

    More than one table exists so backups can be compared: a question about what
    changed between two dates needs both of them open at once."""

    def __init__(self) -> None:
        self.con = duckdb.connect()
        self._paths: list[Path] = []
        self._sealed = False
        self.report_legends: dict[str, dict] = {}   # per table, if it's a report

    def load(
        self, organisation: str, filename: str, backup_date: str, table: str,
        raw: bool = False,
    ) -> str:
        """Read one backup file into `table`. Returns the date it resolved to.

        raw=True bypasses the report flattener (and the ledger's journal aliasing/
        union), reading the file exactly as stored — a debug path for inspecting a
        report's own nested shape."""
        if self._sealed:
            raise RuntimeError("workspace is sealed; load every table first")

        store = _store()
        date = store.resolve_date(organisation, backup_date)

        # .xlsx / .xls are human-facing copies of the same-named .jsonl, and the
        # server has no Excel reader. Read the .jsonl twin when it exists; when it
        # doesn't, say so plainly rather than fail on a missing dependency.
        if Path(filename).suffix.lower() in {".xlsx", ".xls"}:
            sibling = _jsonl_sibling(filename)
            present = {f.name for f in store.files(organisation, date)}
            if sibling in present:
                filename = sibling
            else:
                raise ValueError(
                    f"{filename} is a spreadsheet for people, not analysis; this "
                    f"server reads the .jsonl form, and {sibling} isn't in the "
                    f"{date} backup for {organisation}. Run `browse` to see the "
                    f"readable files."
                )

        # general_ledger on Xero: it has no GL file — its journals ARE the ledger,
        # already flat and keyed — so alias to journals and skip flattening. The
        # journals are chunked (journals.jsonl, journals_1.jsonl, …); union every
        # part so a "sum all postings" isn't a silent undercount of the first 5000.
        gl = _is_general_ledger(filename) and not raw
        journal_parts: list[str] = []
        if gl:
            present = {f.name for f in store.files(organisation, date)}
            if filename not in present:
                parts = [p for p in present
                         if re.fullmatch(r"journals(_\d+)?\.jsonl", p)]
                if parts:
                    parts.sort(key=lambda p: int(re.search(r"_(\d+)", p).group(1))
                               if "_" in p else 0)
                    filename, journal_parts, gl = parts[0], parts, False

        ref = store.object_for(organisation, date, filename)
        body = store.fetch(ref)
        if len(journal_parts) > 1:
            for part in journal_parts[1:]:
                extra = store.fetch(store.object_for(organisation, date, part))
                body = body.rstrip(b"\n") + b"\n" + extra

        # A report file is one (or, for the ledger, many) deeply nested objects.
        # Flatten into a virtual table — statements to
        # [section, account, account_id, row_type, <value columns>], the ledger to
        # one row per posting — so every tool reads ordinary rows. Written back as
        # JSONL and read the normal way, so typing, sealing and cleanup are unchanged.
        suffix = Path(filename).suffix.lower() or ".bin"
        kind = _report_kind(filename) if not raw else None
        if kind:
            obj = None
            for line in body.decode("utf-8", "replace").splitlines():
                if line.strip():
                    obj = json.loads(line)
                    break
            if obj is not None:
                legend, flat = flatten_report(obj)
                if flat:
                    self.report_legends[table] = legend
                    body = "\n".join(json.dumps(r) for r in flat).encode("utf-8")
                    suffix = ".jsonl"
        elif gl:
            objs = [json.loads(line) for line in body.decode("utf-8", "replace").splitlines()
                    if line.strip()]
            flat = flatten_general_ledger(objs)
            if flat:
                body = "\n".join(json.dumps(r) for r in flat).encode("utf-8")
                suffix = ".jsonl"

        fd = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        fd.write(body)
        fd.close()
        path = Path(fd.name)
        self._paths.append(path)

        reader = self._reader(path, table)
        self.con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM {reader}')
        return date

    def seal(self) -> None:
        """Shut off filesystem and network access.

        Done after loading, not before: the setting blocks our own scratch copy
        too, so a file read attempted afterwards fails on the very file it is
        supposed to be reading."""
        self.con.execute("SET enable_external_access = false")
        self._sealed = True

    def columns(self, table: str) -> list[tuple[str, str]]:
        rows = self.con.execute(f'DESCRIBE SELECT * FROM "{table}"').fetchall()
        return [(name, dtype) for name, dtype, *_ in rows]

    def close(self) -> None:
        self.con.close()
        for path in self._paths:
            if path.exists():
                path.unlink()

    def __enter__(self) -> "_workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _reader(self, path: Path, table: str) -> str:
        suffix = path.suffix.lower()

        if suffix in {".jsonl", ".ndjson"}:
            return f"read_json_auto('{path}', format='newline_delimited')"
        if suffix == ".json":
            return f"read_json_auto('{path}')"
        if suffix in {".csv", ".tsv", ".txt"}:
            return f"read_csv_auto('{path}')"
        if suffix in {".parquet", ".pq"}:
            return f"read_parquet('{path}')"
        if suffix in {".xlsx", ".xls"}:
            # A spreadsheet is a human-facing copy of the same-named .jsonl, which
            # load() redirects to, so an .xlsx should never reach here. Guard
            # clearly rather than pull heavy pandas into the Lambda to read one.
            raise ValueError(
                f"cannot read '{suffix}' directly — read the .jsonl of the same "
                f"name, which holds the same data."
            )

        raise ValueError(
            f"cannot read '{suffix}' files — this server reads .jsonl, .json, "
            f".csv and .parquet backups"
        )


class _dataset:
    """One backup file, loaded as `data`. The common case."""

    def __init__(self, organisation: str, filename: str, backup_date: str,
                 raw: bool = False) -> None:
        self.organisation = organisation
        self.filename = filename
        self.backup_date = backup_date
        self.raw = raw
        self.workspace: _workspace | None = None
        self.report_legend: dict | None = None   # value-slug -> label, if a report

    def __enter__(self) -> tuple[duckdb.DuckDBPyConnection, str]:
        self.workspace = _workspace()
        try:
            date = self.workspace.load(
                self.organisation, self.filename, self.backup_date, "data",
                raw=self.raw,
            )
            self.workspace.seal()
        except Exception:
            self.workspace.close()
            raise
        self.report_legend = self.workspace.report_legends.get("data")
        return self.workspace.con, date

    def __exit__(self, *exc) -> None:
        if self.workspace is not None:
            self.workspace.close()

__all__ = [
    '_server_icons',
    'SERVER_VERSION',
    'SERVER_CHANGELOG',
    'mcp',
    '_changelog',
    '_store',
    '_jsonl_sibling',
    '_denoise',
    '_short_error',
    '_group_skips',
    '_as_table',
    '_report_kind',
    'flatten_report',
    '_is_general_ledger',
    'flatten_general_ledger',
    '_OPERATIONS',
    '_COMPARISONS',
    '_CONDITION',
    '_quote',
    '_resolve_column',
    '_PATH_SEG',
    '_DATE_VALUE',
    '_NUMERIC_VALUE',
    '_PERIODS',
    '_as_date',
    '_is_list_type',
    '_resolve_ref',
    '_WHERE_KEYWORD',
    '_tokenize_where',
    '_compile_condition',
    '_build_where',
    '_leaf_conditions',
    '_explain_empty',
    '_aggregate',
    '_workspace',
    '_dataset',
]
