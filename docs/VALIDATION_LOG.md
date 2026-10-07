# Validation log

What has been checked against real SEC filings, how, and what the checking found. This is a log of **verification**, not a backtest: it shows the numbers on screen match the source documents, not that the ratings predict defaults.

## 1. Infosys Ltd, Form 20-F, fiscal year ended 31 March 2026 (IFRS)

Accession `0001193125-26-270520`. The original project task was to validate the app's figures against this filing. The values below (US$ millions, read from the filing's inline XBRL) are the basis of the loader test fixtures.

| Item | Reported |
|---|---|
| Total assets | 16,446 |
| Total liabilities | 6,606 |
| Total equity | 9,840 |
| Current assets / current liabilities | 10,912 / 5,516 |
| Retained earnings | 13,459 |
| Goodwill / other intangibles | 1,278 / 298 |
| Lease liabilities (current 333 + non-current 634) | 967 |
| Cash / current investments | 2,341 / 1,365 |

Derived: financial debt 967 (all leases, as IFRS 16 treats them), debt-to-equity 967 / 9,840 = 0.10. The earlier version of the loader fell back to hard-coded numbers; this comparison led to its replacement (design decisions, step 5).

## 2. Live runs recorded in the audit log

| Company | Outcome recorded | Notes |
|---|---|---|
| Microsoft (MSFT) | Completed | Was blocked before the generic rules (missing principal repayment); confirmed working after the fix |
| Apple (AAPL) | Referred to analyst | The designed outcome when a guardrail input cannot be determined; no LLM call made |
| Tesla (TSLA) | Completed (two runs) | Previously failed to load |
| Infosys (INFY) | Completed | IFRS path, USD |
| Coca-Cola (KO) | Completed | Cross-checked in detail below |

Planned next (not yet run): Boeing (negative equity), Carvana (leverage and debt tagging), Walmart (January fiscal year), Toyota (non-USD 20-F), Goldman Sachs (bank, should be refused).

## 3. Coca-Cola (KO) deep check

Source: SEC `companyfacts` for Form 10-K, accession `0001628280-26-010047`, fiscal year ended 31 December 2025. App run recorded in the audit log.

| Item | In the filing | In the app | Match |
|---|---|---|---|
| Total assets | 104.816B | n/a (used in checks) | |
| Equity incl. non-controlling interest | 34.275B | 34.275B (5% cap = 1.71375B) | Yes |
| Operating income (EBIT) | 13.762B | used for coverage | Yes |
| Interest expense | 1.654B | used for coverage | Yes |
| Interest coverage | 13.762 / 1.654 = 8.32x | 8.32x | Yes |
| Financial debt | 42.119B long-term + 1.822B current + 1.495B commercial paper = 45.436B | D/E 1.33x (45.436 / 34.275 = 1.326) | Yes |
| Net debt | 45.436B − cash 10.270B = 35.166B | 35.166B | Yes |
| Total liabilities | not tagged; assets − equity = 70.541B | derived (INFO issue) | Yes |
| Goodwill | 15.491B | subtracted in TOL/TNW | Yes |
| **Intangibles** | **12.531B indefinite-lived trademarks** | **not subtracted (zero)** | **No: defect 1** |
| TOL/TNW | 70.541 / (34.275 − 15.491 − 12.531) = **11.3x** | 3.76x | **No: defect 1** |

Minor, accepted: about 56M of "other short-term borrowings" is not in debt (0.1% of debt); operating leases (1.7B) are excluded for US-GAAP filers (would add about 0.05x to D/E); net debt nets only cash, not 3.6B of short-term investments (conservative).

### Defects found (all fixed, with tests)

| # | Defect | How it was found | Impact | Fix |
|---|---|---|---|---|
| 1 | Indefinite-lived trademarks not counted as intangibles, so tangible net worth overstated and TOL/TNW shown as 3.76x instead of about 11.3x | Reconciling the app's TOL/TNW to the filing's balance-sheet lines | A TOL/TNW breach could be hidden for companies whose intangibles are mostly trademarks. KO's HIGH flag fired either way | Added trademark / indefinite-lived intangible tags; warning when intangible-like balances remain unmapped |
| 2 | Qualitative step produced no findings and the memo said "no credit-relevant risks identified" | Reading the run's audit trail: two findings "failed schema validation" | A false "all clear" on the qualitative side | The prompt now carries the exact finding schema; malformed answers are retried; persistent failure is marked FAILED and the memo says the review was not performed |
| 3 | Memo text said a $23.1B facility while the enforced limit was $1.71B | Reading the memo against the audit trail | Contradictory deliverable | Model is given absolute per-rating caps; if rules still change terms, the memo opens with the final terms |

Additional improvement from this check: audit records now store the input figures themselves (not just a hash), so a reviewer can reconcile a run without re-fetching the filing.

## 4. Automated test suite

113 tests (loader 51, decision rules and audit log 25, agents 17, narrative and qualitative agent 11, graph 4). They run offline: SEC and LLM calls are replaced at single seams. Fixtures use real filing values plus decoys (prior-year facts, segment-dimensioned facts, partial-year periods, non-money units).

## 5. What this does not show

- No backtest against defaults or rating-agency ratings; no accuracy claim.
- Coverage is a handful of companies, mostly large caps.
- The LLM's memo quality has been read on individual runs, not scored systematically.
- Spread and limit calibrations are indicative market-based values, not a bank's approved policy.
