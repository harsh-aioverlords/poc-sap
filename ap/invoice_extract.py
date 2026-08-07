"""Vendor invoice -> structured JSON, via OpenAI structured outputs.

Only invoices go through the model. Purchase orders are parsed deterministically
(ap/po_parser.py) because they are master data and a hallucinated PO price would
silently corrupt every downstream variance.

Extraction is **cache-first**: every result is written to ``ap/fixtures/`` keyed
by document content hash and re-read on the next run. That makes a client demo
deterministic and fully offline, while a document handed over on the spot still
extracts live. Set AP_MODE=live to force a fresh call, AP_MODE=fixtures to
forbid network access entirely.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .ingest import Document, content_hash, invoice_po_reference, raw_po_reference, strip_po_suffix
from .models import Invoice, InvoiceCharge, InvoiceLine

MODEL = os.getenv("AP_MODEL", "gpt-4o")

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI()
    return _client


def mode() -> str:
    """``fixtures`` (offline) or ``live``. Defaults to fixtures with no API key."""
    explicit = os.getenv("AP_MODE", "").strip().lower()
    if explicit in {"fixtures", "live"}:
        return explicit
    return "live" if os.getenv("OPENAI_API_KEY") else "fixtures"


# --- Extraction schema ----------------------------------------------------

_SYSTEM = """You extract structured data from an accounts-payable vendor invoice \
for Jindal Pipe USA (SAP ECC). Return header fields, goods line items, and \
non-goods charges separately.

Rules:
- Use the exact values printed. Never invent a value; use "" for absent text and 0 for absent numbers.
- `lines` holds only GOODS/MATERIAL line items actually being billed.
- `charges` holds non-goods amounts: freight, fuel surcharge, customs, demurrage,
  detention, storage, transloading, service fees, wire fees, disbursement fees.
  Classify each with the closest `kind`.
- Do NOT put sales tax in `charges`; put it in `tax_amount`.
- `cash_discount` is any early-payment discount shown; record it but never
  subtract it from the total.
- `po_number_raw` is the customer PO exactly as printed, including any dash
  suffix such as -BT04.
- `grand_total` is the amount actually due."""


def _schema() -> dict[str, Any]:
    """JSON schema for structured outputs (strict mode)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "invoice_no", "invoice_date", "due_date", "vendor_name",
            "po_number_raw", "lines", "charges", "net_total", "tax_amount",
            "cash_discount", "grand_total", "payment_terms",
        ],
        "properties": {
            "invoice_no": {"type": "string"},
            "invoice_date": {"type": "string"},
            "due_date": {"type": "string"},
            "vendor_name": {"type": "string"},
            "po_number_raw": {"type": "string"},
            "payment_terms": {"type": "string"},
            "net_total": {"type": "number"},
            "tax_amount": {"type": "number"},
            "cash_discount": {"type": "number"},
            "grand_total": {"type": "number"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "line_ref", "po_line_ref", "vendor_item_code",
                        "description", "qty", "uom", "unit_price", "amount",
                    ],
                    "properties": {
                        "line_ref": {"type": "string"},
                        "po_line_ref": {"type": "string"},
                        "vendor_item_code": {"type": "string"},
                        "description": {"type": "string"},
                        "qty": {"type": "number"},
                        "uom": {"type": "string"},
                        "unit_price": {"type": "number"},
                        "amount": {"type": "number"},
                    },
                },
            },
            "charges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "description", "amount"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "freight", "customs", "demurrage", "detention",
                                "storage", "service_fee", "wire_fee",
                                "disbursement", "tax", "cash_discount", "other",
                            ],
                        },
                        "description": {"type": "string"},
                        "amount": {"type": "number"},
                    },
                },
            },
        },
    }


# --- Cache ----------------------------------------------------------------


def _fixture_path(doc: Document) -> str:
    return os.path.join(FIXTURES_DIR, f"{doc.hash}.json")


