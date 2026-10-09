"""Private offline processing of native GSS v2 responses. No network or logging."""
import hashlib
import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path


def blob_sha(content):
    raw = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def money(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing amount")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc
    if not result.is_finite():
        raise ValueError("Non-finite amount")
    return result


def validate_page(payload, page, limit):
    if payload.get("ok") is not True:
        raise ValueError("Source response is not successful")
    if payload.get("page") != page or payload.get("limit") != limit:
        raise ValueError("Page or limit mismatch")
    if not isinstance(payload.get("items"), list):
        raise ValueError("Missing items")
    if payload.get("count") != len(payload["items"]):
        raise ValueError("Page count mismatch")
    if "next_page" not in payload:
        raise ValueError("Missing pagination marker")
    if payload["next_page"] not in (None, page + 1):
        raise ValueError("Broken pagination")
    if payload["next_page"] is not None and not payload["items"]:
        raise ValueError("Empty non-terminal page")
    if payload.get("has_more") is not None and payload["has_more"] != (payload["next_page"] is not None):
        raise ValueError("Contradictory pagination")


def verify_export(root, manifest):
    """Resume only from committed pages; validate content and source stability."""
    state = manifest["payments"]
    records, seen, counts = [], {}, set()
    for expected, checkpoint in enumerate(state["pages"], 1):
        if checkpoint["page"] != expected:
            raise ValueError("Missing checkpoint page")
        # Stored paths are repository-relative, never trust absolute or parent paths.
        relative = Path(checkpoint["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe checkpoint path")
        content = (Path(root) / relative).read_text(encoding="utf-8")
        if blob_sha(content) != checkpoint["blob_sha"]:
            raise ValueError("Checkpoint content hash mismatch")
        payload = json.loads(content)
        validate_page(payload, expected, state["limit"])
        if checkpoint["next_page"] != payload["next_page"] or checkpoint["count"] != payload["count"]:
            raise ValueError("Checkpoint metadata mismatch")
        if expected < len(state["pages"]) and payload["next_page"] != expected + 1:
            raise ValueError("Page after terminal marker")
        if payload.get("total_count") is not None:
            counts.add(payload["total_count"])
        for item in payload["items"]:
            key = item.get("id")
            if key is None or str(key) in seen:
                raise ValueError("Missing or duplicate payment ID")
            seen[str(key)] = item
            records.append(item)
    expected_next = state["pages"][-1]["next_page"] if state["pages"] else 1
    if state["next_page"] != expected_next or state["complete"] != (expected_next is None):
        raise ValueError("Manifest cursor mismatch")
    if len(counts) > 1:
        raise ValueError("Source total changed during export")
    if state["complete"] and counts and len(records) != next(iter(counts)):
        raise ValueError("Source total does not match complete export")
    return records


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def stage_page(root, run_path, manifest, payload):
    """Local write-ahead intake. Git bridge must commit page+manifest atomically.

    An unreferenced page left by a crash is checked and reused, never fetched anew.
    Existing committed pages must pass verify_export before requesting the cursor.
    """
    verify_export(root, manifest)
    page = manifest["payments"]["next_page"]
    if page is None:
        raise ValueError("Export is already complete")
    validate_page(payload, page, manifest["payments"]["limit"])
    path = Path(run_path) / "payments" / f"page-{page:06d}.json"
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe run path")
    target = Path(root) / path
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8")) != payload:
            raise ValueError("Uncommitted page conflicts with new source response")
    else:
        atomic_write(target, payload)
    content = target.read_text(encoding="utf-8")
    checkpoint = {"page": page, "path": str(path), "blob_sha": blob_sha(content),
                  "count": payload["count"], "next_page": payload["next_page"],
                  "total_count": payload.get("total_count")}
    updated = json.loads(json.dumps(manifest))
    updated["payments"]["pages"].append(checkpoint)
    updated["payments"]["next_page"] = payload["next_page"]
    updated["payments"]["complete"] = payload["next_page"] is None
    # Reconciliation rejects ID collisions and changing totals before checkpoint write.
    verify_export(root, updated)
    atomic_write(Path(root) / run_path / "manifest.json", updated)
    return updated


def currency(value):
    if isinstance(value, dict):
        return value.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def allocate_pro_rata(total, weights, quantum=Decimal("0.01")):
    """Allocate a monetary total by non-negative weights with an exact checksum."""
    total = money(total)
    clean = {str(key): money(value) for key, value in weights.items()}
    if total < 0 or not clean or any(value < 0 for value in clean.values()):
        raise ValueError("Invalid pro-rata allocation")
    denominator = sum(clean.values(), Decimal(0))
    if denominator <= 0:
        raise ValueError("Zero pro-rata denominator")
    units = (total / quantum).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if units * quantum != total:
        raise ValueError("Total is not representable in allocation quantum")
    exact = {key: units * value / denominator for key, value in clean.items()}
    allocated_units = {key: value.quantize(Decimal("1"), rounding=ROUND_DOWN)
                       for key, value in exact.items()}
    remaining = int(units - sum(allocated_units.values(), Decimal(0)))
    order = sorted(clean, key=lambda key: (-(exact[key] - allocated_units[key]), key))
    for key in order[:remaining]:
        allocated_units[key] += 1
    result = {key: value * quantum for key, value in allocated_units.items()}
    if sum(result.values(), Decimal(0)) != total:
        raise ValueError("Allocation checksum failed")
    return result


def reconcile(records, offers, item_pages, reviews, payer_id, payer_name,
              paid_from, paid_to):
    """Classification requires evidence for every invoice row.

    Partial payments of reviewed mixed invoices are allocated by gross row value.
    This is an explicit analytical policy, not a bank-confirmed item allocation.
    """
    start, end = date.fromisoformat(paid_from), date.fromisoformat(paid_to)
    if start > end:
        raise ValueError("Invalid period")
    totals, problems, exclusions, seen = {}, [], [], set()
    allowed = {"materials", "services", "works", "rent", "delivery", "tax", "travel", "other"}
    for payment in records:
        key = payment.get("id")
        def reject(reason):
            problems.append({"payment_id": key, "reason": reason})
        if key is None or str(key) in seen:
            reject("missing_or_duplicate_id")
            continue
        seen.add(str(key))
        try:
            paid = date.fromisoformat(payment.get("paymentDate", ""))
        except (ValueError, TypeError):
            reject("invalid_payment_date")
            continue
        if not start <= paid <= end:
            reject("payment_outside_period")
            continue
        offer_id = (payment.get("offer") or {}).get("id")
        offer = offers.get(str(offer_id))
        if not offer or offer.get("id") != offer_id:
            reject("missing_linked_offer")
            continue
        payer = offer.get("payer") or {}
        if not payer.get("id") or not payer.get("name"):
            reject("missing_payer_identity")
            continue
        if payer["id"] != payer_id:
            exclusions.append({"payment_id": key, "reason": "other_payer"})
            continue
        if payer["name"] != payer_name:
            reject("payer_name_mismatch")
            continue
        cur = currency(payment.get("currency"))
        if cur is None or cur != currency(offer.get("currency")) or payment.get("hasNonMainCurrency") is True:
            reject("unresolved_currency")
            continue
        if offer.get("deleted") is not False:
            reject("deleted_or_unknown_offer")
            continue
        try:
            amount = money(payment.get("amount"))
            vat = money(payment.get("vatAmount")) if payment.get("vatAmount") is not None else None
            invoice_total = money(offer.get("totalAmount"))
            if amount <= 0 or invoice_total <= 0 or (vat is not None and not 0 <= vat <= amount):
                raise ValueError("Invalid monetary bounds")
            if offer.get("hasVat") is False and vat not in (None, Decimal(0)):
                raise ValueError("VAT contradicts invoice")
        except ValueError:
            reject("invalid_amount_or_vat")
            continue
        pages = item_pages.get(str(offer_id), [])
        rows = []
        try:
            if not pages:
                raise ValueError("No invoice rows")
            for index, payload in enumerate(pages, 1):
                validate_page(payload, index, payload["limit"])
                if payload.get("offer_id") != offer_id:
                    raise ValueError("Wrong invoice rows")
                if payload["next_page"] != (index + 1 if index < len(pages) else None):
                    raise ValueError("Incomplete invoice rows")
                rows.extend(payload["items"])
            if not rows or abs(sum((money(row.get("amount")) for row in rows), Decimal(0)) - invoice_total) > Decimal("0.01"):
                raise ValueError("Invoice rows do not reconcile")
        except (ValueError, KeyError):
            reject("incomplete_or_unreconciled_invoice_rows")
            continue
        review = reviews.get(str(offer_id), {})
        categories = review.get("row_categories", [])
        if not review.get("evidence") or len(categories) != len(rows) or any(c not in allowed for c in categories):
            reject("classification_requires_evidence")
            continue
        weights = {}
        for row, category in zip(rows, categories):
            weights[category] = weights.get(category, Decimal(0)) + money(row.get("amount"))
        try:
            gross_allocation = allocate_pro_rata(amount, weights)
            vat_allocation = allocate_pro_rata(vat, weights) if vat is not None else None
        except ValueError:
            reject("pro_rata_allocation_failed")
            continue
        mixed = len(weights) > 1
        for cat, allocated in gross_allocation.items():
            bucket = totals.setdefault(str(cur), {}).setdefault(cat, {"gross": Decimal(0), "vat_known": Decimal(0), "vat_unknown_payments": 0, "payments": 0, "pro_rata_payments": 0})
            bucket["gross"] += allocated
            bucket["payments"] += 1
            bucket["pro_rata_payments"] += int(mixed)
            if vat_allocation is None:
                bucket["vat_unknown_payments"] += 1
            else:
                bucket["vat_known"] += vat_allocation[cat]
    rendered = {cur: {cat: {k: str(v) if isinstance(v, Decimal) else v for k, v in vals.items()} for cat, vals in cats.items()} for cur, cats in totals.items()}
    return {"status": "REVIEW_REQUIRED" if problems else "READY_FOR_RECONCILIATION",
            "input_payments": len(records), "amounts_by_currency_id": rendered,
            "problems": problems, "excluded_payments": exclusions,
            "final_result": False}
