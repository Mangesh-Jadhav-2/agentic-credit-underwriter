"""
Versioned credit policy parameters.

Every guardrail threshold lives here (not scattered through the agents) so that:
  * a change is a one-line, reviewable diff, and
  * every audit-log entry can record WHICH policy version produced a decision.

Bump POLICY_VERSION whenever any value below changes.
"""
from typing import Any, Dict

POLICY_VERSION = "2026.10-p4"

POLICY: Dict[str, float] = {
    # Debt service coverage: EBIT-based operating income / (principal + interest)
    "dscr_min": 1.35,
    # Leverage rule 1: financial debt (borrowings + lease liabilities) / total equity
    "debt_to_equity_max": 2.00,
    # Leverage rule 2: total outside liabilities / tangible net worth
    # (tangible net worth = equity - goodwill - other intangibles)
    # 3.0x: a widely used ceiling in bank credit appraisal. Indian working-capital appraisal (the
    # Tandon / Chore committee tradition) conventionally stipulates TOL/TNW <= 3.0, and 3.0-4.0x is common as a
    # total-liabilities-to-tangible-net-worth covenant in US middle-market lending. 3.0 is the strict end of that
    # range, chosen so a borrower passing it is clearly not over-geared. Showcase value; a real bank replaces it
    # with its own approved limit. The rule is not meaningful when tangible net worth is zero or negative (common
    # after large buybacks or acquisitions); that case is reported as a negative capital base, not as a ratio.
    "tol_tnw_max": 3.00,
    # EBIT / interest (finance cost)
    "interest_coverage_min": 2.50,
    # Altman Z''-score (non-manufacturers, book equity, no constant term):
    #   Z'' < 1.10 distress, 1.10-2.60 grey zone, > 2.60 safe
    "altman_z2_distress": 1.10,
    "altman_z2_safe": 2.60,
    # Data-integrity gate
    "balance_sheet_tolerance": 0.005,  # |Assets - (Liabilities + Equity)| / Assets
    "stale_filing_months": 18,
    # A debt component below this share of total financial debt may lack a tagged repayment line
    # (e.g. Apple's $1.2B finance leases). It is waived with a logged warning. Set 0 for strict blocking.
    "immaterial_component_share": 0.02,
    # Interest expense / counted financial debt above this means debt is almost certainly under-counted
    # (e.g. debt tagged only under segment dimensions, as at Ford). Blocks the decision.
    "implied_interest_rate_max": 0.30,
}

# Deterministic decision rules applied AFTER the LLM memo (see src/decision_rules.py). The LLM can be
# stricter than these, never looser.
#
# Showcase calibration (indicative market conventions, NOT a bank's approved credit policy):
#  * Spread floors, bps over SOFR, by rating tier. They follow how loans are priced by credit tier:
#    investment-grade-like borrowers price roughly SOFR+100-150 (revolver/term loan grids), BB-type
#    roughly +200-250, single-B roughly +350-450. The leveraged-loan index spread was ~453 bps at
#    30 Jun 2026 (Fidelity Q2 2026 Leveraged Loan Market Review), which anchors the HIGH_RISK tier.
#    Floors are minimums: the LLM may price higher, never lower.
#  * Limit = the LOWER of two caps:
#      1. concentration cap: share of borrower equity by rating (single-name exposure kept small
#         relative to the borrower's loss-absorbing capital; the Basel large-exposure standard caps one
#         counterparty at 25% of the LENDER's Tier 1, so a per-name hold well below that is conventional);
#      2. leverage headroom: debt_to_equity_max x equity - existing financial debt. Lending beyond this
#         would push the borrower past the bank's own D/E guardrail, so the limit cannot exceed it.
#  * No numeric leverage ceiling (e.g. 6x debt/EBITDA) is used: the 2013 US interagency leveraged-lending
#    guidance that implied one was rescinded by the OCC and FDIC on 5 Dec 2025.
DECISION_POLICY: Dict[str, Any] = {
    "limit_pct_of_equity": {"LOW_RISK": 0.10, "MODERATE_RISK": 0.05, "HIGH_RISK": 0.02, "REJECT": 0.0},
    "min_spread_bps": {"LOW_RISK": 125, "MODERATE_RISK": 225, "HIGH_RISK": 400, "REJECT": 0},
    # This many HIGH-severity guardrail flags force at least HIGH_RISK (one HIGH flag forces MODERATE_RISK)
    "high_flags_for_high_risk": 2,
}

# Values that are PLACEHOLDERS awaiting confirmation from the bank's own credit policy.
# Any guardrail message that uses one of these says so explicitly.
UNCONFIRMED: set = set()          # nothing is left as a bare placeholder; see the justification beside each value


def policy_snapshot() -> Dict[str, Any]:
    """Immutable copy of the policy for embedding in audit records."""
    import copy
    return {"version": POLICY_VERSION, "values": dict(POLICY), "decision_rules": copy.deepcopy(DECISION_POLICY),
            "unconfirmed": sorted(UNCONFIRMED)}
