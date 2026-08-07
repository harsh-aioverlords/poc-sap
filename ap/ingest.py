"""PDF text extraction, content-based document typing, and PO/invoice pairing.

Filenames in ``poandinvoices/`` are misleading — ``2600498-1.pdf`` is PO
4745000031, not the Bull Customs invoice that references it, and two files are
byte-identical copies of the same PO. So document type is always decided from
the document *content*, never the filename.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

import pdfplumber

DocKind = Literal["po", "work_order", "invoice"]

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "poandinvoices")

# A Jindal PO always prints this header field; vendor invoices never do.
_PO_HEADER = re.compile(r"Purchase Order No\s*:\s*(\d{10})")
_WORK_ORDER = re.compile(r"\bWORK ORDER\b")

# 10-digit SAP PO numbers, optionally with the internal -BT04/-BT05 suffix that
# Emily explained must be stripped before matching.
PO_NUMBER = re.compile(r"\b(4\d{9})\b")
PO_WITH_SUFFIX = re.compile(r"\b(4\d{9})(-[A-Z]{2}\d{2})?\b")


def pdf_to_text(file: Any) -> str:
    """Extract concatenated text from a PDF file-like object or path."""
    parts: list[str] = []
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
    return "\n".join(parts)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def classify(text: str) -> DocKind:
    """Decide document type from content."""
    head = text[:1500]
    if _PO_HEADER.search(text):
        return "work_order" if _WORK_ORDER.search(head) else "po"
    return "invoice"


def strip_po_suffix(raw: str) -> str | None:
    """``4741030582-BT04`` -> ``4741030582``; ``42259`` -> None.

    The trailing dash portion is internal Jindal processing notation and must
    not participate in matching (notes.txt, 00:01:47).
    """
    if not raw:
        return None
    m = PO_NUMBER.search(str(raw))
    return m.group(1) if m else None


@dataclass
class Document:
    path: str
    filename: str
    text: str
    kind: DocKind
    po_number: str | None = None
    hash: str = ""
    referenced_pos: list[str] = field(default_factory=list)

    @property
    def is_po(self) -> bool:
        return self.kind in {"po", "work_order"}


def build_document(text: str, filename: str, path: str = "") -> Document:
    """Wrap already-extracted text as a classified Document."""
    kind = classify(text)
    m = _PO_HEADER.search(text)
    refs: list[str] = []
    for cand in PO_NUMBER.findall(text):
        if cand not in refs:
            refs.append(cand)
    return Document(
        path=path or filename,
        filename=filename,
        text=text,
        kind=kind,
        po_number=m.group(1) if m else None,
        hash=content_hash(text),
        referenced_pos=refs,
    )


def load_document(path: str) -> Document:
    return build_document(pdf_to_text(path), os.path.basename(path), path)


def load_upload(file: Any, filename: str | None = None) -> Document:
    """Classify an uploaded file object (Streamlit UploadedFile or any stream).

    Type is decided from content exactly as for bundled documents, so a PO and
    an invoice can be dropped in together in any order and still land correctly.
    """
    name = filename or getattr(file, "name", "uploaded.pdf")
    try:
        file.seek(0)
    except Exception:
        pass

    try:
        text = pdf_to_text(file)
    except Exception:
        # Accept a plain-text stream too. Real uploads are always PDFs; this
        # keeps the tests runnable on a checkout where the sample PDFs are
        # absent and only their extracted text is available.
        try:
            file.seek(0)
            raw = file.read()
        except Exception:
            raise
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not _PO_HEADER.search(raw) and "INVOICE" not in raw.upper():
            raise
        text = raw

    return build_document(text, os.path.basename(name))


@lru_cache(maxsize=1)
def load_corpus(docs_dir: str = DOCS_DIR) -> tuple[Document, ...]:
    """Load and classify every PDF in the document folder."""
    if not os.path.isdir(docs_dir):
        return ()
    docs = []
    for name in sorted(os.listdir(docs_dir)):
        if name.lower().endswith(".pdf"):
            try:
                docs.append(load_document(os.path.join(docs_dir, name)))
            except Exception:
                continue
        # A corrupt or unreadable PDF must not take the whole demo down.
    return tuple(docs)


def invoice_po_reference(doc: Document) -> str | None:
    """Best-guess PO number an invoice refers to.

    Prefers an explicit labelled reference ("PO NUMBER:", "CUSTOMER REF#",
    "Customer PO No.", "P.O.NO.") over any loose 10-digit token, because
    invoices also print order numbers and bank accounts that can collide.
    """
    labelled = re.findall(
        r"(?:PO\s*NUMBER|P\.?O\.?\s*NO\.?|CUSTOMER\s*REF#?|CUSTOMER\s*P\.?O\.?\s*(?:NUMBER|NO\.?)?"
        r"|PO/RELEASE\s*NUMBER|Customer\s*Ref)\s*[:#]?\s*([0-9A-Za-z\-]+)",
        doc.text,
        flags=re.IGNORECASE,
    )
    for raw in labelled:
        po = strip_po_suffix(raw)
        if po:
            return po
    # Fall back to a bare 10-digit token appearing anywhere.
    return doc.referenced_pos[0] if doc.referenced_pos else None


def raw_po_reference(doc: Document) -> str:
    """The PO reference exactly as printed, suffix included, for display."""
    m = re.search(
        r"(?:PO\s*NUMBER|P\.?O\.?\s*NO\.?|CUSTOMER\s*REF#?|CUSTOMER\s*P\.?O\.?\s*(?:NUMBER|NO\.?)?"
        r"|PO/RELEASE\s*NUMBER)\s*[:#]?\s*(4\d{9}(?:-[A-Z]{2}\d{2})?)",
        doc.text,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m2 = PO_WITH_SUFFIX.search(doc.text)
    return m2.group(0) if m2 else ""


def split_documents(docs) -> tuple[list[Document], list[Document]]:
    """Split documents into (purchase orders, invoices), deduplicating POs.

    Two files in the sample set are identical copies of PO 4741030582; only the
    first is kept so the PO master has one row per PO number.
    """
    pos: dict[str, Document] = {}
    invoices: list[Document] = []
    for doc in docs:
        if doc.is_po and doc.po_number:
            pos.setdefault(doc.po_number, doc)
        elif not doc.is_po:
            invoices.append(doc)
    return list(pos.values()), invoices


def pair_corpus(docs_dir: str = DOCS_DIR) -> tuple[list[Document], list[Document]]:
    """Load and split the bundled PDF corpus."""
    return split_documents(load_corpus(docs_dir))
