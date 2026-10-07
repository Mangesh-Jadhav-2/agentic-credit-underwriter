# Design decisions: how the project was built, step by step

Each step lists the **problem**, the **decision**, **why**, the **trade-off** accepted, and the **evidence** (tests or checks) that it works. The order is the order the work happened, so you can see how the design matured under real-data pressure.

Policy version at the time of writing: `2026.10-p4`. Tests: 113 passing.

---

## Step 1. One definition of the inputs (`src/fields.py`)

- **Problem.** Financial amounts arrive in different units ("20,158" meaning millions, or full units), and every agent was parsing them its own way.
- **Decision.** One file defines every input field, its label, and which fields are `REQUIRED`, `DEBT_FIELDS` or `SOFT`. All amounts are stored in **full currency units**. Unit conversion happens exactly once, in the loader, using the scale the filing itself declares.
- **Why.** Unit bugs are silent and enormous (a 1,000,000x error still produces plausible-looking ratios). A single conversion point makes them testable.
- **Trade-off.** Slightly more ceremony in the loader.
- **Evidence.** Loader tests assert exact full-unit values from a fixture built from Infosys's real 20-F figures.

## Step 2. A data-integrity gate that blocks instead of guessing (`src/validation.py`)

- **Problem.** The first version filled missing inputs with defaults, so a company with a missing line item got a confident-looking decision based on zeros.
- **Decision.** A gate runs before any agent. Issues have three severities: **BLOCK** (no automated decision), **WARN** (shown to the analyst and recorded), **INFO**. Examples: missing required input, balance sheet that doesn't balance (tolerance 0.5%), current assets larger than total assets, an override without a reason, interest expense implausible against the debt counted.
- **Why.** In credit, a wrong number that looks right is worse than no number. The gate makes "I can't tell" a first-class outcome.
- **Trade-off.** Some real filings will be blocked until an analyst resolves a field. That is the intended behaviour; step 6 shows how the blocking was kept from becoming excessive.
- **Evidence.** Tests for each block code, and for the rule that a BLOCK stops the graph before any LLM call.

## Step 3. Ratio definitions chosen and justified (`src/agents/quant_agent.py`)

| Ratio | Definition used | Why this definition |
|---|---|---|
| Current ratio | Current assets / current liabilities | Standard liquidity measure |
| Debt-to-equity | **Financial debt** (borrowings + lease liabilities) / total equity | Total liabilities include payables and accruals that are not borrowed money; leverage should measure what must be repaid with interest |
| TOL/TNW | Total liabilities / (equity − goodwill − intangibles) | Classic bank covenant; strips assets that vanish in distress |
| Interest coverage | EBIT / interest expense | Ability to pay the cost of debt |
| DSCR | EBIT-based operating income / (principal repayments + interest) | Ability to pay full debt service |
| Altman Z″ | 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4, book equity, no constant | The original Z-score is for manufacturers; Z″ is the variant for non-manufacturers and non-public firms. Distress < 1.10, safe > 2.60 |

- **Key rule.** A ratio that cannot be computed is `None` with a status (`missing_input`, `negative_equity`, `negative_tangible_net_worth`, `not_applicable_no_interest` …). It is never `0`. A company with no debt has no debt service, which is "not applicable", not a pass or a failure.
- **Evidence.** Tests for each status path (negative equity, zero interest, no debt, negative tangible net worth).

## Step 4. Policy as versioned configuration (`src/policy.py`)

- **Problem.** Thresholds were scattered through the agents, so a change was hard to review and impossible to tie to a past decision.
- **Decision.** Every threshold lives in one file with a `POLICY_VERSION`. Every audit record stores the version and the full set of values.
- **Why.** Reproducibility: "this decision was made under policy 2026.10-p4 with DSCR minimum 1.35" must be answerable months later.
- **Values and their basis.**
  - DSCR ≥ 1.35, interest coverage ≥ 2.5, D/E ≤ 2.0, Altman Z″ distress < 1.10 / safe > 2.60: common credit-policy conventions.
  - **TOL/TNW ≤ 3.0**: a widely used ceiling in bank credit appraisal (Indian working-capital practice, and 3-4x as a total-liabilities-to-tangible-net-worth covenant in US middle-market lending). 3.0 is the strict end of that range. Written next to the value in the file.
