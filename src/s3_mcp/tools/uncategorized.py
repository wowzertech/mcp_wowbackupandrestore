"""find_uncategorized_balances: net money still parked in catch-all accounts."""
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
# Bookkeeping checks
# --------------------------------------------------------------------------- #

# The catch-all accounts a bookkeeper still has to clear. Two things make them
# easy to miss: their balance stays at zero whatever is posted to them, and a
# transaction line names the account by numeric code/id, never by this label —
# so a search by name or balance reports "none" while transactions sit there.
#
# Uncategorized and Ask-My-Accountant are the real catch-alls; a plain "Suspense"
# account is one too, but a tax-clearing account like "GST/HST Suspense" or
# "Receiver General Suspense" is legitimate and must not be swept in — so "suspense"
# only counts when it stands alone. The broad form is used only as a cheap file
# pre-filter; membership is always decided by the exact rule.
_UNCAT_NAME = re.compile(r"uncateg|ask my accountant", re.I)
_UNCAT_QUICK = re.compile(r"uncateg|suspense|ask my accountant", re.I)


def _is_catchall(name: str) -> bool:
    low = (name or "").strip().lower()
    return bool(_UNCAT_NAME.search(low)) or low == "suspense"


# QuickBooks and Xero spread postings across different files. Reports (general
# ledger, trial balance) and master data (accounts, contacts) are left out, as is
# billable_account_expenses — a duplicate view of purchases/bills/journals that
# would double-count. Only files carrying original coded lines are read.
_QBO_TXN = {"purchase", "bill", "journal_entry", "deposit", "invoice",
            "sales_receipt", "credit_memo", "vendor_credit", "transfer",
            "credit_card_payment", "bill_payment", "payment"}
_XERO_TXN = {"transactions", "invoices", "manual_journals", "credit_notes"}


def _txn_base(filename: str) -> str:
    """'transactions_3.jsonl' -> 'transactions' — undo the paging suffix."""
    stem = filename[:-6] if filename.endswith(".jsonl") else filename
    return re.sub(r"_\d+$", "", stem)


