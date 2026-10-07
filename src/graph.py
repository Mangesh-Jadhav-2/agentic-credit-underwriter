
import os
import json
import uuid
from typing import Dict, List, TypedDict, Optional, Any
from langgraph.graph import StateGraph, END  # Imports LangGraph's state machine builder and terminal node

# Import individual agent functions built in prior steps
from src.agents.quant_agent import calculate_quantitative_metrics
from src.agents.qual_agent import analyze_qualitative_risks
from src.agents.compliance_agent import evaluate_compliance_guardrails
from src.agents.decision_agent import synthesize_credit_decision
from src.validation import assess_data_integrity, summarize
from src.policy import policy_snapshot
from src.decision_rules import REFER
from src import audit_log

# Define the shared state dictionary passed between every agent node in the graph
class UnderwritingState(TypedDict):
    company_name: str                      # Name of the target borrowing firm
    ticker: str                            # Stock ticker or unique company ID
    raw_financials: Dict[str, Any]         # Key balance sheet & income statement line items (None = missing)
    provenance: Dict[str, Any]             # Per-field source: reported | derived | none_reported | missing | analyst_override
    currency: str                          # Reporting currency of the amounts above
    fiscal_year_end: Optional[str]         # ISO date of the period the figures describe
    warnings: List[str]                    # Loader warnings passed through to the gate
    qualitative_notes: str                 # Unstructured auditor notes, footers, & disclosures
    qualitative_available: bool            # False => narrative disclosures were not extracted
    qualitative_evidence: List[Dict[str, str]]  # Retrieved filing passages the qual agent may cite
    qualitative_status: str                # PENDING | COMPLETED | SKIPPED | FAILED
    override_reason: str                   # Mandatory when any analyst override is present
    data_quality: Optional[Dict[str, Any]] # Output of the data-integrity gate
    decision_status: str                   # PENDING | BLOCKED_DATA_INTEGRITY | REFERRED_TO_ANALYST | COMPLETED | DECISION_FAILED
    run_id: str                            # Unique id of this run (also in the persisted audit record)
    audit_record: Optional[Dict[str, Any]] # {path, record_hash, prev_hash} of the persisted audit record, or {error}
    computed_metrics: Optional[Any]        # Stores Pydantic model output from Node 1 (ratios)
    extracted_risks: List[Dict[str, str]]  # Stores LLM risk extractions from Node 2
    policy_violations: List[Dict[str, str]]# Stores policy flag warnings from Node 3
    final_decision: Optional[Dict[str, Any]]# Stores final Credit Memo from Node 4
    audit_trail: List[str]                 # Sequential audit log recording execution steps


def validate_data_node(state: UnderwritingState) -> UnderwritingState:
    """Node 0: Data-integrity gate. Blocks the automated decision on missing/inconsistent inputs."""
    report = assess_data_integrity({
        "financials": state["raw_financials"],
        "provenance": state["provenance"],
        "currency": state["currency"],
        "fiscal_year_end": state["fiscal_year_end"],
        "qualitative_available": state["qualitative_available"],
        "override_reason": state["override_reason"],
        "warnings": state["warnings"],
    })
    state["data_quality"] = report
    state["audit_trail"].append(summarize(report))
    for issue in report["issues"]:
        if issue["severity"] == "BLOCK":
            state["audit_trail"].append(f"DATA_VALIDATOR BLOCK [{issue['code']}] {issue['message']}")
    if report["status"] == "BLOCK":
        state["decision_status"] = "BLOCKED_DATA_INTEGRITY"
        state["final_decision"] = None
        state["audit_trail"].append(
            "DECISION: Automated credit decision BLOCKED by data-integrity gate; referred to a credit analyst.")
    return state


def route_after_validation(state: UnderwritingState) -> str:
    return "blocked" if state["decision_status"] == "BLOCKED_DATA_INTEGRITY" else "proceed"


def qual_node(state: UnderwritingState) -> UnderwritingState:
    """Node 2 wrapper: skip the LLM when there is no narrative text, so it cannot invent risks."""
    if not state["qualitative_available"]:
        state["extracted_risks"] = []
        state["qualitative_status"] = "SKIPPED"
        state["audit_trail"].append(
            "QUAL_AGENT: Skipped - no narrative disclosures were extracted for this entity (no LLM call made).")
        return state
    return analyze_qualitative_risks(state)