def _read_fixture(doc: Document) -> dict[str, Any] | None:
    path = _fixture_path(doc)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _write_fixture(doc: Document, payload: dict[str, Any]) -> None:
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    payload = dict(payload, _source_file=doc.filename)
    with open(_fixture_path(doc), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


# --- Public API -----------------------------------------------------------


def extract_invoice(doc: Document, *, force_live: bool = False) -> Invoice:
    """Extract one invoice, preferring the cached fixture."""
    payload = None if force_live else _read_fixture(doc)

    if payload is None:
        if mode() == "fixtures" and not force_live:
            # Offline and uncached: fall back to a heuristic so the demo still
            # renders something rather than crashing mid-presentation.
            return _heuristic_invoice(doc)
        payload = _call_model(doc)
        _write_fixture(doc, payload)

    return _to_invoice(payload, doc)


def _call_model(doc: Document) -> dict[str, Any]:
    client = _get_client()
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Invoice text:\n---\n{doc.text}\n---"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "invoice", "schema": _schema(), "strict": True},
        },
    )
    return json.loads(completion.choices[0].message.content or "{}")


# Charge kinds the model reports as "other" but that are unmistakably freight;
# re-classifying them here keeps the planned/unplanned routing correct.
_FREIGHT_HINTS = ("freight", "fuel surcharge", "surcharge", "shipping", "delivery", "cartage")


def _refine_charges(charges: list[InvoiceCharge]) -> list[InvoiceCharge]:
    for charge in charges:
        if charge.kind == "other":
            blob = charge.description.lower()
            if any(h in blob for h in _FREIGHT_HINTS):
                charge.kind = "freight"
            elif "storage" in blob:
                charge.kind = "storage"
    return charges


def _invoice_number(payload: dict[str, Any], doc: Document) -> str:
    """Model value, else a number embedded in the filename, else the filename."""
    if payload.get("invoice_no"):
        return str(payload["invoice_no"])
    m = re.search(r"(\d{6,})", doc.filename)
    return m.group(1) if m else doc.filename.replace(".pdf", "")


def _to_invoice(payload: dict[str, Any], doc: Document) -> Invoice:
    raw_po = payload.get("po_number_raw") or raw_po_reference(doc)
    po_number = strip_po_suffix(raw_po) or invoice_po_reference(doc)

    return Invoice(
        invoice_no=_invoice_number(payload, doc),
        invoice_date=payload.get("invoice_date", ""),
        due_date=payload.get("due_date", ""),
        vendor_name=payload.get("vendor_name", ""),
        po_number_raw=raw_po or "",
        po_number=po_number,
        lines=[InvoiceLine(**l) for l in payload.get("lines", [])],
        charges=_refine_charges([InvoiceCharge(**c) for c in payload.get("charges", [])]),
        net_total=payload.get("net_total", 0.0),
        tax_amount=payload.get("tax_amount", 0.0),
        cash_discount=payload.get("cash_discount", 0.0),
        grand_total=payload.get("grand_total", 0.0),
        payment_terms=payload.get("payment_terms", ""),
        source_file=doc.filename,
    )


_TOTAL_HINTS = (
    r"AMOUNT DUE\s*\$?\s*([\d,]+\.\d{2})",
    r"BALANCE DUE\s*\$?\s*([\d,]+\.\d{2})",
    r"Invoice Total:\s*\$?\s*([\d,]+\.\d{2})",
    r"TOTAL INVOICE USD\s*([\d,]+\.\d{2})",
    r"Amount Due\(USD\)\s*([\d,]+\.\d{2})",
)


def _heuristic_invoice(doc: Document) -> Invoice:
    """Last-resort header-only extraction when offline with no fixture."""
    total = 0.0
    for pattern in _TOTAL_HINTS:
        m = re.search(pattern, doc.text, re.IGNORECASE)
        if m:
            total = float(m.group(1).replace(",", ""))
            break
    m_no = re.search(r"INVOICE\s*(?:NUMBER|NO\.?|#)\s*[:#]?\s*([A-Z0-9\-]+)", doc.text, re.IGNORECASE)
    raw_po = raw_po_reference(doc)
    return Invoice(
        invoice_no=(m_no.group(1) if m_no else doc.filename.replace(".pdf", "")),
        vendor_name=doc.text.split("\n")[0][:60].strip(),
        po_number_raw=raw_po,
        po_number=strip_po_suffix(raw_po) or invoice_po_reference(doc),
        grand_total=total,
        source_file=doc.filename,
    )


def extract_all(docs: list[Document]) -> dict[str, Invoice]:
    """Extract every invoice document, keyed by source filename."""
    return {doc.filename: extract_invoice(doc) for doc in docs}
