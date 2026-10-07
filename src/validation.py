"""
Data-integrity gate.

Runs BEFORE any ratio, guardrail or LLM step. Its job is to make it impossible for the pipeline
to produce an automated credit decision from missing, inconsistent or silently-defaulted inputs.

Severity model
    BLOCK - automated decision must not run (missing/implausible/inconsistent required data)
    WARN  - decision may run, but a human must see the caveat (derived, overridden, unverified...)
    INFO  - provenance detail only
Overall status = BLOCK if any BLOCK, else WARN if any WARN, else PASS.
"""
import datetime as dt
import math
from typing import Any, Dict, List

from src.fields import QUANT_INPUT_FIELDS, FIELD_LABELS, SOFT_FIELDS
from src.policy import POLICY, POLICY_VERSION


def _issue(severity: str, code: str, message: str, field: str = "") -> Dict[str, str]:
    return {"severity": severity, "code": code, "field": field, "message": message}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _months_between(start: dt.date, end: dt.date) -> float:
    return (end - start).days / 30.4375


def assess_data_integrity(dossier: Dict[str, Any], today: dt.date = None) -> Dict[str, Any]:
    """
    dossier keys used: financials, provenance (optional), currency, fiscal_year_end,
    qualitative_available, override_reason, warnings.
    """
    today = today or dt.date.today()
    fin: Dict[str, Any] = dossier.get("financials") or {}
    prov: Dict[str, Any] = dossier.get("provenance") or {}
    issues: List[Dict[str, str]] = []

    # 1. Every input the ratio engine needs must be present and numeric.
    for key in QUANT_INPUT_FIELDS:
        label = FIELD_LABELS.get(key, key)
        value = fin.get(key)
        status = (prov.get(key) or {}).get("status")
        if value is None or status == "missing":
            note = (prov.get(key) or {}).get("note") or "not found in the source data"
            if key in SOFT_FIELDS:
                issues.append(_issue("WARN", "MISSING_INPUT_SOFT",
                                     f"{label}: {note}. No default is substituted; ratios that need it are "
                                     "'not computable' and the compliance agent flags them as CRITICAL.", key))
            else:
                issues.append(_issue("BLOCK", "MISSING_INPUT",
                                     f"{label}: {note}. No default is substituted.", key))
        elif not _is_number(value):
            issues.append(_issue("BLOCK", "NON_NUMERIC_INPUT", f"{label}: value {value!r} is not a finite number.", key))

    # 2. Plausibility / consistency checks (only when the operands exist).
    ta, tl, eq, ca = (fin.get(k) for k in ("total_assets", "total_liabilities", "shareholder_equity", "current_assets"))
    if all(_is_number(v) for v in (ta, tl, eq)):
        if ta <= 0:
            issues.append(_issue("BLOCK", "NON_POSITIVE_ASSETS", "Total assets must be positive.", "total_assets"))
        else:
            diff = abs(ta - (tl + eq))
            if diff > POLICY["balance_sheet_tolerance"] * ta:
                issues.append(_issue(
                    "BLOCK", "BALANCE_SHEET_DOES_NOT_BALANCE",
                    f"Assets ({ta:,.0f}) != Liabilities ({tl:,.0f}) + Equity ({eq:,.0f}); "
                    f"difference {diff:,.0f} exceeds {POLICY['balance_sheet_tolerance']:.1%} of assets. "
                    "Likely a unit, period or extraction error.", "total_assets"))
    if _is_number(ca) and _is_number(ta) and ta > 0 and ca > ta:
        issues.append(_issue("BLOCK", "CURRENT_ASSETS_EXCEED_TOTAL",
                             f"Current assets ({ca:,.0f}) exceed total assets ({ta:,.0f}).", "current_assets"))

    # 2b. Cross-check: interest expense must be plausible against the debt counted (catches debt that is
    # tagged only under segment dimensions and therefore silently missing from the consolidated facts).
    debt, interest = fin.get("financial_debt"), fin.get("interest_expense")
    if _is_number(debt) and _is_number(interest) and interest > 0:
        if debt <= 0:
            issues.append(_issue("WARN", "INTEREST_WITHOUT_DEBT",
                                 f"Interest expense of {interest:,.0f} is reported but no financial debt was found; "
                                 "debt may be under-counted (or the figure includes non-debt finance costs).",
                                 "financial_debt"))
        elif interest / debt > POLICY["implied_interest_rate_max"]:
            issues.append(_issue("BLOCK", "DEBT_INTEREST_INCONSISTENT",
                                 f"Interest expense ({interest:,.0f}) is {interest / debt:.0%} of the financial debt "
                                 f"counted ({debt:,.0f}); above the {POLICY['implied_interest_rate_max']:.0%} plausibility "
                                 "limit. Debt is probably under-counted (e.g. tagged only under segments). "
                                 "Confirm total debt from the filing.", "financial_debt"))

    # 3. Provenance caveats.
    overridden = [k for k, p in prov.items() if (p or {}).get("status") == "analyst_override"]
    if overridden:
        if not (dossier.get("override_reason") or "").strip():
            issues.append(_issue("BLOCK", "OVERRIDE_WITHOUT_REASON",
                                 "Analyst overrides were made but no reason was recorded: "
                                 + ", ".join(FIELD_LABELS.get(k, k) for k in overridden)))
        else:
            issues.append(_issue("WARN", "ANALYST_OVERRIDE",
                                 "Inputs overridden by an analyst (not as reported in the filing): "
                                 + ", ".join(FIELD_LABELS.get(k, k) for k in overridden)))

    derived = [k for k, p in prov.items() if (p or {}).get("status") == "derived"]
    if derived:
        issues.append(_issue("INFO", "DERIVED_INPUTS", "Derived (computed from reported facts): "
                             + ", ".join(FIELD_LABELS.get(k, k) for k in derived)))
    none_reported = [k for k, p in prov.items() if (p or {}).get("status") == "none_reported"]
    if none_reported:
        issues.append(_issue("INFO", "NONE_REPORTED", "Not reported by the company, recorded as 0: "
                             + ", ".join(FIELD_LABELS.get(k, k) for k in none_reported)))

    if not prov:
        issues.append(_issue("WARN", "UNVERIFIED_INPUTS",
                             "Inputs carry no source provenance (sample or manually supplied data); "
                             "they have not been reconciled to a filing."))

    # 4. Context caveats.
    currency = dossier.get("currency") or "USD"
    if currency != "USD":
        issues.append(_issue("WARN", "NON_USD_REPORTING",
                             f"Amounts are reported in {currency}; no FX conversion is applied and "
                             "any credit limit produced is denominated in that currency. Spread floors are calibrated to USD "
                             "markets and apply over the local benchmark rate."))
    if dossier.get("qualitative_available") is False:
        issues.append(_issue("WARN", "QUALITATIVE_NOT_EXTRACTED",
                             "Narrative disclosures (risk factors, legal, covenants) were not extracted; "
                             "the qualitative step is skipped and the decision rests on financial data only."))
    fye = dossier.get("fiscal_year_end")
    if fye:
        try:
            age = _months_between(dt.date.fromisoformat(fye), today)
            if age > POLICY["stale_filing_months"]:
                issues.append(_issue("WARN", "STALE_FINANCIALS",
                                     f"Fiscal year ended {fye} ({age:.0f} months ago)."))
        except ValueError:
            issues.append(_issue("WARN", "BAD_FISCAL_YEAR_END", f"Unparseable fiscal_year_end {fye!r}."))
    for w in dossier.get("warnings") or []:
        issues.append(_issue("WARN", "LOADER_WARNING", str(w)))

    severities = {i["severity"] for i in issues}
    status = "BLOCK" if "BLOCK" in severities else "WARN" if "WARN" in severities else "PASS"
    return {
        "status": status,
        "issues": issues,
        "policy_version": POLICY_VERSION,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def summarize(report: Dict[str, Any]) -> str:
    """One-line summary suitable for the audit trail."""
    counts = {s: sum(1 for i in report["issues"] if i["severity"] == s) for s in ("BLOCK", "WARN", "INFO")}
    codes = sorted({i["code"] for i in report["issues"] if i["severity"] in ("BLOCK", "WARN")})
    return (f"DATA_VALIDATOR: status={report['status']} (BLOCK={counts['BLOCK']}, WARN={counts['WARN']}, "
            f"INFO={counts['INFO']}) codes={codes} policy={report['policy_version']}")