def _num(v: object) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _first_acctref(node: object, ids: set) -> str | None:
    """First *AccountRef within a QuickBooks line/transaction whose account is a
    catch-all, or None. The ref sits inside a *LineDetail, so this walks down."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                if (key.endswith("AccountRef") and isinstance(val, dict)
                        and str(val.get("value")) in ids):
                    return str(val.get("value"))
                stack.append(val)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _uncat_detail(t: dict, source: str, ids: set) -> tuple[str | None, float]:
    """(catch-all account touched, amount posted to catch-all accounts on this txn).

    The amount is summed from the coded lines — a Xero LineItem's LineAmount, or
    a QuickBooks line's Amount beside its AccountRef — rather than a transaction
    total, which journal entries do not carry."""
    code0: str | None = None
    total = 0.0
    if source == "xero":
        for li in t.get("LineItems") or []:
            if isinstance(li, dict) and str(li.get("AccountCode")) in ids:
                code0 = code0 or str(li.get("AccountCode"))
                total += _num(li.get("LineAmount"))
    else:
        lines = t.get("Line")
        # Most QBO transactions carry a Line[]; transfers hold the refs at the top.
        rows = lines if isinstance(lines, list) else [t]
        for ln in rows:
            hit = _first_acctref(ln, ids)
            if hit:
                code0 = code0 or hit
                total += _num(ln.get("Amount") if isinstance(ln, dict) else None)
    return code0, round(total, 2)


def _txn_contact(t: dict) -> str | None:
    for ref in (t.get("Contact"), t.get("EntityRef"), t.get("VendorRef")):
        if isinstance(ref, dict):
            name = ref.get("Name") or ref.get("name")
            if name:
                return name
    return None


def _qbo_pl_net(text: str) -> tuple[dict[str, float], tuple]:
    """{account id: net amount} from a QuickBooks Profit & Loss report, plus its
    (start, end) period. P&L accounts always report a $0 CurrentBalance, so their
    real movement has to come from this report — which tags each account row with
    its id, making the match exact."""
    net: dict[str, float] = {}
    period = (None, None)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rep = json.loads(line)
        except ValueError:
            continue
        h = rep.get("Header", {})
        period = (h.get("StartPeriod"), h.get("EndPeriod"))
        stack = [rep.get("Rows")]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                cd = cur.get("ColData")
                if isinstance(cd, list) and cd and cd[0].get("id"):
                    try:
                        net[str(cd[0]["id"])] = float(cd[-1].get("value") or 0)
                    except (TypeError, ValueError):
                        pass
                for v in cur.values():
                    stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
    return net, period


# QBO account types that live on the Profit & Loss (CurrentBalance is always 0);
# everything else is a balance-sheet account whose CurrentBalance is real.
_QBO_PL_TYPES = {"Income", "Other Income", "Cost of Goods Sold",
                 "Expense", "Other Expense"}


def _uncategorized_scan(store: "UserStore", organisation: str, backup_date: str) -> dict:
    """The shared core of find_uncategorized: catch-all accounts and their net
    unresolved balance for one organisation. Returns structured data used by both
    the single-org report and the portfolio sweep. `no_chart` marks a backup with
    no chart of accounts; otherwise `total` is the net still to clear."""
    date = store.resolve_date(organisation, backup_date)
    present = {f.name for f in store.files(organisation, date)}
    if "account.jsonl" in present:
        source, acct_file = "qbo", "account.jsonl"
    elif "accounts.jsonl" in present:
        source, acct_file = "xero", "accounts.jsonl"
    else:
        return {"date": date, "no_chart": True}

    def read(name: str) -> str:
        return store.fetch(store.object_for(organisation, date, name)).decode(
            "utf-8", "replace"
        )

    # 1) catch-all accounts (active or not) -> their type and reported balance.
    accts: dict[str, dict] = {}
    for line in read(acct_file).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            a = json.loads(line)
        except ValueError:
            continue
        name = a.get("Name") or ""
        if _is_catchall(name):
            key = str(a.get("Id") if source == "qbo" else a.get("Code"))
            accts[key] = {
                "name": name,
                "type": a.get("AccountType") or a.get("Class") or a.get("Type") or "",
                "balance": a.get("CurrentBalance"),
            }
    if not accts:
        return {"date": date, "no_chart": False, "source": source, "accts": {},
                "unresolved": [], "total": 0.0, "reclassified": [], "deleted": 0,
                "skipped": [], "pl_period": (None, None), "no_pl": False}
    ids = set(accts)

    # 2) postings to those accounts, bucketed per account (candidates; and, for
    #    Xero, the net itself since its lines already carry the current coding).
    tx_set = _QBO_TXN if source == "qbo" else _XERO_TXN
    hits: dict[str, list[dict]] = {k: [] for k in accts}
    deleted = 0
    skipped: list[str] = []
    for name in sorted(f for f in present
                       if f.endswith(".jsonl") and _txn_base(f) in tx_set):
        try:
            txt = read(name)
        except Exception:  # over the inline-size cap, or a transient S3 error
            skipped.append(name)
            continue
        # Cheap skip: QBO refs embed the label; Xero lines carry only the code.
        if source == "qbo":
            if not _UNCAT_QUICK.search(txt):
                continue
        elif not any(f'"{code}"' in txt for code in ids):
            continue
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except ValueError:
                continue
            code, posted = _uncat_detail(t, source, ids)
            if code is None:
                continue
            if (t.get("Status") or t.get("status") or "").upper() in ("DELETED", "VOIDED"):
                deleted += 1
                continue
            hits[code].append({
                "date": (t.get("DateString") or t.get("Date") or t.get("TxnDate") or "")[:10],
                "type": t.get("Type") or _txn_base(name).replace("_", " ").title(),
                "amount": posted,
                "contact": _txn_contact(t),
                "id": t.get("BankTransactionID") or t.get("InvoiceID")
                      or t.get("ManualJournalID") or t.get("Id"),
                "file": name,
            })

    # 3) net balance per account — the authoritative "still unresolved" figure.
    pl_net: dict[str, float] = {}
    pl_period = (None, None)
    if source == "qbo" and "profit_and_loss.jsonl" in present:
        pl_net, pl_period = _qbo_pl_net(read("profit_and_loss.jsonl"))

    unresolved: list[tuple] = []   # (name, type, net, candidates)
    reclassified: list[str] = []   # postings exist but net to zero
    for key, info in accts.items():
        cand = hits.get(key, [])
        if source == "qbo":
            net = pl_net.get(key, 0.0) if info["type"] in _QBO_PL_TYPES \
                else _num(info["balance"])
        else:  # Xero lines are the current coding, so they are the net.
            net = sum(_num(c["amount"]) for c in cand)
        net = round(net, 2)
        if abs(net) >= 0.01:
            unresolved.append((info["name"], info["type"], net, cand))
        elif cand:
            reclassified.append(info["name"])

    total = round(sum(n for _, _, n, _ in unresolved), 2)
    return {"date": date, "no_chart": False, "source": source, "accts": accts,
            "unresolved": unresolved, "total": total, "reclassified": reclassified,
            "deleted": deleted, "skipped": skipped, "pl_period": pl_period,
            "no_pl": source == "qbo" and "profit_and_loss.jsonl" not in present}


@mcp.tool(name="find_uncategorized_balances")
def find_uncategorized(
    organisation: str = "", backup_date: str = "latest", limit: int = 100,
    organisations: str = "",
) -> str:
    """Report money still parked in an Uncategorized / Suspense / "Ask My
    Accountant" account — what a bookkeeper still has to clear.

    The honest figure is the NET balance left in each catch-all account, not a
    count of transactions that touched it: in QuickBooks a reclassification is a
    new journal entry that leaves the original posting in place, so counting
    postings double-counts anything already moved out. So for QuickBooks this
    reads each account's real balance — the all-time Profit & Loss for P&L
    accounts, the CurrentBalance for balance-sheet accounts — and flags only
    accounts with a non-zero net. Xero recodes in place, so its lines already
    reflect current coding and are used directly.

    Individual postings are listed under each flagged account as candidates; for
    QuickBooks a later entry may offset some, so the net shown is what remains.
    QuickBooks bank-feed "For Review" items are not part of a backup, so nothing
    here can surface those.

    Set organisations="*" (or a comma-separated list) for the portfolio view —
    the net still-to-clear per organisation, biggest first — so a bookkeeping
    firm sees where the clean-up work is across every client in one call.

    Args:
        organisation: Organisation name as shown by `browse`. Omit when using
            `organisations`.
        backup_date: A date from `browse`, or "latest".
        limit: Most candidate postings to list per account (single-org only).
        organisations: "*" for every organisation, or a comma-separated list, to
            sweep the portfolio and rank each one's net unresolved balance.
    """
    current_principal().require("backups:read")
    # Accept the portfolio request in either field: organisations="*", or "*"/
    # "all"/a comma-list typed into the singular organisation.
    org_single = organisation.strip()
    to_sweep = org_single.lower() in ("*", "all") or "," in org_single
    sweep_spec = organisations.strip() or (org_single if to_sweep else "")
    if sweep_spec:
        return _uncategorized_sweep(sweep_spec, backup_date)
    if not organisation.strip():
        raise ValueError(
            'give an organisation, or organisations="*" to sweep the portfolio'
        )

    store = _store()
    scan = _uncategorized_scan(store, organisation, backup_date)
    date = scan["date"]
    if scan.get("no_chart"):
        return f"Backup {date} for {organisation} has no chart of accounts to check."
    source = scan["source"]
    accts = scan["accts"]
    if not accts:
        return (
            f"{organisation} / {date}\n"
            "No Uncategorized, Suspense or Ask-My-Accountant account exists in "
            "this chart, so nothing can be parked as uncategorized."
        )
    unresolved = scan["unresolved"]
    reclassified = scan["reclassified"]
    deleted = scan["deleted"]
    skipped = scan["skipped"]
    pl_period = scan["pl_period"]

    head = f"{organisation} / {date}"
    if source == "qbo" and pl_period[0]:
        head += f"   (P&L basis {pl_period[0]} → {pl_period[1]})"
    elif scan["no_pl"]:
        # No P&L means P&L-account nets can't be verified — say so rather than
        # silently reporting them as zero.
        head += ("\n⚠ no profit_and_loss.jsonl in this backup — nets for P&L "
                 "accounts (Uncategorized Income/Expense, income-type Suspense) "
                 "could not be verified; only balance-sheet accounts and the "
                 "candidate postings below are reliable here")
    head += ("\nCatch-all accounts checked: "
             + ", ".join(sorted(a["name"] for a in accts.values())))
    if skipped:
        head += f"\nnot read (too large to read inline): {', '.join(skipped)}"

    def offsets_note() -> str:
        if not reclassified:
            return ""
        return ("\nPostings exist in " + ", ".join(sorted(set(reclassified)))
                + " but net to zero — posted then reclassified, nothing to clear.")

    if not unresolved:
        tail = offsets_note()
        if deleted:
            tail += f"\n({deleted} deleted/void posting(s) ignored.)"
        return f"{head}\n\nNo unresolved balance in any catch-all account.{tail}"

    total = scan["total"]
    parts = [head,
             f"\n{len(unresolved)} account(s) hold an unresolved balance, "
             f"{total:,.2f} in total:"]
    for nm, typ, net, cand in sorted(unresolved, key=lambda r: -abs(r[2])):
        parts.append(f"\n▸ {nm} ({typ}) — net {net:,.2f}")
        if cand:
            off = (" (a later entry may offset some; the net above is what remains)"
                   if source == "qbo" else "")
            parts.append(f"  {len(cand)} posting(s){off}:")
            parts.append(json.dumps(cand[:limit], indent=2, default=str))
            if len(cand) > limit:
                parts.append(f"  [showing {limit} of {len(cand)}]")
    note = offsets_note()
    if note:
        parts.append(note)
    if deleted:
        parts.append(f"({deleted} deleted/void posting(s) ignored.)")
    return "\n".join(parts)


def _uncategorized_sweep(organisations: str, backup_date: str) -> str:
    """Portfolio view: the net unresolved catch-all balance per organisation,
    biggest first, so a bookkeeping firm sees where the clean-up work is."""
    settings = get_settings()
    store = _store()
    available = store.organisations()
    spec = organisations.strip()
    if spec and spec not in ("*", "all"):
        wanted = [name.strip() for name in spec.split(",") if name.strip()]
        unknown = [n for n in wanted if n not in available]
        if unknown:
            raise PermissionError("not on this account: " + ", ".join(unknown))
    else:
        wanted = available
    capped = wanted[: settings.max_organisations_per_sweep]

    def scan(name: str):
        try:
            s = _uncategorized_scan(_store(), name, backup_date)
            if s.get("no_chart"):
                return name, None, None, None, "no chart of accounts"
            return (name, s["date"], s["total"], len(s["unresolved"]),
                    "no P&L — P&L nets unverified" if s["no_pl"] else None)
        except Exception as exc:
            return name, None, None, None, _short_error(exc)

    collected = []
    workers = max(1, min(settings.sweep_concurrency, len(capped) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, scan, name)
                   for name in capped]
        for future in futures:
            collected.append(future.result())

    lines = [f"Unresolved catch-all balance across {len(capped)} organisation(s):",
             "",
             f"  {'organisation':40} {'backup':12} {'accounts':>9} {'net to clear':>16}"]
    rows = []
    skipped = []
    for name, date, total, n_acct, warn in collected:
        if total is None:
            skipped.append((name, warn))
        else:
            rows.append((name, date, total, n_acct, f"  ⚠ {warn}" if warn else ""))
    rows.sort(key=lambda r: -abs(r[2]))          # biggest balances first
    for name, date, total, n_acct, flag in rows:
        lines.append(
            f"  {name[:40]:40} {str(date):12} {n_acct:>9} {total:>16,.2f}{flag}")
    if not rows:
        lines.append("  (no organisation reported an unresolved balance)")
    if skipped:
        lines += ["", f"not checked ({len(skipped)}):"] + _group_skips(skipped)
    if len(wanted) > len(capped):
        lines += ["", f"[covered {len(capped)} of {len(wanted)} organisations — "
                  f"name the ones you want in `organisations` for the rest]"]
    return "\n".join(lines)
