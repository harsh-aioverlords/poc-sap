"""Deterministic regex parser for Jindal Pipe USA purchase orders.

POs are master data. A hallucinated PO price silently corrupts every downstream
number — variance, tolerance, tax base — so POs are parsed with regex against a
known template rather than sent to an LLM. Only the heterogeneous vendor
invoices go through the model (see ap/invoice_extract.py).

Two line grammars appear in the real documents:

  main line   ``00010 10313140 FILTER ELEMENT P-V3073056 Quantity: 5.000 each``
              with ``122.80 0.00 0.00 614.00`` on a later "Rate / Unit" row.

  sub-line    ``00020.1 Fuel Surcharge - Marion, TX 1.000 EA 100,488.75 100,488.75``
              all values inline; used by the freight work order.
"""

from __future__ import annotations

import re

from .ingest import Document
from .models import PO, POLine

# --- Header fields --------------------------------------------------------

_PO_NO = re.compile(r"Purchase Order No\s*:\s*(\d{10})")
_EFFECTIVE = re.compile(r"Effective Date\s*:\s*(\S+)")
_VENDOR_NO = re.compile(r"Vendor\s*:\s*(\d+)")
_SUBTOTAL = re.compile(r"Order Subtotal\s*:\s*\(USD\)\s*([\d,]+\.\d{2})", re.IGNORECASE)
_SALES_TAX = re.compile(r"^\s*Sales Tax\s+([\d,]+\.\d{2})", re.MULTILINE)
_OTHER_CHARGES = re.compile(r"^\s*Other Charges\s+([\d,]+\.\d{2})", re.MULTILINE)
_TOTAL = re.compile(r"TOTAL ORDER VALUE\s*\n?\s*\$?\s*([\d,]+\.\d{2})")
_PRICE_BASIS = re.compile(r"Price Basis\s*:\s*(.+)")
_PAY_TERMS = re.compile(r"Payment Terms\s*:\s*(.+)")

# --- Line grammars --------------------------------------------------------

# 00010 [MATERIAL] DESCRIPTION Quantity: 5.000 each
_MAIN_LINE = re.compile(
    r"^(?P<line>000\d0)\s+(?P<rest>.+?)\s+Quantity:\s*(?P<qty>[\d,]+\.\d+)\s+(?P<uom>.+?)\s*$"
)
# 00020.1 Fuel Surcharge - Marion, TX 1.000 EA 100,488.75 100,488.75
_SUB_LINE = re.compile(
    r"^(?P<line>000\d0\.\d+)\s+(?P<desc>.+?)\s+(?P<qty>[\d,]+\.\d+)\s+(?P<uom>[A-Za-z]+)\s+"
    r"(?P<rate>[\d,]+\.\d{2})\s+(?P<total>[\d,]+\.\d{2})\s*$"
)
# The price row that follows a main line's "Rate / Unit ..." header.
_RATE_ROW = re.compile(
    r"^(?P<rate>[\d,]+\.\d+)\s+(?P<disc>[\d,]+\.\d+)\s+(?P<pf>[\d,]+\.\d+)\s+(?P<total>[\d,]+\.\d{2})\s*$"
)
_TAX_TEXT = re.compile(r"Applicable Tax\s*:\s*(?P<tax>.+?)\s*$")
_TAX_PCT = re.compile(r"@\s*([\d.]+)\s*%")

# Material codes are 8-digit numbers on most POs, but the intercompany work
# order uses alphanumeric codes like CLX065P224000444O.
_MATERIAL = re.compile(r"^(?P<code>[0-9]{8}|[A-Z]{2,}[0-9A-Z]{6,})\s+(?P<desc>.*)$")

# Phrases that mark a line as freight/customs rather than goods (R3).
# Matched on word boundaries: "HEAVY DUTY" on a degreaser is not a customs duty,
# and mis-flagging a goods line would wrongly hold it open forever.
_DELIVERY_WORDS = (
    "freight", "fuel surcharge", "surcharge", "shipping", "transport", "haulage",
    "customs", "customs duty", "import duty", "brokerage", "demurrage",
    "detention", "holdover", "storage", "transloading", "dwell", "cartage",
    "flatbed delivery", "delivery charge",
)
_SERVICE_WORDS = (
    "service charge", "service fee", "labour", "labor", "commissioning",
    "inspection", "wire fee", "disbursement",
)


