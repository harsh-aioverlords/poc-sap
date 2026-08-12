# Testing Guide — AP Booking POC

Internal guide. For the client-facing version see `CLIENT-GUIDE.md`.

---

## Run it

```bash
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

No API key needed — the sample invoices are pre-extracted and cached in
`ap/fixtures/`. A key is only required to extract a new uploaded document.

```bash
pytest -q          # 70 tests, offline
```

---

## Scope

Matching invoices to purchase orders and goods receipts, then staging the MIRO
document.

Out of scope: payments, F110, NACHA, reconciliation, OCR, email ingestion,
multi-currency.

---

## Screens

| Screen | Purpose |
| --- | --- |
| Upload | Add POs and invoices; set GRN/QM on uploaded POs |
| Inbox | All invoices with status |
| Match Workbench | Line resolution, charge routing, exceptions |
| MIRO Simulation | Staged document, balance, GL preview, post |
| Exceptions & Audit | Grouped by rule; QM toggles; consumption ledger |

Sidebar: filter between uploaded and sample documents; **Reset** rewinds
postings.

---

## Scenarios

### 1. Normal invoice — `TX85-01021160` (Motion, $717.04)

- PO suffix `-BT04` stripped
- Vendor item `V3.0730-56` matched to PO item `P-V3073056`
- Invoice prints PO line `X00020`; correct line is **00030** — matched on
  quantity and price (5 @ $122.80 = $614.00)
- Tax recalculates to **$54.65** (goods alone give $50.66; freight belongs in
  the base)
- Cash discount $6.14 not deducted
- Balance $0.00, posts

### 2. Multi-vendor import — `2600498-1` (Bull Customs, $8,811.67)

- PO 4745000031 raised on PP Tube Mills; invoice from the customs broker
- Vendor mismatch flagged; invoicing party switched
- No delivery-cost line on the PO → $8,811.67 as **unplanned delivery cost**,
  3.84% of PO value
- Goods line 00010 not selected (quantity consumed by GRN)
- Balance $0.00

### 3. Quality gate — same invoice

- Post refused while QM is pending
- Release on **Exceptions & Audit** → posts

### 4. Planned freight — `769984` (Freight Solutions, $100,488.75)

- Matches work-order line 00020.1 exactly
- Holdover line 00020.2 ($20,400) left open

### 5. Freight over tolerance — `9972782578` (Grainger, $1,098.64)

- $259.69 freight with no PO delivery-cost line = 24.1% of PO value
- Held

### 6. No PO reference — `KGC-26-2249`, `424543`

- Katoen Natie prints no PO; 3L Energy prints its own order number
- Both blocked with a routing action

---

## Rules

| Rule | Behaviour |
| --- | --- |
| R1 | Quality pending → hold |
| R2 | Derive tax code from the PO and recalculate; freight in the base |
| R3 | Freight lines billed later stay open |
| R4 | Over ~20% of PO value → flag for PO amendment |
| R5 | Cash discount displayed, never deducted |
| R6 | No goods receipt or no PO reference → block |
| R7 | Invoicing party may differ from the PO vendor |

---

## Uploading

Upload POs and invoices together in any order — type is decided from content,
not filename.

Uploaded POs default to received and quality released. Change GRN or QM at the
bottom of the Upload page to test holds.

---

## Key figures

| Check | Expected |
| --- | --- |
| Motion tax base | (614.00 + 48.39) × 8.25% = 54.65 |
| Motion total | 614.00 + 48.39 + 54.65 = 717.04 |
| Bull Customs charges | 8,811.67 = 3.84% of 229,574 |
| Grainger freight | 259.69 = 24.1% of 1,077.22 |
| Freight Solutions | 100,488.75 matches WO line 00020.1 |

```bash
pytest tests/test_booking.py -q     # scenarios
pytest tests/test_upload.py -q      # uploads
pytest -k motion -v                 # tax base and line 00030
```

---

## Deployment

`poandinvoices/` is git-ignored, so the app falls back to a committed text
snapshot of the sample documents.

| Committed | Size |
| --- | --- |
| `ap/corpus_snapshot.json` | ~96 KB |
| `ap/fixtures/*.json` | ~40 KB |
| `data/seed_grn_qm.json` | ~4 KB |

Deploy to Streamlit Cloud with `app.py` as the entry point. No API key, no
database.

After changing the sample set:

```bash
python -m ap.snapshot
pytest tests/test_snapshot.py -q
```

---

## Simulated

- GRN and QM status — seeded, editable in the app; normally read from SAP
- GL accounts — five illustrative accounts
- Posting — writes to an in-session ledger, not SAP

---

## Troubleshooting

| Issue | Fix |
| --- | --- |
| Inbox empty | Set the document filter to **Both** |
| "Could not read this PDF" | Scanned file — digital PDFs only |
| "PO not found" | Upload the PO, or check the reference is a 10-digit number |
| Extraction poor on a new document | Add `OPENAI_API_KEY` to `.env` |
| "Already posted" | Click **Reset** |
| No samples on the deployed app | `ap/corpus_snapshot.json` missing — run `python -m ap.snapshot` and commit |

---

## Code

| Path | Job |
| --- | --- |
| `app.py` | The five screens |
| `ap/ingest.py` | PDF text, document typing, uploads, PO suffix stripping |
| `ap/po_parser.py` | Regex PO parser — POs are master data, no LLM |
| `ap/invoice_extract.py` | Invoice extraction, cache-first |
| `ap/matching.py` | Line resolution and assignment |
| `ap/costing.py` | Planned vs unplanned cost, tax, tolerance |
| `ap/rules.py` | R1–R7 |
| `ap/miro.py` | MIRO assembly, simulate, post |
| `ap/store.py` | PO master, GRN/QM, uploads, ledger |
| `ap/snapshot.py` | Bundled corpus as text for deployment |
| `tests/` | 70 tests |

POs are parsed by regex rather than the LLM — a wrong PO price would corrupt
every downstream variance. Only invoices go through the model.
