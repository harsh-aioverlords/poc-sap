# AP Booking POC — How to Test

Matching of invoices against purchase orders and goods receipts, and staging the
MIRO document for posting.

Nothing is posted to SAP. Everything is simulated on screen.

---

## Scope

**Included**

- Read the invoice and the PO
- Find the right PO for each invoice
- Match invoice lines to PO lines
- Check the goods receipt (GRN)
- Check tax, freight and tolerance
- Stage the MIRO document for one-click posting

**Not included**

- Payments, F110, NACHA, reconciliation (Phase 2)
- Scanned or handwritten invoices
- Currencies other than USD

---

## Screens

| Screen | Purpose |
| --- | --- |
| Exceptions & Audit | Matching results for every invoice (opens here) |
| Upload | Add your own POs and invoices |
| Inbox | All invoices and their status |
| Match Workbench | How one invoice matched its PO |
| MIRO Simulation | The staged posting |

---

## Status colours

|                  | Meaning                                |
| ---------------- | -------------------------------------- |
| 🟢 Ready to post | Everything matches                     |
| 🟡 Needs review  | Posts, but worth a look                |
| 🟠 Held          | Quality pending, or over tolerance     |
| 🔴 Blocked       | No PO reference, or goods not received |

Red and orange rows are expected. Those are the cases the system caught.

---

## Test 1 — A normal invoice

Invoice `TX85-01021160` — Motion Industries, $717.04

**Match Workbench**

- PO printed as `4741030582-BT04`, read as `4741030582` (suffix removed)
- Invoice item `V3.0730-56` matched to PO item `P-V3073056`
- Invoice prints PO line `X00020`, but goods are on line **00030** — matched on
  quantity and price (5 @ $122.80), not the printed reference
- Lines 00010 and 00020 left untouched

**MIRO Simulation**

- Tax recalculated to **$54.65**, same as the invoice
- Cash discount $6.14 shown but not deducted
- Balance **$0.00**

Click **Post MIRO**.

---

## Test 2 — One PO, two vendors

Invoice `2600498-1` — Bull Customs Brokerage, $8,811.67 against PO 4745000031

PO 4745000031 was raised on PP Tube Mills for the slitter assembly. The customs
broker bills against the same PO.

**What to check**

- Vendor mismatch is flagged, not treated as an error
- Invoicing party switched to Bull Customs
- $8,811.67 posted as **unplanned delivery cost** — 3.84% of PO value, inside
  the 20% limit
- Goods line 00010 not selected (quantity already consumed by the GRN)
- Balance **$0.00**

The PO has no freight or customs line, so the charges have nowhere else to sit.
Adding those lines to the import PO template would remove this at source.

---

## Test 3 — Quality gate

Same invoice, `2600498-1`

1. Click **Post MIRO** → refused, quality pending
2. Go to **Upload** → Goods receipt & quality status → PO 4745000031 / line
   00010 → set QM to **released**
3. Return to **MIRO Simulation** and click **Post MIRO** → posts

In production this status is read from SAP. The button is here so you can see
both sides.

---

## Test 4 — Other cases

| Invoice       | Vendor            | What it shows                                                                 |
| ------------- | ----------------- | ----------------------------------------------------------------------------- |
| `769984`      | Freight Solutions | Freight matched exactly. Holdover line $20,400 left open for a later invoice. |
| `9972782578`  | Grainger          | $259.69 freight with no PO line — 24% of a small PO, over tolerance.          |
| `9021454336`  | RS Americas       | PO says laser sensor @ $156.64, invoice says photoelectric @ $88.00. Held.    |
| `KGC-26-2249` | Katoen Natie      | No PO number on the invoice. Blocked.                                         |
| `424543`      | 3L Energy         | PO field shows their order number, not ours. Blocked.                         |

---

## Test 5 — Your own documents

1. **Upload** → drop in a PO and its invoice together, any order
2. **Process uploads**
3. Check **Inbox → Match Workbench → MIRO Simulation**

The system works out which file is the PO and which is the invoice.

Goods receipt and quality status are not in the PDFs, so uploaded POs are
treated as received and released. To test a hold, change GRN or QM at the bottom
of the Upload page.

---

## What is simulated

- Goods receipt and quality status — set in the app, normally from SAP
- GL accounts — illustrative, not your chart of accounts
- The posting — nothing is written to SAP

---

## Open question

Two tolerance limits are in use:

- $1–2 absolute, applied manually today
- 20% of PO value, from the PO

These behave very differently on a $1,000 PO versus a $229,000 PO. The demo
currently uses:

| Variance  | Action          |
| --------- | --------------- |
| Up to $2  | Post            |
| $2 to 20% | Flag for review |
| Over 20%  | Block           |

Please confirm what these should be.

---

## If something looks wrong

| Issue                     | Fix                                  |
| ------------------------- | ------------------------------------ |
| Inbox empty               | Set**Documents to show** to **Both** |
| "Could not read this PDF" | Scanned file — digital PDFs only     |
| "PO not found" on upload  | Upload the PO as well                |
| "Already posted"          | Click**Reset** in the sidebar        |

Anything handled incorrectly — send us the document and what you expected.
