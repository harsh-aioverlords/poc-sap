"""PDF -> text -> structured JSON extraction.

pdfplumber pulls raw text out of a digital PDF; OpenAI's structured-outputs API
turns that text into a validated ``ExtractedDoc`` (no prose parsing, no backtick
stripping). See plan.md section 4 for the schema.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pdfplumber
from openai import OpenAI
from pydantic import BaseModel, Field

MODEL = "gpt-4o-mini"

# One client for the module; reads OPENAI_API_KEY from the environment.
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class LineItem(BaseModel):
    item_code: str = Field(description="Product/material code, or '' if none printed")
    description: str = Field(description="Line item description text")
    quantity: float = Field(description="Ordered/invoiced/received quantity as a number")
    unit_price: float = Field(description="Price per unit, or 0 if not shown")


class ExtractedDoc(BaseModel):
    doc_type: Literal["po", "invoice", "grn"]
    document_number: str = Field(description="This document's own number")
    po_number: str = Field(description="Referenced purchase-order number, or '' ")
    vendor: str = Field(description="Vendor/supplier name, or '' ")
    line_items: list[LineItem]


def pdf_to_text(file: Any) -> str:
    """Extract concatenated text from a PDF file-like object (Streamlit upload)."""
    parts: list[str] = []
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
    return "\n".join(parts)


_SYSTEM = (
    "You extract structured data from an accounts-payable document (purchase order, "
    "invoice, or goods receipt note). Return the header fields and every line item. "
    "Use the exact values printed in the document. For any field that is genuinely "
    "absent, return an empty string for text or 0 for a number — never invent values."
)


def extract_document(text: str, expected_doc_type: str) -> dict[str, Any]:
    """Send raw document text to OpenAI and return a validated dict.

    ``expected_doc_type`` is one of "po" | "invoice" | "grn" and is passed to the
    model as a hint. Uses ``chat.completions.parse`` (structured outputs) so the
    result is schema-valid; falls back to ``create`` + json_schema if the installed
    SDK lacks ``.parse``.
    """
    client = _get_client()
    user = (
        f"Expected document type: {expected_doc_type}.\n\n"
        f"Document text:\n---\n{text}\n---"
    )

    parse = getattr(client.beta.chat.completions, "parse", None) or getattr(
        client.chat.completions, "parse", None
    )
    if parse is not None:
        completion = parse(
            model=MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format=ExtractedDoc,
        )
        parsed = completion.choices[0].message.parsed
        return parsed.model_dump()

    # Fallback path for older SDKs without structured-output parsing helpers.
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "extracted_doc",
                "schema": ExtractedDoc.model_json_schema(),
                "strict": True,
            },
        },
    )
    raw = completion.choices[0].message.content or "{}"
    return ExtractedDoc(**json.loads(raw)).model_dump()
