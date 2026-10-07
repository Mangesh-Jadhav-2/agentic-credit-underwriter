# Resume and portfolio kit

Everything here is stated so that it is **true and defensible in an interview**. Edit the wording to your voice; keep the facts.

## 1. Project title options
- Agentic Credit Underwriting & Decision Intelligence Engine
- AI Credit Underwriter with Deterministic Guardrails (SEC EDGAR, LangGraph)

## 2. Resume bullets

**Compact (2 bullets)**
- Built an agentic credit-underwriting engine (Python, LangGraph, Groq LLM, Pydantic) that pulls a company's latest SEC 10-K / 20-F, computes credit ratios (DSCR, TOL/TNW, interest coverage, Altman Z″), applies a versioned bank-style policy, and generates a credit decision memo, with a 113-test automated suite.
- Designed the LLM as advisor and deterministic code as decision-maker: post-LLM rules enforce rating floors, limit caps and spread floors, missing data blocks or refers the case without an LLM call, and a hash-chained audit log makes every decision reproducible and tamper-evident.

**Standard (4 bullets)**
- Built a four-agent LangGraph workflow (quantitative, qualitative RAG, policy guardrails, memo synthesis) over live SEC EDGAR filings, with a Streamlit analyst interface and per-field data provenance back to XBRL tags.
- Replaced hard-coded fallbacks and per-company fixes with generic XBRL extraction rules across US-GAAP and IFRS filers, adding safety nets (debt containment rule, implied-interest-rate check) so relaxed rules cannot silently understate debt.
- Added guardrails around the LLM: strict Pydantic output validation with retries, a deterministic rating/limit/spread clamp, and a qualitative agent whose findings must quote the filing verbatim (ungrounded findings are dropped and logged).
- Reconciled outputs to SEC source data and found and fixed three defects the unit tests had missed (e.g. understated TOL/TNW from uncounted trademark intangibles: 3.76x shown vs about 11.3x actual); documented design decisions, calibrations and limits.

**Extended / project-section version (add any of these)**
- Calibrated limit and spread policy to public market references (leveraged-loan index spread of about 453 bps at 30 Jun 2026; Basel large-exposure principle) and documented why a fixed 6x leverage ceiling was not used after the 2025 rescission of US interagency guidance.
- Scoped the tool honestly: detects and refuses banks and insurers (SIC 6000-6411) with an explanation of why standard corporate ratios do not apply.
- Tamper-evident audit trail (SHA-256 hash chain) recording inputs, policy version, model, overrides and outcome for every run.

## 3. Skills and keywords (for ATS and the skills section)
Agentic AI · LangGraph · LLM guardrails · Retrieval-Augmented Generation · Prompt engineering · Pydantic v2 · Python · Streamlit · SEC EDGAR / XBRL · Financial statement analysis · Credit risk · DSCR · Altman Z-score · Model governance · Audit logging · Pytest / test-driven development · Data validation · Technical documentation

## 4. Short descriptions

**Portfolio card (about 55 words)**
> An AI credit underwriter that reads a company's latest SEC filing, computes credit ratios, applies bank-style policy, and drafts a decision memo, with one rule: the LLM advises, code decides. Missing data blocks the decision, risks must quote the filing, and every run is recorded in a tamper-evident audit log. Python · LangGraph · Streamlit · 113 tests.

**LinkedIn project description**
> Prototype credit-underwriting engine over live SEC EDGAR filings. Four LangGraph agents compute ratios, retrieve and ground qualitative risks, apply a versioned policy, and draft a memo, while deterministic rules clamp rating, limit and spread and a hash-chained log records every decision. Verified against real filings (Infosys 20-F, Coca-Cola 10-K), which exposed and led to fixes for three defects. Built as a showcase of safe LLM use in a regulated workflow.

## 5. Interview talking points

**Why "LLM advises, code decides"?** Models are unreliable at thresholds, arithmetic and saying "I don't know". In lending, a confident wrong answer is the worst case. So the safety properties (rating floor, limit cap, spread floor, blocking on missing data) live in code and hold even if the model misbehaves.

