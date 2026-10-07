import pytest

from src import narrative
from src.agents import qual_agent as qa

pytest.importorskip("langgraph")

RISK = ("The Company has incurred recurring losses and, as disclosed in Note 9, there is substantial doubt about "
        "its ability to continue as a going concern. The Company was not in compliance with the leverage covenant "
        "under its revolving credit facility at year end and obtained a waiver from its lenders.")
LEGAL = ("The Company is a defendant in a putative class action lawsuit alleging securities violations. "
         "The litigation is at an early stage and the Company cannot estimate the possible penalty or settlement.")
FILLER = "<p>" + ("Our products are sold worldwide through distributors and direct channels. " * 40) + "</p>"


def filing():
    return f"""<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
<ix:header><p>HIDDEN HEADER GOING CONCERN substantial doubt</p></ix:header>
<p>Table of Contents</p><p>Item 1A. Risk Factors 12</p><p>Item 1B. Unresolved Staff Comments 20</p>
<p>Item 3. Legal Proceedings 21</p><p>Item 4. Mine Safety Disclosures 22</p>
<p>Item 1A. Risk Factors</p>{FILLER}<p>{RISK}</p>{FILLER}
<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>
<p>Item 3. Legal Proceedings</p>{FILLER}<p>{LEGAL}</p>{FILLER}
<p>Item 4. Mine Safety Disclosures</p><p>Not applicable.</p>
<script>var x = 'substantial doubt';</script></body></html>""".encode()


def test_text_extraction_drops_hidden_header_and_scripts():
    t = narrative.html_to_text(filing())
    assert "HIDDEN HEADER" not in t and "var x" not in t and "going concern" in t


def test_sections_skip_table_of_contents_and_find_real_spans():
    secs = narrative.find_sections(narrative.html_to_text(filing()))
    assert set(secs) == {"Risk Factors", "Legal Proceedings"}
    assert "substantial doubt" in secs["Risk Factors"] and "class action" in secs["Legal Proceedings"]
    assert "class action" not in secs["Risk Factors"]


def test_retrieval_surfaces_the_credit_passages_and_not_filler():
    ev, info = narrative.build_evidence(filing())
    text = " ".join(e["text"] for e in ev)
    assert "substantial doubt" in text and "class action" in text
    assert all("Our products are sold worldwide" not in e["text"] or "substantial doubt" in e["text"]
               or "class action" in e["text"] for e in ev)
    assert sum(len(e["text"]) for e in ev) <= narrative.MAX_EVIDENCE_CHARS
    assert len({e["id"] for e in ev}) == len(ev)


def test_attach_without_credit_text_leaves_qualitative_unavailable():
    d = {"warnings": [], "qualitative_available": False}
    narrative.attach(d, b"<html><body><p>" + b"Nothing relevant here at all. " * 200 + b"</p></body></html>")
    assert d["qualitative_available"] is False and d["warnings"]


# ------------------------------------------------------------------ the qualitative agent
def state_with_evidence():
    ev, _ = narrative.build_evidence(filing())
    return {"company_name": "Acme", "ticker": "ACME", "raw_financials": {}, "qualitative_notes": "",
            "qualitative_evidence": ev, "qualitative_status": "PENDING", "computed_metrics": None,
            "extracted_risks": [], "audit_trail": []}, ev


def cid(ev, needle):
    return next(e["id"] for e in ev if needle in e["text"])


