"""Nested-path grouping, date/quote filters, and pagination for the reader.

Runs on a fabricated in-memory DuckDB table shaped like a QuickBooks purchase —
a top-level struct, a date, and a Line[] array of coded lines — so it needs no
S3 or database. Exercises three fixes surfaced in an early source review:

  1. preview_file offset  -> LIMIT/OFFSET pages a long file
  2. aggregate_file nested -> group by a line-level account inside Line[]
  3. where date filter     -> a date comparison actually filters
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import duckdb  # noqa: E402
from s3_mcp.server import (  # noqa: E402
    _aggregate, _build_where, _resolve_ref, _explain_empty, _leaf_conditions,
    _page_status, _resolve_filename, _jsonl_sibling, _denoise, _parse_measures,
    _natural_key, _short_error, _group_skips,
)
from s3_mcp.tools.compare import _diff_snippet  # noqa: E402
from s3_mcp.tools.aggregate import _as_table  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    print(("  ok  " if ok else "  FAIL") + f"  {label}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        fails += 1


con = duckdb.connect()
con.execute("""
CREATE TABLE data AS
SELECT *, '/Date(' || (epoch(TxnDate::TIMESTAMP) * 1000)::BIGINT || '+0000)/' AS JournalDate
FROM (VALUES
  ('t1', DATE '2025-06-15', 40.0, [
        {'DetailType':'AccountBasedExpenseLineDetail','Amount':40.0,
         'AccountBasedExpenseLineDetail': {'AccountRef': {'name':'Rent'}}}]),
  ('t2', DATE '2025-12-01', 130.0, [
        {'DetailType':'AccountBasedExpenseLineDetail','Amount':100.0,
         'AccountBasedExpenseLineDetail': {'AccountRef': {'name':'Rent'}}},
        {'DetailType':'AccountBasedExpenseLineDetail','Amount':30.0,
         'AccountBasedExpenseLineDetail': {'AccountRef': {'name':'Insurance'}}}]),
  ('t3', DATE '2026-02-20', 60.0, [
        {'DetailType':'AccountBasedExpenseLineDetail','Amount':60.0,
         'AccountBasedExpenseLineDetail': {'AccountRef': {'name':'Insurance'}}}])
) AS t(Id, TxnDate, TotalAmt, Line)
""")
cols_types = [(n, t) for n, t, *_ in con.execute("DESCRIBE SELECT * FROM data").fetchall()]

print("resolve:")
check("top-level column", _resolve_ref("TotalAmt", cols_types), ('"TotalAmt"', None))
expr, unnest = _resolve_ref("Line.AccountBasedExpenseLineDetail.AccountRef.name", cols_types)
check("list path expands", (expr, unnest),
      ("_u.AccountBasedExpenseLineDetail.AccountRef.name", "Line"))
try:
    _resolve_ref("Line.bad;DROP", cols_types); check("injection blocked", "not raised", "raise")
except ValueError:
    check("injection blocked", "raised", "raised")

print("\nfilter:")
sql, _, _ = _build_where("TxnDate>=2025-11-01", cols_types)
check("date -> TIMESTAMP cast", "AS TIMESTAMP" in sql, True)
sql, _, _ = _build_where("TotalAmt>50", cols_types)
check("number -> DOUBLE cast", "AS DOUBLE" in sql, True)
_, params, _ = _build_where('Id="a b"', cols_types)
check("quoted value unwrapped", params, ["a b"])

print("\naggregate — by-category spend (nested unnest):")
_, rows = _aggregate(con, "data", "sum", "Line.Amount",
                     "Line.AccountBasedExpenseLineDetail.AccountRef.name", "", 10)
by_cat = {r[0]: round(r[1], 2) for r in rows}
check("Rent total", by_cat.get("Rent"), 140.0)      # 40 + 100
check("Insurance total", by_cat.get("Insurance"), 90.0)  # 30 + 60

print("\naggregate — date filter actually filters:")
_, r = _aggregate(con, "data", "count", "", "", "TxnDate>=2025-11-01", 1)
check("rows on/after 2025-11-01", r[0][0], 2)        # t2, t3 (not t1 in June)
_, r = _aggregate(con, "data", "count", "", "", "TxnDate>=2099-01-01", 1)
check("rows in the future", r[0][0], 0)

print("\npagination (offset):")
page1 = [x[0] for x in con.execute("SELECT Id FROM data LIMIT 2 OFFSET 0").fetchall()]
page2 = [x[0] for x in con.execute("SELECT Id FROM data LIMIT 2 OFFSET 2").fetchall()]
check("page 1", page1, ["t1", "t2"])
check("page 2 (offset 2)", page2, ["t3"])

print("\nepoch-ms date filter (Xero /Date(ms)/):")
_, r = _aggregate(con, "data", "count", "", "", "JournalDate>=2025-11-01", 1)
check("epoch date >= 2025-11-01", r[0][0], 2)        # t2, t3 (was 0 — /Date()/ won't cast)
_, r = _aggregate(con, "data", "count", "", "", "JournalDate>=2099-01-01", 1)
check("epoch date in the future", r[0][0], 0)

print("\ntype-aware equality (int literal vs stored double):")
_, r = _aggregate(con, "data", "count", "", "", "TotalAmt=40", 1)
check("TotalAmt=40 matches stored 40.0", r[0][0], 1)  # text '=' alone would miss "40.0"

print("\nfail loudly on empty:")
mismatch = _explain_empty(con, "data", "Id>5")        # Id is text -> not numbers
check("type mismatch is named", "read as numbers" in mismatch, True)
genuine = _explain_empty(con, "data", "TotalAmt>9999")  # numeric, just no match
check("genuine miss points at chunks", "another file" in genuine, True)

print("\ntrend — date bucketing (group_by Date:month):")
_, rows = _aggregate(con, "data", "sum", "TotalAmt", "TxnDate:month", "", 12)
months = [str(r[0]) for r in rows]
check("one bucket per month", len(rows), 3)            # Jun-25, Dec-25, Feb-26
check("chronological order", months, sorted(months))

print("\nreconcile — key membership (matched / unmatched):")
con.execute("CREATE TABLE pay AS SELECT * FROM (VALUES (1,'a'),(2,'a'),(3,'x')) AS t(pid, ref)")
con.execute("CREATE TABLE inv AS SELECT * FROM (VALUES ('a'),('b')) AS t(iid)")
con.execute("CREATE VIEW mv AS SELECT * FROM pay WHERE CAST(ref AS VARCHAR) "
            "IN (SELECT CAST(iid AS VARCHAR) FROM inv WHERE iid IS NOT NULL)")
con.execute("CREATE VIEW uv AS SELECT * FROM pay WHERE CAST(ref AS VARCHAR) "
            "NOT IN (SELECT CAST(iid AS VARCHAR) FROM inv WHERE iid IS NOT NULL)")
_, r = _aggregate(con, "mv", "count", "", "", "", 1)
check("matched rows", r[0][0], 2)                      # ref 'a' present -> pid 1,2
_, r = _aggregate(con, "uv", "count", "", "", "", 1)
check("unmatched rows", r[0][0], 1)                    # ref 'x' absent  -> pid 3

print("\nboolean filters — OR / NOT / parentheses (was AND-only):")


def _raises(label, expr, needle):
    global fails
    try:
        _build_where(expr, cols_types)
        print(f"  FAIL  {label}  (no error raised)")
        fails += 1
    except ValueError as e:
        ok = needle.lower() in str(e).lower()
        print(("  ok  " if ok else "  FAIL") + f"  {label}")
        if not ok:
            fails += 1


def _count(expr):
    return _aggregate(con, "data", "count", "", "", expr, 1)[1][0][0]


# TotalAmt: t1=40, t2=130, t3=60
check("OR matches either branch", _count("TotalAmt>100 OR TotalAmt<50"), 2)   # t2, t1
check("AND still narrows", _count("TotalAmt>50 AND TotalAmt<100"), 1)         # t3
check("NOT negates", _count("NOT TotalAmt>100"), 2)                           # t1, t3
check("parens group precedence",
      _count("(TotalAmt>100 OR TotalAmt<50) AND Id!=t1"), 1)                  # t2 only
check("lowercase or/and/not work", _count("TotalAmt>100 or TotalAmt<50"), 2)

# safety and fail-loud are preserved
_raises("word value on > still rejected", "TotalAmt>abc", "number or date")
_raises("unbalanced parens rejected", "(TotalAmt>100", "unbalanced")
_raises("dangling operator rejected", "TotalAmt>100 AND", "expected a condition")
# a bare 'or' contains-fragment is a value, not the boolean OR — must still parse
_, p, _ = _build_where("Id~or", cols_types)
check("bare 'or' fragment kept", p, ["%or%"])
# a quoted value hides its OR from the tokenizer (one literal condition)
check("quoted OR stays one condition", len(_leaf_conditions('Id="a OR b"')), 1)

print("\npreview_file page status (offset past end must read clearly):")
check("normal page + more", _page_status(0, 10, 87, 10),
      "rows 0–9 of 87  ·  more: call again with offset=10")
check("last page, no more", _page_status(80, 7, 87, 10), "rows 80–86 of 87")
check("offset past end is explained",
      "past the end" in _page_status(200, 0, 87, 10)
      and "offsets 0–86" in _page_status(200, 0, 87, 10), True)
check("no nonsensical 200-200", "200–200" not in _page_status(200, 0, 87, 10), True)
check("empty file", _page_status(0, 0, 0, 10), "(this file has no rows)")
check("filtered zero is not 'empty file'",
      _page_status(0, 0, 0, 10, filtered=True), "no rows match the filter")

print("\ncross-org filename resolution (Xero plural vs QuickBooks singular):")
check("exact name wins",
      _resolve_filename("invoices.jsonl", {"invoices.jsonl", "accounts.jsonl"}),
      "invoices.jsonl")
check("Xero plural -> QBO singular",
      _resolve_filename("invoices.jsonl", {"invoice.jsonl", "account.jsonl"}),
      "invoice.jsonl")
check("QBO singular -> Xero plural",
      _resolve_filename("invoice.jsonl", {"invoices.jsonl"}), "invoices.jsonl")
check("accounts <-> account",
      _resolve_filename("accounts.jsonl", {"account.jsonl"}), "account.jsonl")
check("no equivalent -> None (skip + report)",
      _resolve_filename("invoices.jsonl", {"contacts.jsonl"}), None)
check("extension is respected (no .xlsx for .jsonl)",
      _resolve_filename("invoices.jsonl", {"invoice.xlsx"}), None)

print("\n.xlsx redirects to its readable .jsonl twin:")
check("xlsx -> jsonl", _jsonl_sibling("invoices.xlsx"), "invoices.jsonl")
check("xls -> jsonl", _jsonl_sibling("balance_sheet.xls"), "balance_sheet.jsonl")

print("\nfloat noise is stripped for display (money kept):")
check("sum noise -> clean", _denoise(33077.020000000004), 33077.02)
check("whole float -> int", _denoise(5.0), 5)
check("avg keeps precision", _denoise(3.3333333), 3.3333)
check("text passes through", _denoise("PAID"), "PAID")

print("\nmultiple measures in one pass:")
_c, _r = _aggregate(con, "data", "count", "", "", "", 1,
                    [("sum", "TotalAmt"), ("avg", "TotalAmt"), ("count", "")])
check("three measure columns", _c, ["sum_TotalAmt", "avg_TotalAmt", "count"])
check("count value", _r[0][2], 3)

print("\nmeasures parsing:")
check("op:col and bare count",
      _parse_measures("sum:Total, avg:Total, count"),
      [("sum", "Total"), ("avg", "Total"), ("count", "")])
check("empty -> []", _parse_measures("  "), [])

print("\nnatural key for report-style files (no id column):")
con.execute("CREATE TABLE before AS SELECT * FROM (VALUES "
            "('Rent',100.0),('Rates',50.0)) AS t(Account,Balance)")
con.execute("CREATE TABLE after AS SELECT * FROM (VALUES "
            "('Rent',120.0),('Rates',50.0)) AS t(Account,Balance)")


class _WS:
    pass


_ws = _WS()
_ws.con = con
check("picks the label column", _natural_key(_ws, ["Account", "Balance"],
                                             ["Account", "Balance"]), "Account")

print("\nskip-message formatting (no mid-word truncation):")
check("ValueError shows message only",
      _short_error(ValueError("OR isn't supported in a filter")),
      "OR isn't supported in a filter")
check("other errors keep their type",
      _short_error(KeyError("x")), "KeyError: 'x'")
_long = _short_error(ValueError("word " * 60))
check("long message ends cleanly", _long.endswith(" …") and " wor" not in _long[-6:], True)

print("\ndiff-aware windowing (change stays visible deep in a value):")
_b, _a = _diff_snippet("x" * 90 + "DRAFT", "x" * 90 + "AUTHORISED")
check("before window shows the change", "DRAFT" in _b, True)
check("after window shows the change", "AUTHORISED" in _a, True)
check("window is bounded", len(_a) <= 122, True)

print("\nsum/avg on a text column fails loud (was a silent null):")
try:
    _aggregate(con, "data", "sum", "Id", "", "", 1)   # Id is text
    check("sum on text raises", "no raise", "raise")
except ValueError as e:
    check("sum on text raises", "holds text" in str(e), True)
# sum on a real numeric column still works
_c, _r = _aggregate(con, "data", "sum", "TotalAmt", "", "", 1)
check("sum on number works", _r[0][0], 230.0)   # 40 + 130 + 60

print("\ncross-platform column alias (Total -> TotalAmt fallback):")
_expr, _u = _resolve_ref("Total", cols_types)     # file has TotalAmt, not Total
check("Total resolves to TotalAmt", _expr, '"TotalAmt"')

print("\ngrouped skip reasons (one line per reason, not per org):")
_g = _group_skips([("A", "no invoices.jsonl"), ("B", "no invoices.jsonl"),
                   ("C", "wrong type")])
check("two reason lines", len(_g), 2)
check("shared reason lists both orgs", "A, B — no invoices.jsonl" in _g[0], True)
# same error with a per-org tail (column list differs) still collapses to one
_g2 = _group_skips([("A", "'Total' is not a field. Columns: X, Y"),
                    ("B", "'Total' is not a field. Columns: P, Q")])
check("same first sentence, different tails -> one line", len(_g2), 1)
check("first sentence kept", "'Total' is not a field" in _g2[0], True)

print("\naggregate output is a compact table (not repeated-key JSON):")
_t = _as_table(["Status", "count"], [("PAID", 100), ("VOID", 5)])
check("header then rows", _t.splitlines()[0].startswith("Status"), True)
check("keys not repeated per row", _t.count("Status"), 1)
check("values present", "PAID" in _t and "100" in _t, True)
check("float noise denoised in table", "5.0" not in _as_table(["n"], [(5.0,)]), True)
check("pipe in a value is neutralised",
      "|" not in _as_table(["x"], [("a|b",)]).splitlines()[1], True)
check("nested value renders as compact json",
      '{"a":1}' in _as_table(["x"], [({"a": 1},)]), True)

print("\nreport flattener (Phase 1) — statements to flat rows:")
from s3_mcp._reports import (  # noqa: E402
    flatten_report, flatten_general_ledger, _report_kind, _is_general_ledger,
)
check("report_kind detects statements", _report_kind("balance_sheet.jsonl"), "balance_sheet")
check("report_kind ignores non-reports", _report_kind("invoices.jsonl"), None)
check("general_ledger detected", _is_general_ledger("general_ledger.jsonl"), True)

_xero = {"Reports": [{"Rows": [
    {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "31 Dec 2026"}]},
    {"RowType": "Section", "Title": "Assets", "Rows": [
        {"RowType": "Section", "Title": "Bank", "Rows": [
            {"RowType": "Row", "Cells": [
                {"Value": "TD Bank", "Attributes": [{"Id": "account", "Value": "u-td"}]},
                {"Value": "239250.45"}]},
            {"RowType": "SummaryRow", "Cells": [{"Value": "Total Bank"}, {"Value": "243378.79"}]},
        ]}]}]}]}
_leg, _rows = flatten_report(_xero)
check("nested section path joined", _rows[0]["section"], "Assets > Bank")
check("account keyed by uuid", _rows[0]["account_id"], "u-td")
check("summary keyed by section|name", _rows[1]["account_id"], "Assets > Bank|Total Bank")
check("row_type account vs summary",
      (_rows[0]["row_type"], _rows[1]["row_type"]), ("account", "summary"))
check("value column slugged from label", "c_31_dec_2026" in _rows[0], True)

_qbo = {"Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Debit"}]},
        "Rows": {"Row": [
            {"type": "Data", "ColData": [{"value": "Cash", "id": "35"}, {"value": "1,000.00"}]}]}}
_leg2, _rows2 = flatten_report(_qbo)
check("qbo walker: id + comma-stripped value",
      (_rows2[0]["account_id"], _rows2[0]["debit"]), ("35", 1000.0))

print("\nreport flattener (Phase 2) — general ledger to postings:")
_gl = {"Header": {"StartPeriod": "2026-08-01"},
       "Columns": {"Column": [{"ColType": "tx_date"}, {"ColType": "txn_type"},
                              {"ColType": "doc_num"}, {"ColType": "subt_nat_amount"}]},
       "Rows": {"Row": [
           {"type": "Section", "Header": {"ColData": [{"value": "Sales", "id": "79"}]},
            "Rows": {"Row": [
                {"type": "Data", "ColData": [{"value": "2026-08-18", "id": "t1"},
                 {"value": "Invoice"}, {"value": "3645"}, {"value": "330.00"}]}]}}]}}
_posts = flatten_general_ledger([_gl])
_p = _posts[0]
check("posting row_type", _p["row_type"], "posting")
check("posting fields mapped", (_p["txn_date"], _p["doc_num"], _p["amount"]),
      ("2026-08-18", "3645", 330.0))
check("account_id propagated from section (not the name)", _p["account_id"], "79")
check("posting keyed by txn+account+line ordinal", _p["posting_id"], "t1|79|1")

# D3/D4: label rows reclassified, real account id carried down
_gl2 = {"Header": {"StartPeriod": "2026-08-01"},
        "Columns": {"Column": [{"ColType": "tx_date"}, {"ColType": "subt_nat_amount"}]},
        "Rows": {"Row": [{"type": "Section",
                          "Header": {"ColData": [{"value": "Bank", "id": "18"}]},
                          "Rows": {"Row": [
                              {"type": "Data", "ColData": [{"value": "Beginning Balance"},
                                                           {"value": "100.00"}]},
                              {"type": "Data", "ColData": [{"value": "2026-08-05", "id": "tx9"},
                                                           {"value": "50.00"}]}]}}]}}
_p2 = flatten_general_ledger([_gl2])
_open = [r for r in _p2 if r["row_type"] == "opening_balance"]
check("beginning balance reclassified from posting", len(_open), 1)
check("label not stored in txn_date", _open[0]["txn_date"], "")
check("real posting carries account id", next(r for r in _p2 if r["row_type"] == "posting")["account_id"], "18")

# D7: a journal posting many lines to one account must get unique, ordered keys
_je = {"Header": {"StartPeriod": "2026-08-01"},
       "Columns": {"Column": [{"ColType": "tx_date"}, {"ColType": "subt_nat_amount"}]},
       "Rows": {"Row": [{"type": "Section",
                         "Header": {"ColData": [{"value": "AR", "id": "17"}]},
                         "Rows": {"Row": [
                             {"type": "Data", "ColData": [{"value": "2026-08-01", "id": "367"}, {"value": "10.00"}]},
                             {"type": "Data", "ColData": [{"value": "2026-08-01", "id": "367"}, {"value": "20.00"}]},
                             {"type": "Data", "ColData": [{"value": "2026-08-01", "id": "367"}, {"value": "30.00"}]}]}}]}}
_pids = [r["posting_id"] for r in flatten_general_ledger([_je]) if r["row_type"] == "posting"]
check("44-into-one-account no longer collides (unique keys)", len(set(_pids)), 3)
check("ordinal within (txn, account), report order", _pids, ["367|17|1", "367|17|2", "367|17|3"])

# D8/D12: a non-posting key is account|period|type|label — no ordinal, and no
# period_end (which would roll forward each backup)
check("opening key is account|period|type|label",
      _open[0]["posting_id"], "18|2026-08-01|opening_balance|Beginning Balance")

# D11: open-month slices are CUMULATIVE month-to-date snapshots — the later slice
# re-contains the earlier postings. Keep only the latest per month so each posting
# / opening appears once (no inflated aggregates, no re-stated diff rows).
def _slice(end, postings):
    data = [{"type": "Data", "ColData": [{"value": "Beginning Balance"}, {"value": "0"}]}]
    for pid, amt in postings:
        data.append({"type": "Data", "ColData": [{"value": "2026-08-05", "id": pid},
                                                 {"value": amt}]})
    return {"Header": {"StartPeriod": "2026-08-01", "EndPeriod": end},
            "Columns": {"Column": [{"ColType": "tx_date"}, {"ColType": "subt_nat_amount"}]},
            "Rows": {"Row": [{"type": "Section",
                              "Header": {"ColData": [{"value": "AR", "id": "17"}]},
                              "Rows": {"Row": data}}]}}


_early = _slice("2026-08-13", [("a", "10")])
_late = _slice("2026-08-19", [("a", "10"), ("b", "20")])   # cumulative: 'a' again + 'b'
_dd = flatten_general_ledger([_early, _late])
_posts = [r for r in _dd if r["row_type"] == "posting"]
check("cumulative slices dedup to the latest (each posting once)",
      sorted(r["txn_id"] for r in _posts), ["a", "b"])
_ob = [r for r in _dd if r["row_type"] == "opening_balance"]
check("opening appears once, not per slice", len(_ob), 1)
check("kept slice is the latest as-of date", _ob[0]["period_end"], "2026-08-19")
check("period stays month-granular for filters", _ob[0]["period"], "2026-08-01")

# D12: two backups, the open-month as-of rolled forward (16 -> 19) but the
# content is identical — non-posting keys must match, or every row false-flags
# removed+added. period_end is a column, not part of the key.
_k16 = {r["posting_id"] for r in flatten_general_ledger([_slice("2026-08-16", [("a", "10")])])
        if r["row_type"] != "posting"}
_k19 = {r["posting_id"] for r in flatten_general_ledger([_slice("2026-08-19", [("a", "10")])])
        if r["row_type"] != "posting"}
check("non-posting keys stable as the as-of date rolls forward", _k16, _k19)

# D9: map columns by MetaData ColKey — doc_num renders as "#", amount as *_nt
_gld = {"Header": {"StartPeriod": "2026-08-01"},
        "Columns": {"Column": [
            {"ColTitle": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]},
            {"ColTitle": "#", "MetaData": [{"Name": "ColKey", "Value": "doc_num"}]},
            {"ColTitle": "Split", "MetaData": [{"Name": "ColKey", "Value": "split_acc"}]},
            {"ColTitle": "Amount", "MetaData": [{"Name": "ColKey", "Value": "subt_nat_amount_nt"}]}]},
        "Rows": {"Row": [{"type": "Section",
                          "Header": {"ColData": [{"value": "Sales", "id": "82"}]},
                          "Rows": {"Row": [{"type": "Data", "ColData": [
                              {"value": "2026-08-18", "id": "9"},
                              {"value": "3497"},
                              {"value": "Accounts Receivable", "id": "17"},
                              {"value": "330.00"}]}]}}]}}
_d9 = flatten_general_ledger([_gld])[0]
check("doc_num mapped via ColKey despite '#' title", _d9["doc_num"], "3497")
check("split account + its id captured",
      (_d9["split_account"], _d9["split_account_id"]), ("Accounts Receivable", "17"))
check("txn_id is the txn, not the split id", _d9["txn_id"], "9")
check("amount mapped via *_nt ColKey", _d9["amount"], 330.0)

con.close()
print("\n" + ("Reader path/filter/paging behaviour holds." if not fails else f"{fails} FAILED"))
sys.exit(1 if fails else 0)
