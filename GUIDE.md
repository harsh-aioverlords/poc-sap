# Testing Guide — AP Booking Agent POC

How to run the POC, what each screen does, and how to verify it works — on the
bundled Jindal documents or on your own.

---

## 1. Start it

```bash
cd poc-sap
source .venv/bin/activate          # or: python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`.

**No API key needed.** All 10 sample invoices are pre-extracted and cached in
`ap/fixtures/`, so the app runs fully offline. You only need `OPENAI_API_KEY` in
`.env` to extract a *new* document you upload.

Run the tests any time:

```bash
pytest -q          # 70 tests, all offline, ~5 seconds
```

> **On a deployed instance**, the sample PDFs aren't shipped (they're
> git-ignored for size) — the app loads them from a committed text snapshot
> instead, so all six scenarios below still work. See §9.

---

## 2. The five screens

| Screen | What it does |
| --- | --- |
| **📤 Upload** | Drop in POs and invoices. Shows what's loaded; edit GRN/QM for uploaded POs. |
| **📥 Inbox** | The AP mailbox. Every invoice triaged: ready / review / held / blocked. |
| **🔗 Match Workbench** | Why it matched: line-by-line resolution, charge routing, exceptions. |
| **🧾 MIRO Simulation** | The SAP posting screen. Balance must read $0.00. Post here. |
| **⚠️ Exceptions & Audit** | Everything grouped by rule; QM toggles; PO consumption ledger. |

**Sidebar controls** — *Documents to show* filters between uploaded and bundled
documents. *Reset ledger* rewinds all postings so you can re-run a demo.

---

## 3. Uploading your own documents

### How it works

Go to **📤 Upload**, drop in one or more PDFs, click **Process uploads**.

You can upload POs and invoices **together, in any order**. The agent decides
what each document is from its *content*, not its filename — anything printing
`Purchase Order No:` with a 10-digit number is a PO; everything else is an
invoice. (This matters: in the sample set `2600498-1.pdf` is a *purchase order*
named after the invoice it answers. Filenames lie.)

Invoices are then linked to their PO by the printed reference, with the internal
`-BT04` / `-BT05` suffix stripped automatically.

### Try it

1. Go to **📤 Upload**
2. Upload `poandinvoices/TX85-01021160po.pdf` and `poandinvoices/TX85-01021160.pdf`
3. Click **Process uploads** — you should see:
   - `TX85-01021160po.pdf` → **PO** · Added PO 4741030582 — Motion Industries, 3 lines
   - `TX85-01021160.pdf` → **Invoice** · references PO 4741030582
4. Sidebar filter switches to **Uploaded only** automatically
5. Go to **📥 Inbox** → your invoice, triaged
6. **🔗 Match Workbench** → see it resolve to PO line 00030
7. **🧾 MIRO Simulation** → balance $0.00 → **Post MIRO**

To go back to the full sample set, set the sidebar filter to **Both**, or click
**Remove all uploads**.

### GRN and quality status on uploads

Goods-receipt and quality data live in SAP, not in the PDFs. So an uploaded PO
is seeded as **received in full, quality released** — it books straight through.

To demo a hold on your own documents, use the **Goods receipt & quality status**
editor at the bottom of the Upload page:

- Set **QM** to `pending` → the booking is **held** (rule R1)
- Untick **GRN** → the booking is **blocked** (rule R6)

---

## 4. The six scenarios to walk through

All on the bundled documents. Set the sidebar filter to **Both** or **Samples only**.

### Scenario 1 — Clean domestic booking
**Invoice `TX85-01021160`** (Motion Industries)

Go to **Match Workbench**. Look for:
- PO printed as `4741030582-BT04`, resolved to `4741030582` — **suffix stripped**
- The invoice bills vendor code `V3.0730-56`; the PO says material `P-V3073056`.
  Different strings, same part — matched by **normalized material code**.
- The vendor prints PO line reference `X00020`, but the goods are really **line
  00030**. The agent matches on quantity + price instead of trusting the printed
  reference. *A matcher that trusted it would book the wrong line at the wrong price.*
- Only line 00030 is touched; lines 00010 and 00020 stay open.

Go to **MIRO Simulation**:
- Tax recalculates to **$54.65** — matching the invoice exactly.
  *Why it matters:* goods alone (614.00 × 8.25%) gives 50.66. Only
  (614.00 + 48.39 freight) × 8.25% = 54.65. **Freight belongs in the tax base.**
- Cash discount $6.14 is shown but **not deducted** (rule R5 — already priced
  into the PO). Deducting it would give 710.90 and break the balance.
- **Balance $0.00** → **Post MIRO** → posts as document 5105600001.

### Scenario 2 — The multi-vendor import case ⭐
**Invoice `2600498-1`** (Bull Customs Brokerage)

This is the one that failed live on the discovery call.

