import json

import pytest

from src import data_loader as dl
from src.validation import assess_data_integrity
from tests import ixbrl_fixtures as fx

M = 1_000_000.0


def dossier(doc=None, **meta_over):
    return dl.build_dossier_from_ixbrl(doc or fx.infy_doc(), dict(fx.META, **meta_over))


# ---------------------------------------------------------------- golden values (real 20-F)
def test_infosys_golden_values_full_currency_units():
    d = dossier()
    f = d["financials"]
    assert f["total_assets"] == 16446 * M
    assert f["total_liabilities"] == 6606 * M
    assert f["shareholder_equity"] == 9840 * M
    assert f["current_assets"] == 10912 * M
    assert f["current_liabilities"] == 5516 * M
    assert f["retained_earnings"] == 13459 * M
    assert f["sales"] == 20158 * M
    assert f["ebit"] == 4085 * M
    assert f["interest_expense"] == 47 * M
    assert f["financial_debt"] == 967 * M
    assert f["principal_repayment"] == 318 * M
    assert f["goodwill"] == 1278 * M and f["intangibles"] == 298 * M
    assert f["cash_and_current_investments"] == (2341 + 1365) * M
    assert d["currency"] == "USD"
    assert d["fiscal_year_end"] == "2026-03-31"


def test_picks_current_year_consolidated_facts_not_decoys():
    f = dossier()["financials"]
    assert f["total_assets"] == 16446 * M          # not prior-year 17,419 / dimensioned 999,999
    assert f["sales"] == 20158 * M                 # not prior-year 19,277 / half-year 9,000


def test_provenance_records_concept_and_status():
    p = dossier()["provenance"]
    assert p["total_assets"]["status"] == "reported" and p["total_assets"]["concept"] == "ifrs-full:Assets"
    assert p["sales"]["concept"] == "ifrs-full:RevenueFromContractsWithCustomers"
    assert p["financial_debt"]["concept"] == "ifrs-full:LeaseLiabilities"
    assert p["net_operating_income"]["status"] == "derived"


def test_infosys_passes_data_gate_with_expected_caveats_only():
    d = dossier()
    r = assess_data_integrity(d)
    assert r["status"] == "WARN"     # warn only: qualitative text not extracted
    assert not [i for i in r["issues"] if i["severity"] == "BLOCK"]
    assert "QUALITATIVE_NOT_EXTRACTED" in {i["code"] for i in r["issues"]}


# ---------------------------------------------------------------- no defaults, ever
def test_filing_without_any_assets_fact_raises_instead_of_guessing_the_period():
    instant = {k: v for k, v in fx.INFY_INSTANT.items() if k != "ifrs-full:Assets"}
    with pytest.raises(ValueError, match="fiscal year end"):
        dossier(fx.build(instant, fx.INFY_DURATION, with_decoys=False))


def test_missing_current_period_value_blocks_with_reason():
    doc = fx.build({k: v for k, v in fx.INFY_INSTANT.items() if k != "ifrs-full:Equity"}, fx.INFY_DURATION)
    d = dossier(doc)
    assert d["financials"]["shareholder_equity"] is None
    assert d["provenance"]["shareholder_equity"]["status"] == "missing"
    r = assess_data_integrity(d)
    assert r["status"] == "BLOCK"
    assert any(i["code"] == "MISSING_INPUT" and i["field"] == "shareholder_equity" for i in r["issues"])


def test_unclassified_balance_sheet_is_soft_missing_not_a_block():
    """Banks have no current assets/liabilities: warn, never default, never block."""
    instant = {k: v for k, v in fx.INFY_INSTANT.items() if k not in ("ifrs-full:CurrentAssets", "ifrs-full:CurrentLiabilities")}
    d = dossier(fx.build(instant, fx.INFY_DURATION))
    assert d["financials"]["current_assets"] is None and d["financials"]["current_liabilities"] is None
    r = assess_data_integrity(d)
    assert not [i for i in r["issues"] if i["severity"] == "BLOCK"]
    assert {i["field"] for i in r["issues"] if i["code"] == "MISSING_INPUT_SOFT"} == {"current_assets", "current_liabilities"}


