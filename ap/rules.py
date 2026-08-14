"""Validation rules applied to a matched invoice.

  R1  QM gate      — quality must clear before MIRO can post
  R2  Tax code     — correct the tax code and recalculate
  R3  Keep open    — freight lines billed later by a third party stay open
  R4  PO tolerance — a 20% breach needs a PO amendment
  R5  Cash discount— already priced into the PO; never deducted
  R6  GRN first    — no goods receipt, no MIRO
  R7  Multi-vendor — invoicing party may differ from the PO vendor

Each check returns Exception_ objects carrying the rule id, severity, evidence
and a suggested action, so the UI can explain why something was held.
"""

from __future__ import annotations

from .costing import CostPlan, PO_TOLERANCE_PCT, tolerance_band, unplanned_cost_ratio
from .models import PO, Exception_, GRNStatus, Invoice, LineMatch
from .store import Store


def check_grn(store: Store, po: PO, line_nos: list[str]) -> list[Exception_]:
    """R6 — material must be received before an invoice can be booked."""
    out: list[Exception_] = []
    for line_no in line_nos:
        status = store.grn_for(po.po_number, line_no)
        if status is None or not status.has_grn:
            line = po.line(line_no)
            out.append(
                Exception_(
                    code="NO_GRN",
                    rule_id="R6",
                    severity="block",
                    message=f"No goods receipt posted for PO line {line_no}"
                    + (f" ({line.description})" if line else ""),
                    suggested_action="Cannot post until the goods receipt exists.",
                    evidence={"po_number": po.po_number, "line_no": line_no},
                )
            )
    return out


def check_qm(store: Store, po: PO, line_nos: list[str] | None) -> list[Exception_]:
    """R1 — the quality gate between GRN and MIRO.

    ``line_nos=None`` checks every line on the PO, which is what a charge-only
    invoice (customs, freight) needs: it resolves to no goods line of its own
    but still cannot post while the PO's quality decision is outstanding.
    """
    out: list[Exception_] = []
    for status in store.qm_pending(po.po_number, line_nos):
        out.append(
            Exception_(
                code="QM_PENDING" if status.qm_status == "pending" else "QM_REJECTED",
                rule_id="R1",
                severity="hold",
                message=f"Quality decision {status.qm_status} on PO line {status.line_no}",
                suggested_action="MIRO cannot post until quality is cleared.",
                evidence={
                    "po_number": po.po_number,
                    "line_no": status.line_no,
                    "grn_no": status.grn_no,
                    "qm_status": status.qm_status,
                },
            )
        )
    return out


def check_vendor(invoice: Invoice, po: PO) -> list[Exception_]:
    """R7 — a different invoicing party is a signal, not an error."""
    if not invoice.vendor_name or not po.vendor_name:
        return []
    inv_v = invoice.vendor_name.upper()
    po_v = po.vendor_name.upper()
    first_word = po_v.split()[0] if po_v.split() else po_v
    if first_word and (first_word in inv_v or inv_v.split()[0] in po_v):
        return []

    return [
        Exception_(
            code="VENDOR_MISMATCH",
            rule_id="R7",
            severity="warn",
            message=(
                f"Invoice is from {invoice.vendor_name}, but PO {po.po_number} "
                f"was raised on {po.vendor_name}"
            ),
            suggested_action="Invoicing party set to the invoice vendor.",
            evidence={"invoice_vendor": invoice.vendor_name, "po_vendor": po.vendor_name},
        )
    ]


def check_delivery_cost_structure(invoice: Invoice, po: PO, plan: CostPlan) -> list[Exception_]:
    """The import finding: charges with no PO line to land on."""
    if plan.unplanned_total <= 0.005:
        return []

    ratio = unplanned_cost_ratio(plan.unplanned_total, po)
    within = ratio <= PO_TOLERANCE_PCT

    out = [
        Exception_(
            code="NO_DELIVERY_COST_LINE",
            rule_id="R4",
            severity="warn" if within else "hold",
            message=(
                f"${plan.unplanned_total:,.2f} of freight/customs charges have no delivery-cost "
                f"line on PO {po.po_number} ({ratio:.2f}% of PO value)"
            ),
            suggested_action=(
                "Posted as unplanned delivery cost on the header; the goods line is not selected."
                if within
                else "Exceeds the PO tolerance — the PO needs amending."
            ),
            evidence={
                "unplanned_total": plan.unplanned_total,
                "po_total": po.total,
                "ratio_pct": ratio,
                "tolerance_pct": PO_TOLERANCE_PCT,
                "price_basis": po.price_basis,
            },
        )
    ]

    # The root cause, worth naming explicitly for an import PO.
    if po.is_import and not po.has_open_delivery_cost_line:
        out.append(
            Exception_(
                code="IMPORT_PO_WITHOUT_DELIVERY_LINES",
                rule_id="R4",
                severity="info",
                message=(
                    f"PO {po.po_number} is an import PO raised '{po.price_basis}' with no freight "
                    "or customs lines, so those charges cannot match a PO line."
                ),
                suggested_action="Adding freight/customs lines to the PO would let them match directly.",
                evidence={"po_number": po.po_number, "price_basis": po.price_basis, "vendor_country": po.vendor_country},
            )
        )
    return out


