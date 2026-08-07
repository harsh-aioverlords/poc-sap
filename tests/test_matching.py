"""Acceptance tests for invoice-to-PO line resolution.

Runs entirely from cached fixtures — no API key required.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AP_MODE", "fixtures")

from ap.invoice_extract import extract_all  # noqa: E402
from ap.matching import match_charge_to_line, match_lines, normalize_code  # noqa: E402
from ap.store import Store  # noqa: E402


@pytest.fixture(scope="module")
def ctx():
    store = Store.bootstrap()
    invoices = extract_all(store.invoice_docs)
    return store, invoices


def _invoice(invoices, number):
    return next(inv for inv in invoices.values() if inv.invoice_no == number)


@pytest.mark.parametrize(
    "a,b",
    [
        ("P-V3073056", "V3.0730-56"),   # PO material vs Motion's vendor code
        ("P-438MN2", "438MN2"),         # Grainger band saw
        ("P-11HS", "11HS"),
    ],
)
def test_code_normalization_bridges_vendor_and_po_codes(a, b):
    assert normalize_code(a) == normalize_code(b)


def test_motion_resolves_to_po_line_00030(ctx):
    """THE acceptance test.

    The vendor prints `X00020` on this line, but the goods billed are PO line
    00030 (FILTER ELEMENT, qty 5 @ 122.80). A matcher that trusted the printed
    reference would book against the wrong line and the wrong price.
    """
    store, invoices = ctx
    invoice = _invoice(invoices, "TX85-01021160")
    po = store.po(invoice.po_number)

    assert po.po_number == "4741030582"
    matches = match_lines(invoice, po)

    assert len(matches) == 1
    m = matches[0]
    assert m.po_line_no == "00030", f"expected line 00030, got {m.po_line_no} ({m.reason})"
    assert m.po_amount == 614.00
    assert m.confidence > 0.5
    assert m.tier in {"qty_price", "material_code"}


def test_motion_leaves_other_po_lines_untouched(ctx):
    """Partial invoice: lines 00010 and 00020 must not be drawn in."""
    store, invoices = ctx
    invoice = _invoice(invoices, "TX85-01021160")
    po = store.po(invoice.po_number)
    hit = {m.po_line_no for m in match_lines(invoice, po)}
    assert hit == {"00030"}


def test_grainger_single_line_matches(ctx):
    store, invoices = ctx
    invoice = _invoice(invoices, "9972782578")
    po = store.po(invoice.po_number)
    matches = match_lines(invoice, po)
    assert matches[0].po_line_no == "00010"
    assert matches[0].po_amount == 755.22


def test_bull_customs_has_no_goods_line_to_match(ctx):
    """The import case: every amount is a charge, and the PO has no line for them."""
    store, invoices = ctx
    invoice = _invoice(invoices, "2600498-1")
    po = store.po(invoice.po_number)

    assert po.po_number == "4745000031"
    assert po.is_import
    assert not po.has_open_delivery_cost_line

    # Nothing on this invoice may be booked against the goods line 00010.
    for m in match_lines(invoice, po):
        assert m.po_line_no != "00010" or m.confidence < 0.5


def test_freight_surcharge_finds_planned_delivery_line(ctx):
    """Freight Solutions: a charge that DOES have a PO delivery-cost line."""
    store, invoices = ctx
    invoice = _invoice(invoices, "769984")
    po = store.po(invoice.po_number)

    line = match_charge_to_line("Fuel Surcharge for Marion", po)
    assert line is not None
    assert line.line_no == "00020.1"
    assert line.total == 100488.75


def test_holdover_line_stays_open(ctx):
    """R3 — the sibling freight line must remain available for a later invoice."""
    store, _ = ctx
    po = store.po("4743004148")
    holdover = po.line("00020.2")
    assert holdover.keep_open
    assert holdover.amount_open == 20400.00