**How do you stop it hallucinating risks?** The qualitative agent only sees retrieved filing passages, must cite a passage id and quote it word-for-word, and any finding whose quote isn't in that passage is dropped and logged. Severity HIGH is reserved for a current, specific event. If the model's answer is malformed it retries, and if it still fails the run says the qualitative review wasn't performed, never "no risks".

**What went wrong, and what did you learn?** Unit tests passed but a reconciliation of the Coca-Cola run to SEC data found TOL/TNW at 3.76x instead of about 11.3x because $12.5B of indefinite-lived trademarks wasn't counted as an intangible. It also exposed a silent qualitative failure and a memo that contradicted the enforced limit. Lesson: tests prove the code does what you wrote; only comparing to the source proves it's the right thing.

**Why generic rules instead of fixing each company?** Thousands of SEC registrants tag things differently. Per-company fixes never end and hide gaps. Generic ordered rules with disclosed fallbacks scale, and safety nets (debt containment, implied-interest check) stop the relaxed rules from understating debt.

**Why not use an LLM to map the XBRL tags automatically?** It would be the most automatic but the least reproducible and auditable. I chose deterministic rules plus an analyst picker so every number traces to a named tag.

**Why not a vector database?** One filing and six credit topics: keyword retrieval is transparent, deterministic and dependency-free. Embeddings are the next step if recall proves insufficient.

**How did you pick the limits and spreads?** Spread floors follow credit-tier loan pricing, anchored to the leveraged-loan index spread of about 453 bps at 30 June 2026; the limit is the lower of a rating-based share of equity and debt-to-equity headroom. They are indicative showcase calibrations, labelled as such. I avoided a 6x debt/EBITDA rule because the US guidance behind it was rescinded in December 2025.

**What are the limits?** Banks and insurers are refused (different ratio set needed); verification covers a limited set of mostly large companies, with no backtest and no accuracy claim; the audit log's hash chain detects edits but a full rewrite by someone with write access would need an external anchor; policy numbers aren't a real bank's.

**What would you do next?** Bank-specific ratios, a larger validation set saved as a regression suite, embeddings if needed, an external anchor for the audit head hash.

## 6. Claims: safe vs avoid

| Safe to say | Avoid saying |
|---|---|
| "Working prototype / showcase project" | "Production system", "used by a bank" |
| "113 automated tests" | Any accuracy, precision or "X% faster" figure (none was measured) |
| "Verified against real filings (Infosys 20-F, Coca-Cola 10-K)" | "Validated on hundreds of companies" |
| "Policy values calibrated to public market references, documented as illustrative" | "Matches real bank credit policy" |
| "Detects and refuses banks and insurers" | "Supports all SEC companies" |
| "Hash-chained, tamper-evident audit log" | "Tamper-proof" |

Be ready to explain any design decision in the docs from memory; interviewers will probe. If you used AI-assisted development, saying so plainly (and that you own the design decisions, validation and review) is the safer position.

## 7. Two-minute demo script
1. **Microsoft**: clean load; show provenance for a field, the data-quality panel, the memo, and the audit-log verify button.
2. **Apple**: show a referral to an analyst with no LLM call ("it refuses to guess").
3. **Coca-Cola**: tell the defect story: the app said 3.76x, the filing says about 11.3x; show how reconciling found it.
4. **A bank (JPM or Goldman)**: show the refusal message and why.
5. Close on the one idea: the LLM advises, code decides.

## 8. Screenshots to capture before you publish
- Decision banner with rating, limit (with currency) and spread
- Data-quality panel with a BLOCK or WARN example
- Qualitative panel showing a finding with its verbatim quote and source passage
- Audit trail with a `POLICY_OVERRIDE` line, and the "Audit log hash chain is intact" message
- The bank refusal message
- The architecture diagram from the README

Run the planned companies from the validation log first (Boeing, Carvana, Walmart, Toyota, Goldman Sachs) so you can say "verified on N companies" with a true N.
