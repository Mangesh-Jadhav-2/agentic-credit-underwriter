# Case study: an AI credit underwriter that is not allowed to guess

**Role:** sole designer and developer (showcase project) · **Stack:** Python, LangGraph, Streamlit, Groq LLM, Pydantic, lxml, SEC EDGAR APIs · **Status:** working prototype, 113 automated tests

## The problem

"Let an LLM underwrite a loan" is easy to demo and dangerous to trust. A model will read a financial statement and produce a confident rating, limit and price, even when an input is missing, a ratio can't be computed, or the number it quotes was never in the filing. In banking, a plausible wrong answer is worse than no answer.

I wanted to see how far an agentic system can go **while keeping the safety properties independent of the model's behaviour**.

## The approach in one sentence

Use the LLM for what it's good at (reading disclosures, drafting a memo) and use deterministic code for everything that has to be right (numbers, thresholds, limits, and "I can't tell").

## What I built

A four-agent workflow (LangGraph) that takes a ticker, pulls the latest 10-K or 20-F from SEC EDGAR, and produces a credit decision memo:

1. **Quantitative agent**: liquidity, leverage, coverage, DSCR and Altman Z″.
2. **Qualitative agent**: retrieval-augmented review of Risk Factors, Legal Proceedings and Liquidity, where every finding must quote the filing word-for-word.
3. **Policy guardrail agent**: deterministic checks against a versioned policy.
4. **Decision agent**: the LLM drafts the memo; code then enforces a rating floor, a limit cap and a spread floor.

Around it: a data-integrity gate that blocks instead of guessing, per-field provenance back to the filing's XBRL tags, an analyst override path, and a hash-chained audit log.

## Four decisions I'm proud of

**1. The LLM can be stricter than policy, never looser.** The model proposes a rating, limit and spread; code then clamps them. If the model asks for 90% of equity on a clean borrower, the system returns 10% and logs a `POLICY_OVERRIDE`. If a guardrail can't be computed because data is missing, the case goes to an analyst **without calling the model at all**.

**2. Missing data is an outcome, not a default.** Early versions filled gaps with zeros and hard-coded fallbacks, which produced confident results from nothing. I replaced that with a gate (block / warn / info), `None`-with-a-reason ratios, and provenance for every field.

**3. Generic extraction rules instead of per-company fixes.** The loader worked for Infosys and failed for Microsoft, Apple and Tesla. Patching tags one company at a time doesn't scale to thousands of registrants, so I wrote ordered, general rules with disclosed fallbacks. Because relaxing rules creates risk, I added safety nets: a containment rule (debt-like balances that weren't counted block the decision) and an implied-interest-rate check that catches under-counted debt (the Ford finance-arm pattern).

**4. Findings must be grounded.** The qualitative agent only sees retrieved passages and must cite one and quote it exactly. Ungrounded or malformed findings are dropped and logged; a failed review is reported as failed, never as "no risks".

## What testing against real filings taught me

The unit tests passed, and the app still had problems. Running Coca-Cola and reconciling the output to the SEC's own data found three defects:

- **TOL/TNW was understated by about 3x** (3.76x shown, about 11.3x correct): $12.5B of indefinite-lived trademarks wasn't treated as an intangible.
- **The qualitative step silently returned nothing**, and the memo claimed "no credit-relevant risks": a schema mismatch I had introduced.
- **The memo contradicted the enforced limit** ($23.1B in the text, $1.71B actual).

All three are fixed and covered by tests. The takeaway I'd give any team building AI decision tools: *tests prove the code does what you wrote; only comparing to the source of truth proves you wrote the right thing.*

## Calibration with sources

Limits and spreads are not pulled from thin air: spread floors follow credit-tier loan pricing anchored to the leveraged-loan index spread of about 453 bps at 30 June 2026; the limit is the lower of a rating-based share of equity and the borrower's debt-to-equity headroom; and I deliberately did not use a fixed 6x debt/EBITDA ceiling because the US interagency guidance behind it was rescinded in December 2025. They are documented as showcase calibrations, not a bank's policy.

## Honest limits

Banks and insurers are refused with an explanation (their balance sheets need a different ratio set). Verification covers a limited set of mostly large companies, with no backtest and no accuracy claim. The qualitative review reads the most relevant passages, not the entire filing. Policy numbers are indicative.

## What I'd do next

A bank-specific ratio set; a larger validation set with expected outcomes saved as a regression suite; embeddings-based retrieval if keyword recall proves insufficient; and anchoring the audit log's head hash outside the file to close the rewrite-the-whole-log gap.

## Skills demonstrated

Agentic workflow design (LangGraph) · guardrails around LLMs · schema validation (Pydantic) · retrieval-augmented generation with grounding · financial statement analysis and credit ratios · XBRL / SEC data engineering · audit and governance design · test-driven verification against source documents · technical documentation.
