import copy
import json
import os

import pytest

from src.agents.quant_agent import calculate_quantitative_metrics
from src.agents.compliance_agent import evaluate_compliance_guardrails
from src.validation import assess_data_integrity
from src import data_loader as dl
from tests import ixbrl_fixtures as fx

M = 1_000_000.0


def state_for(fin):
    return {"company_name": "T", "ticker": "T", "raw_financials": fin, "qualitative_notes": "",
            "computed_metrics": None, "extracted_risks": [], "policy_violations": [],
            "final_decision": None, "audit_trail": []}


def run(fin, risks=None):
    s = state_for(fin)
    s["extracted_risks"] = risks or []
    s = calculate_quantitative_metrics(s)
    return evaluate_compliance_guardrails(s)


def infy_fin():
    return dl.build_dossier_from_ixbrl(fx.infy_doc(), fx.META)["financials"]


# ---------------------------------------------------------------- Infosys golden ratios (hand-checked)
def test_infosys_ratios_match_hand_calculation():
    m = run(infy_fin())["computed_metrics"]
    assert m.current_ratio == 1.98                 # 10,912 / 5,516
    assert m.debt_to_equity == 0.10                # 967 / 9,840 (financial debt basis)
    assert m.tol_tnw == 0.80                       # 6,606 / (9,840 - 1,278 - 298)
    assert m.interest_coverage == 86.91            # 4,085 / 47
    assert m.dscr == 11.19                         # 4,085 / (318 + 47)
    assert m.altman_z_score == pytest.approx(8.05, abs=0.01)   # Z'' non-manufacturer
    assert m.net_debt == (967 - 3706) * M          # net cash
    assert m.working_capital == 5396 * M
    assert all(v == "ok" for v in m.status.values())


def test_infosys_triggers_no_violations():
    assert run(infy_fin())["policy_violations"] == []


# ---------------------------------------------------------------- no silent defaults in the quant agent
def test_missing_inputs_give_none_not_defaults():
    fin = infy_fin()
    fin["interest_expense"] = None
    fin["principal_repayment"] = None
    m = calculate_quantitative_metrics(state_for(fin))["computed_metrics"]
    assert m.interest_coverage is None and m.dscr is None            # old code returned 5.0 and 2.0
    assert m.status["interest_coverage"] == "missing_input" and m.status["dscr"] == "missing_input"


def test_missing_data_is_a_critical_violation_not_a_pass():
    fin = infy_fin()
    fin["principal_repayment"] = None
    v = run(fin)["policy_violations"]
    assert any(x["severity"] == "CRITICAL" and x["rule"].startswith("Data Integrity: Minimum DSCR") for x in v)


def test_no_debt_is_not_applicable_and_not_a_violation():
    fin = infy_fin()
    fin.update(financial_debt=0.0, interest_expense=0.0, principal_repayment=0.0)
    s = run(fin)
    m = s["computed_metrics"]
    assert m.dscr is None and m.status["dscr"] == "not_applicable_no_debt_service"
    assert m.interest_coverage is None and m.debt_to_equity == 0.0
    assert not [v for v in s["policy_violations"] if "DSCR" in v["rule"] or "Interest" in v["rule"]]


# ---------------------------------------------------------------- guardrails
def stressed(**over):
    fin = infy_fin()
    fin.update(over)
    return fin


def rules(s):
    return {(v["severity"], v["rule"]) for v in s["policy_violations"]}


def test_dscr_below_policy_is_critical():
    s = run(stressed(principal_repayment=4000 * M))        # 4,085 / 4,047 = 1.01
    assert ("CRITICAL", "Minimum DSCR Requirement") in rules(s)


def test_debt_to_equity_over_limit_is_high():
    s = run(stressed(financial_debt=25000 * M, principal_repayment=1 * M))   # 2.54x
    assert ("HIGH", "Maximum Debt-to-Equity Limit") in rules(s)


def test_tol_tnw_over_limit_is_flagged_without_a_placeholder_note():
    s = run(stressed(total_liabilities=30000 * M))
    v = [x for x in s["policy_violations"] if x["rule"] == "Maximum TOL/TNW"][0]
    assert v["severity"] == "HIGH" and "placeholder" not in v["message"]


def test_interest_coverage_below_policy_is_high():
    s = run(stressed(interest_expense=2500 * M))           # 4,085 / 2,500 = 1.63
    assert ("HIGH", "Minimum Interest Coverage") in rules(s)


def test_altman_distress_and_grey_zone():
    distress = run(stressed(ebit=-4000 * M, net_operating_income=-4000 * M, retained_earnings=-9000 * M))
    assert ("CRITICAL", "Bankruptcy Risk Threshold") in rules(distress)
    grey = run(stressed(retained_earnings=0.0, ebit=300 * M, net_operating_income=300 * M,
                        current_assets=5600 * M))
    z = grey["computed_metrics"].altman_z_score
    assert 1.10 <= z < 2.60, z
    assert ("MEDIUM", "Altman Grey Zone") in rules(grey)


def test_negative_equity_is_critical_not_a_ratio():
    s = run(stressed(shareholder_equity=-100 * M, total_liabilities=16546 * M))
    assert s["computed_metrics"].debt_to_equity is None
    assert any(r[0] == "CRITICAL" and r[1].startswith("Negative Capital Base") for r in rules(s))


def test_high_qualitative_risk_still_flagged():
    s = run(infy_fin(), risks=[{"category": "Litigation", "severity": "HIGH", "description": "x"}])
    assert ("HIGH", "High Qualitative Risk Flag") in rules(s)


def test_audit_entries_record_policy_version():
    s = run(infy_fin())
    assert any("policy 2026.10-p4" in e for e in s["audit_trail"])


# ---------------------------------------------------------------- data gate
def test_gate_blocks_unbalanced_balance_sheet():
    d = {"financials": stressed(total_liabilities=1000 * M), "provenance": {}, "currency": "USD"}
    r = assess_data_integrity(d)
    assert r["status"] == "BLOCK" and any(i["code"] == "BALANCE_SHEET_DOES_NOT_BALANCE" for i in r["issues"])


def test_gate_requires_reason_for_overrides():
    d = dl.build_dossier_from_ixbrl(fx.infy_doc(), fx.META)
    d["provenance"]["sales"] = {"status": "analyst_override", "note": ""}
    assert assess_data_integrity(d)["status"] == "BLOCK"
    d["override_reason"] = "Restated per management accounts, ref 123"
    r = assess_data_integrity(d)
    assert r["status"] == "WARN" and "ANALYST_OVERRIDE" in {i["code"] for i in r["issues"]}


def test_gate_flags_stale_financials():
    import datetime as dt
    d = dl.build_dossier_from_ixbrl(fx.infy_doc(), fx.META)
    r = assess_data_integrity(d, today=dt.date(2028, 6, 1))
    assert "STALE_FINANCIALS" in {i["code"] for i in r["issues"]}


def test_sample_company_works_end_to_end_without_blocking():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = json.load(open(os.path.join(root, "data", "sample_company.json")))
    r = assess_data_integrity(sample)
    assert r["status"] == "WARN" and "UNVERIFIED_INPUTS" in {i["code"] for i in r["issues"]}
    m = run(sample["financials"])["computed_metrics"]
    assert m.debt_to_equity == 0.73 and m.tol_tnw == 1.18 and m.dscr == 2.49