def test_no_fabricated_numbers_anywhere():
    """The old loader fell back to 1.2B / 650M / 550M / 1.1B / 185M / 32M. None may appear."""
    d = dossier(fx.build({k: v for k, v in fx.INFY_INSTANT.items() if k != "ifrs-full:Liabilities"},
                         {}, with_decoys=False))
    legacy = {1_200_000_000.0, 650_000_000.0, 550_000_000.0, 1_100_000_000.0, 185_000_000.0,
              32_000_000.0, 450_000_000.0, 280_000_000.0, 150_000_000.0}
    assert not (legacy & {v for v in d["financials"].values() if v is not None})
    assert d["financials"]["sales"] is None and d["financials"]["ebit"] is None


def test_unmapped_debt_concept_larger_than_counted_debt_blocks():
    extra = fx._fact("ifrs-full:LongtermBorrowings", fx.INSTANT_FYE, 5000)      # > the 967 counted
    d = dossier(fx.infy_doc(extra_facts=extra))
    assert d["financials"]["financial_debt"] is None
    assert "LongtermBorrowings" in d["provenance"]["financial_debt"]["note"]
    assert assess_data_integrity(d)["status"] == "BLOCK"


def test_unmapped_debt_concept_within_counted_debt_is_presumed_included_with_warning():
    extra = fx._fact("ifrs-full:LongtermBorrowings", fx.INSTANT_FYE, 500)       # <= 967 counted
    d = dossier(fx.infy_doc(extra_facts=extra))
    assert d["financials"]["financial_debt"] == 967 * M
    assert any("LongtermBorrowings" in w and "presumed already included" in w for w in d["warnings"])


def test_borrowings_with_no_repayment_fact_records_zero_with_a_warning():
    extra = fx._fact("ifrs-full:Borrowings", fx.INSTANT_FYE, 500)
    d = dossier(fx.infy_doc(extra_facts=extra))
    assert d["financials"]["financial_debt"] == (967 + 500) * M
    assert d["financials"]["principal_repayment"] == 318 * M              # lease repayments only
    assert any("recorded as 0" in w for w in d["warnings"])
    assert assess_data_integrity(d)["status"] != "BLOCK"


def test_debt_free_company_gets_explicit_zeros_not_missing():
    instant = {k: v for k, v in fx.INFY_INSTANT.items() if "Lease" not in k}
    duration = {k: v for k, v in fx.INFY_DURATION.items() if k not in
                ("ifrs-full:FinanceCosts", "ifrs-full:PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities")}
    d = dossier(fx.build(instant, duration))
    f, p = d["financials"], d["provenance"]
    assert f["financial_debt"] == 0.0 and p["financial_debt"]["status"] == "none_reported"
    assert f["interest_expense"] == 0.0 and f["principal_repayment"] == 0.0
    assert assess_data_integrity(d)["status"] != "BLOCK"


# ---------------------------------------------------------------- units / scale / signs
def test_scale_is_read_from_the_filing_not_assumed():
    # same displayed numbers but scale="3" (thousands) -> values 1000x smaller than millions
    html = fx.infy_doc().decode().replace('scale="6"', 'scale="3"').encode()
    assert dossier(html)["financials"]["total_assets"] == 16446 * 1000.0


def test_sign_attribute_and_fixed_zero_format():
    extra = (fx._fact("ifrs-full:Goodwill", "i_prior", 5, sign="-")
             + '<ix:nonFraction name="ifrs-full:Dummy" contextRef="d_fy" unitRef="U_USD" scale="6" '
               'format="ixt:fixed-zero">&#8212;</ix:nonFraction>')
    parsed = dl.parse_ixbrl(fx.infy_doc(extra_facts=extra))
    vals = {(f["name"], f["context"]): f["value"] for f in parsed["facts"]}
    assert vals[("ifrs-full:Goodwill", "i_prior")] == -5 * M
    assert vals[("ifrs-full:Dummy", "d_fy")] == 0.0


def test_non_usd_currency_detected_and_warned():
    d = dossier(fx.infy_doc(unit_measure="iso4217:INR"))
    assert d["currency"] == "INR"
    codes = {i["code"] for i in assess_data_integrity(d)["issues"]}
    assert "NON_USD_REPORTING" in codes


# ---------------------------------------------------------------- US GAAP alternatives & derivations
def test_us_gaap_derives_total_liabilities_and_handles_no_debt():
    instant = {"us-gaap:Assets": 1000, "us-gaap:LiabilitiesAndStockholdersEquity": 1000,
               "us-gaap:StockholdersEquity": 600, "us-gaap:AssetsCurrent": 500,
               "us-gaap:LiabilitiesCurrent": 200, "us-gaap:RetainedEarningsAccumulatedDeficit": 300}
    duration = {"us-gaap:Revenues": 800, "us-gaap:OperatingIncomeLoss": 120}
    d = dossier(fx.build(instant, duration, with_decoys=False), form="10-K")
    f, p = d["financials"], d["provenance"]
    assert f["total_liabilities"] == 400 * M and p["total_liabilities"]["status"] == "derived"
    assert f["financial_debt"] == 0.0 and f["interest_expense"] == 0.0
    assert assess_data_integrity(d)["status"] != "BLOCK"