Import PO 4745000031 was raised on **PP Tube Mills (India)** for one line — a
slitter assembly, 1 SET, $229,574 — `Ex Works, freight in scope of buyer`, with
**no freight or customs condition line**. Bull Customs, a *completely different
vendor*, then invoices $8,811.67 against that same PO.

In **Match Workbench**, look for:
- **Vendor mismatch** flagged — invoice from Bull Customs, PO raised on PP Tube Mills
- Every amount routed as a **charge**, not goods — there are no goods on this invoice
- The import-PO root cause called out explicitly

In **MIRO Simulation**:
- **Invoicing party switched** to Bull Customs (the Details-tab change the
  operator makes by hand)
- **Unplanned delivery cost $8,811.67** on the header — **3.84% of PO value,
  inside the 20% tolerance**
- Goods line 00010 is **not selected** — its 1 SET is already consumed by the GRN
- **Balance $0.00**

*Why this is the headline:* on the call the team tried to force these charges onto
the goods line and SAP refused (`quantity already used`, `20% limit exceeded`).
The posting they attempted was impossible. The posting that works — unplanned
delivery cost — was available all along.

### Scenario 3 — The QM quality gate
**Same invoice `2600498-1`**

1. In **MIRO Simulation**, click **Post MIRO** → refused:
   *"Cannot post: the document is held (quality gate or tolerance)."*
2. Go to **⚠️ Exceptions & Audit** → find PO 4745000031 / line 00010 → click **Release QM**
3. Back to **MIRO Simulation** → **Post MIRO** → now posts

The agent detects the pending quality decision and **holds** rather than forcing
a post — exactly the behaviour described on the call.

### Scenario 4 — Planned freight, and a line left open
**Invoice `769984`** (Freight Solutions, $100,488.75)

- Matches work-order line **00020.1** (Fuel Surcharge) **to the cent**
- Sibling line **00020.2** (Holdover Charges, $20,400) is shown **unticked**,
  flagged *keep open*
- Disposition: **ready to post**

*Why:* freight often arrives later on a separate invoice from a third party
(rule R3). Closing that line here would strand the later invoice — the exact
mistake Emily was careful to avoid on the call.

### Scenario 5 — Variance in the grey band
**Invoice `9972782578`** (Grainger)

Invoice $1,098.64 against a PO of $1,077.22 — **+$21.42 (+1.99%)**.

Inside the 20% PO tolerance, but far outside the $1–2 the team applies manually.
This is the band nobody has defined yet, and it's deliberately surfaced rather
than silently posted. See §7.

### Scenario 6 — No usable PO reference
**Invoices `KGC-26-2249`** (Katoen Natie) and **`424543`** (3L Energy)

- Katoen Natie prints **no PO at all** → `NO_PO_REFERENCE`
- 3L Energy prints `42259`, which is *their* order number, not an SAP PO → `PO_NOT_FOUND`

Both **blocked and routed** with a suggested action — never silently failed.

---

## 5. What each rule does

Every exception in the UI carries its rule ID, the evidence, and a suggested action.

| Rule | Behaviour | See it in |
| --- | --- | --- |
| **R1** QM gate | Holds when quality is pending; never forces a post | Scenario 3 |
| **R2** Tax code | Derives V0/B2 from the PO and recalculates; freight in the base | Scenario 1 |
| **R3** Keep open | Freight lines a third party will bill later are never closed | Scenario 4 |
| **R4** Tolerance | ~20% PO breach flagged for procurement, not failed | Scenarios 2, 5 |
| **R5** Cash discount | Displayed, never deducted — already priced into the PO | Scenario 1 |
| **R6** GRN / PO | No receipt or no PO reference → blocked with a routing action | Scenario 6 |
| **R7** Multi-vendor | Invoicing party switched when it differs from the PO vendor | Scenario 2 |

---

## 6. Verifying the numbers yourself

Every figure below was read off the source PDFs and is pinned by a test.

| Check | Expected | Where |
| --- | --- | --- |
| Motion tax base | (614.00 + 48.39) × 8.25% = **54.65** | MIRO Simulation |
| Goods-only tax (wrong) | 614.00 × 8.25% = 50.66 | — |
| Motion total | 614.00 + 48.39 + 54.65 = **717.04** | matches invoice |
| Discount deducted? | would give 710.90 → **not** applied | rule R5 |
| Bull Customs charges | sum = **8,811.67** | Match Workbench |
| As % of PO | 8,811.67 / 229,574 = **3.84%** | MIRO Simulation |
| Grainger variance | 1,098.64 − 1,077.22 = **+1.99%** | Inbox |
| Freight Solutions | 100,488.75 = WO line 00020.1 exactly | Match Workbench |

```bash
pytest -q                              # all 63
pytest tests/test_booking.py -q        # the six scenarios
pytest tests/test_upload.py -q         # upload ingestion
pytest -k motion -v                    # the tax-base and line-00030 proofs
```

---

