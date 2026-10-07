import json
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from src.graph import run_underwriting_pipeline
from src.fields import FIELD_ORDER, FIELD_LABELS
from src.validation import assess_data_integrity
from src.policy import POLICY, POLICY_VERSION, UNCONFIRMED, DECISION_POLICY
from src import audit_log

load_dotenv()

st.set_page_config(
    page_title="Agentic Credit Underwriting Engine",
    layout="wide"
)

st.title("Agentic Credit Underwriting & Decision Intelligence Engine")


def load_sample() -> dict:
    with open("data/sample_company.json", "r") as f:
        return json.load(f)


def set_dossier(d: dict):
    st.session_state.dossier = d
    st.session_state.dossier_version = st.session_state.get("dossier_version", 0) + 1


# Initialize session state for storing dossier data across reruns
if "dossier" not in st.session_state:
    try:
        set_dossier(load_sample())
    except Exception:
        set_dossier({})

st.sidebar.header("Data Mode Selection")
mode = st.sidebar.radio("Select Data Source:", ["Local Sample Data", "Live SEC EDGAR Ticker Search"])

if mode == "Live SEC EDGAR Ticker Search":
    sec_ticker = st.sidebar.text_input("Enter SEC Ticker or CIK (e.g., INFY, MSFT, TSLA, AAPL)", value="INFY")
    if st.sidebar.button("Fetch Live SEC Filing"):
        with st.spinner(f"Pulling live annual filing for '{sec_ticker}' from SEC EDGAR..."):
            try:
                from src.data_loader import fetch_live_sec_dossier, UnsupportedEntityError
                fetched_data = fetch_live_sec_dossier(sec_ticker)
                set_dossier(fetched_data)
                st.sidebar.success(f"Loaded {fetched_data['company_name']}!")
            except UnsupportedEntityError as e:
                st.sidebar.warning(str(e))
            except Exception as e:
                st.sidebar.error(f"Error pulling SEC data: {e}")
else:
    if st.sidebar.button("Load Local Sample Data"):
        try:
            set_dossier(load_sample())
            st.sidebar.success("Loaded local sample data!")
        except Exception as e:
            st.sidebar.error(f"Error loading sample file: {e}")

dossier = st.session_state.dossier
ver = st.session_state.get("dossier_version", 0)
currency = dossier.get("currency", "USD")

st.caption(f"Powered by LangGraph, Groq, and SEC EDGAR | amounts in {currency} (full units)")

company_name = st.sidebar.text_input("Company Name", value=dossier.get("company_name", ""), key=f"name_{ver}")
ticker = st.sidebar.text_input("Ticker / Identifier", value=dossier.get("ticker", ""), key=f"tick_{ver}")

# ----------------------------------------------------------------------------------------
# Financial inputs. A blank box means "missing": nothing is ever defaulted behind the scenes.
# ----------------------------------------------------------------------------------------
st.sidebar.subheader(f"Financial Line Items ({currency})")
fin_src = dossier.get("financials", {})
prov_src = dossier.get("provenance", {})


def parse_number(text: str):
    t = (text or "").strip().replace(",", "").replace(currency, "").replace("$", "")
    if t == "":
        return None
    neg = t.startswith("(") and t.endswith(")")
    value = float(t.strip("()"))
    return -value if neg else value


candidates_src = dossier.get("candidates") or {}
NO_PICK = "(pick a tagged fact from this filing...)"


def cand_label(c: dict) -> str:
    return f"{c['concept']} = {c['value']:,.0f}"


def apply_candidate(field: str, version: int):
    """Selectbox callback: copy the chosen filing fact into the field's text box."""
    choice = st.session_state.get(f"cand_{field}_{version}")
    for c in (st.session_state.dossier.get("candidates") or {}).get(field, []):
        if cand_label(c) == choice:
            st.session_state[f"fin_{field}_{version}"] = f"{c['value']:,.0f}"
            st.session_state[f"picked_{field}_{version}"] = c["concept"]
            return


