"""Acceptance tests for PO parsing, pinned to the real client documents.

Every number here was read off the actual PDFs in poandinvoices/. These tests
run with no OpenAI key — POs are parsed deterministically.
"""

from __future__ import annotations

import pytest

from conftest import corpus_documents
from ap.ingest import split_documents, strip_po_suffix
from ap.po_parser import classify_line, parse_tax

# `master` comes from tests/conftest.py so the suite passes both locally
# (source PDFs present) and on a deployed checkout (snapshot only).


def test_corpus_splits_into_pos_and_invoices():
    pos, invoices = split_documents(corpus_documents())
    # 20 documents, but two are identical copies of PO 4741030582.
    assert len(pos) == 9
    assert len(invoices) == 10
    assert len({p.po_number for p in pos}) == len(pos), "PO numbers must be unique"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4741030582-BT04", "4741030582"),   # Motion
        ("4745000208-BT05", "4745000208"),   # Grainger
        ("4741030682-BT04", "4741030682"),   # Grainger
        ("4745000031", "4745000031"),        # Bull Customs ref, no suffix
        ("42259", None),                     # 3L Energy — not an SAP PO
        ("", None),
    ],
)
def test_po_suffix_stripping(raw, expected):
    """The -BT04/-BT05 dash portion is internal notation, not part of the PO."""
    assert strip_po_suffix(raw) == expected


def test_motion_po_lines(master):
    """The clean domestic scenario; the invoice bills only line 00030."""
    po = master["4741030582"]
    assert po.vendor_name.upper().startswith("MOTION")
    assert len(po.lines) == 3
    l30 = po.line("00030")
    assert l30 is not None
    assert l30.material_code == "10313140"
    assert l30.description == "FILTER ELEMENT P-V3073056"
    assert l30.qty == 5.0
    assert l30.net_price == 122.80
    assert l30.total == 614.00
    assert (l30.tax_code, l30.tax_rate) == ("B2", 0.0825)


def test_import_po_has_no_delivery_cost_line(master):
    """The structural finding that drives the whole multi-vendor design.

    PO 4745000031 is Ex Works with freight in the buyer's scope, so the
    customs broker's charges have no PO line to land on.
    """
    po = master["4745000031"]
    assert po.is_import
    assert po.vendor_country == "India"
    assert len(po.lines) == 1
    assert po.lines[0].total == 229574.00
    assert po.lines[0].tax_code == "V0"
    assert not po.has_open_delivery_cost_line
    assert "Ex Works" in po.price_basis


def test_freight_work_order_sub_lines(master):
    """Sub-lines carry the real values; both must be keep-open (R3)."""
    po = master["4743004148"]
    assert po.doc_kind == "WORK_ORDER"
    fuel = po.line("00020.1")
    holdover = po.line("00020.2")
    assert fuel is not None and holdover is not None
    assert fuel.total == 100488.75      # matches invoice 769984 exactly
    assert holdover.total == 20400.00
    assert fuel.keep_open and holdover.keep_open
    assert fuel.is_sub_line and fuel.parent_line == "00020"


def test_postable_lines_exclude_rolled_up_parents(master):
    """Posting against both a parent and its sub-lines would double-count."""
    po = master["4743004148"]
    nos = {l.line_no for l in po.postable_lines()}
    assert "00020" not in nos and "00010" not in nos
    assert "00020.1" in nos and "00010.4" in nos


def test_alphanumeric_material_codes(master):
    """The intercompany work order uses codes like CLX065P224000444O."""
    po = master["4747000049"]
    assert po.line("00010").material_code == "CLX065P224000444O"
    assert po.line("00010").qty == 270615.450


def test_po_header_totals(master):
    """Grainger 4741030682 is the variance case: sub + tax + other = total."""
    po = master["4741030682"]
    assert po.subtotal == 755.22
    assert po.tax_total == 62.31
    assert po.other_charges == 259.69
    assert po.total == 1077.22
    assert round(po.subtotal + po.tax_total + po.other_charges, 2) == po.total


def test_every_po_line_has_a_price(master):
    """No silently-zero PO line — a zero price would corrupt every variance."""
    for num, po in master.items():
        for line in po.lines:
            assert line.total > 0, f"PO {num} line {line.line_no} parsed with no total"


@pytest.mark.parametrize(
    "text,code,rate",
    [
        ("Sales Tax @ 8.25% #(BT)", "B2", 0.0825),
        ("ZERO INPUT (PURCHASE) TAX FOR USA", "V0", 0.0),
        ("", "V0", 0.0),
    ],
)
def test_tax_code_derivation(text, code, rate):
    assert parse_tax(text) == (code, rate)


@pytest.mark.parametrize(
    "desc,expected",
    [
        ("Fuel Surcharge - Marion, TX", "delivery_cost"),
        ("Holdover Charges", "delivery_cost"),
        ("Flatbed Delivery", "delivery_cost"),
        ("DEGREASE P-1020 HEAVY DUTY", "material"),   # 'DUTY' is not customs duty
        ("FILTER ELEMENT P-V3073056", "material"),
        ("SLITTER ASSLY WITH STAND&PARKING TROLLEY", "material"),
    ],
)
def test_line_classification(desc, expected):
    assert classify_line(desc) == expected