# ---------------------------------------------------------------- real US GAAP 10-K tag sets
MSFT_INSTANT = {   # Microsoft FY2026 10-K (USD millions)
    "us-gaap:Assets": 758376, "us-gaap:Liabilities": 315989,
    "us-gaap:LiabilitiesAndStockholdersEquity": 758376, "us-gaap:StockholdersEquity": 442387,
    "us-gaap:AssetsCurrent": 207710, "us-gaap:LiabilitiesCurrent": 168825,
    "us-gaap:RetainedEarningsAccumulatedDeficit": 328265,
    "us-gaap:LongTermDebt": 40294, "us-gaap:LongTermDebtCurrent": 9227, "us-gaap:LongTermDebtNoncurrent": 31067,
    "us-gaap:FinanceLeaseLiability": 66594, "us-gaap:Goodwill": 119651,
    "us-gaap:FiniteLivedIntangibleAssetsNet": 18609,
    "us-gaap:CashAndCashEquivalentsAtCarryingValue": 20935, "us-gaap:ShortTermInvestments": 55908,
    "us-gaap:LongTermDebtFairValue": 36500,                      # must not be mistaken for debt
    "us-gaap:LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": 9250,
}
MSFT_DURATION = {
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": 331839,
    "us-gaap:OperatingIncomeLoss": 155237, "us-gaap:InterestExpenseNonoperating": 3051,
    "us-gaap:RepaymentsOfDebtMaturingInMoreThanThreeMonths": 3000,
    "us-gaap:FinanceLeasePrincipalPayments": 3101,
    "us-gaap:ProceedsFromRepaymentsOfShortTermDebtMaturingInThreeMonthsOrLess": 0,
}
AAPL_INSTANT = {   # Apple FY2025 10-K (USD millions)
    "us-gaap:Assets": 359241, "us-gaap:Liabilities": 285508,
    "us-gaap:LiabilitiesAndStockholdersEquity": 359241, "us-gaap:StockholdersEquity": 73733,
    "us-gaap:AssetsCurrent": 147957, "us-gaap:LiabilitiesCurrent": 165631,
    "us-gaap:RetainedEarningsAccumulatedDeficit": 14264,
    "us-gaap:CommercialPaper": 7979, "us-gaap:LongTermDebtCurrent": 12350,
    "us-gaap:LongTermDebtNoncurrent": 78328,
    "us-gaap:FinanceLeaseLiabilityCurrent": 538, "us-gaap:FinanceLeaseLiabilityNoncurrent": 692,
}
AAPL_DURATION = {
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": 416161,
    "us-gaap:OperatingIncomeLoss": 133050, "us-gaap:RepaymentsOfLongTermDebt": 10932,
    "us-gaap:ProceedsFromRepaymentsOfCommercialPaper": 2032,
}


def test_msft_10k_resolves_fully_and_passes_the_gate():
    d = dossier(fx.build(MSFT_INSTANT, MSFT_DURATION, with_decoys=False), form="10-K")
    f, p = d["financials"], d["provenance"]
    assert f["financial_debt"] == (40294 + 66594) * M            # LongTermDebt + finance leases
    assert f["interest_expense"] == 3051 * M
    assert f["principal_repayment"] == (3000 + 3101) * M
    assert f["intangibles"] == 18609 * M and f["goodwill"] == 119651 * M
    assert p["principal_repayment"]["status"] == "derived"
    assert not [i for i in assess_data_integrity(d)["issues"] if i["severity"] == "BLOCK"]


def test_aapl_counts_commercial_paper_and_term_debt_and_flags_missing_interest_softly():
    d = dossier(fx.build(AAPL_INSTANT, AAPL_DURATION, with_decoys=False), form="10-K")
    f = d["financials"]
    assert f["financial_debt"] == (7979 + 12350 + 78328 + 538 + 692) * M
    assert f["principal_repayment"] == (10932 + 538) * M  # term-debt repayments + current finance-lease portion
    assert f["interest_expense"] is None                  # Apple no longer tags interest expense: never defaulted
    r = assess_data_integrity(d)
    assert not [i for i in r["issues"] if i["severity"] == "BLOCK"]
    assert [i["field"] for i in r["issues"] if i["code"] == "MISSING_INPUT_SOFT"] == ["interest_expense"]


