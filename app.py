"""AP Booking Agent — POC for Jindal Pipe USA (SAP MIRO).

Four screens: Inbox, Match Workbench, MIRO Simulation, Exceptions & Audit.
Runs entirely offline from cached extractions, so a client demo never depends
on a live API call. See README.md for the demo path.
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from ap.ingest import invoice_po_reference, load_upload  # noqa: E402
from ap.invoice_extract import extract_invoice, mode  # noqa: E402
from ap.miro import build_booking, post, simulate  # noqa: E402
from ap.snapshot import available as pdfs_available  # noqa: E402
from ap.store import Store  # noqa: E402

st.set_page_config(page_title="AP Booking Agent — MIRO", layout="wide", page_icon="📄")

DISPOSITION = {
    "auto_post": ("🟢", "Ready to post", "#0f9d58"),
    "review": ("🟡", "Needs review", "#f4b400"),
    "hold": ("🟠", "Held", "#ef6c00"),
    "block": ("🔴", "Blocked", "#d93025"),
}

SEVERITY_ICON = {"block": "🔴", "hold": "🟠", "warn": "🟡", "info": "🔵"}


# --- State ----------------------------------------------------------------


@st.cache_resource
def _store() -> Store:
    """One mutable store for the session; uploads and QM toggles mutate it."""
    return Store.bootstrap()


@st.cache_data(show_spinner=False)
def _extract_cached(doc_hash: str, _doc):
    """Extract one invoice, memoised on content hash."""
    return extract_invoice(_doc)


def get_invoices(store: Store):
    """Extract every invoice document currently in the store."""
    out = {}
    for doc in store.invoice_docs:
        invoice = _extract_cached(doc.hash, doc)
        invoice.source_hash = doc.hash
        out[doc.hash] = invoice
    return out


def bookings(store, invoices):
    """Re-run the pipeline for every invoice (cheap; keeps QM toggles live)."""
    results = {}
    for invoice in sorted(invoices.values(), key=lambda i: i.invoice_no):
        results[invoice.invoice_no] = build_booking(store, invoice)
    return results


def money(value: float) -> str:
    return f"${value:,.2f}"


# --- Sidebar --------------------------------------------------------------

store = _store()

with st.sidebar:
    st.title("AP Booking Agent")
    st.caption("Phase 1 — Invoice Processing & MIRO")

    page = st.radio(
        "Screen",
        [
            "📤 Upload",
            "📥 Inbox",
            "🔗 Match Workbench",
            "🧾 MIRO Simulation",
            "⚠️ Exceptions & Audit",
        ],
        label_visibility="collapsed",
    )

    st.divider()

    has_uploads = bool(store.uploaded_pos or store.uploaded_invoices)
    corpus = st.radio(
        "Documents to show",
        ["Both", "Uploaded only", "Samples only"],
        index=1 if has_uploads else 0,
        horizontal=False,
        key="corpus_filter",
    )

    extraction_mode = mode()
    st.caption(
        f"Extraction: **{extraction_mode}**"
        + ("  ·  offline, cached" if extraction_mode == "fixtures" else "  ·  live API")
    )
    st.caption(
        "Samples: **source PDFs**" if pdfs_available() else "Samples: **bundled snapshot**"
    )

    if st.button("↺ Reset ledger", width='stretch'):
        store.reset_ledger()
        st.rerun()

    with st.expander("Scope"):
        st.markdown(
            "**In scope** — booking (MIRO): intake, extraction, PO resolution, "
            "three-way match, tax codes, freight/customs, QM gate, tolerance, "
            "staged single-click posting.\n\n"
            "**Out of scope** — payments, F110/NACHA, reconciliation (Phase 2)."
        )

all_invoices = get_invoices(store)

# Apply the corpus filter.
if corpus == "Uploaded only":
    visible = {h: i for h, i in all_invoices.items() if store.is_uploaded_invoice(h)}
elif corpus == "Samples only":
    visible = {h: i for h, i in all_invoices.items() if not store.is_uploaded_invoice(h)}
else:
    visible = all_invoices

with st.sidebar:
    st.caption(
        f"Showing **{len(visible)}** of {len(all_invoices)} invoices · "
        f"**{len(store.po_master)}** POs"
        + (f" (**{len(store.uploaded_pos)}** uploaded)" if store.uploaded_pos else "")
    )

results = bookings(store, visible)


# --- Page 0: Upload -------------------------------------------------------

if page.endswith("Upload"):
    st.header("Upload documents")
    st.caption(
        "Drop in purchase orders and vendor invoices together — the agent decides "
        "which is which from the document content, not the filename."
    )

    uploaded = st.file_uploader(
        "Purchase orders and invoices (PDF)",
        type=["pdf"],
        accept_multiple_files=True,
        key="uploader",
        help="Upload the PO first or the invoice first — order does not matter.",
    )

    c1, c2 = st.columns([1, 3])
    process = c1.button("⚙️ Process uploads", type="primary", width='stretch', disabled=not uploaded)
    if c2.button("🗑 Remove all uploads", width='stretch', disabled=not has_uploads):
        store.clear_uploads()
        _extract_cached.clear()
        st.rerun()

    if process and uploaded:
        summaries = []
        with st.spinner("Reading documents…"):
            for file in uploaded:
                try:
                    doc = load_upload(file)
                    summaries.append(store.add_document(doc))
                except Exception as exc:  # a bad PDF must not kill the page
                    summaries.append(
                        {"ok": False, "filename": getattr(file, "name", "?"),
                         "kind": "unreadable", "detail": f"Could not read this PDF: {exc}"}
                    )
        st.session_state["upload_summaries"] = summaries
        st.rerun()

    for summary in st.session_state.get("upload_summaries", []):
        icon = {"PO": "📄", "Work order": "📄", "Invoice": "🧾"}.get(summary["kind"], "⚠️")
        if summary["ok"]:
            st.success(f"{icon} **{summary['filename']}** → {summary['kind']} · {summary['detail']}")
        else:
            st.error(f"⚠️ **{summary['filename']}** — {summary['detail']}")

    if st.session_state.get("upload_summaries"):
        st.info(
            "Switch to **Inbox** to see the triage verdict, then **Match Workbench** "
            "and **MIRO Simulation** to walk the booking.",
            icon="➡️",
        )

    st.divider()

    # --- What is currently loaded ---
    st.subheader("Loaded purchase orders")
    po_rows = []
    for number, po in sorted(store.po_master.items()):
        po_rows.append(
            {
                "Source": "uploaded" if store.is_uploaded_po(number) else "sample",
                "PO": number,
                "Type": po.doc_kind,
                "Vendor": po.vendor_name[:30],
                "Country": po.vendor_country or "—",
                "Lines": len(po.postable_lines()),
                "Value": money(po.total),
                "Import": "yes" if po.is_import else "",
            }
        )
    st.dataframe(pd.DataFrame(po_rows), width='stretch', hide_index=True)

    st.subheader("Loaded invoices")
    inv_rows = []
    for doc_hash, invoice in all_invoices.items():
        inv_rows.append(
            {
                "Source": "uploaded" if store.is_uploaded_invoice(doc_hash) else "sample",
                "Invoice": invoice.invoice_no,
                "Vendor": invoice.vendor_name[:30],
                "PO printed": invoice.po_number_raw or "—",
                "PO resolved": invoice.po_number or "— none —",
                "Total": money(invoice.grand_total),
                "PO known?": "yes" if store.po(invoice.po_number) else "no",
            }
        )
    st.dataframe(pd.DataFrame(inv_rows), width='stretch', hide_index=True)

    # --- GRN / QM editor for uploaded POs ---
    if store.uploaded_pos:
        st.divider()
        st.subheader("Goods receipt & quality status (uploaded POs)")
        st.warning(
            "GRN and QM status live in SAP, not in the PDFs. Uploaded POs are seeded as "
            "**received in full, quality released** so they book straight through. "
            "Change a line below to demonstrate the R1 quality hold or the R6 no-receipt block.",
            icon="🧪",
        )
        for po_number in sorted(store.uploaded_pos):
            po = store.po(po_number)
            if not po:
                continue
            st.markdown(f"**PO {po_number}** — {po.vendor_name}")
            for line in po.postable_lines():
                status = store.grn_for(po_number, line.line_no)
                cols = st.columns([3, 1, 2, 2])
                cols[0].caption(f"{line.line_no} · {line.description[:40]} · {money(line.total)}")
                received = cols[1].checkbox(
                    "GRN",
                    value=bool(status and status.has_grn),
                    key=f"grn-{po_number}-{line.line_no}",
                )
                qm = cols[2].selectbox(
                    "QM",
                    ["not_required", "pending", "released", "rejected"],
                    index=["not_required", "pending", "released", "rejected"].index(
                        status.qm_status if status else "not_required"
                    ),
                    key=f"qm-{po_number}-{line.line_no}",
                    label_visibility="collapsed",
                )
                if status and (received != status.has_grn):
                    store.set_grn(po_number, line.line_no, received=received)
                    st.rerun()
                if status and qm != status.qm_status:
                    store.set_qm_status(po_number, line.line_no, qm)
                    st.rerun()

    with st.expander("How document type is decided"):
        st.markdown(
            "A **purchase order** is any document printing `Purchase Order No:` with a "
            "10-digit SAP number — the Jindal PO template. Everything else is treated as a "
            "**vendor invoice**.\n\n"
            "This is content-based on purpose: in the bundled sample set the filenames are "
            "misleading (`2600498-1.pdf` is a *purchase order* named after the invoice it "
            "answers), so trusting filenames would mis-file documents.\n\n"
            "Invoices are then linked to a PO by their printed reference, with the internal "
            "`-BT04` / `-BT05` suffix stripped."
        )


# --- Page 1: Inbox --------------------------------------------------------

elif page.endswith("Inbox"):
    st.header("Invoice Inbox")
    st.caption(
        "Simulates the shared `jpuap` AP mailbox. Every invoice is extracted, "
        "matched to its PO, and triaged automatically."
    )

    if not results:
        st.info(
            "No invoices match the current filter. Upload documents on the **Upload** page, "
            "or set the sidebar filter to **Both**.",
            icon="📤",
        )
        st.stop()

    counts = {k: 0 for k in DISPOSITION}
    for r in results.values():
        counts[r.disposition] += 1

    cols = st.columns(4)
    for col, (key, (icon, label, _)) in zip(cols, DISPOSITION.items()):
        col.metric(f"{icon} {label}", counts[key])

    rows = []
    for number, r in results.items():
        icon, label, _ = DISPOSITION[r.disposition]
        rows.append(
            {
                "Invoice": number,
                "Vendor": r.invoice.vendor_name[:34],
                "PO (as printed)": r.invoice.po_number_raw or "—",
                "PO": r.invoice.po_number or "—",
                "Amount": money(r.invoice.grand_total),
                "Status": f"{icon} {label}",
                "Balance": money(r.doc.balance),
                "Top exception": (
                    r.doc.exceptions[0].message[:70] if r.doc.exceptions else "clean match"
                ),
            }
        )

    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    st.info(
        "**Demo path** — `TX85-01021160` clean domestic booking · "
        "`2600498-1` the multi-vendor import case · `769984` planned freight with a "
        "line left open · `9972782578` grey-band variance · `KGC-26-2249` no PO reference.",
        icon="🎯",
    )


# --- Shared invoice picker ------------------------------------------------

else:
    if not results:
        st.header(page.split(" ", 1)[1])
        st.info(
            "No invoices to show. Upload a PO and an invoice on the **Upload** page, "
            "or switch the document filter in the sidebar to **Both**.",
            icon="📤",
        )
        st.stop()

    # Select by invoice number, not by label: the label carries a disposition
    # icon that changes when a QM gate is released, which would otherwise drop
    # the user's selection mid-demo.
    numbers = list(results)
    # Drop a stale selection left behind when the corpus filter changes.
    if st.session_state.get("selected_invoice") not in numbers:
        st.session_state.pop("selected_invoice", None)
    default = numbers.index("TX85-01021160") if "TX85-01021160" in numbers else 0
    chosen = st.sidebar.selectbox(
        "Invoice",
        numbers,
        index=default,
        key="selected_invoice",
        format_func=lambda num: (
            f"{DISPOSITION[results[num].disposition][0]}  {num} — "
            f"{results[num].invoice.vendor_name[:28]} ({money(results[num].invoice.grand_total)})"
        ),
    )
    result = results[chosen]
    invoice, po, doc = result.invoice, result.po, result.doc


# --- Page 2: Match Workbench ---------------------------------------------

if page.endswith("Match Workbench"):
    st.header("Match Workbench")
    icon, label, _ = DISPOSITION[result.disposition]
    st.subheader(f"{icon} {invoice.invoice_no} — {label}")

    left, right = st.columns(2)
    with left:
        st.markdown("#### Invoice (extracted)")
        st.write(
            {
                "Vendor": invoice.vendor_name,
                "Invoice date": invoice.invoice_date,
                "PO as printed": invoice.po_number_raw or "—",
                "PO resolved": invoice.po_number or "— none —",
                "Net": money(invoice.net_total),
                "Tax": money(invoice.tax_amount),
                "Total": money(invoice.grand_total),
            }
        )
        if invoice.po_number_raw and invoice.po_number and invoice.po_number_raw != invoice.po_number:
            st.success(
                f"Suffix stripped: `{invoice.po_number_raw}` → `{invoice.po_number}` "
                "(the dash portion is internal Jindal notation)",
                icon="✂️",
            )

    with right:
        st.markdown("#### Purchase order")
        if po is None:
            st.error("No purchase order resolved — routed to the non-PO queue.")
        else:
            st.write(
                {
                    "PO": f"{po.po_number} ({po.doc_kind})",
                    "Vendor": f"{po.vendor_no} {po.vendor_name}",
                    "Import": "yes" if po.is_import else "no",
                    "Price basis": po.price_basis,
                    "Payment terms": po.payment_terms,
                    "PO value": money(po.total),
                }
            )
            if po.is_import:
                st.warning(
                    f"Import PO — vendor in {po.vendor_country}. Freight and customs "
                    "arrive separately from other vendors.",
                    icon="🌍",
                )

    if po is not None:
        st.markdown("#### Line resolution")
        if result.matches:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Invoice line": m.invoice_line_ref,
                            "→ PO line": m.po_line_no or "— unmatched —",
                            "Tier": m.tier,
                            "Confidence": f"{m.confidence:.0%}",
                            "Inv qty": f"{m.invoice_qty:g}",
                            "PO qty": f"{m.po_qty:g}" if m.po_qty is not None else "—",
                            "Inv amount": money(m.invoice_amount),
                            "PO amount": money(m.po_amount) if m.po_amount else "—",
                            "Why": m.reason,
                        }
                        for m in result.matches
                    ]
                ),
                width='stretch',
                hide_index=True,
            )
        else:
            st.caption("No goods lines on this invoice — charges only.")

        if result.plan and result.plan.routings:
            st.markdown("#### Charge routing")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Charge": r.charge.description[:44],
                            "Kind": r.charge.kind,
                            "Amount": money(r.charge.amount),
                            "Treatment": r.treatment,
                            "PO line": r.po_line_no or "—",
                            "Why": r.reason,
                        }
                        for r in result.plan.routings
                    ]
                ),
                width='stretch',
                hide_index=True,
            )
            planned, unplanned = result.plan.planned_total, result.plan.unplanned_total
            c1, c2, c3 = st.columns(3)
            c1.metric("Planned delivery cost", money(planned))
            c2.metric("Unplanned delivery cost", money(unplanned))
            c3.metric("Display-only (cash discount)", money(result.plan.display_only_total))

    if doc.exceptions:
        st.markdown("#### Exceptions")
        for exc in doc.exceptions:
            with st.expander(
                f"{SEVERITY_ICON[exc.severity]}  [{exc.rule_id}] {exc.code} — {exc.message[:80]}",
                expanded=exc.severity in {"block", "hold"},
            ):
                st.write(exc.message)
                st.caption(f"**Suggested action** — {exc.suggested_action}")
                if exc.evidence:
                    st.json(exc.evidence, expanded=False)


# --- Page 3: MIRO Simulation ---------------------------------------------

elif page.endswith("MIRO Simulation"):
    st.header("MIRO — staged document")
    st.caption("The agent stages the whole posting; a human commits it in one click.")

    icon, label, _ = DISPOSITION[result.disposition]
    st.subheader(f"{icon} {invoice.invoice_no} — {label}")

    h1, h2, h3 = st.columns(3)
    with h1:
        st.markdown("**Header**")
        st.write(
            {
                "Reference": doc.reference,
                "Invoice date": doc.invoice_date,
                "Posting date": doc.posting_date or "—",
                "PO": doc.po_number or "—",
            }
        )
    with h2:
        st.markdown("**Invoicing party**")
        st.write({"Invoice from": doc.invoicing_party, "PO raised on": doc.po_vendor or "—"})
        if doc.po_vendor and doc.invoicing_party and doc.po_vendor.upper()[:6] not in doc.invoicing_party.upper():
            st.warning("Invoicing party switched on the Details tab (R7)", icon="🔀")
    with h3:
        st.markdown("**Amounts**")
        st.write(
            {
                "Invoice total": money(doc.header_amount),
                "Tax on invoice": money(doc.header_tax),
                "Tax recalculated": money(doc.calculated_tax),
                "Tax base": money(doc.tax_base),
            }
        )

    if doc.unplanned_delivery_cost:
        ratio = (doc.unplanned_delivery_cost / po.total * 100) if po and po.total else 0
        st.info(
            f"**Unplanned delivery cost: {money(doc.unplanned_delivery_cost)}** — "
            f"{ratio:.2f}% of the {money(po.total) if po else '—'} PO value, within the 20% tolerance. "
            "Booked on the header because the PO carries no freight/customs condition line.",
            icon="📦",
        )

    st.markdown("#### Line items")
    if doc.lines:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "✓": "☑" if l.selected else "☐",
                        "PO line": l.po_line_no or "— header —",
                        "Description": l.description[:46],
                        "Qty": f"{l.qty:g}" if l.qty else "—",
                        "Amount": money(l.amount),
                        "Tax": l.tax_code,
                        "Keep open": "yes" if l.keep_open else "",
                        "Note": l.note,
                    }
                    for l in doc.lines
                ]
            ),
            width='stretch',
            hide_index=True,
        )
    else:
        st.caption("No lines staged.")

    balance = doc.balance
    b1, b2 = st.columns([1, 3])
    with b1:
        if abs(balance) < 0.005:
            st.success(f"### Balance {money(0)}")
        else:
            st.error(f"### Balance {money(balance)}")
    with b2:
        st.caption(
            "SAP will not post a MIRO whose balance is non-zero. "
            "The agent recalculates tax and routes freight so the balance ties before a human sees it."
        )

    st.markdown("#### GL simulation")
    st.caption("Illustrative account mapping for the POC — not client GL master data.")
    if doc.gl_preview:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Account": e.account,
                        "Name": e.name,
                        "Debit": money(e.debit) if e.debit else "",
                        "Credit": money(e.credit) if e.credit else "",
                    }
                    for e in doc.gl_preview
                ]
            ),
            width='stretch',
            hide_index=True,
        )

    a1, a2, a3 = st.columns(3)
    if a1.button("▶ Simulate", width='stretch'):
        simulate(doc)
        st.toast("Document simulated.")
    if a2.button("✅ Post MIRO", type="primary", width='stretch'):
        ok, message = post(store, result)
        if ok:
            st.success(f"Posted — MIRO document {message}")
            st.balloons()
        else:
            st.error(message)
    if a3.button("↪ Route to procurement", width='stretch'):
        st.info("Routed to procurement for a PO amendment.")

    if store.is_posted(doc.invoice_no):
        st.success(f"Already posted as MIRO {store.posted[doc.invoice_no]}", icon="📌")


# --- Page 4: Exceptions & Audit ------------------------------------------

elif page.endswith("Exceptions & Audit"):
    st.header("Exceptions & Audit")

    by_rule: dict[str, list] = {}
    for number, r in results.items():
        for exc in r.doc.exceptions:
            by_rule.setdefault(exc.rule_id or "—", []).append((number, exc))

    rule_names = {
        "R1": "QM gate — quality must clear before MIRO",
        "R2": "Tax code correction and recalculation",
        "R3": "Freight lines kept open for later third-party invoices",
        "R4": "PO tolerance, unplanned cost and quantity limits",
        "R5": "Cash discount already priced into the PO",
        "R6": "Goods receipt and PO reference required",
        "R7": "Multi-vendor — invoicing party differs from the PO vendor",
    }

    for rule in sorted(by_rule):
        st.subheader(f"{rule} — {rule_names.get(rule, 'other')}")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Invoice": number,
                        "Severity": f"{SEVERITY_ICON[e.severity]} {e.severity}",
                        "Code": e.code,
                        "Message": e.message,
                        "Suggested action": e.suggested_action,
                    }
                    for number, e in by_rule[rule]
                ]
            ),
            width='stretch',
            hide_index=True,
        )

    st.divider()
    st.subheader("Quality (QM) gate control")
    st.caption(
        "GRN and QM status live in SAP, so they are seeded for this POC. "
        "Release a gate here and re-run the booking to see the hold clear."
    )

    for status in store.grn:
        cols = st.columns([2, 2, 2, 2, 2])
        cols[0].write(f"**{status.po_number}** / {status.line_no}")
        cols[1].write(status.grn_no or "no GRN")
        cols[2].write(f"qty {status.qty_received:g}")
        cols[3].write(f"QM: **{status.qm_status}**")
        if status.qm_status == "pending":
            if cols[4].button("Release QM", key=f"qm-{status.po_number}-{status.line_no}"):
                store.set_qm_status(status.po_number, status.line_no, "released")
                st.rerun()
        elif status.qm_status == "released":
            if cols[4].button("Re-hold", key=f"hold-{status.po_number}-{status.line_no}"):
                store.set_qm_status(status.po_number, status.line_no, "pending")
                st.rerun()

    st.divider()
    st.subheader("PO consumption ledger")
    ledger = [
        {
            "PO": num,
            "Line": l.line_no,
            "Description": l.description[:38],
            "PO qty": f"{l.qty:g}",
            "Invoiced": f"{l.qty_invoiced:g}",
            "Open qty": f"{l.qty_open:g}",
            "Open value": money(l.amount_open),
            "Keep open": "yes" if l.keep_open else "",
        }
        for num, p in sorted(store.po_master.items())
        for l in p.postable_lines()
    ]
    st.dataframe(pd.DataFrame(ledger), width='stretch', hide_index=True)

    if store.posted:
        st.subheader("Posted this session")
        st.dataframe(
            pd.DataFrame(
                [{"Invoice": k, "MIRO document": v} for k, v in store.posted.items()]
            ),
            width='stretch',
            hide_index=True,
        )
