"""The bundled corpus must survive deployment.

``poandinvoices/`` is git-ignored (too large to deploy), so a deployed instance
falls back to the committed text snapshot. These tests prove the fallback loads
the same documents and produces the same numbers — otherwise the demo would
silently be empty in front of a client.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AP_MODE", "fixtures")

from ap.ingest import load_corpus, split_documents  # noqa: E402
from ap.invoice_extract import extract_invoice  # noqa: E402
from ap.miro import build_booking  # noqa: E402
from ap.po_parser import parse_all  # noqa: E402
from ap.snapshot import SNAPSHOT_FILE, available, read_snapshot  # noqa: E402
from ap.store import Store  # noqa: E402


def test_snapshot_is_committed():
    """The snapshot file is what makes a deployed instance non-empty."""
    assert os.path.exists(SNAPSHOT_FILE), "run: python -m ap.snapshot"
    assert read_snapshot(), "snapshot exists but contains no documents"


def test_snapshot_covers_the_whole_corpus():
    docs = read_snapshot()
    assert len(docs) == 20
    pos, invoices = split_documents(docs)
    assert len(pos) == 9
    assert len(invoices) == 10


@pytest.mark.skipif(not available(), reason="source PDFs not present")
def test_snapshot_hashes_match_the_pdfs():
    """Content hashes must be stable, or the cached extractions stop resolving."""
    from_pdf = {d.filename: d.hash for d in load_corpus()}
    from_snap = {d.filename: d.hash for d in read_snapshot()}
    assert from_snap == from_pdf, "snapshot is stale — rerun: python -m ap.snapshot"


def test_store_bootstraps_from_snapshot_when_pdfs_are_absent(monkeypatch):
    """Simulate the deployed environment: no poandinvoices/ directory."""
    monkeypatch.setattr("ap.snapshot.available", lambda *a, **k: False)
    store = Store.bootstrap()

    assert len(store.po_master) == 9, "deployed instance booted with no POs"
    assert len(store.invoice_docs) == 10, "deployed instance booted with no invoices"
    assert store.po("4745000031") is not None, "the import PO must survive deployment"


def test_marquee_scenario_works_without_the_pdfs(monkeypatch):
    """The multi-vendor import case must still book on a deployed instance."""
    monkeypatch.setattr("ap.snapshot.available", lambda *a, **k: False)
    store = Store.bootstrap()

    doc = next(d for d in store.invoice_docs if "Bull_Customs" in d.filename)
    result = build_booking(store, extract_invoice(doc))

    assert result.doc.po_number == "4745000031"
    assert result.doc.unplanned_delivery_cost == 8811.67
    assert abs(result.doc.balance) < 0.005
    assert "BULL CUSTOMS" in result.doc.invoicing_party.upper()


def test_motion_tax_is_identical_from_snapshot(monkeypatch):
    monkeypatch.setattr("ap.snapshot.available", lambda *a, **k: False)
    store = Store.bootstrap()

    doc = next(d for d in store.invoice_docs if "TX85" in d.filename)
    result = build_booking(store, extract_invoice(doc))

    assert result.doc.calculated_tax == 54.65
    assert result.matches[0].po_line_no == "00030"


def test_po_parsing_is_identical_from_snapshot(monkeypatch):
    """Parsed PO values must not drift between PDF and snapshot sources."""
    monkeypatch.setattr("ap.snapshot.available", lambda *a, **k: False)
    snap_master = parse_all(split_documents(read_snapshot())[0])

    motion = snap_master["4741030582"]
    assert motion.line("00030").total == 614.00
    assert motion.line("00030").net_price == 122.80

    import_po = snap_master["4745000031"]
    assert import_po.total == 229574.00
    assert import_po.is_import
    assert not import_po.has_open_delivery_cost_line
