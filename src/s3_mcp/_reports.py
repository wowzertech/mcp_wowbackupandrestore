"""Report flattening layer.

The four financial reports (trial_balance, balance_sheet, profit_and_loss,
cash_flow) are stored as one deeply nested object per file — unreadable to
preview, unmatchable by compare, unreachable by aggregate. This module turns
either platform's shape (Xero Reports[].Rows[] or QuickBooks Rows.Row[]) into a
flat list of rows — [section, account, account_id, row_type, <value columns>] —
so every existing tool consumes reports for free: preview shows real rows,
compare matches accounts by account_id (the account-level delta), aggregate sums
a value column grouped by section, browse counts real rows.

Pure stdlib, imported by _core — no dependency back on the server, so no cycle.
"""
from __future__ import annotations

import re

# Statement reports this layer flattens (Phase 1). Same names on both platforms.
_REPORT_STEMS = {"trial_balance", "balance_sheet", "profit_and_loss", "cash_flow"}
_GL_STEM = "general_ledger"                       # Phase 2 (QuickBooks only)


def _stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0].lower() if "." in filename else filename.lower()


def _report_kind(filename: str) -> str | None:
    """The statement-report type for a filename, or None if it isn't one."""
    stem = _stem(filename)
    return stem if stem in _REPORT_STEMS else None


def _is_general_ledger(filename: str) -> bool:
    return _stem(filename) == _GL_STEM