financials, provenance, overrides, input_errors = {}, dict(prov_src), [], []
for key, label in FIELD_ORDER:
    original = fin_src.get(key)
    p = prov_src.get(key) or {}
    help_text = (f"{p.get('status', 'provided')} | {p.get('concept') or 'no source concept'}"
                 + (f" | {p['note']}" if p.get("note") else ""))
    shown = "" if original is None else f"{original:,.0f}"
    raw = st.sidebar.text_input(label, value=shown, key=f"fin_{key}_{ver}", help=help_text,
                                placeholder="MISSING" if original is None else None)
    if original is None and candidates_src.get(key):
        st.sidebar.selectbox(f"Candidates for {label}", [NO_PICK] + [cand_label(c) for c in candidates_src[key]],
                             key=f"cand_{key}_{ver}", on_change=apply_candidate, args=(key, ver),
                             help="Facts found in this filing that might supply the missing figure. "
                                  "Picking one is an analyst override and needs a reason.")
    try:
        value = parse_number(raw)
    except ValueError:
        input_errors.append(label)
        value = None
    financials[key] = value
    changed = (value is None) != (original is None) or (
        value is not None and original is not None and abs(value - original) > 0.5)
    if changed:
        picked = st.session_state.get(f"picked_{key}_{ver}")
        if picked and not any(cand_label(c) == f"{picked} = {value:,.0f}" for c in candidates_src.get(key, [])
                              if value is not None):
            picked = None                       # the analyst typed a different number afterwards
        provenance[key] = {"status": "analyst_override", "concept": picked or p.get("concept"),
                           "period": p.get("period"),
                           "note": f"analyst selected filing fact {picked}" if picked else "",
                           "previous_status": p.get("status", "provided")}
        overrides.append({"field": key, "original": original, "new": value, "source_concept": picked})

override_reason = ""
if overrides:
    override_reason = st.sidebar.text_input(
        "Override reason (required)", key=f"reason_{ver}",
        help="Why do these inputs differ from the source data? Recorded in the audit trail.")
    st.sidebar.warning("Changed from source: " + ", ".join(FIELD_LABELS[o["field"]] for o in overrides))

if dossier.get("narrative"):
    n = dossier["narrative"]
    st.sidebar.caption(f"Filing narrative: {n.get('chunks_searched', 0)} passages searched in "
                       f"{', '.join(n.get('sections', {}).keys()) or 'full text'}; {n.get('evidence_chunks', 0)} retrieved as evidence.")
qualitative_text = st.sidebar.text_area("Extra analyst notes (optional, treated as additional evidence)", value=dossier.get("qualitative_notes", ""),
                                        height=180, key=f"notes_{ver}")

company_dossier = {
    "company_name": company_name,
    "ticker": ticker,
    "currency": currency,
    "fiscal_year_end": dossier.get("fiscal_year_end"),
    "source": dossier.get("source"),
    "warnings": dossier.get("warnings", []),
    "financials": financials,
    "provenance": provenance,
    "analyst_overrides": overrides,
    "override_reason": override_reason,
    "qualitative_available": dossier.get("qualitative_available", True),
    "qualitative_notes": qualitative_text,
    "qualitative_evidence": dossier.get("qualitative_evidence", []),
}

# ----------------------------------------------------------------------------------------
# Data integrity gate (live, on the values currently in the form)
# ----------------------------------------------------------------------------------------
report = assess_data_integrity(company_dossier)
blocked = report["status"] == "BLOCK" or bool(input_errors)

st.markdown("### Data Integrity & Provenance")
src = dossier.get("source") or {}
if src:
    st.caption(f"Source: {src.get('form')} filed {src.get('filing_date')} | accession {src.get('accession')} | "
               f"fiscal year ended {dossier.get('fiscal_year_end')} | extracted via {src.get('method') or 'n/a'} | "
               f"[filing]({src.get('url')})")
else:
    st.caption("Source: sample / manually supplied data (no filing attached)")

if input_errors:
    st.error("These fields are not valid numbers: " + ", ".join(input_errors))
if report["status"] == "BLOCK":
    st.error("Automated credit decision is BLOCKED. Fix or explicitly override the items below (with a reason), "
             "or refer this case to a credit analyst.")