- **Trade-off.** These are calibrations, not a real bank's approved policy; the documentation says so wherever they are used.

## Step 5. Live SEC loader with no hard-coded fallbacks (`src/data_loader.py`)

- **Problem.** The first live loader fell back to hard-coded numbers when parsing failed, which meant a failed fetch could still display a "result" that was not from the filing. The original task was to validate the app's figures against Infosys's 20-F, and the rebuild came out of that.
- **Decision.**
  1. Resolve ticker → CIK → latest 10-K / 20-F via the SEC submissions API.
  2. Read facts from the SEC **`companyfacts`** JSON API (fast, structured); fall back to parsing the **inline-XBRL** of the filing document (lxml streaming parse), and use the document to cross-check when the API view is incomplete.
  3. Every field records its **provenance**: `reported`, `derived`, `none_reported`, `missing`, `analyst_override`, with the XBRL concept(s) used and the period.
  4. Only undimensioned (consolidated) facts for `us-gaap`, `ifrs-full` and `dei`; the fiscal year-end is the latest instant of `Assets`; durations must be about a year (350-380 days).
- **Why.** Auditable lineage: any number on screen can be traced to a concept in the filing. "Missing" is shown as missing.
- **Alternatives considered.** Letting an LLM map tags to fields ("option 2") would be the most automatic, but the least predictable and not reproducible, so it was rejected for a tool that must be auditable. A deterministic rule set plus an analyst picker gives automation without unreviewable guesses.
- **Evidence.** Fixtures for Infosys (IFRS), Microsoft and Apple (US-GAAP); tests for decoys (prior-year facts, segment-dimensioned facts, partial-year periods, non-money units like USD/shares).

## Step 6. From per-company fixes to generic rules (MSFT, AAPL, TSLA)

- **Problem.** The loader worked for Infosys but produced blocked or empty results for Microsoft, Apple and Tesla. Fixing each company's tags one at a time would never end: SEC has thousands of registrants.
- **Decision.** Replace tag lists per company with **generic, ordered rules** (first alternative that fully resolves wins; tuples are summed):
  - Revenue, borrowings, lease and short-term-debt alternatives written as general US-GAAP/IFRS families.
  - **Fallbacks with disclosure**: EBIT = pre-tax income + interest if operating income isn't tagged; principal repayments from repayment tags, then current maturities, then an explicit zero with a warning.
  - **Soft fields** (interest expense, principal repayment, current assets/liabilities, retained earnings) downgrade a missing value from BLOCK to WARN so one untagged minor line doesn't stop an otherwise complete case.
  - **Candidate picker**: for any field still unresolved, the loader lists the filing's own facts that look like matches and the analyst chooses one; the choice is recorded in the audit log.
