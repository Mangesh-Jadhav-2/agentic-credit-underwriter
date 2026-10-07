"""
Deterministic decision rules, applied AFTER the LLM memo.

The LLM writes the narrative and proposes a rating, limit and spread; this module decides what the bank
is actually allowed to approve. The LLM can be stricter than these rules, never looser:

  * rating  : cannot be better than the floor implied by the guardrail violations
  * limit   : capped at the lower of a rating-based share of equity and the D/E headroom
  * spread  : cannot be below the minimum for the final rating
  * data    : a CRITICAL data-integrity violation (a ratio that cannot be computed) means the case is
              REFERRED to an analyst; no LLM call is made and no limit or price is proposed

All numbers live in src/policy.py (DECISION_POLICY) and are showcase calibrations (see the note there).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.policy import DECISION_POLICY, POLICY

RATINGS = ["LOW_RISK", "MODERATE_RISK", "HIGH_RISK", "REJECT"]
REFER = "REFER_TO_ANALYST"


@dataclass
class Bounds:
    refer: bool = False
    floor_rating: str = "LOW_RISK"
    reasons: List[str] = field(default_factory=list)
    equity: Optional[float] = None
    debt: float = 0.0

    def limit_cap(self, rating: str) -> float:
        pct = DECISION_POLICY["limit_pct_of_equity"].get(rating, 0.0)
        eq = self.equity if self.equity is not None and self.equity > 0 else 0.0
        return min(pct * eq, self.headroom())

    def headroom(self) -> float:
        """Extra debt the borrower can take before breaching the policy D/E maximum."""
        eq = self.equity if self.equity is not None and self.equity > 0 else 0.0
        return max(0.0, POLICY["debt_to_equity_max"] * eq - self.debt)

    def spread_floor(self, rating: str) -> int:
        return int(DECISION_POLICY["min_spread_bps"].get(rating, 0))


def compute_bounds(violations: List[Dict[str, str]], financials: Dict[str, Any]) -> Bounds:
    eq = financials.get("shareholder_equity")
    debt = financials.get("financial_debt")
    b = Bounds(equity=float(eq) if isinstance(eq, (int, float)) else None,
               debt=float(debt) if isinstance(debt, (int, float)) else 0.0)

    refer = [v for v in violations if v.get("severity") == "CRITICAL" and str(v.get("rule", "")).startswith("Data Integrity")]
    if refer:
        b.refer = True
        b.reasons = [f"{v['rule']}: {v['message']}" for v in refer]
        return b

    floor = 0

    def raise_to(rating: str, why: str):
        nonlocal floor
        floor = max(floor, RATINGS.index(rating))
        b.reasons.append(f"floor {rating}: {why}")

    highs = []
    for v in violations:
        rule, sev = str(v.get("rule", "")), v.get("severity")
        if rule == "Minimum DSCR Requirement":
            raise_to("HIGH_RISK", "DSCR below the policy minimum")
        elif rule == "Bankruptcy Risk Threshold":
            raise_to("REJECT", "Altman Z'' in the distress zone")
        elif rule.startswith("Negative Capital Base"):
            raise_to("REJECT", rule)
        elif sev == "HIGH":
            highs.append(rule)
    if highs:
        raise_to("MODERATE_RISK", f"{len(highs)} HIGH guardrail flag(s): {', '.join(highs)}")
        if len(highs) >= DECISION_POLICY["high_flags_for_high_risk"]:
            raise_to("HIGH_RISK", f"{len(highs)} HIGH flags reach the limit of "
                                  f"{DECISION_POLICY['high_flags_for_high_risk']}")
    b.floor_rating = RATINGS[floor]
    return b


def apply_bounds(decision: Dict[str, Any], bounds: Bounds):
    """Return (final_decision, changes). `decision` is the validated LLM output; it is not mutated."""
    d = dict(decision)
    d["key_rationale"] = list(d.get("key_rationale", []))
    changes: List[str] = []

    proposed = d["risk_rating"]
    if RATINGS.index(proposed) < RATINGS.index(bounds.floor_rating):
        d["risk_rating"] = bounds.floor_rating
        changes.append(f"rating {proposed} -> {bounds.floor_rating} (policy floor: "
                       + "; ".join(r for r in bounds.reasons) + ")")
    rating = d["risk_rating"]

    cap = bounds.limit_cap(rating)
    if d["approved_credit_limit"] > cap:
        changes.append(f"limit {d['approved_credit_limit']:,.0f} -> {cap:,.0f} "
                       f"(lower of {DECISION_POLICY['limit_pct_of_equity'][rating]:.0%} of equity for {rating} "
                       f"and D/E headroom {bounds.headroom():,.0f})")
        d["approved_credit_limit"] = cap

    floor_bps = bounds.spread_floor(rating)
    if rating != "REJECT" and d["recommended_interest_spread_bps"] < floor_bps:
        changes.append(f"spread {d['recommended_interest_spread_bps']} -> {floor_bps} bps (minimum for {rating})")
        d["recommended_interest_spread_bps"] = floor_bps
    if rating == "REJECT":
        d["recommended_interest_spread_bps"] = 0

    if changes:
        d["key_rationale"] += [f"POLICY OVERRIDE: {c}" for c in changes]
        final_terms = (f"FINAL TERMS AFTER POLICY RULES: {d['risk_rating']}, limit {d['approved_credit_limit']:,.0f}, "
                       f"spread +{d['recommended_interest_spread_bps']} bps. Any other limit, spread or rating quoted "
                       "below is the model's superseded proposal. ")
        d["credit_memo_summary"] = (final_terms + d["credit_memo_summary"].rstrip() +
                                    " [Deterministic policy rules adjusted the model's proposal: " + "; ".join(changes) + ".]")
    return d, changes


def referral_decision(bounds: Bounds) -> Dict[str, Any]:
    """The outcome when the data cannot support an automated assessment. No LLM involved."""
    return {
        "risk_rating": REFER,
        "approved_credit_limit": 0.0,
        "currency": None,
        "recommended_interest_spread_bps": 0,
        "key_rationale": [f"CRITICAL DATA GAP: {r}" for r in bounds.reasons],
        "credit_memo_summary": ("Automated assessment is not possible because required guardrail inputs could not be "
                                "determined from the source data. The case is referred to a credit analyst; no limit "
                                "or price is proposed."),
    }