elif report["status"] == "WARN":
    st.warning("Data passed the integrity gate with caveats. A human reviewer should read them.")
else:
    st.success("Data passed the integrity gate.")

for issue in report["issues"]:
    if issue["severity"] == "BLOCK":
        st.error(f"[{issue['code']}] {issue['message']}")
    elif issue["severity"] == "WARN":
        st.warning(f"[{issue['code']}] {issue['message']}")

with st.expander("Field-level provenance"):
    rows = []
    for key, label in FIELD_ORDER:
        p = provenance.get(key) or {}
        v = financials.get(key)
        rows.append({"Field": label, f"Value ({currency})": "MISSING" if v is None else f"{v:,.0f}",
                     "Status": p.get("status", "provided"), "Source concept": p.get("concept") or "",
                     "Note": p.get("note") or ""})
    st.dataframe(pd.DataFrame(rows), hide_index=True)

with st.expander("Policy in force"):
    st.json({"version": POLICY_VERSION, "values": POLICY, "decision_rules": DECISION_POLICY,
             "unconfirmed_placeholders": sorted(UNCONFIRMED)})

st.divider()
run_button = st.sidebar.button("Run Credit Underwriting Workflow", type="primary", disabled=blocked)
if blocked:
    st.sidebar.caption("Run is disabled while the data-integrity gate reports BLOCK.")


def fmt_ratio(value, status, suffix="x"):
    if value is not None:
        return f"{value}{suffix}"
    return {"missing_input": "NOT COMPUTABLE (missing input)",
            "not_applicable_no_debt_service": "n/a (no debt service)",
            "not_applicable_no_interest": "n/a (no interest expense)",
            "not_applicable_no_current_liabilities": "n/a (no current liabilities)",
            "not_applicable_no_liabilities": "n/a (no liabilities)",
            "negative_equity": "NOT MEANINGFUL (negative equity)",
            "negative_tangible_net_worth": "NOT MEANINGFUL (negative tangible net worth)"}.get(status, "n/a")