- **Safety nets added because relaxing rules creates risk.**
  - **Containment rule**: any balance-sheet concept that looks like debt but was not counted makes financial debt `missing` (blocks) until confirmed, so debt can't be silently understated.
  - **Implied-interest-rate plausibility check**: if interest expense divided by counted debt exceeds 30%, the debt is almost certainly under-counted (e.g. debt tagged only under segment dimensions, as with Ford's finance arm), so it BLOCKs.
- **Evidence.** In the verification sweep, the facts of 13 of 14 test companies mapped under the generic rules; the remaining one was a bank (see step 13). Dedicated tests for each fallback and each safety net.

## Step 7. Deterministic decision rules after the LLM (`src/decision_rules.py`)

- **Problem.** An LLM asked for a limit and a spread will give plausible numbers that nothing enforces.
- **Decision.** The LLM proposes; code disposes.
  - **Rating floor** from guardrail violations: DSCR below minimum → at least HIGH_RISK; Altman distress or a negative capital base → REJECT; one HIGH flag → at least MODERATE_RISK; two HIGH flags → HIGH_RISK.
  - **Limit cap**: lower of (rating-based share of equity) and (debt-to-equity headroom).
  - **Spread floor** by rating; REJECT carries no price.
  - **Referral**: a CRITICAL *data-integrity* violation (a guardrail that cannot be computed) refers the case to an analyst **without calling the LLM at all**.
  - The LLM can be stricter than these rules, never looser. Every adjustment is logged as `POLICY_OVERRIDE`, and the memo opens with the final terms if anything was changed.
- **Why.** This is what makes the system defensible: the safety properties don't depend on the model behaving.
- **Evidence.** Tests that an LLM asking for 90% of equity is clamped to 10%, that a DSCR failure can't be rated better than HIGH_RISK, that distress forces REJECT with zero limit and no spread, and that a referral makes zero LLM calls.

## Step 8. Calibrating limits and spreads (`src/policy.py`)

- **Problem.** The first caps (10/5/2% of equity) and spreads (75/175/325 bps) were placeholders.
- **Decision.** Replace them with market-grounded values and document why.

| Rating | Spread floor (bps over benchmark) | Limit: share of equity |
|---|---|---|
| LOW_RISK | 125 | 10% |
| MODERATE_RISK | 225 | 5% |
| HIGH_RISK | 400 | 2% |
| REJECT | none | 0 |

- **Spreads.** Follow how loans are priced by credit tier (investment-grade-like about SOFR+100-150, BB-type about +200-250, single-B about +350-450). The leveraged-loan index spread was about 453 bps at 30 June 2026 (Fidelity Q2 2026 Leveraged Loan Market Review), which anchors the HIGH_RISK floor. The tier figures other than the index are indicative.
- **Limit.** The lower of a concentration cap (single-name exposure kept small relative to the borrower's equity; for reference, the Basel large-exposure standard caps one counterparty at 25% of the *lender's* Tier 1) and **debt headroom** = 2.0 × equity − existing financial debt, so a loan can't push a borrower past the bank's own leverage guardrail.
- **Deliberately not used.** A fixed "6x debt/EBITDA" ceiling: the US 2013 interagency leveraged-lending guidance that implied it was rescinded by the OCC and FDIC on 5 December 2025.
- **Trade-off.** Still showcase values, labelled as such.

## Step 9. Strict validation of LLM output, and model choice (`src/agents/decision_agent.py`)

- **Problem.** LLM JSON can be malformed, out of range, or missing fields.
- **Decision.** A Pydantic schema (`risk_rating` as a fixed set, limit ≥ 0 and finite, spread 0-2000 bps, non-empty rationale and memo). Invalid output is **rejected, never repaired silently**; up to two attempts; after that, no automated decision and the audit trail says so.
- **Currency.** The model's output field is `approved_credit_limit`; the **currency is attached by code** from the filing, not by the model, so it can't be mislabelled.
- **Model.** Default `openai/gpt-oss-120b`: the strongest reasoning model in Groq's production tier at the time. The numbers are already bounded by code, so the model's job is a faithful memo from the facts supplied. Overridable by `GROQ_MODEL`. The model name used is written into every audit record. No head-to-head benchmark was run.

## Step 10. Tamper-evident audit log (`src/audit_log.py`)

- **Problem.** An in-memory trail disappears when the app closes and can't prove it wasn't edited.
- **Decision.** One JSON line per run, appended to `logs/audit_log.jsonl`. Each record contains a SHA-256 hash of its own canonical content **and the previous record's hash** (a hash chain). It stores inputs (the figures and their provenance, not only a hash), policy version and values, model, data-quality issues, metrics, violations, retrieved evidence ids and hash, extracted risks, the decision and every override. `verify_chain` recomputes everything and reports editing, deletion or reordering; the UI has a "Verify audit-log integrity" button.
- **Why.** Governance: a reviewer can reconstruct what happened and detect tampering. No secrets or email addresses are stored.
- **Honest limit.** A hash chain detects tampering after the fact; it doesn't prevent someone with write access from rewriting the whole file. Anchoring the head hash externally would close that.
- **Evidence.** Tests: records chained, editing a record detected, deleting a record detected, a log-write failure surfaces as `AUDIT_LOG_WRITE_FAILED` instead of passing silently.

## Step 11. Qualitative analysis with retrieval and grounding (`src/narrative.py`, `src/agents/qual_agent.py`)

- **Problem.** Qualitative risk (litigation, covenant breaches, going-concern doubt) lives in the narrative, not in the XBRL numbers; the first version had no narrative at all, and an LLM asked to "find risks" in free text will happily invent some.
- **Decision.**
  1. Convert the filing HTML to text (dropping hidden XBRL headers and scripts) and locate **Risk Factors, Legal Proceedings and Liquidity and Capital Resources** (longest heading-to-heading span, so table-of-contents hits are skipped; full-text fallback for filings without standard headings, such as many 20-Fs).
  2. Chunk by paragraph and **retrieve per credit topic** (going concern, covenant/default, litigation/regulatory, liquidity/refinancing, concentration, controls/restatement) with keyword scoring. Pure Python, no vector database: transparent, deterministic, no extra dependencies.
  3. Cap evidence at about 16k characters to stay inside token limits.
  4. The LLM may use **only** those passages. Each finding must carry an `evidence_id` and a **verbatim quote**. Findings whose quote isn't found in the cited passage are **dropped and logged** with the reason.
  5. Severity is constrained (HIGH/MEDIUM/LOW): HIGH only for a *current, specific* event; hypothetical boilerplate is LOW or omitted.
  6. Statuses: `COMPLETED`, `SKIPPED` (no evidence, no LLM call), `FAILED` (LLM unusable; the memo is told the review was not performed). A fully malformed answer is retried and never reported as "no risks".
- **Why not a vector database?** At this scale (one filing, six topics) keyword retrieval is enough, easier to audit, and adds no infrastructure. Embeddings would be the next step if recall proved insufficient.
- **Evidence.** 11 tests on extraction, section finding, retrieval, grounding, failure and retry paths.

## Step 12. Currency honesty

- **Decision.** Show every amount, and the credit limit, in the filing's **own reporting currency**; no FX conversion. The spread is quoted over SOFR for USD and over "local benchmark" otherwise, with a warning that spread floors are USD-calibrated.
- **Why.** A conversion needs an exchange rate on a chosen date and adds an unverifiable number to a lending decision. A USD "indicative" view was considered and dropped.

## Step 13. Scope: banks are not supported, and the tool says so

- **Problem.** JPMorgan was blocked with a confusing "missing EBIT" message.
- **Decision.** Detect financial institutions (SEC industry code 6000-6411; or deposits / interest-income / premium concepts with no operating income) and stop with an explanation. REITs and non-financials with finance arms pass.
- **Why.** For a bank, debt and deposits are the product, so D/E, EBIT coverage and Altman Z″ are meaningless. Pretending otherwise would be worse than refusing. A bank-specific ratio set (capital adequacy, asset quality, liquidity) is a separate follow-on project.

## Step 14. Verification against real filings, and what it found

Running the app on Coca-Cola and cross-checking against SEC data found three defects that the unit tests had not caught (details in [`VALIDATION_LOG.md`](VALIDATION_LOG.md)):

1. **TOL/TNW understated**: $12.5B of indefinite-lived trademarks was not treated as an intangible, so the ratio showed 3.76x instead of about 11.3x. Fixed; unmapped intangible-like balances now raise a warning.
2. **Qualitative step silently produced nothing**: a schema mismatch made the model's findings invalid and the memo still said "no risks identified". Fixed; malformed answers retry, and persistent failure is reported as FAILED.
3. **Memo contradicted the enforced limit**: the model's narrative said $23.1B while the final limit was $1.71B. Fixed; the model is now told the absolute caps and, if rules still change the terms, the memo opens with the final terms.

The lesson recorded in the project: unit tests prove the code does what you wrote; only comparing against the source of truth proves you wrote the right thing.

## Step 15. Testing strategy

- **113 tests**, no network or API key needed: SEC and LLM calls are replaced at one seam each (`_http_get`, `_call_llm`).
- Fixtures are built from **real filing values** (Infosys 20-F, Microsoft and Apple 10-K facts) with deliberate decoys, so tests prove the right fact is picked, not just that something is.
- Safety properties are tested directly: blocked runs make no LLM call; referrals make no LLM call; the LLM can't loosen rating, limit or spread; invalid output is rejected; the audit chain detects edits, deletions and write failures.
- Behaviour changes update the tests that encoded the old behaviour, and the policy version is bumped when any value changes.
