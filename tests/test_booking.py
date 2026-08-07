"""End-to-end booking acceptance tests, pinned to the real client documents.

Every expected number was verified against the source PDFs. Runs offline from
cached fixtures — no API key needed.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AP_MODE", "fixtures")

from ap.costing import recalculate_tax, tolerance_band, unplanned_cost_ratio  # noqa: E402
from ap.invoice_extract import extract_all  # noqa: E402
from ap.miro import build_booking, post, simulate  # noqa: E402
from ap.store import Store  # noqa: E402


@pytest.fixture()
def ctx():
    """Fresh store per test so the consumption ledger never leaks between them."""
    store = Store.bootstrap()
    invoices = extract_all(store.invoice_docs)
    return store, invoices


def _inv(invoices, number):
    return next(i for i in invoices.values() if i.invoice_no == number)


# --- Scenario 1: clean domestic booking (Motion) -------------------------


def test_motion_tax_base_includes_freight(ctx):
    """Goods alone give 50.66; only goods+freight reach the printed 54.65."""
    base, tax = recalculate_tax(614.00, 48.39, 0.0825)
    assert base == 662.39
    assert tax == 54.65
    assert recalculate_tax(614.00, 0.0, 0.0825)[1] == 50.66


def test_motion_books_and_balances(ctx):
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "TX85-01021160"))
    doc = result.doc

    assert doc.po_number == "4741030582"
    assert doc.calculated_tax == 54.65, "tax must recalculate to the invoice figure"
    assert abs(doc.balance) < 0.005, f"balance must be zero, got {doc.balance}"
    assert doc.header_amount == 717.04
    # Only PO line 00030 is touched.
    selected = {l.po_line_no for l in doc.lines if l.selected and l.source == "po_line"}
    assert selected == {"00030"}


def test_cash_discount_is_never_deducted(ctx):
    """R5 — the discount is priced into the PO; deducting it breaks the balance."""
    store, invoices = ctx
    invoice = _inv(invoices, "TX85-01021160")
    result = build_booking(store, invoice)
    assert result.doc.header_amount == invoice.grand_total == 717.04
    assert 717.04 - 6.14 != result.doc.header_amount


def test_motion_posts_and_consumes_ledger(ctx):
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "TX85-01021160"))
    simulate(result.doc)
    ok, miro_no = post(store, result)
    assert ok, miro_no
    assert result.doc.status == "posted"

    line = store.po("4741030582").line("00030")
    assert line.qty_invoiced == 5.0
    assert line.amount_invoiced == 614.00
    assert line.qty_open == 0.0


def test_double_post_is_refused(ctx):
    """Regression guard: posting the same invoice twice must not double-count."""
    store, invoices = ctx
    invoice = _inv(invoices, "TX85-01021160")

    first = build_booking(store, invoice)
    ok, _ = post(store, first)
    assert ok

    second = build_booking(store, invoice)
    ok2, message = post(store, second)
    assert not ok2
    assert "already" in message.lower()
    assert store.po("4741030582").line("00030").amount_invoiced == 614.00


# --- Scenario 2: the multi-vendor import case ----------------------------


def test_bull_customs_routes_entirely_to_unplanned_cost(ctx):
    """THE marquee scenario.

    A different vendor invoices against an import PO that has no delivery-cost
    line. The whole amount must become unplanned delivery cost, the goods line
    must stay untouched, and the document must balance.
    """
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "2600498-1"))
    doc = result.doc

    assert doc.po_number == "4745000031"
    assert doc.unplanned_delivery_cost == 8811.67
    assert abs(doc.balance) < 0.005, f"balance must be zero, got {doc.balance}"

    # The goods line is never selected — its quantity is already consumed.
    goods = [l for l in doc.lines if l.po_line_no == "00010"]
    assert all(not l.selected for l in goods)


def test_bull_customs_switches_invoicing_party(ctx):
    """R7 — the Details-tab vendor switch the operator performs by hand."""
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "2600498-1"))
    assert "BULL CUSTOMS" in result.doc.invoicing_party.upper()
    assert "PP TUBE MILLS" in result.doc.po_vendor.upper()
    assert any(e.code == "VENDOR_MISMATCH" for e in result.doc.exceptions)


def test_unplanned_cost_is_inside_po_tolerance(ctx):
    """3.84% of PO value — the posting that works, versus the one that failed."""
    store, _ = ctx
    po = store.po("4745000031")
    assert unplanned_cost_ratio(8811.67, po) == 3.84
    assert unplanned_cost_ratio(8811.67, po) < 20.0


def test_import_po_root_cause_is_flagged(ctx):
    """The procurement-template finding, not just the workaround."""
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "2600498-1"))
    codes = {e.code for e in result.doc.exceptions}
    assert "NO_DELIVERY_COST_LINE" in codes
    assert "IMPORT_PO_WITHOUT_DELIVERY_LINES" in codes


# --- Scenario 3: the QM gate ---------------------------------------------


def test_qm_gate_holds_then_releases(ctx):
    """R1 — held on quality, postable once released. The live demo beat."""
    store, invoices = ctx
    invoice = _inv(invoices, "2600498-1")

    held = build_booking(store, invoice)
    assert any(e.code == "QM_PENDING" for e in held.doc.exceptions)
    assert held.doc.status == "held"
    ok, message = post(store, held)
    assert not ok and "held" in message.lower()

    store.set_qm_status("4745000031", "00010", "released")
    released = build_booking(store, invoice)
    assert not any(e.code == "QM_PENDING" for e in released.doc.exceptions)
    ok2, miro_no = post(store, released)
    assert ok2, miro_no
    assert released.doc.status == "posted"


# --- Scenario 4: planned freight and keep-open ---------------------------


def test_freight_solutions_matches_planned_line_and_balances(ctx):
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "769984"))
    assert result.doc.header_amount == 100488.75
    assert abs(result.doc.balance) < 0.005
    assert result.disposition == "auto_post"


def test_holdover_line_is_left_open(ctx):
    """R3 — never close a freight line a third party will bill later."""
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, "769984"))
    assert any(e.code == "KEEP_LINES_OPEN" for e in result.doc.exceptions)
    holdover = [l for l in result.doc.lines if l.po_line_no == "00020.2"]
    assert holdover and not holdover[0].selected
    assert store.po("4743004148").line("00020.2").amount_open == 20400.00


# --- Scenario 5: the grey-band variance ----------------------------------


def test_grainger_variance_is_surfaced(ctx):
    """+$21.42 on a $1,077.22 PO: inside 20%, outside the $2 manual tolerance."""
    store, invoices = ctx
    invoice = _inv(invoices, "9972782578")
    assert invoice.grand_total == 1098.64
    po = store.po("4741030682")
    assert po.total == 1077.22

    band, _ = tolerance_band(1098.64 - 1077.22, po.total)
    assert band == "review"

    result = build_booking(store, invoice)
    assert result.disposition in {"review", "hold"}
    assert result.doc.status != "posted"


@pytest.mark.parametrize(
    "difference,po_value,expected",
    [
        (0.0, 1000.0, "auto"),
        (1.50, 1000.0, "auto"),      # within the manual $1-2 tolerance
        (21.42, 1077.22, "review"),  # Grainger
        (500.0, 1000.0, "block"),    # beyond 20%
    ],
)
def test_tolerance_bands(difference, po_value, expected):
    assert tolerance_band(difference, po_value)[0] == expected


# --- Scenario 6: no PO reference -----------------------------------------


@pytest.mark.parametrize("number,code", [("KGC-26-2249", "NO_PO_REFERENCE"), ("424543", "PO_NOT_FOUND")])
def test_missing_po_is_routed_not_failed(ctx, number, code):
    store, invoices = ctx
    result = build_booking(store, _inv(invoices, number))
    assert result.disposition == "block"
    assert any(e.code == code for e in result.doc.exceptions)
    # A blocked invoice must still carry a suggested action for the operator.
    assert all(e.suggested_action for e in result.doc.exceptions if e.severity == "block")


# --- Cross-cutting -------------------------------------------------------


def test_every_invoice_produces_a_disposition(ctx):
    """No invoice may fall through the pipeline without a verdict."""
    store, invoices = ctx
    for invoice in invoices.values():
        result = build_booking(store, invoice)
        assert result.disposition in {"auto_post", "review", "hold", "block"}
        assert result.doc.gl_preview, f"{invoice.invoice_no} produced no GL preview"


def test_gl_preview_balances(ctx):
    """Debits must equal credits on every staged document."""
    store, invoices = ctx
    for invoice in invoices.values():
        doc = build_booking(store, invoice).doc
        debits = round(sum(e.debit for e in doc.gl_preview), 2)
        credits = round(sum(e.credit for e in doc.gl_preview), 2)
        assert abs(debits - credits) < 0.02, f"{invoice.invoice_no}: {debits} vs {credits}"
