"""Assemble, simulate and post a MIRO document.

The agent stages the whole document — invoicing party, line selection, unplanned
delivery cost, recalculated tax — so the balance reads 0.00 and a human commits
it in one click, following the simulate-before-post flow.

No SAP connection: postings mutate the in-session consumption ledger.
"""

from __future__ import annotations

from . import rules
from .costing import (
    CostPlan,
    expected_tax_code,
    recalculate_tax,
    reclassify_unmatched_lines,
    route_charges,
    tolerance_band,
)
from .matching import match_lines
from .models import PO, Exception_, GLEntry, Invoice, LineMatch, MiroDoc, MiroLine
from .store import Store

# Illustrative GL accounts — hand-mapped for the POC, not client master data.
GL_GRIR = ("21120000", "GR/IR clearing account")
GL_VENDOR = ("31000000", "Accounts payable - trade")
GL_TAX = ("14560000", "Input tax receivable")
GL_FREIGHT = ("61300000", "Freight, customs and clearing")
GL_PRICE_DIFF = ("61900000", "Price / rounding difference")


class BookingResult:
    """Everything the UI needs to explain one invoice."""

    def __init__(
        self,
        invoice: Invoice,
        po: PO | None,
        doc: MiroDoc,
        matches: list[LineMatch],
        plan: CostPlan | None,
    ) -> None:
        self.invoice = invoice
        self.po = po
        self.doc = doc
        self.matches = matches
        self.plan = plan

    @property
    def disposition(self) -> str:
        """auto_post | review | hold | block — drives the inbox colour."""
        if any(e.severity == "block" for e in self.doc.exceptions):
            return "block"
        if any(e.severity == "hold" for e in self.doc.exceptions):
            return "hold"
        if abs(self.doc.balance) >= 0.005 or any(e.severity == "warn" for e in self.doc.exceptions):
            return "review"
        return "auto_post"


