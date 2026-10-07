import json
import os

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_groq")

from src import audit_log
from src import graph as g
from src import data_loader as dl
from src.agents import decision_agent as da
from src.decision_rules import REFER
from tests import ixbrl_fixtures as fx
from tests.test_loader import AAPL_INSTANT, AAPL_DURATION, MSFT_INSTANT, MSFT_DURATION

EQUITY = 1_000_000_000.0


def good_llm(**over):
    base = {"risk_rating": "LOW_RISK", "approved_credit_limit": 900_000_000.0,
            "recommended_interest_spread_bps": 50, "key_rationale": ["strong liquidity"],
            "credit_memo_summary": "The borrower shows strong liquidity and conservative leverage overall."}
    return dict(base, **over)


def state(violations=(), llm_calls=None):
    return {"company_name": "Acme", "ticker": "ACME", "raw_financials": {"shareholder_equity": EQUITY},
            "computed_metrics": None, "extracted_risks": [], "policy_violations": list(violations),
            "final_decision": None, "audit_trail": [], "qualitative_notes": ""}


def v(severity, rule, msg="x"):
    return {"severity": severity, "rule": rule, "message": msg}


@pytest.fixture
def llm(monkeypatch):
    box = {"calls": 0, "queue": [good_llm()]}

    def fake(payload):
        box["calls"] += 1
        item = box["queue"].pop(0) if len(box["queue"]) > 1 else box["queue"][0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(da, "_call_llm", fake)
    return box


# ------------------------------------------------------------------ deterministic bounds
def test_clean_case_within_bounds_is_accepted_but_limit_still_capped_by_equity(llm):
    llm["queue"] = [good_llm(approved_credit_limit=900_000_000.0)]          # 90% of equity: far above the 10% cap
    out = da.synthesize_credit_decision(state())
    assert out["final_decision"]["risk_rating"] == "LOW_RISK"
    assert out["final_decision"]["approved_credit_limit"] == 0.10 * EQUITY
    assert out["final_decision"]["recommended_interest_spread_bps"] == 125          # raised to the LOW_RISK minimum
    assert any(l.startswith("POLICY_OVERRIDE: limit") for l in out["audit_trail"])


def test_llm_cannot_rate_better_than_the_dscr_floor(llm):
    out = da.synthesize_credit_decision(state([v("CRITICAL", "Minimum DSCR Requirement")]))
    d = out["final_decision"]
    assert d["risk_rating"] == "HIGH_RISK"
    assert d["approved_credit_limit"] == 0.02 * EQUITY
    assert d["recommended_interest_spread_bps"] == 400
    assert any("POLICY OVERRIDE: rating LOW_RISK -> HIGH_RISK" in r for r in d["key_rationale"])
    assert "Deterministic policy rules adjusted" in d["credit_memo_summary"]


def test_altman_distress_forces_reject_with_zero_limit_and_no_spread(llm):
    d = da.synthesize_credit_decision(state([v("CRITICAL", "Bankruptcy Risk Threshold")]))["final_decision"]
    assert d["risk_rating"] == "REJECT" and d["approved_credit_limit"] == 0 and d["recommended_interest_spread_bps"] == 0


def test_negative_capital_base_forces_reject(llm):
    d = da.synthesize_credit_decision(state([v("CRITICAL", "Negative Capital Base: Maximum Debt-to-Equity")]))["final_decision"]
    assert d["risk_rating"] == "REJECT"


def test_one_high_flag_floors_moderate_two_floor_high(llm):
    one = da.synthesize_credit_decision(state([v("HIGH", "Maximum Debt-to-Equity Limit")]))["final_decision"]
    two = da.synthesize_credit_decision(state([v("HIGH", "Maximum Debt-to-Equity Limit"),
                                               v("HIGH", "Minimum Interest Coverage")]))["final_decision"]
    assert one["risk_rating"] == "MODERATE_RISK" and two["risk_rating"] == "HIGH_RISK"


def test_llm_may_be_stricter_than_the_floor(llm):
    llm["queue"] = [good_llm(risk_rating="REJECT")]
    d = da.synthesize_credit_decision(state([v("HIGH", "Maximum Debt-to-Equity Limit")]))["final_decision"]
    assert d["risk_rating"] == "REJECT" and d["approved_credit_limit"] == 0


def test_non_positive_equity_means_zero_limit(llm):
    s = state()
    s["raw_financials"] = {"shareholder_equity": -5.0}
    assert da.synthesize_credit_decision(s)["final_decision"]["approved_credit_limit"] == 0


# ------------------------------------------------------------------ referral on data gaps
def test_critical_data_gap_refers_to_analyst_without_calling_the_llm(llm):
    out = da.synthesize_credit_decision(state([v("CRITICAL", "Data Integrity: Minimum DSCR")]))
    d = out["final_decision"]
    assert d["risk_rating"] == REFER and d["approved_credit_limit"] == 0
    assert llm["calls"] == 0
    assert any("REFERRED TO ANALYST" in l for l in out["audit_trail"])


# ------------------------------------------------------------------ schema validation of LLM output
@pytest.mark.parametrize("bad", [
    {"risk_rating": "SAFE"},
    {"approved_credit_limit": -1},
    {"approved_credit_limit": float("nan")},
    {"recommended_interest_spread_bps": 99999},
    {"key_rationale": []},
    {"credit_memo_summary": "ok"},
])
def test_invalid_llm_output_is_rejected_not_repaired(llm, bad):
    llm["queue"] = [good_llm(**bad)]
    out = da.synthesize_credit_decision(state())
    assert out["final_decision"] is None
    assert sum("REJECTED by schema validation" in l for l in out["audit_trail"]) == da.MAX_LLM_ATTEMPTS
    assert llm["calls"] == da.MAX_LLM_ATTEMPTS


def test_one_bad_answer_then_a_good_one_is_accepted_on_retry(llm):
    llm["queue"] = [good_llm(risk_rating="SAFE"), good_llm()]
    out = da.synthesize_credit_decision(state())
    assert out["final_decision"]["risk_rating"] == "LOW_RISK"
    assert any("attempt 2" in l and "accepted" in l for l in out["audit_trail"])


def test_rating_text_is_normalised_but_nothing_else_is_guessed(llm):
    llm["queue"] = [good_llm(risk_rating=" moderate risk ")]
    assert da.synthesize_credit_decision(state())["final_decision"]["risk_rating"] == "MODERATE_RISK"


def test_llm_exception_is_logged_and_yields_no_decision(llm):
    llm["queue"] = [RuntimeError("quota exceeded")]
    out = da.synthesize_credit_decision(state())
    assert out["final_decision"] is None
    assert any("LLM call failed" in l and "quota exceeded" in l for l in out["audit_trail"])


# ------------------------------------------------------------------ end to end through the graph
def run_graph(instant, duration, monkeypatch, llm_payload=None):
    calls = {"n": 0}

    def fake(payload):
        calls["n"] += 1
        return llm_payload or good_llm()

    monkeypatch.setattr(da, "_call_llm", fake)
    d = dl.build_dossier_from_ixbrl(fx.build(instant, duration, with_decoys=False), dict(fx.META, form="10-K"))
    return g.run_underwriting_pipeline(d), calls


def test_aapl_style_missing_interest_is_referred_to_an_analyst_end_to_end(monkeypatch):
    out, calls = run_graph(AAPL_INSTANT, AAPL_DURATION, monkeypatch)
    assert out["decision_status"] == "REFERRED_TO_ANALYST"
    assert out["final_decision"]["risk_rating"] == REFER
    assert calls["n"] == 0


def test_msft_style_complete_data_gets_a_bounded_llm_decision(monkeypatch):
    out, calls = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    assert out["decision_status"] == "COMPLETED" and calls["n"] == 1
    assert out["final_decision"]["approved_credit_limit"] <= 0.10 * 442387e6


# ------------------------------------------------------------------ persisted, tamper-evident audit log
def test_every_run_is_persisted_with_hashes_policy_and_model(monkeypatch):
    monkeypatch.setenv("GROQ_MODEL", "test-model-1")
    out, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    path = out["audit_record"]["path"]
    rec = json.loads(open(path).read().splitlines()[-1])
    assert rec["run_id"] == out["run_id"] and rec["llm_model"] == "test-model-1"
    assert rec["policy_version"].startswith("2026.10-p4")
    assert len(rec["input_hash"]) == 64 and len(rec["output_hash"]) == 64
    assert rec["decision"]["risk_rating"] == out["final_decision"]["risk_rating"]
    assert rec["decision_rules"]["limit_pct_of_equity"]["REJECT"] == 0.0
    assert audit_log.verify_chain(path) == (True, [])


def test_blocked_runs_are_logged_too(monkeypatch):
    inst = {k: v_ for k, v_ in MSFT_INSTANT.items() if k != "us-gaap:StockholdersEquity"}
    out, calls = run_graph(inst, MSFT_DURATION, monkeypatch)
    assert out["decision_status"] == "BLOCKED_DATA_INTEGRITY" and calls["n"] == 0
    rec = json.loads(open(out["audit_record"]["path"]).read().splitlines()[-1])
    assert rec["decision_status"] == "BLOCKED_DATA_INTEGRITY" and rec["decision"] is None


def test_same_inputs_give_the_same_input_hash_and_chain_links_records(monkeypatch):
    a, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    b, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    lines = [json.loads(l) for l in open(a["audit_record"]["path"]).read().splitlines()]
    assert len(lines) == 2 and lines[0]["input_hash"] == lines[1]["input_hash"]
    assert lines[1]["prev_hash"] == lines[0]["record_hash"] == a["audit_record"]["record_hash"]
    assert lines[0]["run_id"] != lines[1]["run_id"]


def test_tampering_is_detected(monkeypatch):
    out, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    path = out["audit_record"]["path"]
    text = open(path).read()
    open(path, "w").write(text.replace('"LOW_RISK"', '"HIGH_RISK"', 1) if '"LOW_RISK"' in text
                          else text.replace("COMPLETED", "REJECTED", 1))
    ok, problems = audit_log.verify_chain(path)
    assert not ok and any("edited" in p for p in problems)


def test_deleting_a_record_is_detected(monkeypatch):
    out, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    for _ in range(2):
        run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    path = out["audit_record"]["path"]
    lines = open(path).read().splitlines()
    open(path, "w").write("\n".join([lines[0], lines[2]]) + "\n")
    ok, problems = audit_log.verify_chain(path)
    assert not ok and any("link broken" in p for p in problems)


def test_log_write_failure_is_surfaced_not_hidden(monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("AUDIT_LOG_DIR", str(blocker / "sub"))        # cannot create a directory under a file
    out, _ = run_graph(MSFT_INSTANT, MSFT_DURATION, monkeypatch)
    assert "error" in out["audit_record"]
    assert any(l.startswith("AUDIT_LOG_WRITE_FAILED") for l in out["audit_trail"])
    assert out["decision_status"] == "COMPLETED"                     # the decision itself is unaffected


def test_limit_cannot_exceed_debt_to_equity_headroom(llm):
    st = state()
    st["raw_financials"] = {"shareholder_equity": EQUITY, "financial_debt": 1.95 * EQUITY}   # only 0.05x equity of room
    d = da.synthesize_credit_decision(st)["final_decision"]
    assert d["approved_credit_limit"] == pytest.approx(0.05 * EQUITY)
    st["raw_financials"]["financial_debt"] = 2.5 * EQUITY                                      # already over the D/E max
    assert da.synthesize_credit_decision(st)["final_decision"]["approved_credit_limit"] == 0.0


def test_limit_is_labelled_in_the_filing_currency_and_prompt_asks_for_it(monkeypatch):
    seen = {}

    def fake(payload):
        seen.update(payload)
        return good_llm(approved_credit_limit=5e9)
    monkeypatch.setattr(da, "_call_llm", fake)
    st = state()
    st["currency"] = "INR"
    d = da.synthesize_credit_decision(st)["final_decision"]
    assert d["currency"] == "INR" and seen["currency"] == "INR" and "INR" in seen["benchmark"]
    assert any("limit INR" in l for l in st["audit_trail"])


def test_usd_default_and_sofr_benchmark(llm):
    d = da.synthesize_credit_decision(state())["final_decision"]
    assert d["currency"] == "USD" and da.benchmark_for("USD") == "SOFR"


def test_memo_leads_with_the_final_terms_when_policy_changed_them(llm):
    llm["queue"] = [good_llm(approved_credit_limit=900_000_000.0, credit_memo_summary="We approve a facility of 900 million overall.")]
    d = da.synthesize_credit_decision(state())["final_decision"]
    assert d["credit_memo_summary"].startswith("FINAL TERMS AFTER POLICY RULES: LOW_RISK, limit 100,000,000")
    assert "superseded proposal" in d["credit_memo_summary"]


def test_prompt_carries_absolute_limit_caps(monkeypatch):
    seen = {}
    monkeypatch.setattr(da, "_call_llm", lambda p: (seen.update(p), good_llm())[1])
    da.synthesize_credit_decision(state())
    assert '"max_limit_by_rating"' in seen["bounds"] and '"REJECT": 0' in seen["bounds"]
