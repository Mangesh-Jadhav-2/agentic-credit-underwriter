
from pydantic import BaseModel, Field
from typing import Dict, List, TypedDict, Optional, Any

from src.policy import POLICY_VERSION

ALTMAN_MODEL = "Altman Z''-score (non-manufacturer, book equity, no constant)"


# Pydantic schema for structured ratios. None means "not computable"; the reason is in `status`.
class FinancialMetrics(BaseModel):
    current_ratio: Optional[float] = Field(default=None, description="Liquidity: Current Assets / Current Liabilities")
    debt_to_equity: Optional[float] = Field(default=None, description="Leverage: Financial Debt (borrowings + lease liabilities) / Total Equity")
    tol_tnw: Optional[float] = Field(default=None, description="Leverage: Total Liabilities / Tangible Net Worth (equity - goodwill - intangibles)")
    interest_coverage: Optional[float] = Field(default=None, description="Debt service capacity: EBIT / Interest Expense")
    dscr: Optional[float] = Field(default=None, description="Debt Service Coverage Ratio: EBIT-based operating income / (principal + interest)")
    altman_z_score: Optional[float] = Field(default=None, description="Altman Z''-score for non-manufacturers (book equity)")
    altman_model: str = Field(default=ALTMAN_MODEL, description="Which Altman variant produced altman_z_score")
    working_capital: Optional[float] = Field(default=None, description="Current Assets - Current Liabilities")
    net_debt: Optional[float] = Field(default=None, description="Information only: financial debt - (cash + current investments); negative = net cash")
    status: Dict[str, str] = Field(default_factory=dict, description="Per-metric status: ok | missing_input | not_applicable_* | negative_*")


# Shared State Type
class AgentState(TypedDict):
    company_name: str
    ticker: str
    raw_financials: Dict[str, float]
    qualitative_notes: str
    computed_metrics: Optional[Any]
    extracted_risks: List[Dict[str, str]]
    policy_violations: List[Dict[str, str]]
    final_decision: Optional[Dict[str, Any]]
    audit_trail: List[str]


def _get(fin: Dict[str, Any], key: str) -> Optional[float]:
    v = fin.get(key)
    return None if v is None else float(v)


def calculate_quantitative_metrics(state: AgentState) -> AgentState:
    """
    Node 1: Quantitative Analyst Agent.
    Computes liquidity, leverage, coverage and distress ratios. Never substitutes a default for
    missing data: a ratio that cannot be computed is None with a status that says why.
    """
    fin = state["raw_financials"]

    ca, cl = _get(fin, "current_assets"), _get(fin, "current_liabilities")
    ta, tl = _get(fin, "total_assets"), _get(fin, "total_liabilities")
    eq = _get(fin, "shareholder_equity")
    re_ = _get(fin, "retained_earnings")
    ebit = _get(fin, "ebit")
    noi = _get(fin, "net_operating_income")
    if noi is None:
        noi = ebit  # DSCR numerator convention: EBIT-based operating income
    debt = _get(fin, "financial_debt")
    interest = _get(fin, "interest_expense")
    principal = _get(fin, "principal_repayment")
    goodwill, intangibles = _get(fin, "goodwill"), _get(fin, "intangibles")
    cash_inv = _get(fin, "cash_and_current_investments")

    status: Dict[str, str] = {}

    # 1. Current ratio
    current_ratio = None
    if ca is None or cl is None:
        status["current_ratio"] = "missing_input"
    elif cl <= 0:
        status["current_ratio"] = "not_applicable_no_current_liabilities"
    else:
        current_ratio, status["current_ratio"] = ca / cl, "ok"

    working_capital = ca - cl if ca is not None and cl is not None else None

    # 2. Debt-to-equity (financial debt basis)
    debt_to_equity = None
    if debt is None or eq is None:
        status["debt_to_equity"] = "missing_input"
    elif eq <= 0:
        status["debt_to_equity"] = "negative_equity"
    else:
        debt_to_equity, status["debt_to_equity"] = debt / eq, "ok"

    # 3. TOL / TNW
    tol_tnw = None
    if tl is None or eq is None or goodwill is None or intangibles is None:
        status["tol_tnw"] = "missing_input"
    else:
        tnw = eq - goodwill - intangibles
        if tnw <= 0:
            status["tol_tnw"] = "negative_tangible_net_worth"
        else:
            tol_tnw, status["tol_tnw"] = tl / tnw, "ok"

    # 4. Interest coverage
    interest_coverage = None
    if ebit is None or interest is None:
        status["interest_coverage"] = "missing_input"
    elif interest <= 0:
        status["interest_coverage"] = "not_applicable_no_interest"
    else:
        interest_coverage, status["interest_coverage"] = ebit / interest, "ok"

    # 5. DSCR
    dscr = None
    if noi is None or principal is None or interest is None:
        status["dscr"] = "missing_input"
    else:
        service = principal + interest
        if service <= 0:
            status["dscr"] = "not_applicable_no_debt_service"
        else:
            dscr, status["dscr"] = noi / service, "ok"

    # 6. Altman Z'' (non-manufacturers): 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4
    altman_z = None
    if None in (ca, cl, ta, tl, re_, ebit, eq) or ta <= 0:
        status["altman_z_score"] = "missing_input"
    elif tl <= 0:
        status["altman_z_score"] = "not_applicable_no_liabilities"
    else:
        x1, x2, x3, x4 = (ca - cl) / ta, re_ / ta, ebit / ta, eq / tl
        altman_z, status["altman_z_score"] = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4, "ok"

    net_debt = debt - cash_inv if debt is not None and cash_inv is not None else None

    def r(v):
        return None if v is None else round(v, 2)

    metrics = FinancialMetrics(
        current_ratio=r(current_ratio), debt_to_equity=r(debt_to_equity), tol_tnw=r(tol_tnw),
        interest_coverage=r(interest_coverage), dscr=r(dscr), altman_z_score=r(altman_z),
        working_capital=working_capital, net_debt=net_debt, status=status,
    )
    state["computed_metrics"] = metrics

    problems = {k: v for k, v in status.items() if v != "ok"}
    state["audit_trail"].append(
        f"QUANT_AGENT: Metrics for {state['company_name']} (policy {POLICY_VERSION}): "
        f"Current Ratio={metrics.current_ratio}, D/E(fin. debt)={metrics.debt_to_equity}, TOL/TNW={metrics.tol_tnw}, "
        f"Interest Cov={metrics.interest_coverage}, DSCR={metrics.dscr}, Z''={metrics.altman_z_score}. "
        f"Not computed/applicable: {problems if problems else 'none'}"
    )
    return state
