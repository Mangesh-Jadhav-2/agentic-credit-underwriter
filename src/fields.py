"""
Single definition of the financial line items the underwriting pipeline consumes.

All monetary values are in FULL currency units (e.g. 20_158_000_000.0, never "20,158" meaning millions).
Unit conversion happens exactly once, in the loader, using the filing's own declared scale.
"""
from typing import List, Tuple

# (key, label) in the order shown in the UI.
FIELD_ORDER: List[Tuple[str, str]] = [
    ("sales", "Annual Sales / Revenue"),
    ("ebit", "EBIT (Operating Profit)"),
    ("net_operating_income", "Net Operating Income (DSCR numerator)"),
    ("current_assets", "Current Assets"),
    ("current_liabilities", "Current Liabilities"),
    ("total_assets", "Total Assets"),
    ("total_liabilities", "Total Liabilities"),
    ("shareholder_equity", "Total Equity"),
    ("retained_earnings", "Retained Earnings"),
    ("financial_debt", "Financial Debt (borrowings + leases)"),
    ("interest_expense", "Interest Expense / Finance Cost"),
    ("principal_repayment", "Principal Repayment (debt + leases)"),
    ("goodwill", "Goodwill"),
    ("intangibles", "Other Intangible Assets"),
    ("cash_and_current_investments", "Cash + Current Investments (info only)"),
]

# Fields without which NO ratio set can be trusted. Missing -> automated decision is blocked.
REQUIRED_FIELDS: List[str] = [
    "sales", "ebit", "current_assets", "current_liabilities",
    "total_assets", "total_liabilities", "shareholder_equity", "retained_earnings",
]

# Needed by the leverage / coverage rules. The loader sets these to an explicit 0.0 (status
# "none_reported") when a company genuinely reports no debt, so None here means "could not be
# determined" and also blocks the decision.
DEBT_FIELDS: List[str] = [
    "financial_debt", "interest_expense", "principal_repayment", "goodwill", "intangibles",
]

# Inputs whose absence does NOT block the run: the ratios that need them are reported as
# "not computable" and the compliance agent raises a CRITICAL data-integrity flag for each. They are never
# defaulted. Reasons: companies often do not disclose them (Apple has no interest-expense fact) or the
# balance sheet is unclassified (banks have no current assets/liabilities).
SOFT_FIELDS: List[str] = ["interest_expense", "principal_repayment", "current_assets",
                          "current_liabilities", "retained_earnings"]

# Everything the quant agent needs.
QUANT_INPUT_FIELDS: List[str] = REQUIRED_FIELDS + DEBT_FIELDS

FIELD_LABELS = dict(FIELD_ORDER)
