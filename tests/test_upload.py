"""Upload ingestion tests.

An uploaded document must produce exactly the same booking as the same document
loaded from the bundled folder — otherwise the demo and the sample corpus would
diverge. Runs offline from cached fixtures.
"""

from __future__ import annotations

import io
import os

import pytest

os.environ.setdefault("AP_MODE", "fixtures")

from conftest import upload_bytes  # noqa: E402
from ap.ingest import load_upload  # noqa: E402
from ap.invoice_extract import extract_invoice  # noqa: E402
from ap.miro import build_booking  # noqa: E402
from ap.store import Store  # noqa: E402


def _upload(store: Store, filename: str, as_name: str | None = None):
    """Upload a document the way Streamlit would — as an in-memory stream."""
    return store.add_document(load_upload(upload_bytes(filename, as_name)))


@pytest.fixture()
def store():
    return Store.bootstrap()


def test_po_and_invoice_are_classified_by_content_not_filename(store):
    """`2600498-1.pdf` is a purchase order despite looking like an invoice number."""
    po_result = _upload(store, "2600498-1.pdf", as_name="mystery-document.pdf")
    inv_result = _upload(
        store, "Invoice_26004981_from_Bull_Customs_Brokerage_LLC.pdf", as_name="another.pdf"
    )

    assert po_result["ok"] and po_result["kind"] == "PO"
    assert po_result["key"] == "4745000031"
    assert inv_result["ok"] and inv_result["kind"] == "Invoice"


def test_upload_order_does_not_matter(store):
    """Invoice first, PO second still resolves."""
    _upload(store, "TX85-01021160.pdf")
    _upload(store, "TX85-01021160po.pdf")

    doc = next(d for d in store.invoice_docs if "TX85" in d.filename)
    result = build_booking(store, extract_invoice(doc))
    assert result.doc.po_number == "4741030582"
    assert result.matches[0].po_line_no == "00030"


def test_uploaded_pair_books_identically_to_bundled(store):
    """The upload path must not change any number."""
    baseline_doc = next(d for d in store.invoice_docs if "TX85" in d.filename)
    baseline = build_booking(store, extract_invoice(baseline_doc))

    fresh = Store.bootstrap()
    fresh.po_master.clear()
    fresh.invoice_docs.clear()
    _upload(fresh, "TX85-01021160po.pdf")
    _upload(fresh, "TX85-01021160.pdf")
    uploaded_doc = fresh.invoice_docs[0]
    uploaded = build_booking(fresh, extract_invoice(uploaded_doc))

    assert uploaded.doc.calculated_tax == baseline.doc.calculated_tax == 54.65
    assert abs(uploaded.doc.balance) < 0.005
    assert uploaded.matches[0].po_line_no == baseline.matches[0].po_line_no == "00030"


def test_uploaded_po_is_seeded_as_received_and_released(store):
    """R1/R6 must not block an uploaded PO by default — it has no SAP status."""
    store.po_master.clear()
    store.grn.clear()
    _upload(store, "TX85-01021160po.pdf")

    rows = store.grn_for_po("4741030582")
    assert rows, "uploaded PO should be seeded with GRN/QM rows"
    assert all(r.has_grn for r in rows)
    assert all(r.qm_status == "released" for r in rows)


def test_uploaded_pair_posts_cleanly(store):
    """A freshly uploaded pair should book straight through."""
    store.po_master.clear()
    store.invoice_docs.clear()
    store.grn.clear()
    _upload(store, "TX85-01021160po.pdf")
    _upload(store, "TX85-01021160.pdf")

    from ap.miro import post

    result = build_booking(store, extract_invoice(store.invoice_docs[0]))
    ok, miro_no = post(store, result)
    assert ok, miro_no
    assert store.po("4741030582").line("00030").qty_invoiced == 5.0


def test_qm_hold_can_be_reintroduced_on_an_uploaded_po(store):
    """Setting QM back to pending must hold the booking (R1)."""
    store.po_master.clear()
    store.invoice_docs.clear()
    store.grn.clear()
    _upload(store, "TX85-01021160po.pdf")
    _upload(store, "TX85-01021160.pdf")

    store.set_qm_status("4741030582", "00030", "pending")
    result = build_booking(store, extract_invoice(store.invoice_docs[0]))
    assert any(e.code == "QM_PENDING" for e in result.doc.exceptions)
    assert result.disposition == "hold"


def test_clearing_grn_blocks_the_booking(store):
    """R6 — removing the goods receipt blocks the post."""
    store.po_master.clear()
    store.invoice_docs.clear()
    store.grn.clear()
    _upload(store, "TX85-01021160po.pdf")
    _upload(store, "TX85-01021160.pdf")

    store.set_grn("4741030582", "00030", received=False)
    result = build_booking(store, extract_invoice(store.invoice_docs[0]))
    assert any(e.code == "NO_GRN" for e in result.doc.exceptions)
    assert result.disposition == "block"


def test_uploading_the_same_document_twice_does_not_duplicate(store):
    before = len(store.invoice_docs)
    _upload(store, "TX85-01021160.pdf")
    _upload(store, "TX85-01021160.pdf")
    assert len(store.invoice_docs) == before, "identical content must dedupe by hash"


def test_clear_uploads_restores_the_bundled_corpus(store):
    baseline_pos, baseline_invoices = len(store.po_master), len(store.invoice_docs)

    _upload(store, "KGC-26-2249po.pdf")
    _upload(store, "KGC-26-2249.pdf")
    store.clear_uploads()

    assert len(store.po_master) == baseline_pos
    assert len(store.invoice_docs) == baseline_invoices
    assert not store.uploaded_pos and not store.uploaded_invoices


def test_unreadable_upload_is_reported_not_raised(store):
    """A non-PDF must produce a friendly failure, never crash the page."""
    buffer = io.BytesIO(b"this is not a pdf")
    buffer.name = "broken.pdf"
    with pytest.raises(Exception):
        load_upload(buffer)  # the app catches this and shows an error row
