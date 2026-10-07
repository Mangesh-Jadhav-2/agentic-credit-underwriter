

from typing import Dict, List, TypedDict, Optional, Any

from src.policy import POLICY, POLICY_VERSION, UNCONFIRMED

# Agent State Interface
class AgentState(TypedDict):
    company_name: str
    ticker: str
    raw_financials: Dict[str, float]
    qualitative_notes: str
    computed_metrics: Optional[Any]
    extracted_risks: List[Dict[str, str]]
    policy_violations: List[Dict[str, str]]
    audit_trail: List[str]


def _threshold_note(key: str) -> str:
    return " (placeholder threshold, not yet confirmed against bank policy)" if key in UNCONFIRMED else ""


def evaluate_compliance_guardrails(state: AgentState) -> AgentState:
    """
    Node 3: Policy Guardrail & Compliance Agent (deterministic).
    Evaluates computed metrics and qualitative flags against the versioned policy in src/policy.py.

    A metric that could not be computed because INPUT DATA WAS MISSING is itself a CRITICAL
    violation (never a silent pass). A metric that is genuinely not applicable (e.g. a company
    with no debt has no debt service) is not a violation and is listed in the audit entry.
    """
    metrics = state["computed_metrics"]
    extracted_risks = state["extracted_risks"]
    violations: List[Dict[str, str]] = []
    not_applicable: List[str] = []

    if not metrics:
        state["policy_violations"] = [{"severity": "CRITICAL", "rule": "System Error", "message": "Missing financial metrics."}]
        state["audit_trail"].append("COMPLIANCE_AGENT: Failed evaluation due to missing quantitative metrics.")
        return state

    status = metrics.status or {}

    def unevaluable(metric: str, rule: str) -> bool:
        """Record a violation / note when the metric has no value. True if no value to test."""
        s = status.get(metric, "ok")
        if getattr(metrics, metric) is not None:
            return False
        if s == "missing_input":
            violations.append({"severity": "CRITICAL", "rule": f"Data Integrity: {rule}",
                               "message": f"{rule} could not be computed because required inputs are missing. "
                                          "Automated assessment of this rule is impossible."})
        elif s.startswith("negative_"):
            violations.append({"severity": "CRITICAL", "rule": f"Negative Capital Base: {rule}",
                               "message": f"{rule} is not meaningful: {s.replace('_', ' ')}."})
        else:
            not_applicable.append(f"{metric}:{s}")
        return True

    # 1. DSCR
    if not unevaluable("dscr", "Minimum DSCR") and metrics.dscr < POLICY["dscr_min"]:
        violations.append({
            "severity": "CRITICAL", "rule": "Minimum DSCR Requirement",
            "message": f"DSCR is {metrics.dscr}x, below the mandatory banking threshold of {POLICY['dscr_min']:.2f}x."})

    # 2. Debt-to-equity (financial debt basis)
    if not unevaluable("debt_to_equity", "Maximum Debt-to-Equity") and metrics.debt_to_equity > POLICY["debt_to_equity_max"]:
        violations.append({
            "severity": "HIGH", "rule": "Maximum Debt-to-Equity Limit",
            "message": f"Financial-debt-to-equity is {metrics.debt_to_equity}x, exceeding the maximum permitted "
                       f"limit of {POLICY['debt_to_equity_max']:.2f}x."})

    # 3. TOL / TNW
    if not unevaluable("tol_tnw", "Maximum TOL/TNW") and metrics.tol_tnw > POLICY["tol_tnw_max"]:
        violations.append({
            "severity": "HIGH", "rule": "Maximum TOL/TNW",
            "message": f"TOL/TNW is {metrics.tol_tnw}x, exceeding the limit of {POLICY['tol_tnw_max']:.2f}x"
                       f"{_threshold_note('tol_tnw_max')}."})

    # 4. Interest coverage
    if not unevaluable("interest_coverage", "Minimum Interest Coverage") and metrics.interest_coverage < POLICY["interest_coverage_min"]:
        violations.append({
            "severity": "HIGH", "rule": "Minimum Interest Coverage",
            "message": f"Interest Coverage ratio is {metrics.interest_coverage}x, below the required "
                       f"{POLICY['interest_coverage_min']:.2f}x threshold."})

    # 5. Altman Z'' distress / grey zone
    if not unevaluable("altman_z_score", "Altman Z''-score"):
        z = metrics.altman_z_score
        if z < POLICY["altman_z2_distress"]:
            violations.append({
                "severity": "CRITICAL", "rule": "Bankruptcy Risk Threshold",
                "message": f"Altman Z''-score is {z}, placing the firm in the Distress Zone (< {POLICY['altman_z2_distress']})."})
        elif z < POLICY["altman_z2_safe"]:
            violations.append({
                "severity": "MEDIUM", "rule": "Altman Grey Zone",
                "message": f"Altman Z''-score is {z}, in the grey zone ({POLICY['altman_z2_distress']}-{POLICY['altman_z2_safe']})."})

    # 6. Qualitative high-severity risk check
    for risk in [r for r in extracted_risks if r.get("severity") == "HIGH"]:
        violations.append({
            "severity": "HIGH", "rule": "High Qualitative Risk Flag",
            "message": f"Qualitative Risk [{risk.get('category')}]: {risk.get('description')}"})

    state["policy_violations"] = violations

    state["audit_trail"].append(
        f"COMPLIANCE_AGENT: Evaluated compliance rules for {state['company_name']} under policy {POLICY_VERSION}"
        f"{' (unconfirmed placeholders: ' + ', '.join(sorted(UNCONFIRMED)) + ')' if UNCONFIRMED else ''}. "
        f"Triggered {len(violations)} policy flags. Not applicable: {not_applicable if not_applicable else 'none'}."
    )
    return state
