"""
SEC EDGAR loader (10-K and 20-F, IFRS or US GAAP) built on the filing's own inline-XBRL facts.

Design rules (these exist because the previous loader silently substituted hardcoded numbers
whenever a label lookup failed, so a "live" run could show entirely fabricated figures):

  1. NO DEFAULTS. A value that cannot be found is None with an explanatory provenance note.
  2. Match on XBRL CONCEPT names (e.g. ifrs-full:Assets), never on display labels.
  3. UNITS come from the filing: each fact carries scale="6" etc. Values are converted to full
     currency units exactly once, here.
  4. Only the primary fiscal period, and only facts with NO dimensional qualifiers (consolidated
     totals, not segment / equity-component slices).
  5. Every field records provenance: reported | derived | none_reported | missing.
  6. Nothing is invented: no retained-earnings, market-cap or repayment "estimates".
"""
import datetime as dt
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from lxml import etree

from src import narrative
from src.policy import POLICY

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_ARCHIVE_URL ="https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
ANNUAL_FORMS = ("10-K", "20-F")


class UnsupportedEntityError(ValueError):
    """The registrant is outside what this engine can underwrite (e.g. a bank or insurer)."""


FINANCIAL_SIC_RANGE = (6000, 6411)        # depository & credit institutions, brokers, insurers, insurance agents
FINANCIAL_ONLY_CONCEPTS = ("us-gaap:Deposits", "us-gaap:InterestAndDividendIncomeOperating",
                           "us-gaap:PolicyholderBenefitsAndClaimsIncurredNet", "us-gaap:PremiumsEarnedNet",
                           "ifrs-full:DepositsFromBanks", "ifrs-full:DepositsFromCustomers")


def _financial_message(why: str) -> str:
    return (f"Financial institutions (banks, insurers, brokers) are not supported ({why}). This engine underwrites "
            "non-financial corporates: for a bank, debt and deposits are the product, so debt-to-equity, EBIT-based "
            "coverage and Altman Z'' do not measure credit risk. Bank underwriting needs its own ratio set "
            "(capital adequacy, asset quality, liquidity).")


def check_supported_sic(sic: Any, sic_description: str = "") -> None:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return
    if FINANCIAL_SIC_RANGE[0] <= code <= FINANCIAL_SIC_RANGE[1]:
        raise UnsupportedEntityError(_financial_message(f"SEC industry code {code} {sic_description}".strip()))
PLACEHOLDER_IDENTITY = "Credit-Underwriter-Demo contact@example.com"

# --------------------------------------------------------------------------------------
# Concept map. An "alternative" is either one concept or a tuple of concepts that are
# summed (all must be present). The first alternative that resolves wins; alternatives are
# never mixed, which avoids double counting.
# --------------------------------------------------------------------------------------
Alt = Union[str, Tuple[str, ...]]

INSTANT = "instant"
DURATION = "duration"

REQUIRED_CONCEPTS: Dict[str, Tuple[str, List[Alt]]] = {
    "total_assets": (INSTANT, ["ifrs-full:Assets", "us-gaap:Assets"]),
    "shareholder_equity": (INSTANT, [
        "ifrs-full:Equity",
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "us-gaap:StockholdersEquity"]),
    "current_assets": (INSTANT, ["ifrs-full:CurrentAssets", "us-gaap:AssetsCurrent"]),
    "current_liabilities": (INSTANT, ["ifrs-full:CurrentLiabilities", "us-gaap:LiabilitiesCurrent"]),
    "retained_earnings": (INSTANT, ["ifrs-full:RetainedEarnings", "us-gaap:RetainedEarningsAccumulatedDeficit"]),
    "sales": (DURATION, [
        "ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers",
        "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:SalesRevenueNet",
        "us-gaap:SalesRevenueGoodsNet", "us-gaap:RevenuesNetOfInterestExpense",
        "us-gaap:RegulatedAndUnregulatedOperatingRevenue", "us-gaap:SalesRevenueServicesNet"]),
    "ebit": (DURATION, ["ifrs-full:ProfitLossFromOperatingActivities", "us-gaap:OperatingIncomeLoss"]),
}
TOTAL_LIABILITIES_ALTS: List[Alt] = ["ifrs-full:Liabilities", "us-gaap:Liabilities"]
EQUITY_AND_LIABILITIES_ALTS: List[Alt] = ["ifrs-full:EquityAndLiabilities", "us-gaap:LiabilitiesAndStockholdersEquity"]

