"""Generate sample PDFs so the demo runs without hand-crafting documents.

Writes two sets into ``samples/``:
  * matched/        — PO, Invoice, GRN with identical quantities
  * over_invoiced/  — same, but the invoice quantity exceeds the PO

Run once:  python make_samples.py
"""

from __future__ import annotations

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

VENDOR = "Acme Supplies"
PO_NUMBER = "PO-1187"

# (item_code, description, unit_price)
CATALOG = [
    ("BAT-12V", "12V Battery", 450.0),
    ("CBL-2M", "2m Power Cable", 30.0),
]


def _build(path: str, title: str, doc_number: str, rows: list[list[str]]) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=LETTER)
    story = [
        Paragraph(title, styles["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Document Number: {doc_number}", styles["Normal"]),
        Paragraph(f"PO Number: {PO_NUMBER}", styles["Normal"]),
        Paragraph(f"Vendor: {VENDOR}", styles["Normal"]),
        Spacer(1, 0.25 * inch),
    ]
    table = Table(
        [["Item Code", "Description", "Quantity", "Unit Price"]] + rows,
        colWidths=[1.3 * inch, 2.6 * inch, 1.1 * inch, 1.1 * inch],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
            ]
        )
    )
    story.append(table)
    doc.build(story)


def _rows(quantities: dict[str, int]) -> list[list[str]]:
    rows = []
    for code, desc, price in CATALOG:
        qty = quantities[code]
        rows.append([code, desc, str(qty), f"{price:.2f}"])
    return rows


def _make_set(subdir: str, po_qty: dict, inv_qty: dict, grn_qty: dict) -> None:
    out = os.path.join(SAMPLES_DIR, subdir)
    os.makedirs(out, exist_ok=True)
    _build(os.path.join(out, "po.pdf"), "Purchase Order", PO_NUMBER, _rows(po_qty))
    _build(os.path.join(out, "invoice.pdf"), "Invoice", "INV-2043", _rows(inv_qty))
    _build(os.path.join(out, "grn.pdf"), "Goods Receipt Note", "GRN-0091", _rows(grn_qty))
    print(f"  wrote {out}/po.pdf, invoice.pdf, grn.pdf")


def main() -> None:
    os.makedirs(SAMPLES_DIR, exist_ok=True)

    print("Generating matched set…")
    _make_set(
        "matched",
        po_qty={"BAT-12V": 100, "CBL-2M": 50},
        inv_qty={"BAT-12V": 100, "CBL-2M": 50},
        grn_qty={"BAT-12V": 100, "CBL-2M": 50},
    )

    print("Generating over_invoiced set…")
    _make_set(
        "over_invoiced",
        po_qty={"BAT-12V": 100, "CBL-2M": 50},
        inv_qty={"BAT-12V": 120, "CBL-2M": 50},  # invoice qty exceeds PO
        grn_qty={"BAT-12V": 100, "CBL-2M": 50},
    )

    print("Done.")


if __name__ == "__main__":
    main()