def check_qty_consumed(po: PO, matches: list[LineMatch]) -> list[Exception_]:
    """Why the manual attempt failed: the goods line quantity is already used."""
    out: list[Exception_] = []
    for match in matches:
        if not match.po_line_no:
            continue
        line = po.line(match.po_line_no)
        if line is None:
            continue
        if match.invoice_qty > line.qty_open + 0.001:
            out.append(
                Exception_(
                    code="QTY_CONSUMED",
                    rule_id="R4",
                    severity="hold",
                    message=(
                        f"PO line {line.line_no} has only {line.qty_open:g} {line.uom} open "
                        f"but the invoice bills {match.invoice_qty:g}"
                    ),
                    suggested_action="The PO quantity would need increasing.",
                    evidence={
                        "line_no": line.line_no,
                        "qty": line.qty,
                        "qty_invoiced": line.qty_invoiced,
                        "qty_open": line.qty_open,
                        "invoice_qty": match.invoice_qty,
                    },
                )
            )
    return out


def check_unmatched_lines(
    unmatched: list[tuple[str, float]], rerouted: set[str]
) -> list[Exception_]:
    """Invoice lines that matched no PO line and are not delivery costs.

    Without this, an unmatched line would drop out of the document silently and
    the balance would simply not tie — the operator would see a number with no
    explanation.
    """
    out: list[Exception_] = []
    for description, amount in unmatched:
        if description in rerouted or amount <= 0:
            continue
        out.append(
            Exception_(
                code="UNMATCHED_ITEM",
                rule_id="R4",
                severity="warn",
                message=f"Invoice line '{description}' (${amount:,.2f}) matches no line on the PO",
                suggested_action="No PO line agreed on item code, quantity, price or description.",
                evidence={"description": description, "amount": amount},
            )
        )
    return out


def check_tax(invoice: Invoice, expected_code: str, calculated: float) -> list[Exception_]:
    """R2 — tax code correction and recalculation."""
    out: list[Exception_] = []
    difference = round(invoice.tax_amount - calculated, 2)
    if abs(difference) < 0.005:
        return out

    band, explanation = tolerance_band(difference, invoice.grand_total or 1.0)
    out.append(
        Exception_(
            code="TAX_VARIANCE",
            rule_id="R2",
            severity="warn" if band != "block" else "hold",
            message=(
                f"Invoice tax ${invoice.tax_amount:,.2f} differs from recalculated "
                f"${calculated:,.2f} ({expected_code}) by ${difference:,.2f}"
            ),
            suggested_action=(
                "Within tolerance." if band == "auto"
                else "Tax code on the invoice does not agree with the PO."
            ),
            evidence={
                "invoice_tax": invoice.tax_amount,
                "calculated_tax": calculated,
                "difference": difference,
                "expected_code": expected_code,
                "band": band,
            },
        )
    )
    return out


def check_keep_open(po: PO) -> list[Exception_]:
    """R3 — surface freight lines that must not be closed by this posting."""
    open_lines = [
        l for l in po.postable_lines() if l.keep_open and l.amount_open > 0.005
    ]
    if not open_lines:
        return []
    listed = ", ".join(f"{l.line_no} ({l.description}, ${l.amount_open:,.2f})" for l in open_lines)
    return [
        Exception_(
            code="KEEP_LINES_OPEN",
            rule_id="R3",
            severity="info",
            message=f"Freight/customs lines left open: {listed}",
            suggested_action="Not selected — these stay available for a later invoice.",
            evidence={"lines": [l.line_no for l in open_lines]},
        )
    ]


def check_duplicate(store: Store, invoice: Invoice) -> list[Exception_]:
    """Basic duplicate detection before posting."""
    if store.is_posted(invoice.invoice_no):
        return [
            Exception_(
                code="DUPLICATE_INVOICE",
                rule_id="R6",
                severity="block",
                message=f"Invoice {invoice.invoice_no} has already been posted as MIRO {store.posted[invoice.invoice_no]}",
                suggested_action="Already posted — this is a duplicate.",
                evidence={"invoice_no": invoice.invoice_no, "miro_no": store.posted[invoice.invoice_no]},
            )
        ]
    return []


def check_po_resolution(invoice: Invoice, po: PO | None) -> list[Exception_]:
    """Missing or unresolvable PO reference — route, never fail silently."""
    if po is not None:
        return []
    if not invoice.po_number_raw:
        return [
            Exception_(
                code="NO_PO_REFERENCE",
                rule_id="R6",
                severity="block",
                message=f"Invoice {invoice.invoice_no} carries no purchase-order reference",
                suggested_action="Cannot be matched — needs a PO number.",
                evidence={"invoice_no": invoice.invoice_no, "vendor": invoice.vendor_name},
            )
        ]
    return [
        Exception_(
            code="PO_NOT_FOUND",
            rule_id="R6",
            severity="block",
            message=f"PO reference '{invoice.po_number_raw}' does not match any known purchase order",
            suggested_action="May be the vendor's own order number.",
            evidence={"po_number_raw": invoice.po_number_raw, "invoice_no": invoice.invoice_no},
        )
    ]
