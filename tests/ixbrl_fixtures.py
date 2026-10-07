"""
Builds small but structurally faithful inline-XBRL (XHTML) documents for tests.

INFY_FY2026 holds the REAL values (US$ millions, scale=6) read from Infosys's 20-F for the fiscal
year ended 2026-03-31 (accession 0001193125-26-270520). Decoys (prior year, dimensioned slice,
partial-year period) are added so tests prove the loader picks the right fact.
"""
from typing import Dict, List, Optional, Tuple

NS = ('xmlns="http://www.w3.org/1999/xhtml" '
      'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
      'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
      'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
      'xmlns:iso4217="http://www.xbrl.org/2003/iso4217"')

FYE = "2026-03-31"
INSTANT_FYE = "i_fye"
DURATION_FY = "d_fy"

# concept -> value in millions (instants at FYE)
INFY_INSTANT = {
    "ifrs-full:Assets": 16446, "ifrs-full:Liabilities": 6606, "ifrs-full:Equity": 9840,
    "ifrs-full:EquityAndLiabilities": 16446, "ifrs-full:CurrentAssets": 10912,
    "ifrs-full:CurrentLiabilities": 5516, "ifrs-full:RetainedEarnings": 13459,
    "ifrs-full:Goodwill": 1278, "ifrs-full:IntangibleAssetsOtherThanGoodwill": 298,
    "ifrs-full:LeaseLiabilities": 967, "ifrs-full:CurrentLeaseLiabilities": 333,
    "ifrs-full:NoncurrentLeaseLiabilities": 634,
    "ifrs-full:CashAndCashEquivalents": 2341, "ifrs-full:CurrentInvestments": 1365,
}
# concept -> value in millions (duration FY2026)
INFY_DURATION = {
    "ifrs-full:RevenueFromContractsWithCustomers": 20158,
    "ifrs-full:ProfitLossFromOperatingActivities": 4085,
    "ifrs-full:FinanceCosts": 47, "ifrs-full:ProfitLoss": 3316,
    "ifrs-full:PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities": 318,
}


def _ctx(cid: str, instant: Optional[str] = None, start: Optional[str] = None,
         end: Optional[str] = None, dim: bool = False) -> str:
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant
              else f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    seg = ('<xbrli:segment><xbrldi:explicitMember dimension="ifrs-full:ComponentsOfEquityAxis">'
           'ifrs-full:RetainedEarningsMember</xbrldi:explicitMember></xbrli:segment>') if dim else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
            f'0001067491</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>')


def _fact(name: str, ctx: str, value: float, unit: str = "U_USD", scale: int = 6, sign: str = "",
          fmt: str = "ixt:num-dot-decimal") -> str:
    shown = f"{value:,.0f}" if scale else f"{value}"
    sign_attr = f' sign="{sign}"' if sign else ""
    return (f'<ix:nonFraction name="{name}" contextRef="{ctx}" unitRef="{unit}" decimals="-{scale}" '
            f'scale="{scale}"{sign_attr} format="{fmt}">{shown}</ix:nonFraction>')


def build(instant: Dict[str, float], duration: Dict[str, float], extra_facts: str = "",
          unit_measure: str = "iso4217:USD", with_decoys: bool = True) -> bytes:
    resources = [
        _ctx(INSTANT_FYE, instant=FYE),
        _ctx(DURATION_FY, start="2025-04-01", end=FYE),
        _ctx("i_prior", instant="2025-03-31"),
        _ctx("d_prior", start="2024-04-01", end="2025-03-31"),
        _ctx("d_half", start="2025-10-01", end=FYE),
        _ctx("i_dim", instant=FYE, dim=True),
        f'<xbrli:unit id="U_USD"><xbrli:measure>{unit_measure}</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="U_pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>',
    ]
    facts: List[str] = []
    for concept, v in instant.items():
        facts.append(_fact(concept, INSTANT_FYE, v))
    for concept, v in duration.items():
        facts.append(_fact(concept, DURATION_FY, v))
    if with_decoys:
        facts += [
            _fact("ifrs-full:Assets", "i_prior", 17419),            # prior year
            _fact("ifrs-full:Assets", "i_dim", 999999),             # dimensioned slice (must be ignored)
            _fact("ifrs-full:RevenueFromContractsWithCustomers", "d_prior", 19277),
            _fact("ifrs-full:RevenueFromContractsWithCustomers", "d_half", 9000),   # partial period
            _fact("ifrs-full:Assets", INSTANT_FYE, 5, unit="U_pure"),               # non-monetary unit (ignored)
        ]
    body = "<div>" + "".join(facts) + extra_facts + "</div>"
    html = (f'<?xml version="1.0" encoding="utf-8"?><html {NS}><head><title>t</title></head><body>'
            f'<div style="display:none"><ix:header><ix:resources>{"".join(resources)}</ix:resources></ix:header></div>'
            f'{body}</body></html>')
    return html.encode("utf-8")


def infy_doc(**kw) -> bytes:
    return build(INFY_INSTANT, INFY_DURATION, **kw)


META = {"form": "20-F", "accession": "0001193125-26-270520", "filing_date": "2026-06-15",
        "url": "https://www.sec.gov/Archives/edgar/data/1067491/000119312526270520/infy-20260331.htm",
        "ticker": "INFY", "cik": 1067491, "company_name": "Infosys Ltd", "identity_configured": True}
