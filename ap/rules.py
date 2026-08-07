"""Business rules R1-R7, each traceable to the discovery-call transcripts.

  R1  QM gate      — quality must clear before MIRO can post (notes.txt 00:11:04)
  R2  Tax code     — V0 -> B2 correction and recalculation (notes.txt 00:19:26)
  R3  Keep open    — freight lines billed later by a third party stay open (00:07:48)
  R4  PO tolerance — ~20% breach needs a procurement PO amendment (00:51:57)
  R5  Cash discount— already priced into the PO; never deducted (00:27:48)
  R6  GRN first    — no goods receipt, no MIRO (00:12:54)
  R7  Multi-vendor — invoicing party may differ from the PO vendor (00:37:02)

Each check returns Exception_ objects carrying the rule id, severity, evidence
and a suggested action, so the UI can explain *why* something was held.
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
                    suggested_action="Ask the stores team to post the GRN, then re-run the booking.",
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
                message=f"Quality decision {status.qm_status} on PO line {status.line_no} — MIRO cannot post yet",
                suggested_action="Email the end user to complete the QM decision; the invoice stays in the hold queue.",
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
            suggested_action=(
                "Switch the invoicing party on the MIRO Details tab to the invoice vendor. "
                "This is the expected multi-vendor pattern for imports (freight, customs, brokerage)."
            ),
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
                "Post as unplanned delivery cost on the MIRO header — do NOT select the goods line. "
                if within
                else "Unplanned cost exceeds the PO tolerance; route to procurement for a PO amendment. "
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
                    f"PO {po.po_number} is an import PO raised '{po.price_basis}' with no freight or "
                    "customs condition lines, so every downstream customs invoice is structurally unmatched."
                ),
                suggested_action=(
                    "Procurement fix: add freight/customs condition lines to import PO templates. "
                    "The agent can route around this via unplanned delivery cost, but the root cause sits in the PO."
                ),
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
                    suggested_action="Ask procurement to increase the PO quantity, or split the invoice.",
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
                suggested_action=(
                    "Check whether the vendor billed an item that was never ordered, "
                    "or whether the PO needs an additional line."
                ),
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
                "Within manual tolerance — post." if band == "auto"
                else "Confirm the tax code with the vendor before posting; procurement may need to update the PO."
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
            message=f"Freight/customs lines to leave open: {listed}",
            suggested_action=(
                "Do not tick these lines. Freight often arrives later on a separate invoice "
                "from a third party, and closing them here would strand that invoice."
            ),
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
                suggested_action="Do not post again. Check whether the vendor re-sent the same invoice.",
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
                suggested_action=(
                    "Route to the non-PO queue for manual GL coding (FB60), or ask the vendor to quote a PO. "
                    "Client policy requires a PO number on every invoice."
                ),
                evidence={"invoice_no": invoice.invoice_no, "vendor": invoice.vendor_name},
            )
        ]
    return [
        Exception_(
            code="PO_NOT_FOUND",
            rule_id="R6",
            severity="block",
            message=f"PO reference '{invoice.po_number_raw}' does not resolve to a known purchase order",
            suggested_action="Confirm the PO number with the vendor; it may be their own order number rather than ours.",
            evidence={"po_number_raw": invoice.po_number_raw, "invoice_no": invoice.invoice_no},
        )
    ]
