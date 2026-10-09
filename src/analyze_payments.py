#!/usr/bin/env python3
"""Offline and deterministic payment validation. No API calls, no GitHub Actions."""
import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

def decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing amount")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc

def load_pages(root):
    pages = sorted(Path(root).glob("page-*.json"))
    if not pages:
        raise ValueError("No pages supplied")
    records, observed, current = [], set(), 1
    for file in pages:
        payload = json.loads(file.read_text(encoding="utf-8"))
        page = payload.get("page")
        if page != current:
            raise ValueError(f"Missing or duplicate page: expected {current}, got {page}")
        if not isinstance(payload.get("items"), list):
            raise ValueError(f"Missing items array: {file.name}")
        observed.add(page)
        records.extend(payload["items"])
        next_page = payload.get("next_page")
        if page != len(pages) and next_page != current + 1:
            raise ValueError(f"Broken pagination at {page}")
        if page == len(pages) and next_page is not None:
            raise ValueError(f"Incomplete export: next_page={next_page}")
        current += 1
    return records, len(pages)

def classify(item):
    # Conservative policy: never label purchases as materials based only on payment text.
    explicit = item.get("reviewed_category")
    allowed = {"materials", "services", "works", "rent", "delivery", "tax", "travel", "other"}
    return explicit if explicit in allowed else "needs_review"

def analyze(records):
    seen = set()
    by_currency = {}
    problems = []
    accepted = 0
    for pos, item in enumerate(records, 1):
        key = item.get("id")
        if key is None or str(key) in seen:
            problems.append({"row": pos, "problem": "missing_or_duplicate_payment_id"})
            continue
        seen.add(str(key))
        if not item.get("paymentDate"):
            problems.append({"row": pos, "problem": "missing_paymentDate"})
            continue
        if item.get("payer_verified") is not True:
            problems.append({"row": pos, "problem": "payer_not_verified"})
            continue
        currency = item.get("currency_code")
        if not currency:
            problems.append({"row": pos, "problem": "missing_currency"})
            continue
        try:
            amount = decimal(item.get("amount"))
        except ValueError:
            problems.append({"row": pos, "problem": "missing_or_invalid_amount"})
            continue
        category = classify(item)
        by_currency.setdefault(currency, {}).setdefault(category, Decimal(0))
        by_currency[currency][category] += amount
        accepted += 1
    return {"total_input_records": len(records), "validated_records": accepted,
            "all_records_classified": not any("needs_review" in v for v in by_currency.values()),
            "amounts_by_currency_and_category": {
                cur: {cat: str(v) for cat, v in sorted(cats.items())}
                for cur, cats in sorted(by_currency.items())},
            "problems": problems,
            "status": "REVIEW_REQUIRED" if problems or any("needs_review" in v for v in by_currency.values()) else "READY_FOR_RECONCILIATION"}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Directory with page-N.json files")
    p.add_argument("--output", required=True, help="Output JSON file")
    args = p.parse_args()
    records, pages = load_pages(args.input)
    report = analyze(records)
    report["completed_pages"] = pages
    report["input_digest"] = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{report['status']}; {len(records)} records; {pages} pages; saved {output}")

if __name__ == "__main__":
    main()
