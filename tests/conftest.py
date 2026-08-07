"""Shared test helpers.

The sample PDFs are git-ignored, so the suite must pass both locally (PDFs
present) and on a deployed checkout (snapshot only). Tests source documents
through these helpers rather than reading ``poandinvoices/`` directly.
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

import pytest

os.environ.setdefault("AP_MODE", "fixtures")

from ap.ingest import pair_corpus, split_documents  # noqa: E402
from ap.snapshot import available, read_snapshot  # noqa: E402

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "poandinvoices")


@lru_cache(maxsize=1)
def _corpus_cached() -> tuple:
    if available():
        from ap.ingest import load_corpus

        # pair_corpus dedupes POs; parser tests want the raw set.
        return tuple(load_corpus())
    return tuple(read_snapshot())


def corpus_documents():
    """Every sample Document, from the PDFs when present, else the snapshot."""
    return list(_corpus_cached())


def document_named(fragment: str):
    """Find one sample document by a filename fragment."""
    for doc in corpus_documents():
        if fragment in doc.filename:
            return doc
    raise LookupError(f"no sample document matching {fragment!r}")


def upload_bytes(fragment: str, as_name: str | None = None) -> io.BytesIO:
    """A file-like object for a sample document, as a Streamlit upload.

    With the PDFs present this is the real file. Without them, the PDF is
    reconstructed from snapshot text — enough for the ingest path, which only
    ever reads extracted text.
    """
    doc = document_named(fragment)
    if available():
        with open(os.path.join(DOCS_DIR, doc.filename), "rb") as fh:
            buffer = io.BytesIO(fh.read())
    else:
        buffer = io.BytesIO(doc.text.encode("utf-8"))
    buffer.name = as_name or doc.filename
    return buffer


@pytest.fixture()
def master():
    """PO master parsed from whichever corpus source is available."""
    from ap.po_parser import parse_all

    pos, _ = split_documents(corpus_documents())
    return parse_all(pos)
