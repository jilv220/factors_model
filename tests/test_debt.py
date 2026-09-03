from __future__ import annotations

import unittest

from factors_model.debt import debt_series


def payload(values: dict[str, float], **overrides: object) -> dict:
    return {"facts": {"us-gaap": {
        concept: {"units": {"USD": [{
            "val": value, "end": "2025-12-31", "filed": "2026-02-20",
            "form": "10-K", "accn": "annual-2025", **overrides,
        }]}} for concept, value in values.items()
    }}}


def latest(values: dict[str, float]) -> dict:
    return debt_series(payload(values), as_of="2026-08-26", annual_only=True)[-1]


class DebtExtractionTests(unittest.TestCase):
    def test_capital_lease_aliases_and_missing_short_term_are_explicit(self) -> None:
        # KHC's 2025 facts have this LT coverage, with no same-period ST fact.
        row = latest({
            "LongTermDebtAndCapitalLeaseObligations": 19_311_000_000,
            "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_908_000_000,
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 21_219_000_000,
        })
        self.assertIsNone(row["value"])
        self.assertEqual(row["observed_debt"], 21_219_000_000)
        self.assertEqual(row["missing_components"], ["short_term_borrowings"])

    def test_rtx_short_term_does_not_replace_current_long_term(self) -> None:
        row = latest({
            "ShortTermBorrowings": 204_000_000,
            "LongTermDebtAndCapitalLeaseObligationsCurrent": 3_412_000_000,
            "LongTermDebtAndCapitalLeaseObligations": 34_288_000_000,
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 37_700_000_000,
        })
        self.assertEqual(row["value"], 37_904_000_000)
        self.assertEqual(len(row["components"]), 3)

    def test_hershey_nominal_total_actually_contains_only_long_term(self) -> None:
        row = latest({
            "DebtAndCapitalLeaseObligations": 5_184_521_000,
            "ShortTermBorrowings": 218_546_000,
            "LongTermDebtAndCapitalLeaseObligationsCurrent": 503_327_000,
            "LongTermDebtAndCapitalLeaseObligations": 4_681_194_000,
        })
        self.assertEqual(row["value"], 5_403_067_000)
        self.assertIn("reported_total_excludes_short_term_borrowings", row["issues"])

    def test_fis_long_term_total_does_not_omit_short_term(self) -> None:
        row = latest({"LongTermDebt": 10_353_000_000, "ShortTermBorrowings": 2_729_000_000})
        self.assertEqual(row["value"], 13_082_000_000)

    def test_reported_total_is_not_added_to_its_components(self) -> None:
        row = latest({"DebtAndFinanceLeaseObligations": 100, "LongTermDebt": 80, "ShortTermBorrowings": 20})
        self.assertEqual(row["value"], 100)
        self.assertEqual(len(row["components"]), 1)

    def test_current_total_is_not_added_to_current_long_term(self) -> None:
        row = latest({"DebtCurrent": 40, "LongTermDebtCurrent": 30, "ShortTermBorrowings": 10, "LongTermDebtNoncurrent": 60})
        self.assertEqual(row["value"], 100)

    def test_partial_debt_is_not_usable_as_total(self) -> None:
        for values in ({"ShortTermBorrowings": 0}, {"LongTermDebtNoncurrent": 90}, {"LongTermDebt": 100}):
            with self.subTest(values=values):
                self.assertIsNone(latest(values)["value"])
        self.assertEqual(debt_series({"facts": {}}, as_of="2026-08-26"), [])

    def test_explicit_zero_debt_and_zero_components_are_preserved(self) -> None:
        self.assertEqual(latest({"DebtAndFinanceLeaseObligations": 0})["value"], 0)
        self.assertEqual(latest({"LongTermDebt": 0, "ShortTermBorrowings": 0})["value"], 0)

    def test_inconsistent_total_is_flagged_without_an_understated_value(self) -> None:
        row = latest({"DebtAndCapitalLeaseObligations": 40, "LongTermDebt": 80, "ShortTermBorrowings": 20})
        self.assertIsNone(row["value"])
        self.assertEqual(row["status"], "conflicting")
        self.assertEqual(row["observed_debt"], 100)

    def test_as_of_and_annual_filters_do_not_leak_future_data(self) -> None:
        p = payload({"DebtAndFinanceLeaseObligations": 100})
        facts = p["facts"]["us-gaap"]["DebtAndFinanceLeaseObligations"]["units"]["USD"]
        facts.append({**facts[0], "val": 200, "filed": "2026-09-01", "accn": "future-restatement"})
        facts.append({**facts[0], "val": 300, "filed": "2026-03-01", "form": "10-Q", "accn": "quarter"})
        self.assertEqual(debt_series(p, as_of="2026-08-26", annual_only=True)[-1]["value"], 100)
        self.assertEqual(debt_series(p, as_of="2026-08-26")[-1]["value"], 300)
        self.assertEqual(debt_series(p, as_of="2026-02-01"), [])

    def test_components_from_different_filings_are_not_combined(self) -> None:
        p = payload({"LongTermDebt": 80, "ShortTermBorrowings": 20})
        p["facts"]["us-gaap"]["ShortTermBorrowings"]["units"]["USD"][0].update(filed="2026-03-01", accn="amendment")
        row = debt_series(p, as_of="2026-08-26")[-1]
        self.assertIsNone(row["value"])
        self.assertEqual(row["observed_debt"], 20)

    def test_custom_tags_require_an_explicit_semantic_mapping(self) -> None:
        p = payload({"DebtTotal": 125})
        p["facts"]["issuer"] = p["facts"].pop("us-gaap")
        self.assertEqual(debt_series(p, as_of="2026-08-26"), [])
        result = debt_series(p, as_of="2026-08-26", extra_concepts={"total": [("issuer", "DebtTotal")]})
        self.assertEqual(result[-1]["value"], 125)
        self.assertEqual(result[-1]["components"][0]["taxonomy"], "issuer")

    def test_ifrs_current_and_noncurrent_borrowings(self) -> None:
        p = payload({"CurrentBorrowings": 25, "NoncurrentBorrowings": 75})
        p["facts"]["ifrs-full"] = p["facts"].pop("us-gaap")
        self.assertEqual(debt_series(p, as_of="2026-08-26")[-1]["value"], 100)

    def test_finance_lease_aliases(self) -> None:
        row = latest({"LongTermDebtAndFinanceLeaseObligationsCurrent": 5, "LongTermDebtAndFinanceLeaseObligationsNoncurrent": 45, "ShortTermDebt": 0})
        self.assertEqual(row["value"], 50)


if __name__ == "__main__":
    unittest.main()
