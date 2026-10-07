import os
import json
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_LLM_ATTEMPTS = 2
MAX_RISKS = 15

Category = Literal["Going Concern", "Debt Covenant / Default", "Litigation / Regulatory", "Liquidity / Refinancing",
                   "Customer / Supplier Concentration", "Internal Controls / Restatement", "Other"]


class RiskFactor(BaseModel):
    """One finding. It must point at the evidence passage it comes from and quote it verbatim."""
    model_config = ConfigDict(extra="ignore")

    category: Category = Field(description="One of: Going Concern, Debt Covenant / Default, Litigation / Regulatory, "
                                           "Liquidity / Refinancing, Customer / Supplier Concentration, "
                                           "Internal Controls / Restatement, Other")
    severity: Literal["HIGH", "MEDIUM", "LOW"] = Field(description="HIGH, MEDIUM or LOW")
    description: str = Field(min_length=10, description="One or two sentences on the specific risk and why it matters for credit")
    evidence_id: str = Field(description="The id of the evidence passage this comes from, e.g. C0007")
    quote: str = Field(min_length=10, max_length=500, description="An exact, word-for-word excerpt (under 400 characters) from that passage")

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class QualitativeRiskReport(BaseModel):
    """Used for the prompt's format instructions: the model must see the exact field names of a finding."""
    risks: List[RiskFactor] = Field(description="List of risk findings; an empty list if nothing is credit-relevant")


class AgentState(TypedDict):
    company_name: str
    ticker: str
    raw_financials: Dict[str, float]
    qualitative_notes: str
    qualitative_evidence: List[Dict[str, str]]
    qualitative_status: str
    computed_metrics: Optional[object]
    extracted_risks: List[Dict[str, str]]
    audit_trail: List[str]


def _model_name() -> str:
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")).strip().lower()


def ground(risk: RiskFactor, evidence_by_id: Dict[str, Dict[str, str]]) -> Optional[str]:
    """None if the finding is supported by the evidence, otherwise the reason it is not."""
    ev = evidence_by_id.get(risk.evidence_id.strip())
    if ev is None:
        return f"unknown evidence id {risk.evidence_id!r}"
    quote = _norm(risk.quote).strip(" .…")
    if "..." in quote or "…" in risk.quote:
        parts = [p.strip(" .") for p in re.split(r"\.\.\.|…", quote) if len(p.strip(" .")) >= 10]
        ok = bool(parts) and all(p in _norm(ev["text"]) for p in parts)
    else:
        ok = quote in _norm(ev["text"])
    return None if ok else "quote is not a verbatim excerpt of the cited passage"


def _evidence(state: AgentState) -> List[Dict[str, str]]:
    ev = [dict(e) for e in (state.get("qualitative_evidence") or [])]
    notes = (state.get("qualitative_notes") or "").strip()
    if notes:
        ev.append({"id": "ANALYST", "section": "Analyst notes", "text": notes})
    return ev


def _call_llm(company_name: str, evidence: List[Dict[str, str]]) -> Dict[str, Any]:
    """The only place the LLM is called (kept separate so tests can replace it)."""
    from langchain_groq import ChatGroq
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import JsonOutputParser

    llm = ChatGroq(model_name=_model_name(), temperature=0)
    parser = JsonOutputParser(pydantic_object=QualitativeRiskReport)
    passages = "\n\n".join(f"[{e['id']}] ({e['section']})\n{e['text']}" for e in evidence)
    prompt = ChatPromptTemplate.from_template(
        "You are a Senior Risk Officer at an international commercial bank, reviewing disclosures from an annual "
        "filing for corporate loan underwriting.\n\n"
        "Company: {company_name}\n\nEvidence passages (the ONLY source you may use):\n{passages}\n\n"
        "Instructions:\n"
        "- Report credit-relevant risks that the passages actually state: going concern doubt, covenant breach or "
        "default, material litigation or regulatory action, liquidity or refinancing pressure, customer/supplier "
        "concentration, material weakness or restatement.\n"
        "- Every finding needs evidence_id and a quote copied word for word from that passage (under 400 characters).\n"
        "- Severity: HIGH only when the text states a CURRENT, specific event (an actual default or covenant breach, "
        "substantial doubt about going concern, a material adverse ruling or penalty, an identified material weakness). "
        "Generic or hypothetical boilerplate ('we may be subject to...') is LOW, or leave it out.\n"
        "- Do not invent facts, figures or events. If nothing is credit-relevant, return an empty list.\n"
        "- Use at most {max_risks} findings.\n\n{format_instructions}"
    )
    chain = prompt | llm | parser
    return chain.invoke({"company_name": company_name, "passages": passages, "max_risks": MAX_RISKS,
                         "format_instructions": parser.get_format_instructions()})


def analyze_qualitative_risks(state: AgentState) -> AgentState:
    """
    Node 2: Qualitative Risk Agent.
    Reads only the retrieved filing passages, and keeps only findings that validate against the schema AND
    quote their cited passage verbatim. Everything dropped is logged with the reason.
    """
    audit = state["audit_trail"]
    evidence = _evidence(state)
    state["extracted_risks"] = []
    if not evidence:
        state["qualitative_status"] = "SKIPPED"
        audit.append("QUAL_AGENT: Skipped - no evidence passages to analyse (no LLM call made).")
        return state
    by_id = {e["id"]: e for e in evidence}

    parsed: Optional[List[RiskFactor]] = None
    dropped = 0
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            response = _call_llm(state["company_name"], evidence)
            if not isinstance(response, dict) or not isinstance(response.get("risks"), list):
                raise ValueError("response has no 'risks' list")
        except Exception as e:                       # network, quota, unparseable JSON
            audit.append(f"QUAL_AGENT: LLM call failed (attempt {attempt}): {type(e).__name__}: {e}")
            continue
        valid, bad = [], 0
        for raw in response["risks"][:MAX_RISKS]:
            try:
                valid.append(RiskFactor.model_validate(raw))
            except ValidationError as e:
                bad += 1
                audit.append(f"QUAL_AGENT: Finding failed schema validation (attempt {attempt}): "
                             + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
        if bad and not valid:                        # the whole answer was malformed: retry, never treat as "no risks"
            continue
        parsed, dropped = valid, bad
        break

    if parsed is None:
        state["qualitative_status"] = "FAILED"
        audit.append("QUAL_AGENT: No usable LLM response; the qualitative review was NOT performed for this run.")
        return state

    kept: List[Dict[str, str]] = []
    for risk in parsed:
        why = ground(risk, by_id)
        if why:
            dropped += 1
            audit.append(f"QUAL_AGENT: Dropped ungrounded finding [{risk.category}] {risk.description[:80]!r}: {why}")
            continue
        kept.append({"category": risk.category, "severity": risk.severity, "description": risk.description,
                     "evidence_id": risk.evidence_id.strip(), "quote": risk.quote.strip(),
                     "section": by_id[risk.evidence_id.strip()]["section"]})

    state["extracted_risks"] = kept
    state["qualitative_status"] = "COMPLETED"
    audit.append(f"QUAL_AGENT: Analysed {len(evidence)} filing passages for {state['company_name']} with model "
                 f"{_model_name()}; kept {len(kept)} grounded risk finding(s), dropped {dropped}.")
    return state
