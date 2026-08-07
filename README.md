# AP Booking Agent — POC (Jindal Pipe USA, SAP MIRO)

A working proof of concept for the **Booking (MIRO)** module of the Agentic AI for
Accounts Payable proposal, built on the client's own purchase orders and vendor
invoices. Payments & Reconciliation (Phase 2) is deliberately out of scope.

The POC proves the loop **intake → extract → resolve PO → match → apply rules →
stage MIRO → one-click post**, including the scenario that failed live on the
discovery call: **an import transaction where multiple vendors map to a single
purchase order.**

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

**No API key is needed.** Extractions are cached in `ap/fixtures/`, so the demo
runs deterministically offline. Set `OPENAI_API_KEY` (or `AP_MODE=live`) only to
re-extract or to process a new document handed over on the spot.

```bash
pytest -q          # 70 acceptance tests, all offline
```

**Upload your own documents** on the **📤 Upload** screen — POs and invoices
together, in any order. Document type is decided from content, not filename.
See **[GUIDE.md](GUIDE.md)** for a full walkthrough and testing guide.

**Deploying:** the sample PDFs are git-ignored for size; a committed text
snapshot (`ap/corpus_snapshot.json`) keeps every scenario working on a deployed
instance. Regenerate with `python -m ap.snapshot`.

## The headline scenario

Import PO **4745000031** was raised on PP Tube Mills (India) for one line —
a slitter assembly, 1 SET, $229,574 — `Ex Works, freight in scope of buyer`,
with **no freight or customs condition line**.

Bull Customs Brokerage then invoices **$8,811.67** against that same PO. There is
nothing on the PO for those charges to land on. On the call the team tried to
force them onto the goods line, whose 1 SET was already consumed by the GRN, and
SAP refused it (`quantity already used`, `20% limit exceeded`).

The agent resolves it the way SAP actually requires:

| | |
| --- | --- |
| Invoicing party | switched to Bull Customs (PO vendor stays PP Tube Mills) |
| Charges | **$8,811.67 → unplanned delivery cost** on the header |
| Goods line 00010 | **not selected** — quantity already consumed |
| Tolerance | 3.84% of PO value, inside the 20% limit ✓ |
| Balance | **$0.00** — postable |

It also names the root cause: import POs cut without delivery-cost condition
lines leave every downstream customs invoice structurally unmatched. That is a
procurement-template fix, not an AP-automation fix.

## Demo path

All six scenarios run on the real documents in `poandinvoices/`.

| # | Invoice | Shows |
| --- | --- | --- |
| 1 | `TX85-01021160` Motion | Suffix strip `-BT04`; vendor item code `V3.0730-56` resolved to PO material `P-V3073056`; bills 1 of 3 PO lines; tax recalculated to **54.65**; balance 0.00; one-click post |
| 2 | `2600498-1` Bull Customs | **The multi-vendor import case** (above) |
| 3 | `2600498-1` again | **QM gate** — held on quality, released with the toggle on the Exceptions page, then posts |
| 4 | `769984` Freight Solutions | Planned freight matching work-order line 00020.1 exactly ($100,488.75); sibling Holdover line **left open** for a later third-party invoice |
| 5 | `9972782578` Grainger | Variance +$21.42 (+1.99%) — inside 20%, outside the $1–2 manual tolerance: the undefined grey band |
| 6 | `KGC-26-2249`, `424543` | No usable PO reference — routed to the non-PO queue, never silently failed |

## Business rules (from the discovery calls)

| Rule | Behaviour |
| --- | --- |
| **R1** QM gate | Detects quality-pending and **holds**; never forces a post |
| **R2** Tax code | Derives V0/B2 from the PO and recalculates — **tax base includes planned freight** |
| **R3** Keep open | Freight lines a third party will bill later are never auto-closed |
| **R4** Tolerance | ~20% PO breach flagged for procurement amendment, not failed |
| **R5** Cash discount | Already priced into the PO — displayed, **never deducted** |
| **R6** GRN first | No goods receipt / no PO reference → blocked with a routing action |
| **R7** Multi-vendor | Invoicing party may differ from the PO vendor; switched automatically |

Every exception carries its rule ID, evidence, and a suggested action.

## Verified against the source documents

| Check | Result |
| --- | --- |
| Motion tax base | (614.00 + 48.39 freight) × 8.25% = **54.65** ✓ (goods-only gives 50.66 ✗) |
| Motion balance | 614.00 + 48.39 + 54.65 = **717.04** ✓ |
| Cash discount deducted? | → 710.90 ✗ — confirms R5 |
| Bull Customs charges | sum **8,811.67** ✓ = **3.84%** of PO |
| Grainger variance | 1,098.64 vs 1,077.22 = **+1.99%** |

## Layout

| Path | Job |
| --- | --- |
| `GUIDE.md` | **Testing guide** — how to run it, upload documents, and verify each scenario |
| `app.py` | Streamlit UI — Upload, Inbox, Match Workbench, MIRO Simulation, Exceptions & Audit |
| `ap/ingest.py` | PDF text, content-based doc typing, uploads, PO/invoice pairing, suffix stripping |
| `ap/snapshot.py` | Bundled corpus as committed text, so a deployed instance isn't empty |
| `ap/po_parser.py` | Deterministic regex PO parser (POs are master data — no LLM) |
| `ap/invoice_extract.py` | LLM extraction for invoices, cache-first with live fallback |
| `ap/matching.py` | Four-tier line resolution + one-to-one assignment |
| `ap/costing.py` | Planned vs unplanned delivery cost, tax recalculation, tolerance bands |
| `ap/rules.py` | R1–R7 as named, traceable checks |
| `ap/miro.py` | MIRO assembly, simulate, post, consumption ledger |
| `ap/store.py` | PO master, seeded GRN/QM, in-session ledger |
| `data/seed_grn_qm.json` | GRN/QM status (lives in SAP; seeded here, toggleable in the UI) |
| `tests/` | 53 acceptance tests pinned to the real documents |

## What is real vs simulated

**Real** — all PO and invoice parsing, PO resolution, line matching, tax
recalculation, tolerance bands, charge routing, every rule decision.

**Simulated** — GRN/QM status (seeded JSON with a live toggle; it lives in SAP),
GL account determination (illustrative 5-account mapping), and the MIRO posting
itself (writes to an in-session ledger, no SAP connection).

**Not built** — OCR (all sample PDFs are digital text), email ingestion,
payments/F110/NACHA, multi-currency (all USD).

## Two findings for the client

1. **The tolerance bands are contradictory.** The manual tolerance is $1–2
   absolute; the PO tolerance is 20% relative. On a $229K import PO those differ
   by five orders of magnitude. The POC proposes a band matrix
   (auto ≤ $2 · review $2–20% · block > 20%) as a starting point — the threshold
   was deferred to the finance authority on the call.
2. **Import POs carry no delivery-cost lines.** Raised `Ex Works, freight in
   scope of buyer`, they guarantee every customs invoice is unmatched. The agent
   routes around it via unplanned delivery cost, but the durable fix is in the
   procurement PO template.