def build_booking(store: Store, invoice: Invoice, *, posting_date: str = "") -> BookingResult:
    """Run the full booking pipeline for one invoice."""
    po = store.po(invoice.po_number)

    doc = MiroDoc(
        po_number=invoice.po_number or "",
        invoice_no=invoice.invoice_no,
        invoicing_party=invoice.vendor_name,
        po_vendor=po.vendor_name if po else "",
        reference=invoice.invoice_no,
        invoice_date=invoice.invoice_date,
        posting_date=posting_date or invoice.invoice_date,
        header_amount=invoice.grand_total,
        header_tax=invoice.tax_amount,
    )

    # --- PO resolution (R6) ------------------------------------------------
    doc.exceptions.extend(rules.check_po_resolution(invoice, po))
    if po is None:
        # Still show what the posting would look like, so the operator can code
        # it manually (FB60) rather than being handed a blank screen.
        doc.calculated_tax = invoice.tax_amount
        doc.gl_preview = build_gl_preview(doc)
        doc.status = "blocked"
        return BookingResult(invoice, None, doc, [], None)

    doc.exceptions.extend(rules.check_duplicate(store, invoice))

    # --- Line matching -----------------------------------------------------
    matches = match_lines(invoice, po)
    matched_lines = [m.po_line_no for m in matches if m.po_line_no]

    # A broker invoice has no goods at all; whichever of its charge lines the
    # extractor placed in `lines` must still be routed as a delivery cost,
    # otherwise the amount silently disappears from the booking.
    unmatched = [
        (invoice.lines[i].description, invoice.lines[i].amount)
        for i, m in enumerate(matches)
        if m.po_line_no is None and i < len(invoice.lines)
    ]
    extra_charges = reclassify_unmatched_lines(invoice, unmatched)

    # --- Charge routing (planned vs unplanned) -----------------------------
    plan = route_charges(invoice, po, extra_charges)
    planned_line_nos = [r.po_line_no for r in plan.routings if r.treatment == "planned" and r.po_line_no]

    # --- Gates, in order; each contributes exceptions ----------------------
    gate_lines = matched_lines + planned_line_nos
    if gate_lines:
        doc.exceptions.extend(rules.check_grn(store, po, gate_lines))
        doc.exceptions.extend(rules.check_qm(store, po, gate_lines))
    else:
        # A charge-only invoice (customs broker, freight) resolves to no goods
        # line, but the PO it is charged against still has to clear quality
        # before anything can post against it (R1).
        doc.exceptions.extend(rules.check_qm(store, po, None))
    doc.exceptions.extend(rules.check_vendor(invoice, po))
    doc.exceptions.extend(rules.check_qty_consumed(po, matches))
    doc.exceptions.extend(
        rules.check_unmatched_lines(unmatched, {c.description for c in extra_charges})
    )
    doc.exceptions.extend(rules.check_delivery_cost_structure(invoice, po, plan))
    doc.exceptions.extend(rules.check_keep_open(po))

    # --- Build the document lines -----------------------------------------
    tax_code, tax_rate = expected_tax_code(po, matched_lines)

    for match in matches:
        if not match.po_line_no:
            continue
        po_line = po.line(match.po_line_no)
        doc.lines.append(
            MiroLine(
                po_line_no=match.po_line_no,
                description=po_line.description if po_line else "",
                selected=True,
                qty=match.invoice_qty,
                amount=match.invoice_amount,
                tax_code=tax_code,
                source="po_line",
                note=f"{match.tier} · {match.confidence:.0%} confidence",
            )
        )

    for routing in plan.routings:
        if routing.treatment == "planned" and routing.po_line_no:
            po_line = po.line(routing.po_line_no)
            doc.lines.append(
                MiroLine(
                    po_line_no=routing.po_line_no,
                    description=routing.charge.description or (po_line.description if po_line else ""),
                    selected=True,
                    qty=1.0,
                    amount=routing.charge.amount,
                    tax_code=tax_code,
                    source="po_line",
                    keep_open=bool(po_line and po_line.keep_open),
                    note="planned delivery cost",
                )
            )

    if plan.unplanned_total > 0.005:
        doc.unplanned_delivery_cost = plan.unplanned_total
        doc.lines.append(
            MiroLine(
                po_line_no=None,
                description="Unplanned delivery cost (freight / customs / brokerage)",
                selected=True,
                qty=0.0,
                amount=plan.unplanned_total,
                tax_code=tax_code if tax_rate else "V0",
                source="unplanned_delivery_cost",
                note="header field — no PO line selected",
            )
        )

    # Lines the agent deliberately leaves untouched (R3), shown but unticked.
    for po_line in po.postable_lines():
        already = {l.po_line_no for l in doc.lines}
        if po_line.line_no in already:
            continue
        if po_line.keep_open and po_line.amount_open > 0.005:
            doc.lines.append(
                MiroLine(
                    po_line_no=po_line.line_no,
                    description=po_line.description,
                    selected=False,
                    qty=0.0,
                    amount=0.0,
                    tax_code=po_line.tax_code,
                    source="po_line",
                    keep_open=True,
                    note="left open — freight expected later from a third party (R3)",
                )
            )

    # --- Tax recalculation (R2) -------------------------------------------
    goods_total = round(sum(l.amount for l in doc.lines if l.selected and l.source == "po_line"), 2)
    if goods_total <= 0.005 and invoice.goods_total > 0:
        # Nothing resolved to a PO line: tax the invoice's own goods value so
        # the reported variance reflects the real gap rather than taxing only
        # the freight.
        goods_total = invoice.goods_total
    # Unplanned delivery cost is taxed with the goods in SAP's default handling.
    doc.tax_base, doc.calculated_tax = recalculate_tax(
        goods_total, doc.unplanned_delivery_cost if tax_rate else 0.0, tax_rate
    )
    doc.exceptions.extend(rules.check_tax(invoice, tax_code, doc.calculated_tax))

    # --- Balance and status ------------------------------------------------
    _finalize(doc, po)
    return BookingResult(invoice, po, doc, matches, plan)