# Financial debt = borrowings + lease liabilities. (US GAAP operating leases are excluded because
# their cost is already deducted in EBIT; IFRS 16 leases are all on balance sheet, so included.)
DEBT_COMPONENTS: Dict[str, List[Alt]] = {
    "borrowings": [
        "ifrs-full:Borrowings",
        "us-gaap:DebtLongtermAndShorttermCombinedAmount",
        "us-gaap:LongTermDebt",
        ("us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtCurrent"),
        ("us-gaap:LongTermDebtNoncurrent", "us-gaap:DebtCurrent"),
        "us-gaap:DebtAndCapitalLeaseObligations",
        ("us-gaap:LongTermDebtAndCapitalLeaseObligations", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent"),
        # Last resort: only a non-current balance is tagged (no current portion, no total) - e.g. Meta.
        "us-gaap:LongTermDebtNoncurrent"],
    "lease_liabilities": [
        "ifrs-full:LeaseLiabilities",
        ("ifrs-full:CurrentLeaseLiabilities", "ifrs-full:NoncurrentLeaseLiabilities"),
        "us-gaap:FinanceLeaseLiability",
        ("us-gaap:FinanceLeaseLiabilityCurrent", "us-gaap:FinanceLeaseLiabilityNoncurrent")],
    # US GAAP short-term funding (e.g. Apple's commercial paper). Rolled over rather than repaid on
    # a schedule, so it carries no principal-repayment requirement (see SHORT_TERM_COMPONENTS).
    "short_term_borrowings": [
        "us-gaap:ShortTermBorrowings",
        "us-gaap:CommercialPaper",
        "us-gaap:OtherShortTermBorrowings",
        "us-gaap:ShortTermBankLoansAndNotesPayable"],
}
SHORT_TERM_COMPONENTS = {"short_term_borrowings"}
# If borrowings already resolved from one of these all-in totals, short-term debt is inside it.
COMBINED_DEBT_CONCEPTS = {"ifrs-full:Borrowings", "us-gaap:DebtLongtermAndShorttermCombinedAmount",
                          "us-gaap:DebtAndCapitalLeaseObligations"}
# Debt totals that already include finance leases (so the lease component must not be added again).
LEASE_INCLUSIVE_CONCEPTS = {"us-gaap:DebtAndCapitalLeaseObligations", "us-gaap:LongTermDebtAndCapitalLeaseObligations"}
# Concepts recognised as components/subtotals of debt: when unused they are expected, so no warning.
# (DebtCurrent is deliberately NOT silent: it can hold short-term borrowings outside LongTermDebt.)
SILENT_SUBSETS = {c for alts in DEBT_COMPONENTS.values() for a in alts
                  for c in ((a,) if isinstance(a, str) else a)} - {"us-gaap:DebtCurrent"}

# Used only when no standard tag exists; every use is logged as a warning and shown in provenance.
INTEREST_FALLBACK = re.compile(r"^(Interest.*(Expense|Paid)|.*FinanceCosts?)$")
INTEREST_FALLBACK_EXCLUDE = re.compile(r"(Income|Capitalized|Tax|Unrecognized|FairValue|Receivable|Lease|Pension|"
                                       r"Benefit|Accrued|Discontinued|Operating)")
REPAYMENT_FALLBACK = re.compile(r"^Repayments?Of.*(Debt|Notes|Borrowing|Loan|Credit|Convertible|Obligation|Financing)")
REPAYMENT_FALLBACK_EXCLUDE = re.compile(r"(Maturit|Equity|Stock|Dividend|Related|Affiliate|Subsidiar|Parent)")
CURRENT_MATURITY_ALTS: Dict[str, List[Alt]] = {
    "borrowings": ["us-gaap:LongTermDebtCurrent", "us-gaap:DebtCurrent",
                   "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
                   "ifrs-full:CurrentPortionOfLongtermBorrowings",
                   "ifrs-full:CurrentBorrowingsAndCurrentPortionOfNonCurrentBorrowings"],
    "lease_liabilities": ["us-gaap:FinanceLeaseLiabilityCurrent", "ifrs-full:CurrentLeaseLiabilities"],
}
# Operating profit missing (banks, insurers, some industrials): EBIT = pre-tax income + interest expense.
PRETAX_ALTS: List[Alt] = [
    "ifrs-full:ProfitLossBeforeTax",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"]
REPAYMENT_CONCEPTS: Dict[str, List[Alt]] = {
    "borrowings": ["ifrs-full:RepaymentsOfBorrowingsClassifiedAsFinancingActivities",
                   "ifrs-full:RepaymentsOfBorrowings",
                   "us-gaap:RepaymentsOfLongTermDebt", "us-gaap:RepaymentsOfDebt",
                   "us-gaap:RepaymentsOfDebtMaturingInMoreThanThreeMonths",
                   "us-gaap:RepaymentsOfSeniorDebt", "us-gaap:RepaymentsOfNotesPayable",
                   "us-gaap:RepaymentsOfDebtAndCapitalLeaseObligations"],
    "lease_liabilities": ["ifrs-full:PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities",
                          "us-gaap:FinanceLeasePrincipalPayments"],
}
INTEREST_ALTS: List[Alt] = ["ifrs-full:FinanceCosts", "us-gaap:InterestExpense",
                            "us-gaap:InterestExpenseNonoperating", "us-gaap:InterestExpenseDebt"]
GOODWILL_ALTS: List[Alt] = ["ifrs-full:Goodwill", "us-gaap:Goodwill"]
# Indefinite-lived trademarks are intangibles too (Coca-Cola: $12.5B, not subtracted before this fix, which
# understated TOL/TNW). Totals first; component sums only when no total is tagged.
INTANGIBLE_ALTS: List[Alt] = [
    "ifrs-full:IntangibleAssetsOtherThanGoodwill",
    "us-gaap:IntangibleAssetsNetExcludingGoodwill",
    ("us-gaap:IndefiniteLivedIntangibleAssetsExcludingGoodwill", "us-gaap:FiniteLivedIntangibleAssetsNet"),
    ("us-gaap:IndefiniteLivedTrademarks", "us-gaap:FiniteLivedIntangibleAssetsNet"),
    "us-gaap:IndefiniteLivedIntangibleAssetsExcludingGoodwill",
    "us-gaap:IndefiniteLivedTrademarks",
    "us-gaap:FiniteLivedIntangibleAssetsNet",
]
INTANGIBLE_LIKE = re.compile(r"Intangible|Trademark|TradeName", re.I)
INTANGIBLE_LIKE_EXCLUDE = re.compile(r"Amortiz|Impair|Expense|Gross|Accumulated|Acquired|Useful|Additions|Disposal|Fair|"
                                     r"Purchase|Business|Increase|Decrease|Tax|Deferred|Future|Estimated|Written|Writeoff|"
                                     r"Rollforward|Gain|Loss|Income|Cash", re.I)
CASH_ALTS: List[Alt] = ["ifrs-full:CashAndCashEquivalents", "us-gaap:CashAndCashEquivalentsAtCarryingValue"]
CURRENT_INVESTMENT_ALTS: List[Alt] = ["ifrs-full:CurrentInvestments", "us-gaap:ShortTermInvestments",
                                      "us-gaap:MarketableSecuritiesCurrent"]

# Safety net against silently understating debt: any balance-sheet concept that looks like debt
# but is not one of the concepts above makes financial_debt "missing" (-> blocks the decision)
# until an analyst confirms the figure.
DEBT_LIKE = re.compile(r"(Borrowings|LongTermDebt|LongtermDebt|ShortTermDebt|ShorttermDebt|DebtCurrent|"
                       r"DebtNoncurrent|NotesPayable|ConvertibleNotes|ConvertibleDebt|LineOfCredit|"
                       r"CommercialPaper|SeniorNotes|SeniorDebt)")
DEBT_LIKE_EXCLUDE = re.compile(r"(Undiscounted|Maturit|FairValue|Securit|Held|Investment|Gain|Loss|"
                               r"Expense|Rate|Percent|Maximum|Available|Capacity|Proceeds|Repayment|"
                               r"Payment|Issuance|Interest)")


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
def _identity() -> Tuple[str, bool]:
    ident = (os.getenv("SEC_IDENTITY") or "").strip()
    return (ident, True) if ident else (PLACEHOLDER_IDENTITY, False)


def _http_get(url: str) -> bytes:
    ua, _ = _identity()
    resp = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=90)
    resp.raise_for_status()
    return resp.content


def resolve_cik(ticker_or_cik: str) -> Tuple[int, str]:
    """Ticker (or numeric CIK) -> (cik, registrant name)."""
    token = ticker_or_cik.strip().upper()
    if token.isdigit():
        cik = int(token)
        subs = json.loads(_http_get(SEC_SUBMISSIONS_URL.format(cik=cik)))
        return cik, subs.get("name", token)
    data = json.loads(_http_get(SEC_TICKER_MAP_URL))
    for row in data.values():
        if str(row.get("ticker", "")).upper() == token:
            return int(row["cik_str"]), row.get("title", token)
    raise ValueError(f"Ticker '{token}' was not found in the SEC ticker map. "
                     "Enter the numeric CIK instead (EDGAR company search shows it).")


def latest_annual_filing(cik: int) -> Dict[str, str]:
    subs = json.loads(_http_get(SEC_SUBMISSIONS_URL.format(cik=cik)))
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    rows = []
    for i, form in enumerate(forms):
        if form in ANNUAL_FORMS:
            rows.append({
                "form": form,
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": recent.get("reportDate", [""] * len(forms))[i],
                "primary_doc": recent["primaryDocument"][i],
            })
    if not rows:
        raise ValueError(f"No {' or '.join(ANNUAL_FORMS)} annual filing found for CIK {cik}.")
    filing = max(rows, key=lambda r: r["filing_date"])
    filing["sic"], filing["sic_description"] = subs.get("sic"), subs.get("sicDescription", "")
    filing["url"] = SEC_ARCHIVE_URL.format(cik=cik, acc_nodash=filing["accession"].replace("-", ""),
                                           doc=filing["primary_doc"])
    return filing


# --------------------------------------------------------------------------------------
# Inline XBRL parsing
# --------------------------------------------------------------------------------------
def _split(tag: Any) -> Tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


NUMBER_WORDS = {"no": 0, "none": 0, "nil": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _parse_value(text: str, scale: Optional[str], sign: Optional[str], fmt: Optional[str]) -> Optional[float]:
    t = (text or "").strip()
    fmt = (fmt or "").lower()
    if "numwords" in fmt:                      # ixt-sec:numwordsen, e.g. "no", "two"
        if t.lower() not in NUMBER_WORDS:
            return None
        value = float(NUMBER_WORDS[t.lower()])
    elif "zero" in fmt or t in ("", "-", "—", "–"):
        value = 0.0
    else:
        t = t.replace(" ", "").replace("(", "").replace(")", "")
        if "comma" in fmt and "decimal" in fmt and "dot" not in fmt:  # 1.234,5 style
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
        try:
            value = float(t)
        except ValueError:
            return None
    value *= 10 ** int(scale or 0)
    return -value if sign == "-" else value


def parse_ixbrl(content: bytes) -> Dict[str, Any]:
    """Extract contexts, units and numeric facts from an inline-XBRL (XHTML) document."""
    contexts: Dict[str, Dict[str, Any]] = {}
    units: Dict[str, str] = {}
    facts: List[Dict[str, Any]] = []
    dei: Dict[str, str] = {}

    wanted = ("{*}context", "{*}unit", "{*}nonFraction", "{*}nonNumeric")
    for _, el in etree.iterparse(io.BytesIO(content), events=("end",), tag=wanted,
                                 huge_tree=True, recover=True):
        ns, local = _split(el.tag)
        if local == "context" and ns.endswith("/instance"):
            ctx = {"instant": None, "start": None, "end": None, "dims": 0}
            for sub in el.iter():
                sns, sl = _split(sub.tag)
                if sl == "instant":
                    ctx["instant"] = (sub.text or "").strip()
                elif sl == "startDate":
                    ctx["start"] = (sub.text or "").strip()
                elif sl == "endDate":
                    ctx["end"] = (sub.text or "").strip()
                elif sl in ("explicitMember", "typedMember"):
                    ctx["dims"] += 1
            contexts[el.get("id")] = ctx
        elif local == "unit" and ns.endswith("/instance"):
            measures = [(m.text or "").strip() for m in el.iter() if _split(m.tag)[1] == "measure"]
            units[el.get("id")] = "/".join(measures)
        elif local == "nonNumeric" and "inlineXBRL" in ns:
            name = el.get("name", "")
            if name.startswith("dei:"):
                dei[name] = "".join(el.itertext()).strip()
        elif local == "nonFraction" and "inlineXBRL" in ns:
            if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
                continue
            # A fact that wraps another numeric fact would concatenate both texts into a wrong value.
            # Skip the outer one (the inner facts are parsed on their own); worst case a needed value
            # becomes "missing", which blocks the decision instead of silently mis-stating it.
            if any(_split(d.tag)[1] == "nonFraction" for d in el.iterdescendants()):
                continue
            value = _parse_value("".join(el.itertext()), el.get("scale"), el.get("sign"), el.get("format"))
            if value is None:
                continue
            facts.append({"name": el.get("name"), "context": el.get("contextRef"),
                          "unit": el.get("unitRef"), "value": value})
    return {"contexts": contexts, "units": units, "facts": facts, "dei": dei}


def _currency(unit_measure: Optional[str]) -> Optional[str]:
    if unit_measure and unit_measure.startswith("iso4217:") and "/" not in unit_measure:
        return unit_measure.split(":", 1)[1]
    return None


def _index_facts(parsed: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """concept -> list of undimensioned monetary facts with resolved period and currency."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for f in parsed["facts"]:
        ctx = parsed["contexts"].get(f["context"])
        if not ctx or ctx["dims"] != 0:
            continue
        cur = _currency(parsed["units"].get(f["unit"]))
        if cur is None:
            continue
        index.setdefault(f["name"], []).append({
            "value": f["value"], "instant": ctx["instant"], "start": ctx["start"],
            "end": ctx["end"], "currency": cur})
    return index


def _fiscal_year_end(index: Dict[str, List[Dict[str, Any]]], dei: Dict[str, str]) -> Optional[str]:
    instants = [f["instant"] for c in ("ifrs-full:Assets", "us-gaap:Assets") for f in index.get(c, []) if f["instant"]]
    if instants:
        return max(instants)
    hint = dei.get("dei:DocumentPeriodEndDate", "")
    try:
        return dt.datetime.strptime(hint, "%B %d, %Y").date().isoformat()
    except ValueError:
        return hint if re.fullmatch(r"\d{4}-\d{2}-\d{2}", hint or "") else None


def _lookup(index, concept: str, kind: str, fye: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return (fact, conflict). Conflict = two different values for the same concept and period."""
    hits = []
    for f in index.get(concept, []):
        if kind == INSTANT:
            if f["instant"] == fye:
                hits.append(f)
        elif f["end"] == fye and f["start"]:
            days = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
            if 350 <= days <= 380:
                hits.append(f)
    if not hits:
        return None, False
    return hits[0], len({round(h["value"], 2) for h in hits}) > 1


def _resolve(index, alts: List[Alt], kind: str, fye: str):
    """Resolve the first alternative that fully resolves.
    Returns (value, concepts_used, currency, conflict_concept)."""
    for alt in alts:
        parts = (alt,) if isinstance(alt, str) else alt
        found, currencies = [], set()
        for concept in parts:
            fact, conflict = _lookup(index, concept, kind, fye)
            if conflict:
                return None, [concept], None, concept
            if fact is None:
                break
            found.append((concept, fact["value"]))
            currencies.add(fact["currency"])
        else:
            if len(currencies) > 1:
                return None, list(parts), None, "mixed currencies"
            return sum(v for _, v in found), [c for c, _ in found], currencies.pop(), None
    return None, [], None, None


def _prov(status: str, concepts: List[str], period: str, note: str = "", **extra) -> Dict[str, Any]:
    return {"status": status, "concept": " + ".join(concepts) if concepts else None,
            "period": period, "note": note, **extra}


def _unmapped_debt_like(index, fye: str, used: set) -> List[Tuple[str, float]]:
    """Debt-like instant facts at year end that were NOT used in the debt total: [(concept, value)]."""
    hits = []
    for concept, facts in index.items():
        ns, _, local = concept.partition(":")
        if ns not in ("ifrs-full", "us-gaap") or concept in used:
            continue
        if DEBT_LIKE.search(local) and not DEBT_LIKE_EXCLUDE.search(local):
            vals = [f["value"] for f in facts if f["instant"] == fye and f["value"] > 0]
            if vals:
                hits.append((concept, max(vals)))
    return sorted(hits)


def _largest_matching(index, fye: str, kind: str, include, exclude):
    """(concept, fact) of the largest-magnitude consolidated fact whose name matches; else None."""
    best = None
    for concept in index:
        local = concept.partition(":")[2] or concept
        if not include.search(local) or exclude.search(local):
            continue
        fact, conflict = _lookup(index, concept, kind, fye)
        if fact is None or conflict or fact["value"] == 0:
            continue
        if best is None or abs(fact["value"]) > abs(best[1]["value"]):
            best = (concept, fact)
    return best


def _principal_fallback(index, fye: str, component: str):
    """Principal for a component with no standard repayment tag -> (value, concepts, note) or (None, [], '')."""
    if component == "borrowings":
        found = []
        for concept in index:
            local = concept.partition(":")[2] or concept
            if REPAYMENT_FALLBACK.search(local) and not REPAYMENT_FALLBACK_EXCLUDE.search(local):
                fact, conflict = _lookup(index, concept, DURATION, fye)
                if fact is not None and not conflict and fact["value"] != 0:
                    found.append((concept, abs(fact["value"])))
        if found:
            return (sum(v for _, v in found), [c for c, _ in found],
                    "repayments inferred by summing " + ", ".join(f"{c.partition(':')[2]}" for c, _ in found)
                    + " (no standard repayment tag; may double count, which is the conservative direction)")
    fact_alts = CURRENT_MATURITY_ALTS.get(component, [])
    v, concepts, _, conflict = _resolve(index, fact_alts, INSTANT, fye)
    if v is not None and not conflict and v > 0:
        return (v, concepts, f"no repayment fact found; scheduled principal proxied by current maturities "
                             f"({concepts[0].partition(':')[2]} = {v:,.0f})")
    return None, [], ""


# Where to look for a replacement when an input is missing. The analyst picks one in the UI
# (with a reason); nothing is ever chosen automatically.
CANDIDATE_RULES: Dict[str, Tuple[str, re.Pattern, re.Pattern]] = {
    # field: (kind, include, exclude)
    "total_assets": (INSTANT, re.compile(r"Assets$"), re.compile(r"(Other|Noncurrent|Current|Net|Fair|Deferred)")),
    "total_liabilities": (INSTANT, re.compile(r"Liabilities"), re.compile(r"(Current|Other|Deferred|Lease|Fair|Equity)")),
    "shareholder_equity": (INSTANT, re.compile(r"Equity"), re.compile(r"(Liabilities|Other|Fair|Shares|Stock$)")),
    "current_assets": (INSTANT, re.compile(r"(AssetsCurrent|CurrentAssets)"), re.compile(r"Other")),
    "current_liabilities": (INSTANT, re.compile(r"(LiabilitiesCurrent|CurrentLiabilities)"), re.compile(r"Other")),
    "retained_earnings": (INSTANT, re.compile(r"(Retained|AccumulatedDeficit)"), re.compile(r"Other")),
    "sales": (DURATION, re.compile(r"(Revenue|Sales)"), re.compile(r"(Cost|Deferred|Percent|Unearned|Other)")),
    "ebit": (DURATION, re.compile(r"(OperatingIncome|OperatingProfit|ProfitLossFromOperating)"), re.compile(r"Other")),
    "financial_debt": (INSTANT, re.compile(r"(Debt|Borrow|Notes|LeaseLiab|CommercialPaper|LineOfCredit|Lease.*Liabilit)"),
                       re.compile(r"(Maturit|Undiscounted|FairValue|Securit|Gain|Loss|Expense|Interest|Repayment|"
                                  r"Proceeds|Payment|Issuance|Rate|Percent|Capacity|Available|Receivable)")),
    "interest_expense": (DURATION, re.compile(r"(Interest.*(Expense|Paid|Cost)|FinanceCost)"),
                         re.compile(r"(Income|Capitalized|Receivable|Tax|Fair)")),
    "principal_repayment": (DURATION, re.compile(r"(Repayment|PaymentsOf.*(Debt|Borrow|Lease|Notes)|PrincipalPayments)"),
                            re.compile(r"(Maturit|Undiscounted|FairValue|Dividend|Equity|Stock)")),
    "net_operating_income": (DURATION, re.compile(r"(OperatingIncome|OperatingProfit|ProfitLossFromOperating)"),
                             re.compile(r"Other")),
}


def find_candidates(index, fye: str, field: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Facts in this filing that could plausibly supply a missing input (consolidated, current period)."""
    rule = CANDIDATE_RULES.get(field)
    if not rule:
        return []
    kind, include, exclude = rule
    out = []
    for concept in index:
        local = concept.partition(":")[2] or concept
        if not include.search(local) or exclude.search(local):
            continue
        fact, conflict = _lookup(index, concept, kind, fye)
        if fact is None or conflict:
            continue
        out.append({"concept": concept, "value": fact["value"], "currency": fact["currency"]})
    out.sort(key=lambda c: abs(c["value"]), reverse=True)
    return out[:limit]


def build_dossier_from_ixbrl(content: bytes, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Pure function (no network): inline-XBRL bytes + filing metadata -> underwriting dossier."""
    parsed = parse_ixbrl(content)
    return build_dossier_from_index(_index_facts(parsed), parsed["dei"], dict(meta, method="inline XBRL (filing document)"))


def index_from_companyfacts(data: Dict[str, Any], accession: str) -> Dict[str, List[Dict[str, Any]]]:
    """SEC 'companyfacts' JSON -> the same concept index the inline-XBRL path produces, restricted to
    facts reported in ONE filing (accession). The API only carries consolidated (undimensioned) facts."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for taxonomy, concepts in (data.get("facts") or {}).items():
        if taxonomy not in ("us-gaap", "ifrs-full"):
            continue
        for name, body in concepts.items():
            for unit, rows in (body.get("units") or {}).items():
                if not re.fullmatch(r"[A-Z]{3}", unit):
                    continue                       # shares, pure, USD/shares ...
                for r in rows:
                    if r.get("accn") != accession or r.get("val") is None:
                        continue
                    start, end = r.get("start"), r.get("end")
                    index.setdefault(f"{taxonomy}:{name}", []).append({
                        "value": float(r["val"]),
                        "instant": None if start else end,
                        "start": start, "end": end if start else None, "currency": unit})
    return index


def build_dossier_from_companyfacts(data: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    index = index_from_companyfacts(data, meta["accession"])
    if not index:
        raise ValueError(f"companyfacts has no facts for accession {meta['accession']}")
    return build_dossier_from_index(index, {}, dict(meta, method="SEC companyfacts API"))


def build_dossier_from_index(index: Dict[str, List[Dict[str, Any]]], dei: Dict[str, str],
                             meta: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = list(meta.get("warnings") or [])
    fin: Dict[str, Optional[float]] = {}
    prov: Dict[str, Dict[str, Any]] = {}
    currencies: set = set()

    fye = _fiscal_year_end(index, dei)
    if not fye:
        raise ValueError("Could not determine the fiscal year end: no consolidated 'Assets' fact in this filing.")

    def take(key: str, alts: List[Alt], kind: str) -> Optional[float]:
        value, concepts, cur, conflict = _resolve(index, alts, kind, fye)
        if conflict:
            fin[key] = None
            prov[key] = _prov("missing", concepts, fye, f"conflicting facts for {conflict}")
            return None
        if value is None:
            return None
        currencies.add(cur)
        fin[key] = value
        prov[key] = _prov("derived" if len(concepts) > 1 else "reported", concepts, fye,
                          "sum of components" if len(concepts) > 1 else "")
        return value

    # ---- required statement lines
    for key, (kind, alts) in REQUIRED_CONCEPTS.items():
        if take(key, alts, kind) is None and key not in fin:
            fin[key] = None
            prov[key] = _prov("missing", [], fye, "no matching XBRL concept for this period (tried: "
                              + ", ".join(a if isinstance(a, str) else "+".join(a) for a in alts) + ")")

    # ---- total liabilities (reported, else derived from equity & liabilities)
    if take("total_liabilities", TOTAL_LIABILITIES_ALTS, INSTANT) is None and "total_liabilities" not in fin:
        el_val, el_concepts, el_cur, el_conf = _resolve(index, EQUITY_AND_LIABILITIES_ALTS, INSTANT, fye)
        eq = fin.get("shareholder_equity")
        if el_val is not None and eq is not None and not el_conf:
            fin["total_liabilities"] = el_val - eq
            currencies.add(el_cur)
            prov["total_liabilities"] = _prov("derived", el_concepts + [prov["shareholder_equity"]["concept"]], fye,
                                              "total equity-and-liabilities minus total equity")
        else:
            fin["total_liabilities"] = None
            prov["total_liabilities"] = _prov("missing", [], fye, "no total liabilities and cannot derive it")

    # ---- financial debt (borrowings + lease liabilities)
    parts: Dict[str, float] = {}
    used: List[str] = []
    conflict_note = None
    for name, alts in DEBT_COMPONENTS.items():
        v, concepts, cur, conflict = _resolve(index, alts, INSTANT, fye)
        if conflict:
            conflict_note = f"conflicting facts for {conflict}"
        if v is not None:
            if name in SHORT_TERM_COMPONENTS and COMBINED_DEBT_CONCEPTS & set(used):
                continue        # already inside the all-in borrowings total; don't double count
            if name == "lease_liabilities" and LEASE_INCLUSIVE_CONCEPTS & set(used):
                continue        # finance leases already inside a debt-and-lease total
            parts[name] = v
            used += concepts
            currencies.add(cur)
    debt_total = sum(parts.values())
    # Containment rule (tag-agnostic): a debt-like concept this loader did not use that is NOT larger
    # than the debt already counted is presumed to be a component/subtotal of it (listed in a warning);
    # one that is LARGER proves the counted debt is understated, so the decision is blocked.
    unmapped = _unmapped_debt_like(index, fye, set(used))
    too_big = [(c, v) for c, v in unmapped if v > debt_total + 0.5]
    presumed = [(c, v) for c, v in unmapped if v <= debt_total + 0.5 and c not in SILENT_SUBSETS]
    if conflict_note or too_big:
        fin["financial_debt"] = None
        prov["financial_debt"] = _prov(
            "missing", used, fye,
            conflict_note or ("balance sheet contains debt-like concept(s) larger than the debt counted ("
                              + ", ".join(f"{c} = {v:,.0f}" for c, v in too_big)
                              + "); analyst must confirm total financial debt"))
    elif parts:
        fin["financial_debt"] = debt_total
        note = "; ".join(f"{k}={v:,.0f}" for k, v in parts.items())
        if presumed:
            msg = ("debt-like concept(s) not used but no larger than the debt counted, presumed already included: "
                   + ", ".join(f"{c.partition(':')[2]} = {v:,.0f}" for c, v in presumed))
            note += "; " + msg
            warnings.append("Financial debt: " + msg)
        prov["financial_debt"] = _prov("derived" if len(used) > 1 else "reported", used, fye, note)
    else:
        fin["financial_debt"] = 0.0
        prov["financial_debt"] = _prov("none_reported", [], fye,
                                       "no borrowings or lease liabilities reported on the balance sheet")

    # ---- interest expense and principal repayment (needed only if there is debt)
    debt = fin.get("financial_debt")
    v, concepts, cur, conflict = _resolve(index, INTEREST_ALTS, DURATION, fye)
    if v is not None and not conflict:
        fin["interest_expense"] = v
        currencies.add(cur)
        prov["interest_expense"] = _prov("reported", concepts, fye,
                                         "IFRS 'finance costs' may include non-interest items" if "FinanceCosts" in concepts[0] else "")
    elif debt == 0.0:
        fin["interest_expense"] = 0.0
        prov["interest_expense"] = _prov("none_reported", [], fye, "no debt reported, so no interest expense")
    elif not conflict and (hit := _largest_matching(index, fye, DURATION, INTEREST_FALLBACK,
                                                    INTEREST_FALLBACK_EXCLUDE)):
        fin["interest_expense"] = hit[1]["value"]
        currencies.add(hit[1]["currency"])
        msg = f"interest expense inferred from {hit[0]} (largest interest-expense-like fact; no standard tag found)"
        prov["interest_expense"] = _prov("derived", [hit[0]], fye, msg)
        warnings.append("Interest expense: " + msg)
    else:
        fin["interest_expense"] = None
        prov["interest_expense"] = _prov("missing", [], fye,
                                         conflict and f"conflicting facts for {conflict}" or
                                         "company has financial debt but discloses no interest-expense fact")

    if debt is None:
        fin["principal_repayment"] = None
        prov["principal_repayment"] = _prov("missing", [], fye, "financial debt unknown")
    else:
        total, concepts_used, ok, notes, inferred = 0.0, [], True, [], False
        zero_assumed = False
        for name in DEBT_COMPONENTS:
            if name in SHORT_TERM_COMPONENTS:
                if parts.get(name, 0.0) > 0:
                    notes.append(f"{name} (rolled over; no scheduled principal)")
                continue
            if parts.get(name, 0.0) > 0:
                rv, rconcepts, rcur, rconf = _resolve(index, REPAYMENT_CONCEPTS[name], DURATION, fye)
                share = parts[name] / debt if debt else 1.0
                if rv is not None and not rconf:
                    total += abs(rv)
                    concepts_used += rconcepts
                    currencies.add(rcur)
                    notes.append(f"{name} repayments={abs(rv):,.0f}")
                    continue
                if rconf:
                    ok = False
                    notes.append(f"{name}: conflicting repayment facts for {rconf}")
                    continue
                # No standard repayment tag. Fallbacks, each logged: (1) other repayment-like cash-flow facts,
                # (2) current maturities from the balance sheet (the scheduled principal for the year ahead).
                fb_val, fb_concepts, fb_note = _principal_fallback(index, fye, name)
                if fb_val is not None:
                    total += fb_val
                    concepts_used += fb_concepts
                    inferred = True
                    notes.append(f"{name}: {fb_note}")
                    warnings.append(f"Principal repayment ({name}): {fb_note}")
                elif share < POLICY["immaterial_component_share"]:
                    msg = (f"{name} ({share:.1%} of financial debt) has no tagged repayment line; "
                           f"waived as immaterial (< {POLICY['immaterial_component_share']:.0%}), "
                           "repayments for it are NOT included in DSCR")
                    notes.append(msg)
                    warnings.append("Principal repayment: " + msg)
                elif name == "borrowings":
                    # The cash-flow statement lists no debt repayment at all: the usual reason is that none occurred.
                    zero_assumed = True
                    msg = ("no debt-repayment fact or current maturity found in the filing; recorded as 0 "
                           "(a company that repaid nothing tags nothing). Verify if repayments are expected")
                    notes.append(msg)
                    warnings.append("Principal repayment: " + msg)
                else:
                    ok = False
                    notes.append(f"{name} outstanding but no repayment fact found")
        if ok:
            fin["principal_repayment"] = total
            if concepts_used:
                prov["principal_repayment"] = _prov(
                    "derived" if (len(concepts_used) > 1 or inferred) else "reported", concepts_used, fye, "; ".join(notes))
            else:
                prov["principal_repayment"] = _prov(
                    "none_reported", [], fye,
                    "; ".join(notes) if notes else "no debt outstanding, so no principal repayments")
        else:
            fin["principal_repayment"] = None
            prov["principal_repayment"] = _prov("missing", concepts_used, fye, "; ".join(notes))

    # ---- EBIT fallback: no operating-profit fact -> pre-tax income + interest expense (the standard definition)
    if fin.get("ebit") is None and fin.get("interest_expense") is not None:
        pv, pconcepts, pcur, pconf = _resolve(index, PRETAX_ALTS, DURATION, fye)
        if pv is not None and not pconf:
            fin["ebit"] = pv + fin["interest_expense"]
            currencies.add(pcur)
            msg = (f"no operating-profit fact; EBIT derived as pre-tax income ({pconcepts[0].partition(':')[2]} = "
                   f"{pv:,.0f}) + interest expense ({fin['interest_expense']:,.0f})")
            prov["ebit"] = _prov("derived", pconcepts + [prov["interest_expense"].get("concept") or "interest expense"],
                                 fye, msg)
            warnings.append("EBIT: " + msg)

    # ---- optional intangibles (absent => none reported, which is a real and common state)
    for key, alts in (("goodwill", GOODWILL_ALTS), ("intangibles", INTANGIBLE_ALTS)):
        if take(key, alts, INSTANT) is None and key not in fin:
            fin[key] = 0.0
            prov[key] = _prov("none_reported", [], fye, "no such asset reported")
    if prov["intangibles"]["status"] == "none_reported":
        unmapped = [(c, max(f["value"] for f in fs if f["instant"] == fye and f["value"] > 0))
                    for c, fs in index.items()
                    if INTANGIBLE_LIKE.search(c.partition(":")[2]) and not INTANGIBLE_LIKE_EXCLUDE.search(c.partition(":")[2])
                    and any(f["instant"] == fye and f["value"] > 0 for f in fs)]
        if unmapped:
            warnings.append("Intangibles were taken as zero, but the filing carries intangible-like balances that were "
                            "not mapped: " + ", ".join(f"{c} = {v:,.0f}" for c, v in sorted(unmapped))
                            + ". Tangible net worth (TOL/TNW) may be overstated; confirm or override.")

    # ---- cash + current investments (information only)
    cash, c_concepts, c_cur, _ = _resolve(index, CASH_ALTS, INSTANT, fye)
    inv, i_concepts, i_cur, _ = _resolve(index, CURRENT_INVESTMENT_ALTS, INSTANT, fye)
    if cash is not None:
        fin["cash_and_current_investments"] = cash + (inv or 0.0)
        prov["cash_and_current_investments"] = _prov("derived", c_concepts + i_concepts, fye,
                                                     "cash and cash equivalents + current investments")
    else:
        fin["cash_and_current_investments"] = None
        prov["cash_and_current_investments"] = _prov("missing", [], fye, "cash not found (information only)")

    # ---- DSCR numerator: operating profit (EBIT-based), same convention the pipeline always used
    if fin.get("ebit") is not None:
        fin["net_operating_income"] = fin["ebit"]
        prov["net_operating_income"] = _prov("derived", [prov["ebit"]["concept"] or ""], fye,
                                             "equal to operating profit (EBIT)")
    else:
        fin["net_operating_income"] = None
        prov["net_operating_income"] = _prov("missing", [], fye, "operating profit not found")

    # ---- currency: one currency across every fact used, or refuse
    currencies.discard(None)
    if len(currencies) > 1:
        for k in fin:
            if prov.get(k, {}).get("status") in ("reported", "derived"):
                prov[k] = _prov("missing", [], fye, f"mixed currencies in filing: {sorted(currencies)}")
                fin[k] = None
        currency = "MIXED"
    else:
        currency = currencies.pop() if currencies else (meta.get("currency") or "USD")

    if fin.get("sales") is None and fin.get("ebit") is None and any(c in index for c in FINANCIAL_ONLY_CONCEPTS):
        raise UnsupportedEntityError(_financial_message("the filing reports deposits / interest income / premiums "
                                                        "and no operating income"))

    ident_ok = meta.get("identity_configured", True)
    if not ident_ok:
        warnings.append("SEC_IDENTITY is not set; using the placeholder identity. SEC fair-access rules expect a real "
                        "contact. Set SEC_IDENTITY='Your Name your@email.com' in .env.")

    name = meta.get("company_name", "Unknown")
    notes = (f"SEC {meta.get('form', 'annual filing')} filed {meta.get('filing_date', 'n/a')} "
             f"(accession {meta.get('accession', 'n/a')}) for {name}, fiscal year ended {fye}. "
             "Structured financial data only: narrative disclosures (risk factors, legal proceedings, covenants, "
             "going concern) have NOT been extracted in this build, so no qualitative conclusion can be drawn from this text.")

    return {
        "company_name": name,
        "ticker": meta.get("ticker", ""),
        "cik": meta.get("cik"),
        "currency": currency,
        "unit": "full currency units (converted from the filing's declared scale)",
        "fiscal_year_end": fye,
        "source": {k: meta.get(k) for k in ("form", "accession", "filing_date", "url", "method")},
        "financials": fin,
        "provenance": prov,
        "candidates": {k: c for k in fin if prov.get(k, {}).get("status") == "missing"
                       and (c := find_candidates(index, fye, k))},
        "warnings": warnings,
        "qualitative_available": False,
        "qualitative_notes": notes,
    }


def fetch_live_sec_dossier(ticker: str) -> Dict[str, Any]:
    """Fetch the latest 10-K / 20-F for a ticker (or CIK) and build the dossier from its XBRL facts."""
    ident, configured = _identity()
    cik, name = resolve_cik(ticker)
    filing = latest_annual_filing(cik)
    check_supported_sic(filing.get("sic"), filing.get("sic_description", ""))
    meta = dict(filing, ticker=ticker.strip().upper(), cik=cik, company_name=name,
                identity_configured=configured)

    mode = (os.getenv("SEC_SOURCE") or "auto").strip().lower()      # auto | companyfacts | ixbrl

    def finish(dossier, content=None):
        """Attach narrative evidence (risk factors, legal, liquidity) from the filing document."""
        try:
            if content is None:
                content = _http_get(filing["url"])
            narrative.attach(dossier, content)
        except Exception as exc:                                    # never lose the numbers over the narrative
            dossier.setdefault("warnings", []).append(
                f"Narrative disclosures could not be read from the filing ({type(exc).__name__}: {exc}); "
                "the qualitative step was skipped.")
        return dossier

    cf_dossier, fallback_note = None, None
    if mode in ("auto", "companyfacts"):
        try:
            data = json.loads(_http_get(SEC_COMPANYFACTS_URL.format(cik=cik)))
            cf_dossier = build_dossier_from_companyfacts(data, meta)
        except Exception as exc:                                    # network, parse or not-yet-indexed filing
            if mode == "companyfacts":
                raise
            fallback_note = (f"SEC companyfacts API could not be used ({type(exc).__name__}: {exc}); "
                             "facts were read from the filing document instead.")
        if cf_dossier is not None and (mode == "companyfacts" or _count_missing(cf_dossier) == 0):
            return finish(cf_dossier)

    # Filing document: the fallback, and the cross-check when the API view is incomplete.
    content = _http_get(filing["url"])
    if fallback_note:
        meta["warnings"] = [fallback_note]
    doc_dossier = build_dossier_from_ixbrl(content, meta)
    if cf_dossier is not None:
        if _count_missing(cf_dossier) <= _count_missing(doc_dossier):
            return finish(cf_dossier, content)      # the document adds nothing; keep the API view
        doc_dossier["warnings"].append("SEC companyfacts API was incomplete for this filing; "
                                       "the filing document was used instead.")
    return finish(doc_dossier, content)


def _count_missing(dossier: Dict[str, Any]) -> int:
    """Missing inputs that matter (cash is information only)."""
    return sum(1 for k, p in dossier["provenance"].items()
               if p.get("status") == "missing" and k != "cash_and_current_investments")
