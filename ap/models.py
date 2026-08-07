"""Data model for the AP booking (MIRO) agent.

Shapes follow the real Jindal Pipe USA documents in ``poandinvoices/`` and the
business rules observed in the discovery calls (see notes.txt). Rule IDs R1-R7
are referenced throughout and defined in ap/rules.py.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Vocabularies ---------------------------------------------------------

LineCategory = Literal["material", "service", "delivery_cost"]

QMStatus = Literal["not_required", "pending", "released", "rejected"]

ChargeKind = Literal[
    "freight",
    "customs",
    "demurrage",
    "detention",
    "storage",
    "service_fee",
    "wire_fee",
    "disbursement",
    "tax",
    "cash_discount",
    "other",
]

MatchTier = Literal[
    "po_line_ref",       # vendor printed a PO line number (needs corroboration)
    "material_code",     # normalized material code agreement
    "qty_price",         # quantity + unit price agreement
    "description",       # fuzzy description
    "unmatched",
]

Severity = Literal["block", "hold", "warn", "info"]

MiroStatus = Literal["draft", "simulated", "blocked", "held", "posted"]


# --- Purchase order (master data, parsed deterministically) ---------------


class POLine(BaseModel):
    """One purchase-order line. Sub-lines keep their dotted number (00010.1)."""

    line_no: str
    material_code: str = ""
    description: str = ""
    qty: float = 0.0
    uom: str = ""
    net_price: float = 0.0
    total: float = 0.0
    tax_text: str = ""
    tax_code: str = "V0"
    tax_rate: float = 0.0
    line_category: LineCategory = "material"
    is_sub_line: bool = False
    parent_line: str | None = None

    # R3 — freight/customs lines that must never be auto-closed.
    keep_open: bool = False

    # Consumption ledger; mutated as invoices post within a session.
    qty_received: float = 0.0
    qty_invoiced: float = 0.0
    amount_invoiced: float = 0.0

    @property
    def delivery_cost(self) -> bool:
        return self.line_category == "delivery_cost"

    @property
    def qty_open(self) -> float:
        return round(self.qty - self.qty_invoiced, 6)

    @property
    def amount_open(self) -> float:
        return round(self.total - self.amount_invoiced, 2)


class PO(BaseModel):
    po_number: str
    doc_kind: Literal["PO", "WORK_ORDER"] = "PO"
    vendor_no: str = ""
    vendor_name: str = ""
    vendor_country: str = ""
    currency: str = "USD"
    subtotal: float = 0.0
    tax_total: float = 0.0
    other_charges: float = 0.0
    total: float = 0.0
    price_basis: str = ""
    payment_terms: str = ""
    effective_date: str = ""
    lines: list[POLine] = Field(default_factory=list)
    tolerance_pct: float = 20.0
    source_file: str = ""

    @property
    def is_import(self) -> bool:
        """Import PO: vendor sits outside the USA."""
        return bool(self.vendor_country) and self.vendor_country.upper() not in {
            "USA",
            "US",
            "UNITED STATES",
        }

    @property
    def has_open_delivery_cost_line(self) -> bool:
        return any(l.delivery_cost and l.amount_open > 0.005 for l in self.lines)

    def line(self, line_no: str) -> POLine | None:
        return next((l for l in self.lines if l.line_no == line_no), None)

    def postable_lines(self) -> list[POLine]:
        """Lines a MIRO can act on — sub-lines where present, else main lines.

        The freight work order carries its real values on sub-lines (00020.1
        Fuel Surcharge) while the parent (00020) is a rolled-up total, so
        posting against both would double-count.
        """
        parents = {l.parent_line for l in self.lines if l.is_sub_line and l.parent_line}
        return [l for l in self.lines if l.line_no not in parents]


# --- Vendor invoice (LLM-extracted) ---------------------------------------


class InvoiceLine(BaseModel):
    line_ref: str = Field(default="", description="Line number printed on the invoice")
    po_line_ref: str = Field(default="", description="PO line the vendor claims, if printed")
    vendor_item_code: str = ""
    description: str = ""
    qty: float = 0.0
    uom: str = ""
    unit_price: float = 0.0
    amount: float = 0.0


class InvoiceCharge(BaseModel):
    kind: ChargeKind = "other"
    description: str = ""
    amount: float = 0.0


class Invoice(BaseModel):
    invoice_no: str
    invoice_date: str = ""
    due_date: str = ""
    vendor_name: str = ""
    vendor_no: str | None = None
    po_number_raw: str = Field(default="", description="As printed, e.g. 4741030582-BT04")
    po_number: str | None = Field(default=None, description="After suffix strip")
    lines: list[InvoiceLine] = Field(default_factory=list)
    charges: list[InvoiceCharge] = Field(default_factory=list)
    net_total: float = 0.0
    tax_amount: float = 0.0
    cash_discount: float = 0.0
    grand_total: float = 0.0
    payment_terms: str = ""
    source_file: str = ""
    source_hash: str = Field(default="", description="Content hash of the source PDF")

    def charge_total(self, *kinds: ChargeKind) -> float:
        wanted = set(kinds) if kinds else None
        return round(
            sum(c.amount for c in self.charges if wanted is None or c.kind in wanted), 2
        )

    @property
    def goods_total(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)


# --- GRN / QM (seeded; lives in SAP, not in the PDFs) --------------------


class GRNStatus(BaseModel):
    po_number: str
    line_no: str
    grn_no: str | None = None
    qty_received: float = 0.0
    posting_date: str | None = None
    qm_status: QMStatus = "not_required"
    note: str = ""

    @property
    def has_grn(self) -> bool:
        return bool(self.grn_no) and self.qty_received > 0

    @property
    def qm_blocks_posting(self) -> bool:
        return self.qm_status in {"pending", "rejected"}


# --- Matching results ----------------------------------------------------


class LineMatch(BaseModel):
    invoice_line_ref: str = ""
    po_line_no: str | None = None
    tier: MatchTier = "unmatched"
    confidence: float = 0.0
    reason: str = ""
    invoice_qty: float = 0.0
    invoice_amount: float = 0.0
    po_qty: float | None = None
    po_amount: float | None = None


class ChargeRouting(BaseModel):
    charge: InvoiceCharge
    treatment: Literal["planned", "unplanned", "tax", "display_only", "unroutable"]
    po_line_no: str | None = None
    reason: str = ""


# --- Exceptions ----------------------------------------------------------


class Exception_(BaseModel):
    """A named, traceable finding. Named Exception_ to avoid shadowing builtins."""

    code: str
    rule_id: str = ""
    severity: Severity = "warn"
    message: str = ""
    suggested_action: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


# --- MIRO document -------------------------------------------------------


class MiroLine(BaseModel):
    po_line_no: str | None = None
    description: str = ""
    selected: bool = False          # the checkbox Emily toggles in SAP
    qty: float = 0.0
    amount: float = 0.0
    tax_code: str = "V0"
    source: Literal["po_line", "unplanned_delivery_cost", "tax", "manual"] = "po_line"
    keep_open: bool = False
    note: str = ""


class GLEntry(BaseModel):
    account: str
    name: str
    debit: float = 0.0
    credit: float = 0.0


class MiroDoc(BaseModel):
    po_number: str = ""
    invoice_no: str = ""
    invoicing_party: str = ""       # R7 — may differ from the PO vendor
    po_vendor: str = ""
    reference: str = ""
    invoice_date: str = ""
    posting_date: str = ""
    currency: str = "USD"

    header_amount: float = 0.0      # invoice grand total incl. tax
    header_tax: float = 0.0         # tax as printed on the invoice
    calculated_tax: float = 0.0     # recomputed from PO tax code
    tax_base: float = 0.0
    unplanned_delivery_cost: float = 0.0

    lines: list[MiroLine] = Field(default_factory=list)
    gl_preview: list[GLEntry] = Field(default_factory=list)
    exceptions: list[Exception_] = Field(default_factory=list)
    status: MiroStatus = "draft"

    @property
    def selected_total(self) -> float:
        """Value of ticked PO lines only.

        The unplanned-delivery-cost row is rendered in the grid for visibility
        but is a *header* field in SAP, so it is excluded here and added once
        in ``balance``.
        """
        return round(
            sum(
                l.amount
                for l in self.lines
                if l.selected and l.source != "unplanned_delivery_cost"
            ),
            2,
        )

    @property
    def balance(self) -> float:
        """Header amount minus everything allocated. Must be 0.00 to post."""
        return round(
            self.header_amount
            - self.selected_total
            - self.unplanned_delivery_cost
            - self.calculated_tax,
            2,
        )

    @property
    def can_post(self) -> bool:
        return (
            abs(self.balance) < 0.005
            and not any(e.severity in {"block", "hold"} for e in self.exceptions)
        )

    def worst_severity(self) -> Severity:
        for sev in ("block", "hold", "warn"):
            if any(e.severity == sev for e in self.exceptions):
                return sev  # type: ignore[return-value]
        return "info"