def _aapl_with_total_lease(amount):
    inst = {k: v for k, v in AAPL_INSTANT.items() if "FinanceLeaseLiability" not in k}
    inst["us-gaap:FinanceLeaseLiability"] = amount       # total only: no current portion to proxy repayments
    return dossier(fx.build(inst, AAPL_DURATION, with_decoys=False), form="10-K")


def test_small_lease_with_no_repayment_or_current_portion_is_waived_and_logged(monkeypatch):
    d = _aapl_with_total_lease(1230)
    assert any("waived as immaterial" in w for w in d["warnings"])
    assert d["financials"]["principal_repayment"] == 10932 * M
    monkeypatch.setitem(dl.POLICY, "immaterial_component_share", 0.0)
    assert _aapl_with_total_lease(1230)["financials"]["principal_repayment"] is None


def test_material_lease_with_no_repayment_or_current_portion_leaves_principal_missing():
    assert _aapl_with_total_lease(9000)["financials"]["principal_repayment"] is None


def test_current_lease_portion_proxies_missing_lease_repayments():
    d = dossier(fx.build(AAPL_INSTANT, AAPL_DURATION, with_decoys=False), form="10-K")
    assert any("proxied by current maturities" in w and "FinanceLeaseLiabilityCurrent" in w for w in d["warnings"])


def test_analyst_supplied_interest_unblocks_aapl():
    d = dossier(fx.build(AAPL_INSTANT, AAPL_DURATION, with_decoys=False), form="10-K")
    d["financials"]["interest_expense"] = 3000 * M
    d["provenance"]["interest_expense"] = {"status": "analyst_override", "note": ""}
    d["override_reason"] = "interest taken from earnings call"
    assert not [i for i in assess_data_integrity(d)["issues"] if i["severity"] == "BLOCK"]


def test_short_term_debt_not_double_counted_when_combined_total_reported():
    inst = dict(MSFT_INSTANT, **{"us-gaap:DebtLongtermAndShorttermCombinedAmount": 50000,
                                 "us-gaap:CommercialPaper": 5000})
    f = dossier(fx.build(inst, MSFT_DURATION, with_decoys=False), form="10-K")["financials"]
    assert f["financial_debt"] == (50000 + 66594) * M


def test_unmapped_us_gaap_debt_concept_is_contained_or_blocks_by_size():
    small = dossier(fx.build(dict(MSFT_INSTANT, **{"us-gaap:SeniorNotes": 9000}), MSFT_DURATION,
                             with_decoys=False), form="10-K")
    assert small["financials"]["financial_debt"] == (40294 + 66594) * M
    assert any("SeniorNotes" in w for w in small["warnings"])
    big = dossier(fx.build(dict(MSFT_INSTANT, **{"us-gaap:SeniorNotes": 500000}), MSFT_DURATION,
                           with_decoys=False), form="10-K")
    assert big["financials"]["financial_debt"] is None
    assert "SeniorNotes" in big["provenance"]["financial_debt"]["note"]


# ---------------------------------------------------------------- generic rules seen on real 10-Ks
def test_debt_current_tag_does_not_block_tsla_or_nvda_style_filings():
    inst = dict(AAPL_INSTANT)
    for k in ("us-gaap:CommercialPaper", "us-gaap:LongTermDebtCurrent", "us-gaap:LongTermDebtNoncurrent"):
        inst.pop(k)
    inst.update({"us-gaap:LongTermDebt": 8468, "us-gaap:DebtCurrent": 999})     # NVDA-like
    d = dossier(fx.build(inst, dict(AAPL_DURATION, **{"us-gaap:InterestExpenseNonoperating": 63}), with_decoys=False),
                form="10-K")
    assert d["financials"]["financial_debt"] == (8468 + 538 + 692) * M
    assert not [i for i in assess_data_integrity(d)["issues"] if i["severity"] == "BLOCK"]


