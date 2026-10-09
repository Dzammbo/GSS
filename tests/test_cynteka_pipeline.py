import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cynteka_pipeline import allocate_pro_rata, money, stage_page, verify_export, reconcile


class NativePipelineTests(unittest.TestCase):
    def test_resume_and_hash_tampering(self):
        state = {"payments": {"limit": 100, "pages": [], "next_page": 1, "complete": False}}
        page = {"ok": True, "page": 1, "limit": 100, "count": 1, "items": [{"id": 1}], "next_page": None, "has_more": False, "total_count": 1}
        with tempfile.TemporaryDirectory() as root:
            updated = stage_page(root, "run", state, page)
            self.assertEqual(verify_export(root, updated), [{"id": 1}])
            self.assertEqual(state["payments"]["next_page"], 1)
            # Simulate crash before checkpoint: reuse same written page.
            self.assertEqual(stage_page(root, "run", state, page), updated)
            path = Path(root) / updated["payments"]["pages"][0]["path"]
            path.write_text(path.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "hash"):
                verify_export(root, updated)

    def test_missing_pagination_not_complete(self):
        state = {"payments": {"limit": 100, "pages": [], "next_page": 1, "complete": False}}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "pagination"):
                stage_page(root, "run", state, {"ok": True, "page": 1, "limit": 100, "count": 0, "items": []})

    def test_non_finite_money(self):
        for value in [None, True, "NaN", "Infinity"]:
            with self.assertRaises(ValueError):
                money(value)

    def fixture(self):
        payment = {"id": 1, "amount": "122", "vatAmount": "22", "paymentDate": "2026-01-05", "accepted": False, "currency": 643, "offer": {"id": 9}}
        offer = {"id": 9, "payer": {"id": 2, "name": "Example payer"}, "currency": {"id": 643, "name": "RUB"}, "deleted": False, "hasVat": True, "totalAmount": "122"}
        rows = {"ok": True, "page": 1, "limit": 100, "count": 1, "offer_id": 9, "next_page": None, "items": [{"amount": "122"}]}
        review = {"evidence": "Synthetic reviewed invoice", "row_categories": ["materials"]}
        return payment, offer, rows, review

    def run_case(self, p, o, rows, review):
        return reconcile([p], {"9": o}, {"9": [rows]}, {"9": review}, 2, "Example payer", "2026-01-01", "2026-10-09")

    def test_native_schema_payment_date_not_acceptance(self):
        p, o, rows, review = self.fixture()
        result = self.run_case(p, o, rows, review)
        self.assertEqual(result["amounts_by_currency_id"]["643"]["materials"]["gross"], "122.00")
        self.assertEqual(result["amounts_by_currency_id"]["643"]["materials"]["vat_known"], "22.00")
        self.assertFalse(result["final_result"])
        p["paymentDate"] = None
        p["accepted"] = True
        self.assertEqual(self.run_case(p, o, rows, review)["problems"][0]["reason"], "invalid_payment_date")

    def test_other_payer_and_currency_mismatch(self):
        p, o, rows, review = self.fixture()
        o["payer"]["id"] = 3
        self.assertEqual(len(self.run_case(p, o, rows, review)["excluded_payments"]), 1)
        o["payer"]["id"] = 2
        p["currency"] = 840
        self.assertEqual(self.run_case(p, o, rows, review)["problems"][0]["reason"], "unresolved_currency")

    def test_mixed_invoice_is_allocated_proportionally(self):
        p, o, rows, review = self.fixture()
        rows["items"] = [{"amount": "100"}, {"amount": "22"}]
        rows["count"] = 2
        review["row_categories"] = ["materials", "delivery"]
        result = self.run_case(p, o, rows, review)
        self.assertEqual(result["amounts_by_currency_id"]["643"]["materials"]["gross"], "100.00")
        self.assertEqual(result["amounts_by_currency_id"]["643"]["delivery"]["gross"], "22.00")
        self.assertEqual(result["amounts_by_currency_id"]["643"]["materials"]["pro_rata_payments"], 1)

    def test_pro_rata_rounding_has_exact_checksum(self):
        result = allocate_pro_rata("0.05", {"materials": "1", "delivery": "1", "services": "1"})
        self.assertEqual(sum(result.values()), money("0.05"))
        self.assertEqual(result, {"materials": money("0.02"), "delivery": money("0.02"), "services": money("0.01")})

    def test_unknown_vat_not_zero(self):
        p, o, rows, review = self.fixture()
        p.pop("vatAmount")
        bucket = self.run_case(p, o, rows, review)["amounts_by_currency_id"]["643"]["materials"]
        self.assertEqual(bucket["vat_unknown_payments"], 1)

    def test_duplicate_source_ids_stop_checkpoint(self):
        state = {"payments": {"limit": 100, "pages": [], "next_page": 1, "complete": False}}
        page = {"ok": True, "page": 1, "limit": 100, "count": 2, "items": [{"id": 1}, {"id": 1}], "next_page": None}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "duplicate"):
                stage_page(root, "run", state, page)


if __name__ == "__main__":
    unittest.main()
