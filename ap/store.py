"""In-memory store: PO master, seeded GRN/QM status, and the consumption ledger.

No database — this is a POC. The consumption ledger survives across invoices
within a session so a second invoice sees a PO line whose quantity has already
been consumed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from . import snapshot
from .ingest import DOCS_DIR, Document, invoice_po_reference, pair_corpus, split_documents
from .models import PO, GRNStatus
from .po_parser import parse_all, parse_po

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SEED_FILE = os.path.join(DATA_DIR, "seed_grn_qm.json")


@lru_cache(maxsize=2)
def _corpus(docs_dir: str = DOCS_DIR) -> tuple[tuple[Document, ...], tuple[Document, ...]]:
    """Bundled documents, split and cached — PDFs locally, snapshot when deployed."""
    if snapshot.available(docs_dir):
        pos, invoices = pair_corpus(docs_dir)
    else:
        pos, invoices = split_documents(snapshot.read_snapshot())
    return tuple(pos), tuple(invoices)


@lru_cache(maxsize=2)
def _po_master(docs_dir: str = DOCS_DIR) -> dict[str, PO]:
    """Parsed PO master, cached. Callers must copy before mutating."""
    return parse_all(list(_corpus(docs_dir)[0]))


def load_seed(path: str = SEED_FILE) -> list[GRNStatus]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return [GRNStatus(**row) for row in payload.get("statuses", [])]


@dataclass
class Store:
    """Everything the booking agent needs to reason about a PO."""

    po_master: dict[str, PO] = field(default_factory=dict)
    grn: list[GRNStatus] = field(default_factory=list)
    invoice_docs: list[Document] = field(default_factory=list)
    posted: dict[str, str] = field(default_factory=dict)  # invoice_no -> miro doc no
    # Provenance, so the UI can filter uploaded documents from bundled samples.
    uploaded_pos: set[str] = field(default_factory=set)
    uploaded_invoices: set[str] = field(default_factory=set)
    _counter: int = 5105600000

    # --- construction ---------------------------------------------------

    @classmethod
    def bootstrap(cls, docs_dir: str = DOCS_DIR) -> "Store":
        """Load the bundled corpus.

        Prefers the real PDFs when present (local development). On a deployed
        instance those are git-ignored for size, so the committed text snapshot
        is used instead — same documents, same content hashes, same results.
        """
        _, invoice_docs = _corpus(docs_dir)
        store = cls(
            # Deep-copy the parsed POs: the consumption ledger mutates them, and
            # the parse is cached across Store instances.
            po_master={num: po.model_copy(deep=True) for num, po in _po_master(docs_dir).items()},
            grn=load_seed(),
            invoice_docs=list(invoice_docs),
        )
        store.apply_grn_to_lines()
        return store

    def apply_grn_to_lines(self) -> None:
        """Push seeded receipt quantities onto the PO lines."""
        for status in self.grn:
            po = self.po_master.get(status.po_number)
            if not po:
                continue
            line = po.line(status.line_no)
            if line:
                line.qty_received = status.qty_received

    # --- Uploads --------------------------------------------------------

    def add_document(self, doc: Document) -> dict[str, Any]:
        """Ingest one uploaded document. Returns a summary for the UI.

        Document type is decided from content, so POs and invoices can be
        uploaded together in any order.
        """
        if doc.is_po:
            po = parse_po(doc)
            if not po.po_number:
                return {"ok": False, "filename": doc.filename, "kind": "unknown",
                        "detail": "Looks like a purchase order but no PO number could be read."}
            replaced = po.po_number in self.po_master
            self.po_master[po.po_number] = po
            self.uploaded_pos.add(po.po_number)
            self.seed_grn_for(po, assume_received=True)
            return {
                "ok": True,
                "filename": doc.filename,
                "kind": "PO" if po.doc_kind == "PO" else "Work order",
                "key": po.po_number,
                "detail": (
                    f"{'Replaced' if replaced else 'Added'} PO {po.po_number} — "
                    f"{po.vendor_name or 'unknown vendor'}, {len(po.lines)} line(s), {po.total:,.2f}"
                ),
            }

        # Invoice: keep the raw document; extraction happens in the app layer
        # so the caller controls fixtures-vs-live and can show a spinner.
        self.invoice_docs = [d for d in self.invoice_docs if d.hash != doc.hash]
        self.invoice_docs.append(doc)
        self.uploaded_invoices.add(doc.hash)
        return {
            "ok": True,
            "filename": doc.filename,
            "kind": "Invoice",
            "key": doc.hash,
            "detail": f"Added invoice — references PO {invoice_po_reference(doc) or 'none found'}",
        }

    def seed_grn_for(self, po: PO, *, assume_received: bool = True) -> None:
        """Create GRN/QM rows for an uploaded PO.

        Real GRN and QM status live in SAP. For an uploaded PO we assume goods
        were received in full and quality released, so the document books
        straight through; the UI lets you set either back to demonstrate the
        R1 quality hold or the R6 no-receipt block on your own documents.
        """
        existing = {(g.po_number, g.line_no) for g in self.grn}
        for line in po.postable_lines():
            if (po.po_number, line.line_no) in existing:
                continue
            self.grn.append(
                GRNStatus(
                    po_number=po.po_number,
                    line_no=line.line_no,
                    grn_no=f"UPL{abs(hash((po.po_number, line.line_no))) % 10**7:07d}" if assume_received else None,
                    qty_received=line.qty if assume_received else 0.0,
                    posting_date=po.effective_date or None,
                    qm_status="released" if assume_received else "pending",
                    note="Auto-seeded on upload — GRN/QM actually live in SAP. Edit to demo a hold.",
                )
            )
        self.apply_grn_to_lines()

    def set_grn(self, po_number: str, line_no: str, *, received: bool, qty: float | None = None) -> None:
        """Toggle whether a line has been goods-receipted (drives R6)."""
        row = self.grn_for(po_number, line_no)
        po = self.po_master.get(po_number)
        line = po.line(line_no) if po else None
        if row is None:
            row = GRNStatus(po_number=po_number, line_no=line_no)
            self.grn.append(row)
        if received:
            row.grn_no = row.grn_no or f"UPL{abs(hash((po_number, line_no))) % 10**7:07d}"
            row.qty_received = qty if qty is not None else (line.qty if line else 0.0)
        else:
            row.grn_no = None
            row.qty_received = 0.0
        self.apply_grn_to_lines()

    def is_uploaded_po(self, po_number: str) -> bool:
        return po_number in self.uploaded_pos

    def is_uploaded_invoice(self, doc_hash: str) -> bool:
        return doc_hash in self.uploaded_invoices

    def clear_uploads(self) -> None:
        """Drop everything uploaded and rebuild from the bundled corpus.

        Rebuilding rather than deleting matters: an uploaded PO may *replace* a
        bundled one (same PO number), and removing it outright would leave the
        sample corpus short a document.
        """
        fresh = Store.bootstrap()
        self.po_master = fresh.po_master
        self.grn = fresh.grn
        self.invoice_docs = fresh.invoice_docs
        self.uploaded_pos.clear()
        self.uploaded_invoices.clear()
        self.apply_grn_to_lines()

    # --- lookups --------------------------------------------------------

    def po(self, po_number: str | None) -> PO | None:
        return self.po_master.get(po_number) if po_number else None

    def grn_for(self, po_number: str, line_no: str) -> GRNStatus | None:
        return next(
            (g for g in self.grn if g.po_number == po_number and g.line_no == line_no),
            None,
        )

    def grn_for_po(self, po_number: str) -> list[GRNStatus]:
        return [g for g in self.grn if g.po_number == po_number]

    def qm_pending(self, po_number: str, line_nos: list[str] | None = None) -> list[GRNStatus]:
        """Lines blocking a post on quality (R1)."""
        rows = self.grn_for_po(po_number)
        if line_nos:
            rows = [g for g in rows if g.line_no in line_nos]
        return [g for g in rows if g.qm_blocks_posting]

    # --- mutation -------------------------------------------------------

    def set_qm_status(self, po_number: str, line_no: str, status: str) -> None:
        """Release (or re-hold) a quality gate — the live demo toggle."""
        row = self.grn_for(po_number, line_no)
        if row:
            row.qm_status = status  # type: ignore[assignment]

    def next_miro_number(self) -> str:
        self._counter += 1
        return str(self._counter)

    def record_posting(self, invoice_no: str, miro_no: str) -> None:
        self.posted[invoice_no] = miro_no

    def is_posted(self, invoice_no: str) -> bool:
        return invoice_no in self.posted

    def consume(self, po_number: str, line_no: str, qty: float, amount: float) -> None:
        """Write back to the consumption ledger after a successful post."""
        po = self.po_master.get(po_number)
        if not po:
            return
        line = po.line(line_no)
        if line:
            line.qty_invoiced = round(line.qty_invoiced + qty, 6)
            line.amount_invoiced = round(line.amount_invoiced + amount, 2)

    def reset_ledger(self) -> None:
        """Rewind consumption + postings so a demo can be re-run cleanly."""
        for po in self.po_master.values():
            for line in po.lines:
                line.qty_invoiced = 0.0
                line.amount_invoiced = 0.0
        self.posted.clear()