def test_grounded_findings_kept_ungrounded_and_malformed_dropped(monkeypatch):
    st, ev = state_with_evidence()
    gc, lg = cid(ev, "going concern"), cid(ev, "class action")
    good = {"category": "Going Concern", "severity": "high", "description": "Auditor-level going concern doubt is disclosed.",
            "evidence_id": gc, "quote": "there is substantial doubt about its ability to continue as a going concern"}
    invented = dict(good, category="Litigation / Regulatory", evidence_id=lg,
                    quote="The Company paid a 500 million dollar fine to the SEC")
    bad_id = dict(good, evidence_id="C9999")
    bad_enum = dict(good, severity="CATASTROPHIC")
    ellipsis = {"category": "Debt Covenant / Default", "severity": "HIGH", "description": "Covenant breach with waiver obtained.",
                "evidence_id": gc, "quote": "was not in compliance with the leverage covenant ... obtained a waiver from its lenders"}
    monkeypatch.setattr(qa, "_call_llm", lambda name, evidence: {"risks": [good, invented, bad_id, bad_enum, ellipsis]})
    out = qa.analyze_qualitative_risks(st)
    assert out["qualitative_status"] == "COMPLETED"
    assert [r["category"] for r in out["extracted_risks"]] == ["Going Concern", "Debt Covenant / Default"]
    assert out["extracted_risks"][0]["severity"] == "HIGH" and out["extracted_risks"][0]["section"] == "Risk Factors"
    log = "\n".join(out["audit_trail"])
    assert "kept 2 grounded" in log and "dropped 3" in log and "not a verbatim excerpt" in log and "unknown evidence id" in log


def test_llm_failure_is_reported_not_silent(monkeypatch):
    st, _ = state_with_evidence()
    calls = {"n": 0}

    def boom(name, evidence):
        calls["n"] += 1
        raise RuntimeError("quota")
    monkeypatch.setattr(qa, "_call_llm", boom)
    out = qa.analyze_qualitative_risks(st)
    assert calls["n"] == qa.MAX_LLM_ATTEMPTS and out["qualitative_status"] == "FAILED" and out["extracted_risks"] == []
    assert any("NOT performed" in l for l in out["audit_trail"])


def test_no_evidence_means_no_llm_call(monkeypatch):
    monkeypatch.setattr(qa, "_call_llm", lambda *a: pytest.fail("LLM must not be called"))
    out = qa.analyze_qualitative_risks({"company_name": "A", "qualitative_notes": "", "qualitative_evidence": [],
                                        "extracted_risks": [], "audit_trail": []})
    assert out["qualitative_status"] == "SKIPPED"


def test_analyst_notes_are_citable_evidence(monkeypatch):
    note = "Management confirmed the largest customer, 40% of revenue, is renegotiating its contract."
    st = {"company_name": "A", "qualitative_notes": note, "qualitative_evidence": [], "extracted_risks": [], "audit_trail": []}
    r = {"category": "Customer / Supplier Concentration", "severity": "MEDIUM", "description": "One customer is 40% of revenue.",
         "evidence_id": "ANALYST", "quote": "largest customer, 40% of revenue, is renegotiating"}
    monkeypatch.setattr(qa, "_call_llm", lambda n, e: {"risks": [r]})
    assert len(qa.analyze_qualitative_risks(st)["extracted_risks"]) == 1


def test_malformed_answer_is_retried_and_never_reported_as_no_risks(monkeypatch):
    st, ev = state_with_evidence()
    gc = cid(ev, "going concern")
    wrong_keys = {"risk_type": "Going concern", "risk_description": "x"}
    good = {"category": "Going Concern", "severity": "HIGH", "description": "Going concern doubt is disclosed.",
            "evidence_id": gc, "quote": "there is substantial doubt about its ability to continue as a going concern"}
    answers = [{"risks": [wrong_keys, wrong_keys]}, {"risks": [good]}]
    monkeypatch.setattr(qa, "_call_llm", lambda n, e: answers.pop(0))
    out = qa.analyze_qualitative_risks(st)
    assert out["qualitative_status"] == "COMPLETED" and len(out["extracted_risks"]) == 1 and not answers


def test_persistently_malformed_answer_marks_the_review_failed(monkeypatch):
    st, _ = state_with_evidence()
    monkeypatch.setattr(qa, "_call_llm", lambda n, e: {"risks": [{"risk_type": "x"}]})
    out = qa.analyze_qualitative_risks(st)
    assert out["qualitative_status"] == "FAILED" and out["extracted_risks"] == []


def test_prompt_schema_names_the_finding_fields():
    schema = qa.QualitativeRiskReport.model_json_schema()
    text = str(schema)
    assert "evidence_id" in text and "quote" in text and "category" in text
