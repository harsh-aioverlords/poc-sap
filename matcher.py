"""Three-way match logic for AP: PO vs Invoice vs GRN.

Pure Python over plain dicts — no OpenAI, no Streamlit — so it can be unit-tested
in isolation. See plan.md sections 4 and 5 for the data shapes and match rules.
"""

from __future__ import annotations

import re
from typing import Any


def normalize_key(item: dict[str, Any]) -> str:
    """Return a stable key used to align a line item across documents.

    Prefer ``item_code``; fall back to a normalized ``description`` (lowercased,
    whitespace-collapsed) when the code is missing or blank so items still line
    up when different documents omit or vary the code.
    """
    code = str(item.get("item_code", "")).strip()
    if code:
        return code.upper()
    desc = str(item.get("description", "")).strip().lower()
    return re.sub(r"\s+", " ", desc)


def _qty(item: dict[str, Any]) -> float:
    try:
        return float(item.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _index(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map normalize_key -> line item for one document's line_items."""
    index: dict[str, dict[str, Any]] = {}
    for item in doc.get("line_items", []) or []:
        key = normalize_key(item)
        if key:
            index[key] = item
    return index


def _label(key: str, item: dict[str, Any] | None) -> str:
    """Human-friendly label for a line: the item code/key itself is fine."""
    if item:
        code = str(item.get("item_code", "")).strip()
        if code:
            return code
    return key


def three_way_match(
    po: dict[str, Any], invoice: dict[str, Any], grn: dict[str, Any]
) -> dict[str, Any]:
    """Compare three extracted documents and produce a result row.

    Rules (exact quantity, price ignored) — matched by normalize_key:
      * on invoice but not PO      -> unmatched_item
      * invoice qty > PO qty       -> over_invoiced
      * invoice qty < PO qty       -> under_invoiced
      * GRN qty != PO qty (PO item) -> receipt_variance
      * all aligned                -> matched

    Overall status is "matched" only if every per-item verdict is "matched";
    otherwise "mismatch" with the reasons collected.
    """
    po_idx = _index(po)
    inv_idx = _index(invoice)
    grn_idx = _index(grn)

    # Union of invoice + PO items (per plan section 4).
    keys = list(dict.fromkeys(list(inv_idx.keys()) + list(po_idx.keys())))

    line_detail: list[dict[str, Any]] = []
    reasons: list[str] = []

    for key in keys:
        po_item = po_idx.get(key)
        inv_item = inv_idx.get(key)
        grn_item = grn_idx.get(key)

        po_qty = _qty(po_item) if po_item else None
        inv_qty = _qty(inv_item) if inv_item else None
        grn_qty = _qty(grn_item) if grn_item else None
        label = _label(key, inv_item or po_item)

        if po_item is None:
            verdict = "unmatched_item"
            reasons.append(f"unmatched_item: {label} (on invoice, not on PO)")
        elif inv_item is not None and inv_qty > po_qty:
            verdict = "over_invoiced"
            reasons.append(f"over_invoiced: {label} (inv {inv_qty:g} vs po {po_qty:g})")
        elif inv_item is not None and inv_qty < po_qty:
            verdict = "under_invoiced"
            reasons.append(f"under_invoiced: {label} (inv {inv_qty:g} vs po {po_qty:g})")
        elif grn_item is not None and grn_qty != po_qty:
            verdict = "receipt_variance"
            reasons.append(
                f"receipt_variance: {label} (grn {grn_qty:g} vs po {po_qty:g})"
            )
        elif grn_item is None:
            # PO+invoice aligned but nothing received yet.
            verdict = "receipt_variance"
            reasons.append(f"receipt_variance: {label} (no GRN line)")
        else:
            verdict = "matched"

        line_detail.append(
            {
                "item": label,
                "po_qty": po_qty,
                "inv_qty": inv_qty,
                "grn_qty": grn_qty,
                "verdict": verdict,
            }
        )

    status = "matched" if all(d["verdict"] == "matched" for d in line_detail) else "mismatch"
    if not line_detail:
        status = "mismatch"
        reasons.append("no comparable line items found")

    return {
        "po_number": po.get("document_number") or po.get("po_number") or "",
        "invoice_number": invoice.get("document_number") or "",
        "grn_number": grn.get("document_number") or "",
        "vendor": invoice.get("vendor") or po.get("vendor") or "",
        "status": status,
        "reason": "; ".join(reasons) if reasons else "all line items matched",
        "line_detail": line_detail,
    }