def _num(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return 0.0


def _mentions(blob: str, phrases: tuple[str, ...]) -> bool:
    """Whole-word/phrase containment, so 'duty' does not fire on 'HEAVY DUTY'."""
    return any(re.search(rf"\b{re.escape(p)}\b", blob) for p in phrases)


def classify_line(description: str, material_code: str = "") -> str:
    """material | service | delivery_cost — drives R3 keep-open handling."""
    blob = f"{description} {material_code}".lower()
    if _mentions(blob, _DELIVERY_WORDS):
        return "delivery_cost"
    if _mentions(blob, _SERVICE_WORDS):
        return "service"
    return "material"


def parse_tax(tax_text: str) -> tuple[str, float]:
    """``Sales Tax @ 8.25% #(BT)`` -> ("B2", 0.0825); zero-tax -> ("V0", 0.0).

    The client corrects V0 -> B2 by hand in MIRO today (notes.txt, 00:19:26);
    deriving the expected code from the PO is what lets the agent do it.
    """
    text = (tax_text or "").strip()
    if not text or "ZERO" in text.upper() or "NO TAX" in text.upper():
        return "V0", 0.0
    m = _TAX_PCT.search(text)
    if m:
        return "B2", round(float(m.group(1)) / 100.0, 6)
    return "V0", 0.0


def _split_material(rest: str) -> tuple[str, str]:
    """Separate a leading material code from the description."""
    m = _MATERIAL.match(rest.strip())
    if m:
        return m.group("code"), m.group("desc").strip()
    return "", rest.strip()


def _vendor_block(text: str) -> tuple[str, str, str]:
    """Vendor number, name, and country from the PO header block."""
    m = _VENDOR_NO.search(text)
    if not m:
        return "", "", ""
    lines = text[: m.end()].split("\n")
    idx = len(lines) - 1
    all_lines = text.split("\n")
    # The vendor name is the line directly after the "Vendor :NNNN" line.
    name, country = "", ""
    for i, line in enumerate(all_lines):
        if _VENDOR_NO.search(line):
            for nxt in all_lines[i + 1 : i + 8]:
                cand = nxt.strip()
                if not cand:
                    continue
                if not name and not cand.lower().startswith(("consignee", "buyer", "tel", "fax", "e-mail")):
                    # Strip the consignee address that shares the same visual row.
                    name = re.sub(r"\s{2,}.*$", "", cand)
                    name = re.sub(r"\s+(JINDAL PIPE USA INC\.?|Texas.*|BAYTOWN.*)$", "", name).strip(" .")
                low = cand.lower()
                if "india" in low:
                    country = "India"
                elif re.search(r"\busa\b", low):
                    country = country or "USA"
            break
    return m.group(1), name, country


def parse_po(doc: Document) -> PO:
    """Parse one purchase-order document into a PO with line items."""
    text = doc.text
    m_no = _PO_NO.search(text)
    po_number = m_no.group(1) if m_no else (doc.po_number or "")
    vendor_no, vendor_name, vendor_country = _vendor_block(text)

    m_basis = _PRICE_BASIS.search(text)
    m_terms = _PAY_TERMS.search(text)
    m_eff = _EFFECTIVE.search(text)

    po = PO(
        po_number=po_number,
        doc_kind="WORK_ORDER" if doc.kind == "work_order" else "PO",
        vendor_no=vendor_no,
        vendor_name=vendor_name,
        vendor_country=vendor_country,
        subtotal=_num(_SUBTOTAL.search(text).group(1)) if _SUBTOTAL.search(text) else 0.0,
        tax_total=_num(_SALES_TAX.search(text).group(1)) if _SALES_TAX.search(text) else 0.0,
        other_charges=_num(_OTHER_CHARGES.search(text).group(1)) if _OTHER_CHARGES.search(text) else 0.0,
        total=_num(_TOTAL.search(text).group(1)) if _TOTAL.search(text) else 0.0,
        price_basis=m_basis.group(1).strip() if m_basis else "",
        payment_terms=m_terms.group(1).strip() if m_terms else "",
        effective_date=m_eff.group(1).strip() if m_eff else "",
        source_file=doc.filename,
    )
    po.lines = _parse_lines(text)

    # Header tax fills in for lines that print no tax row of their own.
    if po.tax_total > 0:
        header_code, header_rate = "B2", 0.0825
        for line in po.lines:
            if not line.tax_text and line.tax_rate == 0.0:
                line.tax_code, line.tax_rate = header_code, header_rate
    return po


def _parse_lines(text: str) -> list[POLine]:
    lines = text.split("\n")
    out: list[POLine] = []
    seen: set[str] = set()

    for i, raw in enumerate(lines):
        stripped = raw.strip()

        # --- sub-line (all values inline) ---
        sub = _SUB_LINE.match(stripped)
        if sub and sub.group("line") not in seen:
            desc = sub.group("desc").strip()
            cat = classify_line(desc)
            out.append(
                POLine(
                    line_no=sub.group("line"),
                    description=desc,
                    qty=_num(sub.group("qty")),
                    uom=sub.group("uom"),
                    net_price=_num(sub.group("rate")),
                    total=_num(sub.group("total")),
                    line_category=cat,
                    is_sub_line=True,
                    parent_line=sub.group("line").split(".")[0],
                    keep_open=(cat == "delivery_cost"),
                )
            )
            seen.add(sub.group("line"))
            continue

        # --- main line ---
        main = _MAIN_LINE.match(stripped)
        if not main or main.group("line") in seen:
            continue

        code, desc = _split_material(main.group("rest"))
        cat = classify_line(desc, code)
        line = POLine(
            line_no=main.group("line"),
            material_code=code,
            description=desc,
            qty=_num(main.group("qty")),
            uom=main.group("uom").strip(),
            line_category=cat,
            keep_open=(cat == "delivery_cost"),
        )

        # Scan forward for this line's price row and tax text, stopping at the
        # next line marker. The Item Text block can be many lines long, so a
        # fixed lookahead window is not safe.
        for nxt in lines[i + 1 : i + 40]:
            cand = nxt.strip()
            if _MAIN_LINE.match(cand) or _SUB_LINE.match(cand):
                break
            rate = _RATE_ROW.match(cand)
            if rate and line.total == 0.0:
                line.net_price = _num(rate.group("rate"))
                line.total = _num(rate.group("total"))
            tax = _TAX_TEXT.search(cand)
            if tax and not line.tax_text:
                line.tax_text = tax.group("tax").strip()
                line.tax_code, line.tax_rate = parse_tax(line.tax_text)

        out.append(line)
        seen.add(line.line_no)

    return out


def parse_all(docs: list[Document]) -> dict[str, PO]:
    """Parse every PO document into a {po_number: PO} master."""
    master: dict[str, PO] = {}
    for doc in docs:
        if doc.is_po:
            po = parse_po(doc)
            if po.po_number:
                master[po.po_number] = po
    return master
