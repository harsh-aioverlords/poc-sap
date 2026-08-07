"""Bundled corpus snapshot — the sample documents as extracted text.

The source PDFs in ``poandinvoices/`` are ~830KB and git-ignored, so a deployed
instance (Streamlit Cloud) never sees them. Without a fallback the app would
boot completely empty and every demo scenario would be gone.

This module stores the *extracted text* of each sample document instead: ~200KB
of JSON that is safe to commit, replays the whole corpus byte-for-byte through
the same parser and matcher, and keeps the content hashes stable so the cached
invoice extractions in ``ap/fixtures/`` still resolve.

Regenerate after changing the sample set:

    python -m ap.snapshot
"""

from __future__ import annotations

import json
import os

from .ingest import DOCS_DIR, Document, build_document, load_corpus

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus_snapshot.json")


def write_snapshot(docs_dir: str = DOCS_DIR, path: str = SNAPSHOT_FILE) -> int:
    """Capture every sample document's extracted text. Returns the count."""
    docs = load_corpus(docs_dir)
    if not docs:
        raise SystemExit(f"No PDFs found in {docs_dir} — nothing to snapshot.")

    payload = {
        "_comment": (
            "Extracted text of the bundled sample documents. The source PDFs are "
            "git-ignored (too large to deploy); this snapshot keeps the demo corpus "
            "available on a deployed instance. Regenerate with: python -m ap.snapshot"
        ),
        "documents": [
            {"filename": doc.filename, "text": doc.text} for doc in sorted(docs, key=lambda d: d.filename)
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=False)
    return len(docs)


def read_snapshot(path: str = SNAPSHOT_FILE) -> list[Document]:
    """Rebuild sample Documents from the snapshot, or [] if there is none."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return [
        build_document(row["text"], row["filename"])
        for row in payload.get("documents", [])
    ]


def available(docs_dir: str = DOCS_DIR) -> bool:
    """True when the real PDFs are present (local dev), False when deployed."""
    return os.path.isdir(docs_dir) and any(
        name.lower().endswith(".pdf") for name in os.listdir(docs_dir)
    )


if __name__ == "__main__":
    count = write_snapshot()
    size = os.path.getsize(SNAPSHOT_FILE) / 1024
    print(f"Wrote {count} documents to {SNAPSHOT_FILE} ({size:.0f} KB)")