if run_button:
    with st.spinner("Executing LangGraph Multi-Agent Underwriting Graph..."):
        results = run_underwriting_pipeline(company_dossier)

    status_now = results.get("decision_status")
    if status_now == "BLOCKED_DATA_INTEGRITY":
        st.error("Automated decision blocked by the data-integrity gate. See the audit trail below.")
    elif status_now == "REFERRED_TO_ANALYST":
        st.warning("Referred to a credit analyst: a required guardrail could not be computed from the source data, "
                   "so no automated limit or price is proposed. See the flags and audit trail below.")
    elif status_now == "DECISION_FAILED":
        st.error("The decision agent produced no valid decision (LLM unavailable or its output failed validation). "
                 "See the audit trail below.")
    else:
        st.success("Underwriting Evaluation Complete!")

    decision = results.get("final_decision") or {}
    metrics = results.get("computed_metrics")

    # Row 1: Executive Decision Banner
    st.markdown("### Executive Credit Decision Memorandum")
    col1, col2, col3 = st.columns(3)

    if decision:
        risk_rating = decision.get("risk_rating", "N/A")
        credit_limit = decision.get("approved_credit_limit", 0.0)
        credit_limit_m = credit_limit / 1000000.0
        spread_bps = decision.get("recommended_interest_spread_bps", 0)

        col1.metric("Risk Classification", risk_rating)
        limit_ccy = decision.get("currency") or currency
        col2.metric("Approved Credit Limit", f"{limit_ccy} {credit_limit:,.2f}", f"~{credit_limit_m:,.2f} million {limit_ccy}")
        col3.metric("Interest Rate Spread", f"+{spread_bps} bps over {'SOFR' if limit_ccy == 'USD' else 'local benchmark'}")

        st.info(f"**Executive Memo Summary:** {decision.get('credit_memo_summary')}")

        st.markdown("**Strategic Rationale:**")
        for rationale in decision.get("key_rationale", []):
            st.markdown(f"- {rationale}")
    else:
        st.warning(f"No automated decision was produced (status: {results.get('decision_status')}).")

    st.divider()

    # Row 2: Quantitative Ratios & Compliance Flags
    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("### Quantitative Ratio Engine (Node 1)")
        if metrics:
            s = metrics.status
            st.write(f"**DSCR:** {fmt_ratio(metrics.dscr, s.get('dscr'))} (Threshold: >= {POLICY['dscr_min']:.2f}x)")
            st.write(f"**Debt-to-Equity (financial debt):** {fmt_ratio(metrics.debt_to_equity, s.get('debt_to_equity'))} "
                     f"(Threshold: <= {POLICY['debt_to_equity_max']:.2f}x)")
            st.write(f"**TOL / TNW:** {fmt_ratio(metrics.tol_tnw, s.get('tol_tnw'))} "
                     f"(Threshold: <= {POLICY['tol_tnw_max']:.2f}x{', unconfirmed placeholder' if 'tol_tnw_max' in UNCONFIRMED else ''})")
            st.write(f"**Interest Coverage:** {fmt_ratio(metrics.interest_coverage, s.get('interest_coverage'))} "
                     f"(Threshold: >= {POLICY['interest_coverage_min']:.2f}x)")
            st.write(f"**Current Ratio:** {fmt_ratio(metrics.current_ratio, s.get('current_ratio'))}")
            st.write(f"**Altman Z'' (non-manufacturer):** {fmt_ratio(metrics.altman_z_score, s.get('altman_z_score'), '')} "
                     f"(Distress: < {POLICY['altman_z2_distress']}, Safe: > {POLICY['altman_z2_safe']})")
            if metrics.net_debt is not None:
                label = "net cash" if metrics.net_debt < 0 else "net debt"
                st.caption(f"Information only: {label} of {abs(metrics.net_debt):,.0f} {currency}")
        else:
            st.info("Ratios were not computed because the run was blocked.")

    with col_right:
        st.markdown("### Policy Guardrails & Compliance (Node 3)")
        if results.get("decision_status") == "BLOCKED_DATA_INTEGRITY":
            st.info("Guardrails were not evaluated because the run was blocked.")
        else:
            violations = results.get("policy_violations", [])
            if not violations:
                st.success("No compliance flags raised.")
            else:
                for v in violations:
                    st.warning(f"**[{v['severity']}] {v['rule']}**: {v['message']}")

    st.divider()

    # Row 3: Qualitative Risk Extractions & Audit Trail
    st.markdown("### Qualitative Disclosures Analysis (Node 2)")
    q_status = results.get("qualitative_status")
    if q_status == "SKIPPED":
        st.info("No narrative disclosures were available for this entity, so the qualitative step was skipped.")
    elif q_status == "FAILED":
        st.warning("The qualitative review failed (LLM unavailable or unusable response) and was NOT performed for this run.")
    risks = results.get("extracted_risks", [])
    if q_status == "COMPLETED" and not risks:
        st.success("No credit-relevant risks were found in the retrieved filing passages.")
    for r in risks:
        box = st.error if r["severity"] == "HIGH" else st.warning if r["severity"] == "MEDIUM" else st.info
        box(f"**[{r['severity']}] {r['category']}**: {r['description']}\n\n"
            f"> \"{r.get('quote', '')}\"  \n> _{r.get('section', '')}, passage {r.get('evidence_id', '')}_")

    st.divider()
    st.markdown("### System Audit Trail")
    rec = results.get("audit_record") or {}
    if rec.get("error"):
        st.error(f"Audit log could NOT be written: {rec['error']}. Do not rely on this run for governance purposes.")
    elif rec:
        st.caption(f"Run {results.get('run_id')} | persisted to {rec.get('path')} | record hash {rec.get('record_hash')}")
        if st.button("Verify audit-log integrity"):
            ok, problems = audit_log.verify_chain()
            if ok:
                st.success("Audit log hash chain is intact.")
            else:
                st.error("Audit log has been altered: " + "; ".join(problems))
    for log in results.get("audit_trail", []):
        st.code(log, language="text")

else:
    st.info("Select a data source in the sidebar and click 'Run Credit Underwriting Workflow' to execute.")