def _finalize(doc: MiroDoc, po: PO) -> None:
    """Resolve any residual balance and set the document status."""
    balance = doc.balance
    if abs(balance) >= 0.005:
        band, explanation = tolerance_band(balance, po.total)
        doc.exceptions.append(
            Exception_(
                code="BALANCE_VARIANCE",
                rule_id="R4",
                severity={"auto": "info", "review": "warn", "block": "hold"}[band],
                message=f"MIRO balance is ${balance:,.2f}, not zero — {explanation}",
                suggested_action=(
                    "Within tolerance — posts to rounding difference." if band == "auto"
                    else "Invoice total does not agree with the matched PO lines."
                ),
                evidence={"balance": balance, "band": band},
            )
        )

    doc.gl_preview = build_gl_preview(doc)

    if any(e.severity == "block" for e in doc.exceptions):
        doc.status = "blocked"
    elif any(e.severity == "hold" for e in doc.exceptions):
        doc.status = "held"
    else:
        doc.status = "draft"


def build_gl_preview(doc: MiroDoc) -> list[GLEntry]:
    """Illustrative GL postings — hand-mapped accounts, not client master data."""
    entries: list[GLEntry] = []

    goods = round(sum(l.amount for l in doc.lines if l.selected and l.source == "po_line"), 2)
    if goods:
        entries.append(GLEntry(account=GL_GRIR[0], name=GL_GRIR[1], debit=goods))
    if doc.unplanned_delivery_cost:
        entries.append(
            GLEntry(account=GL_FREIGHT[0], name=GL_FREIGHT[1], debit=doc.unplanned_delivery_cost)
        )
    if doc.calculated_tax:
        entries.append(GLEntry(account=GL_TAX[0], name=GL_TAX[1], debit=doc.calculated_tax))

    residual = round(
        doc.header_amount - goods - doc.unplanned_delivery_cost - doc.calculated_tax, 2
    )
    if abs(residual) >= 0.005:
        entries.append(
            GLEntry(
                account=GL_PRICE_DIFF[0],
                name=GL_PRICE_DIFF[1],
                debit=residual if residual > 0 else 0.0,
                credit=-residual if residual < 0 else 0.0,
            )
        )

    entries.append(
        GLEntry(account=GL_VENDOR[0], name=f"{GL_VENDOR[1]} — {doc.invoicing_party}", credit=doc.header_amount)
    )
    return entries


def simulate(doc: MiroDoc) -> MiroDoc:
    """Mark the document simulated (the SAP 'simulate before post' step)."""
    doc.gl_preview = build_gl_preview(doc)
    if doc.status == "draft":
        doc.status = "simulated"
    return doc


def post(store: Store, result: BookingResult) -> tuple[bool, str]:
    """Commit the MIRO document, writing back to the consumption ledger."""
    doc, po = result.doc, result.po

    if store.is_posted(doc.invoice_no):
        return False, f"Invoice {doc.invoice_no} is already posted as MIRO {store.posted[doc.invoice_no]}."
    blocking = [e for e in doc.exceptions if e.severity == "block"]
    if doc.status == "blocked" or blocking:
        detail = blocking[0].message if blocking else "a blocking exception is open"
        return False, f"Cannot post: {detail}."
    if any(e.severity == "hold" for e in doc.exceptions):
        return False, "Cannot post: the document is held (quality gate or tolerance)."
    if abs(doc.balance) >= 0.005:
        return False, f"Cannot post: balance is ${doc.balance:,.2f}, must be 0.00."

    if po is not None:
        for line in doc.lines:
            if line.selected and line.po_line_no and line.source == "po_line":
                store.consume(po.po_number, line.po_line_no, line.qty, line.amount)

    miro_no = store.next_miro_number()
    store.record_posting(doc.invoice_no, miro_no)
    doc.status = "posted"
    return True, miro_no
