import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analyze_payments import load_pages, analyze

class PaymentTests(unittest.TestCase):
    def test_completed_export(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "page-000001.json").write_text(json.dumps({"page":1,"next_page":2,"items":[{"id":1,"paymentDate":"2026-09-03","payer_verified":True,"currency_code":"RUB","amount":"120.00","reviewed_category":"materials"}]}))
            (p / "page-000002.json").write_text(json.dumps({"page":2,"next_page":None,"items":[{"id":2,"paymentDate":"2026-09-04","payer_verified":True,"currency_code":"RUB","amount":"80","reviewed_category":"services"}]}))
            items, pages = load_pages(p)
            self.assertEqual(pages, 2)
            result = analyze(items)
            self.assertEqual(result["amounts_by_currency_and_category"]["RUB"]["materials"], "120.00")
            self.assertEqual(result["amounts_by_currency_and_category"]["RUB"]["services"], "80")
            self.assertEqual(result["status"], "READY_FOR_RECONCILIATION")

    def test_incomplete_export_must_fail(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/"page-000001.json").write_text(json.dumps({"page":1,"next_page":2,"items":[]}))
            with self.assertRaisesRegex(ValueError, "Incomplete export"):
                load_pages(d)

    def test_missing_payer_not_included(self):
        result = analyze([{"id":1,"paymentDate":"2026-09-03","currency_code":"RUB","amount":100,"reviewed_category":"materials"}])
        self.assertEqual(result["validated_records"], 0)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_duplicate_not_included(self):
        x={"id":1,"paymentDate":"2026-09-03","currency_code":"RUB","amount":100,"payer_verified":True,"reviewed_category":"materials"}
        result = analyze([x,x])
        self.assertEqual(result["validated_records"], 1)
        self.assertEqual(len(result["problems"]), 1)

    def test_unknown_classification_not_materials(self):
        result=analyze([{"id":9,"paymentDate":"2026-09-03","currency_code":"RUB","amount":"22","payer_verified":True}])
        self.assertEqual(result["amounts_by_currency_and_category"]["RUB"]["needs_review"], "22")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
if __name__ == "__main__":
    unittest.main()