def test_no_repayment_line_at_all_uses_inferred_sum_then_current_maturity_then_zero():
    base = {k: v for k, v in AAPL_INSTANT.items() if "FinanceLease" not in k and k != "us-gaap:CommercialPaper"}
    dur = {k: v for k, v in AAPL_DURATION.items() if k != "us-gaap:RepaymentsOfLongTermDebt"}
    dur["us-gaap:InterestExpenseNonoperating"] = 100
    # (a) TSLA-like: only a convertible-debt repayment fact
    a = dossier(fx.build(base, dict(dur, **{"us-gaap:RepaymentsOfConvertibleDebt": 500}), with_decoys=False), form="10-K")
    assert a["financials"]["principal_repayment"] == 500 * M and a["provenance"]["principal_repayment"]["status"] == "derived"
    # (b) no repayment fact but a current-maturity balance
    b = dossier(fx.build(base, dur, with_decoys=False), form="10-K")
    assert b["financials"]["principal_repayment"] == 12350 * M
    # (c) META-like: no repayment fact and no current maturity -> explicit zero with a warning
    c_inst = {k: v for k, v in base.items() if k not in ("us-gaap:LongTermDebtCurrent", "us-gaap:LongTermDebtNoncurrent")}
    c_inst["us-gaap:LongTermDebt"] = 90678
    c = dossier(fx.build(c_inst, dur, with_decoys=False), form="10-K")
    assert c["financials"]["principal_repayment"] == 0.0 and c["provenance"]["principal_repayment"]["status"] == "none_reported"
    assert any("recorded as 0" in w for w in c["warnings"])


def test_interest_expense_inferred_from_a_nonstandard_tag_with_warning():
    d = dossier(fx.build(AAPL_INSTANT, dict(AAPL_DURATION, **{"us-gaap:InterestAndDebtExpense": 3000}),
                         with_decoys=False), form="10-K")
    assert d["financials"]["interest_expense"] == 3000 * M
    assert d["provenance"]["interest_expense"]["status"] == "derived"
    assert any("inferred from us-gaap:InterestAndDebtExpense" in w for w in d["warnings"])


