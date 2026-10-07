import os
import json
from typing import Any, Dict, List, Literal, Optional, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.decision_rules import (REFER, Bounds, RATINGS, apply_bounds, compute_bounds, referral_decision)
from src.policy import DECISION_POLICY

MAX_LLM_ATTEMPTS = 2


class CreditDecision(BaseModel):
    """What the LLM must return. Anything outside these bounds is rejected, never repaired silently."""
    model_config = ConfigDict(extra="ignore")

    risk_rating: Literal["LOW_RISK", "MODERATE_RISK", "HIGH_RISK", "REJECT"] = Field(
        description="Overall risk classification: LOW_RISK, MODERATE_RISK, HIGH_RISK, or REJECT")
    approved_credit_limit: float = Field(
        ge=0, allow_inf_nan=False, description="Recommended maximum working capital / term credit limit, in the borrower's reporting currency")
    recommended_interest_spread_bps: int = Field(
        ge=0, le=2000, description="Recommended interest spread over SOFR / Prime rate in basis points")
    key_rationale: List[str] = Field(min_length=1, description="Bullet points explaining core reasons for this credit decision")
    credit_memo_summary: str = Field(min_length=20, description="Executive paragraph summarizing final credit recommendation for Credit Committee")

    @field_validator("risk_rating", mode="before")
    @classmethod
    def _normalise_rating(cls, v):
        return v.strip().upper().replace(" ", "_") if isinstance(v, str) else v


class AgentState(TypedDict):
    company_name: str
    ticker: str
    currency: str
    raw_financials: Dict[str, float]
    qualitative_notes: str
    computed_metrics: Optional[Any]
    extracted_risks: List[Dict[str, str]]
    policy_violations: List[Dict[str, str]]
    final_decision: Optional[Dict[str, Any]]
    audit_trail: List[str]


def benchmark_for(currency: str) -> str:
    return "SOFR" if currency == "USD" else f"local benchmark rate for {currency}"


def _model_name() -> str:
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def _call_llm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The only place the LLM is called (kept separate so tests can replace it)."""
    from langchain_groq import ChatGroq
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import JsonOutputParser

    llm = ChatGroq(model_name=_model_name(), temperature=0)
    parser = JsonOutputParser(pydantic_object=CreditDecision)
    prompt = ChatPromptTemplate.from_template(
        "You are the Chief Credit Officer (CCO) at an international commercial bank. "
        "Review the underwriting dossier for {company_name} and render a final lending decision.\n\n"
        "1. Quantitative Financial Metrics:\n{metrics}\n\n"
        "2. Qualitative Risk Factors Identified:\n{qual_risks}\n\n"
        "3. Bank Policy & Compliance Violations:\n{violations}\n\n"
        "4. Hard policy bounds (applied automatically after your answer; you may be stricter, never looser):\n{bounds}\n\n"
        "Instructions:\n"
        "- Evaluate financial solvency, liquidity, and qualitative risk notes.\n"
        "- Determine the overall Risk Rating (LOW_RISK, MODERATE_RISK, HIGH_RISK, or REJECT).\n"
        "- Calculate an appropriate Approved Credit Limit in {currency} (the filing's reporting currency; do not convert).\n"
        "- Recommend an interest rate spread in basis points over the {benchmark} (e.g., 200 bps = 2.00%).\n"
        "- Provide a structured list of key rationale points and an executive summary memo.\n"
        "- State a limit no higher than max_limit_by_rating for the rating you choose; do not quote a larger figure.\n"
        "- Use only the facts above. Do not invent figures, covenants or events.\n\n"
        "{format_instructions}"
    )
    chain = prompt | llm | parser
    return chain.invoke({**payload, "format_instructions": parser.get_format_instructions()})


def _qual_text(state: AgentState) -> str:
    status = state.get("qualitative_status")
    if status in ("SKIPPED", "FAILED"):
        return (f"Qualitative review NOT performed (status {status}). State this limitation in the memo; "
                "do not infer any qualitative risks.")
    risks = [{k: r.get(k) for k in ("category", "severity", "description", "quote")} for r in state["extracted_risks"]]
    return json.dumps(risks, indent=2) if risks else "No credit-relevant risks were found in the retrieved filing passages."


def _bounds_for_prompt(bounds: Bounds) -> str:
    return json.dumps({
        "rating_cannot_be_better_than": bounds.floor_rating,
        "reasons": bounds.reasons,
        "max_limit_as_share_of_equity_by_rating_and_never_above_DE_headroom": DECISION_POLICY["limit_pct_of_equity"],
        "debt_headroom": bounds.headroom(),
        "max_limit_by_rating": {r: round(bounds.limit_cap(r)) for r in RATINGS},
        "min_spread_bps_by_rating": DECISION_POLICY["min_spread_bps"],
    }, indent=2)


def synthesize_credit_decision(state: AgentState) -> AgentState:
    """
    Node 4: Chief Underwriting Officer Agent.
      1. Compute deterministic bounds from the guardrail violations.
      2. Data gaps that make a guardrail uncomputable -> refer to an analyst (no LLM call).
      3. Otherwise ask the LLM, validate its JSON strictly (Pydantic), then enforce the bounds.
    """
    audit = state["audit_trail"]
    bounds = compute_bounds(state.get("policy_violations", []), state.get("raw_financials", {}))

    if bounds.refer:
        state["final_decision"] = referral_decision(bounds)
        audit.append("DECISION_AGENT: REFERRED TO ANALYST - guardrail inputs could not be determined "
                     "(no LLM call made, no limit proposed): " + " | ".join(bounds.reasons))
        return state

    currency = state.get("currency") or "USD"
    metrics = state["computed_metrics"]
    metrics_dict = {k: (getattr(metrics, k) if metrics else "N/A") for k in
                    ("current_ratio", "debt_to_equity", "tol_tnw", "interest_coverage", "dscr", "altman_z_score")}
    payload = {
        "company_name": state["company_name"],
        "currency": currency,
        "benchmark": benchmark_for(currency),
        "metrics": json.dumps(metrics_dict, indent=2),
        "qual_risks": _qual_text(state),
        "violations": json.dumps(state["policy_violations"], indent=2),
        "bounds": _bounds_for_prompt(bounds),
    }

    decision: Optional[CreditDecision] = None
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            decision = CreditDecision.model_validate(_call_llm(payload))
            audit.append(f"DECISION_AGENT: LLM output accepted (model {_model_name()}, attempt {attempt}).")
            break
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            audit.append(f"DECISION_AGENT: LLM output REJECTED by schema validation (attempt {attempt}): {problems}")
        except Exception as e:                      # network, quota, unparseable JSON ...
            audit.append(f"DECISION_AGENT: LLM call failed (attempt {attempt}): {type(e).__name__}: {e}")

    if decision is None:
        state["final_decision"] = None
        audit.append("DECISION_AGENT: No valid LLM decision after retries; no automated decision produced.")
        return state

    final, changes = apply_bounds(decision.model_dump(), bounds)
    final["currency"] = currency
    state["final_decision"] = final
    for c in changes:
        audit.append(f"POLICY_OVERRIDE: {c}")
    audit.append(
        f"DECISION_AGENT: Final decision for {state['company_name']}: rating {final['risk_rating']}, "
        f"limit {currency} {final['approved_credit_limit']:,.2f}, spread +{final['recommended_interest_spread_bps']} bps "
        f"({'adjusted by policy rules' if changes else 'within policy bounds'}).")
    return state
