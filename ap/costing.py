"""Delivery-cost routing, tax recalculation, and tolerance bands.

Two rules drive this module:

1. The tax base is goods **plus planned freight**. Goods alone do not
   reconcile to the tax printed on the invoice.

2. Cash discount is never subtracted — it is already priced into the PO,
   so deducting it would break the balance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .matching import match_charge_to_line
from .models import PO, ChargeRouting, Invoice, InvoiceCharge, LineMatch

# Charge kinds that are delivery costs in SAP terms (planned or unplanned).
DELIVERY_KINDS = {
    "freight", "customs", "demurrage", "detention", "storage",
    "transloading", "disbursement", "service_fee", "wire_fee",
}

# Tolerance bands. Two scales are in play — a small absolute amount and a
# percentage of PO value. The thresholds are not final.
AUTO_POST_ABS = 2.00      # <= $2 absolute: post without review
PO_TOLERANCE_PCT = 20.0   # > 20% of PO line value: hard SAP error


@dataclass
class CostPlan:
    """How every non-goods amount on an invoice should be treated."""

    routings: list[ChargeRouting]
    planned_total: float = 0.0
    unplanned_total: float = 0.0
    display_only_total: float = 0.0

    @property
    def delivery_total(self) -> float:
        return round(self.planned_total + self.unplanned_total, 2)


def reclassify_unmatched_lines(
    invoice: Invoice, unmatched_descriptions: list[tuple[str, float]]
) -> list[InvoiceCharge]:
    """Turn unmatched 'goods' lines that are really charges into charges.

    A customs-broker invoice has no goods at all — Bull Customs bills Flatbed
    Delivery, Transloading and Demurrage. Whichever of those the extractor
    happens to place in `lines` rather than `charges` must still be routed as a
    delivery cost, or the amount silently disappears from the booking.
    """
    from .po_parser import classify_line  # local import: avoids a cycle

    extra: list[InvoiceCharge] = []
    for description, amount in unmatched_descriptions:
        if amount <= 0:
            continue
        if classify_line(description) == "delivery_cost":
            extra.append(
                InvoiceCharge(kind="freight", description=description, amount=amount)
            )
    return extra


def route_charges(
    invoice: Invoice, po: PO, extra_charges: list[InvoiceCharge] | None = None
) -> CostPlan:
    """Decide planned vs unplanned treatment for each charge.

    A charge is *planned* when the PO carries an open delivery-cost line it can
    post against; otherwise it is *unplanned* and goes to the MIRO header. An
    import PO with no freight or customs condition line therefore routes its
    whole charge total to unplanned delivery cost.
    """
    plan = CostPlan(routings=[])

    for charge in list(invoice.charges) + list(extra_charges or []):
        if charge.kind == "cash_discount":
            plan.routings.append(
                ChargeRouting(
                    charge=charge,
                    treatment="display_only",
                    reason="already priced into the PO under its payment terms (R5) — never deducted",
                )
            )
            plan.display_only_total = round(plan.display_only_total + charge.amount, 2)
            continue

        if charge.kind == "tax":
            plan.routings.append(
                ChargeRouting(charge=charge, treatment="tax", reason="handled in tax recalculation")
            )
            continue

        po_line = match_charge_to_line(charge.description, po)
        if po_line is not None:
            plan.routings.append(
                ChargeRouting(
                    charge=charge,
                    treatment="planned",
                    po_line_no=po_line.line_no,
                    reason=f"matches open PO delivery-cost line {po_line.line_no}",
                )
            )
            plan.planned_total = round(plan.planned_total + charge.amount, 2)
        else:
            plan.routings.append(
                ChargeRouting(
                    charge=charge,
                    treatment="unplanned",
                    reason="no PO delivery-cost line to post against — book as unplanned delivery cost",
                )
            )
            plan.unplanned_total = round(plan.unplanned_total + charge.amount, 2)

    return plan


def expected_tax_code(po: PO, line_nos: list[str]) -> tuple[str, float]:
    """The tax code the matched PO lines imply (V0 or B2 with its rate)."""
    for line_no in line_nos:
        line = po.line(line_no)
        if line and line.tax_rate > 0:
            return line.tax_code, line.tax_rate
    for line in po.lines:
        if line.tax_rate > 0:
            return line.tax_code, line.tax_rate
    return "V0", 0.0


def recalculate_tax(goods_total: float, planned_freight: float, rate: float) -> tuple[float, float]:
    """Return (tax_base, tax_amount). The base is goods plus planned freight."""
    base = round(goods_total + planned_freight, 2)
    return base, round(base * rate, 2)


def tolerance_band(difference: float, po_value: float) -> tuple[str, str]:
    """Classify a variance into auto / review / block.

    Returns (band, explanation).
    """
    diff = abs(difference)
    if diff < 0.005:
        return "auto", "no variance"
    if diff <= AUTO_POST_ABS:
        return "auto", f"${diff:,.2f} is within the ${AUTO_POST_ABS:,.2f} tolerance"
    pct = (diff / po_value * 100) if po_value else 0.0
    if pct > PO_TOLERANCE_PCT:
        return "block", f"${diff:,.2f} is {pct:.1f}% of PO value — beyond the {PO_TOLERANCE_PCT:.0f}% PO tolerance"
    return "review", f"${diff:,.2f} ({pct:.2f}% of PO value) exceeds the ${AUTO_POST_ABS:,.2f} tolerance"


def unplanned_cost_ratio(amount: float, po: PO) -> float:
    """Unplanned delivery cost as a percentage of PO value."""
    return round(amount / po.total * 100, 2) if po.total else 0.0