def decision_node(state: UnderwritingState) -> UnderwritingState:
    """Node 4 wrapper: record a terminal status for the run."""
    state = synthesize_credit_decision(state)
    decision = state.get("final_decision")
    if not decision:
        state["decision_status"] = "DECISION_FAILED"
    elif decision.get("risk_rating") == REFER:
        state["decision_status"] = "REFERRED_TO_ANALYST"
    else:
        state["decision_status"] = "COMPLETED"
    return state


def build_underwriting_graph():
    """
    Constructs and compiles the graph defining agent execution order.
    """
    workflow = StateGraph(UnderwritingState)

    workflow.add_node("data_validator", validate_data_node)               # Node 0: integrity gate
    workflow.add_node("quant_analyst", calculate_quantitative_metrics)     # Node 1: ratios
    workflow.add_node("qual_analyst", qual_node)                           # Node 2: LLM risk extraction
    workflow.add_node("compliance_officer", evaluate_compliance_guardrails)# Node 3: deterministic guardrails
    workflow.add_node("chief_underwriter", decision_node)                  # Node 4: LLM memo

    workflow.set_entry_point("data_validator")
    workflow.add_conditional_edges("data_validator", route_after_validation,
                                   {"blocked": END, "proceed": "quant_analyst"})
    workflow.add_edge("quant_analyst", "qual_analyst")
    workflow.add_edge("qual_analyst", "compliance_officer")
    workflow.add_edge("compliance_officer", "chief_underwriter")
    workflow.add_edge("chief_underwriter", END)

    return workflow.compile()


def run_underwriting_pipeline(company_data: Dict[str, Any]) -> UnderwritingState:
    """
    Entry point called by the Streamlit dashboard or external APIs.
    Accepts a dossier, sets initial state, and invokes the LangGraph state machine.
    """
    audit: List[str] = []
    run_id = str(uuid.uuid4())
    snap = policy_snapshot()
    audit.append(f"RUN_START: run_id={run_id} policy_version={snap['version']} "
                 f"unconfirmed_policy_values={snap['unconfirmed']}")
    src = company_data.get("source") or {}
    if src:
        audit.append(f"DATA_SOURCE: {src.get('form')} accession {src.get('accession')} filed {src.get('filing_date')} "
                     f"| fiscal year end {company_data.get('fiscal_year_end')} | currency {company_data.get('currency', 'USD')} "
                     f"| extraction method {src.get('method') or 'n/a'} | {src.get('url')}")
    else:
        audit.append("DATA_SOURCE: no filing metadata (sample or manually supplied data)")
    for o in company_data.get("analyst_overrides", []):
        picked = f" | picked filing fact: {o['source_concept']}" if o.get("source_concept") else ""
        audit.append(f"ANALYST_OVERRIDE: {o['field']}: {o.get('original')} -> {o.get('new')}{picked} "
                     f"| reason: {company_data.get('override_reason') or '(none given)'}")

    initial_state: UnderwritingState = {
        "company_name": company_data.get("company_name", "Unknown Entity"),
        "ticker": company_data.get("ticker", "N/A"),
        "raw_financials": company_data.get("financials", {}),
        "provenance": company_data.get("provenance", {}),
        "currency": company_data.get("currency", "USD"),
        "fiscal_year_end": company_data.get("fiscal_year_end"),
        "warnings": company_data.get("warnings", []),
        "qualitative_notes": company_data.get("qualitative_notes", ""),
        "qualitative_available": company_data.get("qualitative_available", True),
        "qualitative_evidence": company_data.get("qualitative_evidence", []),
        "qualitative_status": "PENDING",
        "override_reason": company_data.get("override_reason", ""),
        "data_quality": None,
        "decision_status": "PENDING",
        "run_id": run_id,
        "audit_record": None,
        "computed_metrics": None,
        "extracted_risks": [],
        "policy_violations": [],
        "final_decision": None,
        "audit_trail": audit,
    }
    graph_app = build_underwriting_graph()
    final_state = graph_app.invoke(initial_state)

    # Persist the run (every run, including blocked ones). A logging failure is surfaced, never hidden.
    try:
        record = audit_log.build_record(run_id, company_data, final_state, snap,
                                        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"))
        final_state["audit_record"] = audit_log.append_record(record)
        final_state["audit_trail"].append(
            f"AUDIT_LOG: record {final_state['audit_record']['record_hash'][:16]}... written to "
            f"{final_state['audit_record']['path']}")
    except Exception as exc:
        final_state["audit_record"] = {"error": f"{type(exc).__name__}: {exc}"}
        final_state["audit_trail"].append(f"AUDIT_LOG_WRITE_FAILED: {type(exc).__name__}: {exc}")
    return final_state