## 7. Two things to raise with the client

**1. The tolerance bands contradict each other.**
The manual tolerance today is **$1–2 absolute**; the PO tolerance is **20%
relative**. On a $229K import PO those differ by five orders of magnitude. You
can see the tension directly: Grainger's $259.69 freight reads as 24% of its
small PO and trips the gate, while the same dollar amount on the import PO would
be negligible. The POC proposes `auto ≤ $2 · review $2–20% · block > 20%` as a
starting point — the threshold was deferred to the finance authority on the call,
so it's surfaced as an open decision rather than quietly hard-coded.

**2. Import POs carry no delivery-cost lines.**
They're raised `Ex Works, freight in scope of buyer` with no freight or customs
condition lines, which *guarantees* every downstream customs invoice is
structurally unmatched. The agent routes around it via unplanned delivery cost,
but the durable fix is in the procurement PO template — a process change, not an
automation one. This is the highest-value finding in the document set.

---

## 8. What's real vs simulated

**Real** — PO parsing, invoice extraction, PO resolution and suffix stripping,
line matching, tax recalculation, tolerance bands, charge routing, every rule
decision, the consumption ledger.

**Simulated** — GRN/QM status (seeded, editable in the UI; it lives in SAP), GL
account determination (illustrative 5-account mapping), and the MIRO posting
itself (writes to an in-session ledger; there is no SAP connection).

**Not built** — OCR (all sample PDFs are digital text), email ingestion,
payments / F110 / NACHA / reconciliation (Phase 2), multi-currency (all USD).

---

## 9. Deploying it

The sample PDFs in `poandinvoices/` (~830 KB) are **git-ignored**, so a deployed
instance never receives them. To keep every demo scenario working anyway, the
repo commits a **text snapshot** of the corpus instead:

| Committed | Size | Purpose |
| --- | --- | --- |
| `ap/corpus_snapshot.json` | ~96 KB | Extracted text of all 20 sample documents |
| `ap/fixtures/*.json` | ~40 KB | Cached invoice extractions (no API key needed) |
| `data/seed_grn_qm.json` | ~4 KB | Seeded GRN/QM status |

At startup the app uses the real PDFs when present (local development) and falls
back to the snapshot when they're absent (deployed). **Same documents, same
content hashes, same results** — the sidebar shows which source is active.

Deploy to Streamlit Cloud by pointing it at the repo with `app.py` as the entry
point. Nothing else is required: no API key, no PDFs, no database. Users can
still upload their own documents, which is the primary path when deployed.

**After changing the sample set**, regenerate the snapshot:

```bash
python -m ap.snapshot        # rewrites ap/corpus_snapshot.json
pytest tests/test_snapshot.py -q
```

`tests/test_snapshot.py` fails if the snapshot goes stale, so a drifted corpus
is caught before it reaches a client.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "No invoices match the current filter" | Sidebar filter is on *Uploaded only* with nothing uploaded. Switch to **Both**. |
| Upload says "Could not read this PDF" | Not a valid PDF, or a scanned image. The POC handles digital text only — no OCR. |
| An uploaded invoice shows *PO not found* | Its PO isn't loaded. Upload the PO too, or check the printed reference is a 10-digit SAP number. |
| Extraction seems wrong on a new document | Without an API key, uncached documents fall back to a header-only heuristic. Add `OPENAI_API_KEY` to `.env` for full extraction. |
| Everything re-books after a QM change | Intended — the pipeline re-runs on every interaction so toggles take effect immediately. |
| Want to re-run a demo | **Reset ledger** in the sidebar rewinds all postings. |
| Posting says "already posted" | The duplicate guard. Use **Reset ledger**. |
| Deployed app shows no samples | `ap/corpus_snapshot.json` is missing from the commit. Run `python -m ap.snapshot` and commit it. |

---

## 11. Where the code lives

| Path | Job |
| --- | --- |
| `app.py` | The five screens |
| `ap/ingest.py` | PDF text, content-based typing, uploads, suffix stripping |
| `ap/po_parser.py` | Deterministic regex PO parser — POs are master data, no LLM |
| `ap/invoice_extract.py` | LLM extraction for invoices, cache-first |
| `ap/matching.py` | Four-tier line resolution + one-to-one assignment |
| `ap/costing.py` | Planned vs unplanned cost, tax, tolerance bands |
| `ap/rules.py` | R1–R7 as named checks |
| `ap/miro.py` | MIRO assembly, simulate, post, ledger |
| `ap/store.py` | PO master, GRN/QM, uploads, consumption ledger |
| `ap/snapshot.py` | Bundled corpus as text, so a deployed instance isn't empty |
| `tests/` | 70 acceptance tests pinned to the real documents |

**Design note:** POs are parsed by regex, not the LLM. They're master data — a
hallucinated PO price would silently corrupt every downstream variance. Only the
heterogeneous vendor invoices go through the model.