def test_ebit_derived_from_pretax_income_plus_interest_when_no_operating_income():
    dur = {k: v for k, v in MSFT_DURATION.items() if k != "us-gaap:OperatingIncomeLoss"}
    dur["us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = 1000
    d = dossier(fx.build(MSFT_INSTANT, dur, with_decoys=False), form="10-K")
    assert d["financials"]["ebit"] == (1000 + 3051) * M
    assert d["provenance"]["ebit"]["status"] == "derived"
    assert d["financials"]["net_operating_income"] == (1000 + 3051) * M


def test_cap_lease_inclusive_debt_total_is_not_double_counted_with_finance_leases():
    inst = {k: v for k, v in MSFT_INSTANT.items() if "LongTermDebt" not in k}
    inst["us-gaap:DebtAndCapitalLeaseObligations"] = 5000
    d = dossier(fx.build(inst, MSFT_DURATION, with_decoys=False), form="10-K")
    assert d["financials"]["financial_debt"] == 5000 * M                         # FinanceLeaseLiability not added again





# ---------------------------------------------------------------- network layer (mocked)
def test_fetch_live_end_to_end_with_mocked_sec(monkeypatch):
    tickers = {"0": {"cik_str": 1067491, "ticker": "INFY", "title": "INFOSYS LTD"}}
    subs = {"filings": {"recent": {
        "form": ["6-K", "20-F", "20-F"], "accessionNumber": ["a-1", "0001193125-26-270520", "old-acc"],
        "filingDate": ["2026-07-01", "2026-06-15", "2025-06-20"], "reportDate": ["", "2026-03-31", "2025-03-31"],
        "primaryDocument": ["x.htm", "infy-20260331.htm", "infy-20250331.htm"]}}}
    doc = fx.infy_doc()

    def fake_get(url):
        if "companyfacts" in url:
            raise RuntimeError("404 not found")          # forces the filing-document path
        if url == dl.SEC_TICKER_MAP_URL:
            return json.dumps(tickers).encode()
        if url.startswith("https://data.sec.gov/submissions/"):
            return json.dumps(subs).encode()
        assert url.endswith("/000119312526270520/infy-20260331.htm"), url
        return doc

    monkeypatch.setattr(dl, "_http_get", fake_get)
    monkeypatch.setenv("SEC_IDENTITY", "Test User test@example.com")
    d = dl.fetch_live_sec_dossier("infy")
    assert d["company_name"] == "INFOSYS LTD" and d["source"]["form"] == "20-F"
    assert d["source"]["filing_date"] == "2026-06-15"      # latest 10-K/20-F, ignoring the later 6-K
    assert d["financials"]["total_assets"] == 16446 * M


    assert any("companyfacts API could not be used" in w for w in d["warnings"])
    assert d["source"]["method"].startswith("inline XBRL")


# ---------------------------------------------------------------- companyfacts API + candidates
ACCN = "0001193125-26-323660"


def companyfacts_json(instant, duration, accn=ACCN, extra_rows=()):
    facts = {"us-gaap": {}}

    def add(concept, row, unit="USD"):
        facts["us-gaap"].setdefault(concept.split(":")[1], {"units": {}})["units"].setdefault(unit, []).append(row)

    for c, v in instant.items():
        add(c, {"end": "2026-06-30", "val": v * M, "accn": accn})
    for c, v in duration.items():
        add(c, {"start": "2025-07-01", "end": "2026-06-30", "val": v * M, "accn": accn})
    add("us-gaap:Assets", {"end": "2025-06-30", "val": 1.0, "accn": "0000950170-25-100235"})   # prior filing
    add("us-gaap:Assets", {"end": "2026-06-30", "val": 5.0, "accn": accn}, unit="USD/shares")  # not money
    for concept, row in extra_rows:
        add(concept, row)
    return {"facts": facts}


META_10K = {"form": "10-K", "accession": ACCN, "filing_date": "2026-07-29", "ticker": "MSFT",
            "company_name": "MICROSOFT CORP", "url": "https://example/x.htm"}


def test_companyfacts_path_gives_identical_numbers_to_the_filing_document():
    from_api = dl.build_dossier_from_companyfacts(companyfacts_json(MSFT_INSTANT, MSFT_DURATION), META_10K)
    from_doc = dl.build_dossier_from_ixbrl(fx.build(MSFT_INSTANT, MSFT_DURATION, with_decoys=False), META_10K)
    assert from_api["financials"] == from_doc["financials"]
    assert from_api["fiscal_year_end"] == "2026-06-30"       # (fixture builder's own year-end is Infosys's March)
    assert from_api["source"]["method"] == "SEC companyfacts API"


def test_companyfacts_ignores_other_filings_and_non_monetary_units():
    d = dl.build_dossier_from_companyfacts(companyfacts_json(MSFT_INSTANT, MSFT_DURATION), META_10K)
    assert d["financials"]["total_assets"] == 758376 * M      # not the prior filing's 1.0 or USD/shares 5.0


def test_companyfacts_with_no_facts_for_the_filing_raises():
    with pytest.raises(ValueError, match="no facts for accession"):
        dl.build_dossier_from_companyfacts(companyfacts_json(MSFT_INSTANT, MSFT_DURATION, accn="other"), META_10K)


def test_candidates_offered_for_a_missing_input_but_never_applied():
    dur = dict(AAPL_DURATION, **{"us-gaap:InterestPaidNet": 3000, "us-gaap:InterestIncomeOther": 999})
    d = dossier(fx.build(AAPL_INSTANT, dur, with_decoys=False), form="10-K")
    assert d["financials"]["interest_expense"] is None                 # not auto-filled
    cands = d["candidates"]["interest_expense"]
    assert [c["concept"] for c in cands] == ["us-gaap:InterestPaidNet"]   # interest income is not offered
    assert cands[0]["value"] == 3000 * M
    assert "financial_debt" not in d["candidates"]                     # only missing fields get candidates


def _fetch_mocks(monkeypatch, cf_payload, doc):
    calls = []
    tickers = {"0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}
    subs = {"filings": {"recent": {"form": ["10-K"], "accessionNumber": [ACCN], "filingDate": ["2026-07-29"],
                                   "reportDate": ["2026-06-30"], "primaryDocument": ["msft-20260630.htm"]}}}

    def fake_get(url):
        calls.append(url)
        if url == dl.SEC_TICKER_MAP_URL:
            return json.dumps(tickers).encode()
        if url.startswith("https://data.sec.gov/submissions/"):
            return json.dumps(subs).encode()
        if "companyfacts" in url:
            return json.dumps(cf_payload).encode()
        return doc

    monkeypatch.setattr(dl, "_http_get", fake_get)
    monkeypatch.setenv("SEC_IDENTITY", "Test User test@example.com")
    monkeypatch.delenv("SEC_SOURCE", raising=False)
    return calls


def test_fetch_takes_numbers_from_companyfacts_and_reads_the_document_only_for_narrative(monkeypatch):
    calls = _fetch_mocks(monkeypatch, companyfacts_json(MSFT_INSTANT, MSFT_DURATION), b"")
    d = dl.fetch_live_sec_dossier("msft")
    assert d["source"]["method"] == "SEC companyfacts API"
    assert d["financials"]["total_assets"] is not None   # numbers intact
    assert any(u.endswith(".htm") for u in calls)                          # fetched for the narrative step
    assert d["qualitative_available"] is False                              # unreadable document must not break the numbers
    assert any("Narrative disclosures could not be read" in w for w in d["warnings"])


def test_fetch_attaches_narrative_evidence_when_the_document_has_it(monkeypatch):
    from tests.test_narrative import filing
    _fetch_mocks(monkeypatch, companyfacts_json(MSFT_INSTANT, MSFT_DURATION), filing())
    d = dl.fetch_live_sec_dossier("msft")
    assert d["qualitative_available"] is True and d["qualitative_evidence"]
    assert any("substantial doubt" in e["text"] for e in d["qualitative_evidence"])


def test_fetch_falls_back_to_document_when_api_view_is_missing_more_than_the_document(monkeypatch):
    thin = companyfacts_json({k: v for k, v in MSFT_INSTANT.items() if k != "us-gaap:AssetsCurrent"}, MSFT_DURATION)
    calls = _fetch_mocks(monkeypatch, thin, fx.build(MSFT_INSTANT, MSFT_DURATION, with_decoys=False))
    d = dl.fetch_live_sec_dossier("msft")
    assert d["source"]["method"].startswith("inline XBRL")
    assert d["financials"]["current_assets"] == 207710 * M
    assert any("companyfacts API was incomplete" in w for w in d["warnings"])


def test_fetch_keeps_api_view_when_the_document_adds_nothing(monkeypatch):
    # AAPL-style: interest expense genuinely absent from both views
    calls = _fetch_mocks(monkeypatch, companyfacts_json(AAPL_INSTANT, AAPL_DURATION),
                         fx.build(AAPL_INSTANT, AAPL_DURATION, with_decoys=False))
    d = dl.fetch_live_sec_dossier("msft")
    assert d["source"]["method"] == "SEC companyfacts API"
    assert d["financials"]["interest_expense"] is None


def test_sec_source_ixbrl_forces_the_document(monkeypatch):
    calls = _fetch_mocks(monkeypatch, {}, fx.build(MSFT_INSTANT, MSFT_DURATION, with_decoys=False))
    monkeypatch.setenv("SEC_SOURCE", "ixbrl")
    d = dl.fetch_live_sec_dossier("msft")
    assert d["source"]["method"].startswith("inline XBRL")
    assert not any("companyfacts" in u for u in calls)


def test_unknown_ticker_raises_clear_error(monkeypatch):
    monkeypatch.setattr(dl, "_http_get", lambda url: json.dumps({}).encode())
    with pytest.raises(ValueError, match="not found in the SEC ticker map"):
        dl.resolve_cik("NOPE")


def test_nested_numeric_facts_are_skipped_not_concatenated():
    outer = ('<ix:nonFraction name="ifrs-full:Goodwill" contextRef="i_fye" unitRef="U_USD" scale="6">'
             '<ix:nonFraction name="ifrs-full:Dummy2" contextRef="i_fye" unitRef="U_USD" scale="6">12</ix:nonFraction>'
             '34</ix:nonFraction>')
    parsed = dl.parse_ixbrl(fx.infy_doc(extra_facts=outer))
    names = [f["name"] for f in parsed["facts"]]
    assert "ifrs-full:Dummy2" in names
    assert not any(f["name"] == "ifrs-full:Goodwill" and f["value"] == 1234 * M for f in parsed["facts"])


def test_number_words_format():
    assert dl._parse_value("no", "0", None, "ixt-sec:numwordsen") == 0.0
    assert dl._parse_value("Two", "0", None, "ixt-sec:numwordsen") == 2.0
    assert dl._parse_value("many", "0", None, "ixt-sec:numwordsen") is None


def test_only_noncurrent_long_term_debt_tagged_is_used_not_blocked():
    """Meta-style: LongTermDebtNoncurrent is the only debt fact (no current portion, no total)."""
    inst = {k: v for k, v in MSFT_INSTANT.items() if "LongTermDebt" not in k and "FinanceLease" not in k}
    inst["us-gaap:LongTermDebtNoncurrent"] = 28826
    d = dossier(fx.build(inst, {k: v for k, v in MSFT_DURATION.items() if "Repayments" not in k and "FinanceLease" not in k},
                         with_decoys=False), form="10-K")
    assert d["financials"]["financial_debt"] == 28826 * M
    assert not [i for i in assess_data_integrity(d)["issues"] if i["severity"] == "BLOCK"]


def test_interest_far_above_counted_debt_blocks_as_probable_undercount():
    """Ford-style: consolidated debt not tagged at consolidated level, so only a small lease is counted."""
    inst = {k: v for k, v in MSFT_INSTANT.items() if "LongTermDebt" not in k}
    inst["us-gaap:FinanceLeaseLiability"] = 890
    d = dossier(fx.build(inst, dict(MSFT_DURATION, **{"us-gaap:InterestExpenseNonoperating": 1254}), with_decoys=False),
                form="10-K")
    r = assess_data_integrity(d)
    assert [i["code"] for i in r["issues"] if i["severity"] == "BLOCK"] == ["DEBT_INTEREST_INCONSISTENT"]


def test_normal_interest_to_debt_ratio_passes_the_cross_check():
    d = dossier(fx.build(MSFT_INSTANT, MSFT_DURATION, with_decoys=False), form="10-K")        # 3,051 / 106,888
    assert "DEBT_INTEREST_INCONSISTENT" not in {i["code"] for i in assess_data_integrity(d)["issues"]}


def test_bank_is_rejected_by_sic_code_before_any_extraction(monkeypatch):
    calls = _fetch_mocks(monkeypatch, companyfacts_json(MSFT_INSTANT, MSFT_DURATION), b"")
    orig = dl._http_get

    def with_sic(url):
        out = orig(url)
        if url.startswith("https://data.sec.gov/submissions/"):
            d = json.loads(out)
            d.update(sic="6021", sicDescription="National Commercial Banks")
            return json.dumps(d).encode()
        return out
    monkeypatch.setattr(dl, "_http_get", with_sic)
    with pytest.raises(dl.UnsupportedEntityError, match="not supported.*6021"):
        dl.fetch_live_sec_dossier("msft")
    assert not any("companyfacts" in u or u.endswith(".htm") for u in calls)


def test_non_financial_sic_codes_pass():
    dl.check_supported_sic("7372", "Services-Prepackaged Software")
    dl.check_supported_sic("3711")
    dl.check_supported_sic(None)
    dl.check_supported_sic("6798")                       # REITs are not banks


def test_bank_like_facts_without_sic_are_rejected_when_no_operating_income():
    inst = {k: v for k, v in MSFT_INSTANT.items()}
    dur = {k: v for k, v in MSFT_DURATION.items() if k not in ("us-gaap:OperatingIncomeLoss", "us-gaap:Revenues",
                                                              "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")}
    inst["us-gaap:Deposits"] = 1000
    with pytest.raises(dl.UnsupportedEntityError):
        dl.build_dossier_from_ixbrl(fx.build(inst, dur, with_decoys=False), dict(fx.META, form="10-K"))


NO_INTANG = {k: v for k, v in MSFT_INSTANT.items() if "Intangible" not in k}


def test_indefinite_lived_trademarks_count_as_intangibles_ko_case():
    inst = dict(NO_INTANG, **{"us-gaap:IndefiniteLivedTrademarks": 12531})
    d = dl.build_dossier_from_ixbrl(fx.build(inst, MSFT_DURATION, with_decoys=False), dict(fx.META, form="10-K"))
    assert d["financials"]["intangibles"] == 12531 * M
    assert d["provenance"]["intangibles"]["status"] == "reported"


def test_total_intangibles_tag_beats_component_sum_and_nothing_is_double_counted():
    inst = dict(NO_INTANG, **{"us-gaap:IntangibleAssetsNetExcludingGoodwill": 900,
                                 "us-gaap:IndefiniteLivedTrademarks": 700})
    d = dl.build_dossier_from_ixbrl(fx.build(inst, MSFT_DURATION, with_decoys=False), dict(fx.META, form="10-K"))
    assert d["financials"]["intangibles"] == 900 * M


def test_zero_intangibles_with_unmapped_intangible_like_balance_warns():
    inst = dict(NO_INTANG, **{"us-gaap:CustomerRelationshipsIntangibleNet": 500})
    d = dl.build_dossier_from_ixbrl(fx.build(inst, MSFT_DURATION, with_decoys=False), dict(fx.META, form="10-K"))
    assert d["provenance"]["intangibles"]["status"] == "none_reported"
    assert any("intangible-like balances" in w and "CustomerRelationshipsIntangibleNet" in w for w in d["warnings"])
