# AP Three-Way Match — Streamlit PoC

A single-page Streamlit app that proves the loop **upload → extract → validate → display**
for Accounts Payable three-way matching. Upload three PDFs (Purchase Order, Invoice, Goods
Receipt Note), extract line items with OpenAI structured outputs, compare quantities, and see
the verdict as a row in an on-page table. No database — results live in session state.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                # then edit .env and add your OpenAI API key
```

`.env`:

```
OPENAI_API_KEY=sk-...
```

## Generate sample PDFs (optional, recommended)

```bash
python make_samples.py
```

This writes two sets under `samples/`:

- `samples/matched/` — PO, Invoice, GRN with identical quantities.
- `samples/over_invoiced/` — the invoice quantity exceeds the PO.

## Run

```bash
streamlit run app.py
```

## Demo path

1. Upload `samples/matched/` PO, Invoice, GRN → **Run match** → one 🟢 `matched` row.
2. Upload `samples/over_invoiced/` set → **Run match** → one 🔴 `mismatch` row reading
   `over_invoiced: BAT-12V (inv 120 vs po 100)`.
3. Both rows sit in the table; expand the latest to see the per-item comparison. That's the PoC.

## Layout

| File | Job |
| --- | --- |
| `app.py` | Streamlit UI + orchestration |
| `extractor.py` | pdfplumber (PDF→text) + OpenAI (text→structured JSON) |
| `matcher.py` | pure-Python three-way comparison (unit-testable) |
| `make_samples.py` | generates the sample PDFs |

## Match rules (exact quantity; price ignored)

Items are aligned by `item_code`, falling back to a normalized `description`. Per item:

- invoice qty > PO qty → `over_invoiced`
- invoice qty < PO qty → `under_invoiced`
- GRN qty ≠ PO qty → `receipt_variance`
- on invoice but not PO → `unmatched_item`
- all aligned → `matched`

Overall status is `matched` only if every line item matches; otherwise `mismatch` with reasons.

## Not in scope

Database, email ingestion, OCR/scanned docs, SAP posting/payments, multi-PO / split shipments.
Uses model `gpt-4o-mini`.
