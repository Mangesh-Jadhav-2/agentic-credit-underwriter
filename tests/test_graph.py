import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_groq")

from src import graph as g
from src import data_loader as dl
from tests import ixbrl_fixtures as fx


@pytest.fixture
def no_llm(monkeypatch):
    """Replace the two LLM nodes so tests never touch Groq, and record whether they were called."""
    calls = {"qual": 0, "decision": 0}

    def fake_qual(state):
        calls["qual"] += 1
        state["extracted_risks"] = []
        return state

    def fake_decision(state):
        calls["decision"] += 1
        state["final_decision"] = {"risk_rating": "LOW_RISK", "approved_credit_limit": 1.0,
                                   "recommended_interest_spread_bps": 100, "key_rationale": [],
                                   "credit_memo_summary": "ok"}
        return state

    monkeypatch.setattr(g, "analyze_qualitative_risks", fake_qual)
    monkeypatch.setattr(g, "synthesize_credit_decision", fake_decision)
    return calls


def test_blocked_data_stops_before_any_agent_or_llm(no_llm):
    doc = fx.build({k: v for k, v in fx.INFY_INSTANT.items() if k != "ifrs-full:Equity"}, fx.INFY_DURATION)
    d = dl.build_dossier_from_ixbrl(doc, fx.META)
    out = g.run_underwriting_pipeline(d)
    assert out["decision_status"] == "BLOCKED_DATA_INTEGRITY"
    assert out["final_decision"] is None and out["computed_metrics"] is None
    assert no_llm == {"qual": 0, "decision": 0}
    assert any("BLOCKED by data-integrity gate" in e for e in out["audit_trail"])
    assert any("MISSING_INPUT" in e for e in out["audit_trail"])


def test_clean_data_runs_all_nodes_and_logs_provenance(no_llm):
    d = dl.build_dossier_from_ixbrl(fx.infy_doc(), fx.META)
    out = g.run_underwriting_pipeline(d)
    assert out["decision_status"] == "COMPLETED"
    assert out["computed_metrics"].dscr == 11.19
    assert no_llm["decision"] == 1 and no_llm["qual"] == 0      # no narrative -> qualitative LLM skipped
    trail = "\n".join(out["audit_trail"])
    assert "policy_version=2026.10-p4" in trail and f"run_id={out['run_id']}" in trail
    assert "accession 0001193125-26-270520" in trail
    assert "QUAL_AGENT: Skipped" in trail


def test_analyst_override_is_logged(no_llm):
    d = dl.build_dossier_from_ixbrl(fx.infy_doc(), fx.META)
    d["provenance"]["sales"] = {"status": "analyst_override", "note": ""}
    d["analyst_overrides"] = [{"field": "sales", "original": 20158e6, "new": 20000e6}]
    d["override_reason"] = "management accounts"
    out = g.run_underwriting_pipeline(d)
    assert any("ANALYST_OVERRIDE: sales" in e and "management accounts" in e for e in out["audit_trail"])
    assert out["decision_status"] == "COMPLETED"


def test_missing_interest_expense_runs_but_flags_dscr_as_not_computable(no_llm):
    from tests.test_loader import AAPL_INSTANT, AAPL_DURATION
    d = dl.build_dossier_from_ixbrl(fx.build(AAPL_INSTANT, AAPL_DURATION, with_decoys=False), dict(fx.META, form="10-K"))
    out = g.run_underwriting_pipeline(d)
    assert out["decision_status"] == "COMPLETED"
    assert out["computed_metrics"].dscr is None and out["computed_metrics"].interest_coverage is None
    assert out["computed_metrics"].status["dscr"] == "missing_input"
    assert any(v["severity"] == "CRITICAL" and "Data Integrity" in v["rule"] for v in out["policy_violations"])