def _money(value: object) -> float:
    """Parse a report cell to a number: '' -> 0.0, commas/'$' stripped,
    parentheses read as negative, already-numeric passed through, other text 0.0."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        f = float(s)
    except ValueError:
        return 0.0
    return -f if neg else f


def _slug(label: str) -> str:
    s = re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_")
    if s and s[0].isdigit():          # a value column can't start a SQL identifier
        s = "c_" + s
    return s


def _unique_slugs(labels: list[str]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, lab in enumerate(labels):
        base = _slug(lab) or f"value_{i + 1}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return out


def _walk_xero(report: dict) -> tuple[list[str], list[dict]]:
    """Flatten a Xero report: Rows[] of RowType Header/Section/Row/SummaryRow.
    Column labels come from the Header row; the account key is the Cells[0]
    attribute with Id 'account'."""
    labels: list[str] = []
    rows: list[dict] = []

    def acct_key(cells: list) -> str | None:
        for a in (cells[0].get("Attributes") or []) if cells else []:
            if a.get("Id") == "account":
                return a.get("Value")
        return None

    def walk(node_rows: list, path: str) -> None:
        for r in node_rows or []:
            rt = r.get("RowType")
            cells = r.get("Cells") or []
            if rt == "Header":
                if not labels:
                    labels.extend((c.get("Value") or "") for c in cells[1:])
            elif rt == "Section":
                title = (r.get("Title") or "").strip()
                child = f"{path} > {title}" if path and title else (title or path)
                walk(r.get("Rows") or [], child)
            elif rt in ("Row", "SummaryRow"):
                name = (cells[0].get("Value") or "") if cells else ""
                rows.append({
                    "section": path, "name": name, "key": acct_key(cells),
                    "values": [_money(c.get("Value")) for c in cells[1:]],
                    "kind": "summary" if rt == "SummaryRow" else "account",
                })

    walk(report.get("Rows") or [], "")
    return labels, rows


def _walk_qbo(report: dict) -> tuple[list[str], list[dict]]:
    """Flatten a QuickBooks report: Rows.Row[] recursive, type Section/Data.
    Column labels come from Columns.Column[].ColTitle; the account key is
    ColData[0].id."""
    cols = ((report.get("Columns") or {}).get("Column")) or []
    labels = [(c.get("ColTitle") or "") for c in cols[1:]]
    rows: list[dict] = []

    def values(coldata: list) -> list[float]:
        return [_money(c.get("value")) for c in coldata[1:]]

    def walk(node: list, path: str) -> None:
        for r in node or []:
            nested = (r.get("Rows") or {}).get("Row")
            if r.get("type") == "Section" or nested is not None:
                hcd = (r.get("Header") or {}).get("ColData") or []
                title = ((hcd[0].get("value") if hcd else "") or "").strip()
                child = f"{path} > {title}" if path and title else (title or path)
                walk(nested or [], child)
                summ = (r.get("Summary") or {}).get("ColData") or []
                if summ:
                    rows.append({
                        "section": child,
                        "name": (summ[0].get("value") or f"Total {title}"),
                        "key": None, "values": values(summ), "kind": "summary",
                    })
            else:
                cd = r.get("ColData") or []
                rows.append({
                    "section": path,
                    "name": (cd[0].get("value") if cd else "") or "",
                    "key": (cd[0].get("id") if cd else None),
                    "values": values(cd), "kind": "account",
                })

    walk(((report.get("Rows") or {}).get("Row")) or [], "")
    return labels, rows


def _walk_report(obj: dict) -> tuple[list[str], list[dict]]:
    """Detect the platform shape and flatten to (column_labels, raw_rows)."""
    if isinstance(obj.get("Reports"), list) and obj["Reports"]:
        return _walk_xero(obj["Reports"][0])
    if isinstance(obj.get("Rows"), dict):            # QuickBooks: Rows.Row[]
        return _walk_qbo(obj)
    if isinstance(obj.get("Rows"), list):            # Xero report object direct
        return _walk_xero(obj)
    raise ValueError(
        "unrecognised report structure — expected a Xero or QuickBooks report."
    )


def flatten_report(obj: dict) -> tuple[dict, list[dict]]:
    """Turn a report object into (legend, rows).

    Each row is a flat dict: section, account, account_id (the real account key,
    or a stable "section|name" fallback so summaries and keyless rows still match
    one-to-one across backups), row_type (account|summary), and one column per
    value slug. The legend maps each value slug to its original column label."""
    labels, raw = _walk_report(obj)
    raw = [r for r in raw if r["name"] or any(r["values"])]   # drop spacer rows
    ncols = max((len(r["values"]) for r in raw), default=0)
    while len(labels) < ncols:
        labels.append(f"column {len(labels) + 1}")
    labels = labels[:ncols] if ncols else labels
    slugs = _unique_slugs(labels)
    legend = dict(zip(slugs, labels))

    rows: list[dict] = []
    for r in raw:
        row = {
            "section": r["section"],
            "account": r["name"],
            "account_id": r["key"] or f'{r["section"]}|{r["name"]}',
            "row_type": r["kind"],
        }
        for i, slug in enumerate(slugs):
            row[slug] = r["values"][i] if i < len(r["values"]) else None
        rows.append(row)
    return legend, rows


# --------------------------------------------------------------------------- #
# Phase 2 — general ledger (QuickBooks). One monthly report per line, each a
# tree of account sections holding posting lines. Flatten to one row per posting.
# --------------------------------------------------------------------------- #

_GL_FIELDS = {                       # QBO ColKey (preferred) / ColType / title -> field
    "tx_date": "txn_date", "date": "txn_date",
    "txn_type": "txn_type", "transaction_type": "txn_type", "type": "txn_type",
    "doc_num": "doc_num", "num": "doc_num", "no": "doc_num",
    "docnum": "doc_num", "ref": "doc_num", "reference": "doc_num",
    "is_adj": "is_adj", "adj": "is_adj",
    "name": "name",
    "memo": "memo", "memo_description": "memo", "description": "memo",
    "split_acc": "split_account", "split": "split_account",
    "subt_nat_amount": "amount", "subt_nat_amount_nt": "amount", "amount": "amount",
    "rbal_nat_amount": "running_balance", "rbal_nat_amount_nt": "running_balance",
    "balance": "running_balance",
}
_GL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")        # a real posting date


def _gl_colkey(column: dict) -> str:
    """QuickBooks' stable machine key for a column (Columns[].MetaData ColKey) —
    immune to the display ColTitle, which localises and abbreviates (doc_num
    renders as '#')."""
    for m in column.get("MetaData") or []:
        if m.get("Name") == "ColKey":
            return (m.get("Value") or "").lower()
    return ""


def _gl_field_map(columns: list) -> dict:
    """Map each general-ledger column index to a posting field — by the machine
    ColKey first, then ColType, then the display title — so the walker doesn't
    depend on fixed positions or localised labels."""
    out: dict[int, str | None] = {}
    for i, c in enumerate(columns):
        ct = (c.get("ColType") or "").lower()
        title = re.sub(r"[^a-z]+", "_", (c.get("ColTitle") or "").lower()).strip("_")
        out[i] = (_GL_FIELDS.get(_gl_colkey(c)) or _GL_FIELDS.get(ct)
                  or _GL_FIELDS.get(title))
    return out


def flatten_general_ledger(objs: list[dict]) -> list[dict]:
    """Flatten QuickBooks general_ledger — a monthly report per entry — into one
    row per posting: period, account_path, account_id (the account this posting
    sits under, carried down from its section), the txn fields, amount,
    running_balance, and posting_id.

    posting_id is transaction id + account id, plus a line ordinal within that
    (txn, account) group — one journal can post many lines to the same account
    (a conversion JE hits AR 44 times), which would otherwise collide and defeat
    the unique key compare needs. The ordinal is the line's position in report
    order, which QuickBooks renders stably, so it survives re-flattening and
    matches across backups. Balance/label rows (no transaction) key on
    account+period+kind+label with the same ordinal guard. A label sitting in the
    date column ("Beginning Balance", a stray total) is reclassified, not stored
    as a date."""
    def _month_of(obj: dict) -> tuple[str, str]:
        h = obj.get("Header") or {}
        anchor = h.get("StartPeriod") or h.get("EndPeriod") or h.get("ReportName") or ""
        month = anchor[:7] if re.match(r"^\d{4}-\d{2}", anchor) else anchor
        as_of = h.get("EndPeriod") or h.get("StartPeriod") or ""
        return month, as_of

    # The open month is captured as several CUMULATIVE month-to-date snapshots,
    # one appended per backup run — the latest re-contains every earlier posting.
    # Keep only the latest (max EndPeriod) struct per month, so each posting,
    # opening and summary appears once: aggregates are right and a backup-to-backup
    # diff is a true change journal, not a re-statement of old rows.
    latest: dict[str, tuple[str, dict]] = {}
    for obj in objs:
        month, as_of = _month_of(obj)
        if month not in latest or as_of >= latest[month][0]:
            latest[month] = (as_of, obj)
    objs = [latest[m][1] for m in latest]           # one struct per month, in order

    rows: list[dict] = []
    seen: dict[str, int] = {}                       # base key -> lines seen so far
    for obj in objs:
        header = obj.get("Header") or {}
        # period stays month-granular so filters keep working (period=2026-08-01
        # means "August"); period_start/period_end carry the kept slice's range —
        # period_end is its as-of date, useful metadata.
        period_start = header.get("StartPeriod") or ""
        period_end = header.get("EndPeriod") or ""
        anchor = period_start or period_end or header.get("ReportName") or ""
        period = anchor[:7] + "-01" if re.match(r"^\d{4}-\d{2}", anchor) else anchor
        columns = ((obj.get("Columns") or {}).get("Column")) or []
        fmap = _gl_field_map(columns)

        def emit(coldata: list, account_path: str, account_id: str) -> None:
            row = {"period": period, "period_start": period_start,
                   "period_end": period_end, "account_path": account_path,
                   "account_id": account_id, "txn_id": "", "txn_date": "",
                   "txn_type": "", "doc_num": "", "is_adj": "", "name": "",
                   "memo": "", "split_account": "", "split_account_id": "",
                   "amount": 0.0, "running_balance": None, "row_type": "posting"}
            for i, cell in enumerate(coldata):
                field = fmap.get(i)
                # The txn id rides on an early cell; the split cell also carries an
                # id (the offsetting account), so never mistake that for the txn.
                if cell.get("id") and not row["txn_id"] and field != "split_account":
                    row["txn_id"] = cell["id"]
                if field == "split_account":
                    row["split_account"] = cell.get("value") or ""
                    row["split_account_id"] = cell.get("id") or ""
                elif field in ("amount", "running_balance"):
                    row[field] = _money(cell.get("value"))
                elif field:
                    row[field] = cell.get("value") or ""
            dt = row["txn_date"]
            if dt and not _GL_DATE.match(dt):        # a label, not a posting
                row["name"] = row["name"] or dt
                row["txn_date"] = ""
                row["row_type"] = ("opening_balance"
                                   if "beginning" in dt.lower() else "summary")
            if row["row_type"] == "posting":
                # A journal can post many lines to one account, so key on a line
                # ordinal within the (txn, account) group — stable in report order.
                base = f'{row["txn_id"]}|{account_id}'
                seen[base] = seen.get(base, 0) + 1
                row["posting_id"] = f"{base}|{seen[base]}"
            else:
                # After the slice dedup there is exactly one struct per month, so
                # account+period+type+label is unique per file — and, crucially,
                # stable across backups. period_end must NOT be in the key: the
                # open month's as-of date rolls forward each backup, which would
                # false-flag every byte-identical row as removed+added. It stays
                # as a column (metadata), ignored by the differ.
                row["posting_id"] = f'{account_id}|{period}|{row["row_type"]}|{row["name"]}'
            rows.append(row)

        def walk(node: list, path: str, path_id: str) -> None:
            for r in node or []:
                nested = (r.get("Rows") or {}).get("Row")
                if r.get("type") == "Section" or nested is not None:
                    hcd = (r.get("Header") or {}).get("ColData") or []
                    title = ((hcd[0].get("value") if hcd else "") or "").strip()
                    acct_id = next((c.get("id") for c in hcd if c.get("id")), "") or path_id
                    child = f"{path} > {title}" if path and title else (title or path)
                    walk(nested or [], child, acct_id)
                    summ = (r.get("Summary") or {}).get("ColData") or []
                    if summ:
                        emit(summ, child, acct_id)
                elif r.get("type") == "Data":
                    emit(r.get("ColData") or [], path, path_id)

        walk(((obj.get("Rows") or {}).get("Row")) or [], "", "")
    return rows
