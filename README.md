# AP Booking POC — SAP MIRO

Matches vendor invoices against purchase orders and goods receipts, then stages
the MIRO document for posting.

Scope is the booking module only. Payments and reconciliation are Phase 2.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

No API key needed — sample invoices are pre-extracted and cached in
`ap/fixtures/`. A key is only required to extract a newly uploaded document.

```bash
pytest -q          # 70 tests, offline
```

## Guides

- **[CLIENT-GUIDE.md](CLIENT-GUIDE.md)** — share with the client alongside the link
- **[GUIDE.md](GUIDE.md)** — internal testing guide

## What it does

| Step | |
| --- | --- |
| Read | Extract header and line items from the PO and the invoice |
| Resolve | Find the PO, stripping the internal `-BT04` suffix |
| Match | Map invoice lines to PO lines by item code, quantity and price |
| Check | Goods receipt, quality gate, tax code, freight, tolerance |
| Stage | Build the MIRO document with a zero balance for one-click posting |

## Rules

| Rule | Behaviour |
| --- | --- |
| R1 | Quality pending → hold |
| R2 | Derive the tax code from the PO and recalculate; freight in the base |
| R3 | Freight lines billed later by a third party stay open |
| R4 | Over ~20% of PO value → flag for PO amendment |
| R5 | Cash discount displayed, never deducted |
| R6 | No goods receipt or no PO reference → block |
| R7 | Invoicing party may differ from the PO vendor |

## Multi-vendor imports

An import PO raised *Ex Works, freight in scope of buyer* carries no freight or
customs line, so a customs broker invoicing against it has nothing to match. The
agent switches the invoicing party and posts the charges as unplanned delivery
cost, leaving the goods line untouched.

Adding freight and customs condition lines to import PO templates would remove
this at source.

## Layout

| Path | Job |
| --- | --- |
| `app.py` | Upload, Inbox, Match Workbench, MIRO Simulation, Exceptions |
| `ap/ingest.py` | PDF text, document typing, uploads, PO suffix stripping |
| `ap/po_parser.py` | Regex PO parser — POs are master data, no LLM |
| `ap/invoice_extract.py` | Invoice extraction, cache-first |
| `ap/matching.py` | Line resolution and assignment |
| `ap/costing.py` | Planned vs unplanned cost, tax, tolerance |
| `ap/rules.py` | R1–R7 |
| `ap/miro.py` | MIRO assembly, simulate, post |
| `ap/store.py` | PO master, GRN/QM, uploads, ledger |
| `ap/snapshot.py` | Bundled corpus as text for deployment |
| `data/seed_grn_qm.json` | GRN/QM status — lives in SAP, seeded here |
| `tests/` | 70 tests |

## Simulated

GRN and QM status (seeded, editable in the app), GL accounts (illustrative), and
the posting itself (in-session ledger, no SAP connection).

Not built: OCR, email ingestion, payments, multi-currency.

## Deployment

`poandinvoices/` is git-ignored; the app falls back to
`ap/corpus_snapshot.json`. Regenerate with `python -m ap.snapshot` after
changing the sample set.
