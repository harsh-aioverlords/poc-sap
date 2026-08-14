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

from ap.ingest import load_upload  # noqa: E402
from ap.invoice_extract import extract_invoice  # noqa: E402
from ap.miro import build_booking, post, simulate  # noqa: E402
from ap.store import Store  # noqa: E402

st.set_page_config(page_title="AP Booking Agent — MIRO", layout="wide", page_icon="📄")

DISPOSITION = {
    "auto_post": ("🟢", "Ready to post", "#0f9d58"),
    "review": ("🟡", "Needs review", "#f4b400"),
    "hold": ("🟠", "Held", "#ef6c00"),
    "block": ("🔴", "Blocked", "#d93025"),
}

SEVERITY_ICON = {"block": "🔴", "hold": "🟠", "warn": "🟡", "info": "🔵"}

# Goods-receipt and quality status are process gates, not matching outcomes.
NON_MATCHING_CODES = {"QM_PENDING", "QM_REJECTED", "NO_GRN"}

MATCH_LABEL = {
    "qty_price": "quantity & price",
    "material_code": "item code",
    "po_line_ref": "PO line reference",
    "description": "description",
    "unmatched": "no match",
}


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
            "⚠️ Exceptions & Audit",
            "📤 Upload",
            "📥 Inbox",
            "🔗 Match Workbench",
            "🧾 MIRO Simulation",
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

    if st.button("↺ Reset", width='stretch'):
        store.reset_ledger()
        st.rerun()

all_invoices = get_invoices(store)

# Apply the corpus filter.
if corpus == "Uploaded only":
    visible = {h: i for h, i in all_invoices.items() if store.is_uploaded_invoice(h)}
elif corpus == "Samples only":
    visible = {h: i for h, i in all_invoices.items() if not store.is_uploaded_invoice(h)}
else:
    visible = all_invoices

with st.sidebar:
    st.caption(f"{len(visible)} invoices · {len(store.po_master)} POs")

results = bookings(store, visible)


# --- Page 0: Upload -------------------------------------------------------

if page.endswith("Upload"):
    st.header("Upload documents")
    st.caption("Purchase orders and invoices, in any order.")

    uploaded = st.file_uploader(
        "Purchase orders and invoices (PDF)",
        type=["pdf"],
        accept_multiple_files=True,
        key="uploader",
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
        st.info("Go to **Inbox** to see the result.", icon="➡️")

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

    # --- GRN / QM editor ---
    editable = sorted({g.po_number for g in store.grn} & set(store.po_master))
    if editable:
        st.divider()
        st.subheader("Goods receipt & quality status")
        st.caption(
            "Not present in the PDFs — set here. Uploaded POs default to received "
            "and quality released."
        )
        for po_number in editable:
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


# --- Page 1: Inbox --------------------------------------------------------

elif page.endswith("Inbox"):
    st.header("Invoice Inbox")

    if not results:
        st.info("No invoices to show. Upload documents, or set the filter to **Both**.")
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


# --- Shared invoice picker (single-invoice pages only) --------------------

elif page.endswith("Match Workbench") or page.endswith("MIRO Simulation"):
    if not results:
        st.header(page.split(" ", 1)[1])
        st.info("No invoices to show. Upload documents, or set the filter to **Both**.")
        st.stop()

    # Select by invoice number, not label: the label carries a status icon that
    # changes when a status changes, which would drop the selection.
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
            st.success(f"PO number read as {invoice.po_number}", icon="✂️")

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
                st.warning(f"Import PO — vendor in {po.vendor_country}", icon="🌍")

    if po is not None:
        st.markdown("#### Line resolution")
        if result.matches:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Invoice line": m.invoice_line_ref,
                            "→ PO line": m.po_line_no or "— unmatched —",
                            "Matched on": MATCH_LABEL.get(m.tier, m.tier),
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
            st.warning("Invoicing party switched", icon="🔀")
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
            f"**Unplanned delivery cost {money(doc.unplanned_delivery_cost)}** — "
            f"{ratio:.2f}% of PO value. No delivery-cost line on the PO, so it posts "
            "on the header.",
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
        st.caption("The balance must be zero before a MIRO can post.")

    st.markdown("#### GL simulation")
    st.caption("Illustrative accounts.")
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

    st.caption("How each invoice matched its purchase order.")

    if not results:
        st.info("No invoices to show. Upload documents, or set the filter to **Both**.")
        st.stop()

    # --- Match summary, one row per invoice ---
    summary = []
    for number, r in results.items():
        icon, label, _ = DISPOSITION[r.disposition]
        matched = [m for m in r.matches if m.po_line_no]
        unmatched = [m for m in r.matches if not m.po_line_no]
        if r.po is None:
            outcome = "no PO"
        elif not r.matches:
            outcome = "charges only"
        elif unmatched and not matched:
            outcome = "no lines matched"
        elif unmatched:
            outcome = f"{len(matched)} of {len(r.matches)} lines matched"
        else:
            outcome = f"all {len(matched)} line(s) matched"

        summary.append(
            {
                "Invoice": number,
                "Vendor": r.invoice.vendor_name[:26],
                "PO": r.invoice.po_number or "—",
                "Invoice total": money(r.invoice.grand_total),
                "PO lines matched": outcome,
                "Balance": money(r.doc.balance) if r.po else "—",
                "Status": f"{icon} {label}",
            }
        )
    st.dataframe(pd.DataFrame(summary), width='stretch', hide_index=True)

    # --- Line-level detail across every invoice ---
    st.subheader("Line matching")
    lines = []
    for number, r in results.items():
        for m in r.matches:
            lines.append(
                {
                    "Invoice": number,
                    "Invoice line": m.invoice_line_ref,
                    "Qty": f"{m.invoice_qty:g}",
                    "Amount": money(m.invoice_amount),
                    "→ PO line": m.po_line_no or "— none —",
                    "PO qty": f"{m.po_qty:g}" if m.po_qty is not None else "—",
                    "PO amount": money(m.po_amount) if m.po_amount else "—",
                    "Matched on": MATCH_LABEL.get(m.tier, m.tier),
                    "Confidence": f"{m.confidence:.0%}",
                }
            )
    if lines:
        st.dataframe(pd.DataFrame(lines), width='stretch', hide_index=True)
    else:
        st.caption("No goods lines to match.")

    # --- Charges: which found a PO line, which did not ---
    charges = []
    for number, r in results.items():
        for routing in (r.plan.routings if r.plan else []):
            charges.append(
                {
                    "Invoice": number,
                    "Charge": routing.charge.description[:38],
                    "Amount": money(routing.charge.amount),
                    "→ PO line": routing.po_line_no or "— none —",
                    "Treatment": routing.treatment,
                }
            )
    if charges:
        st.subheader("Freight & charges")
        st.dataframe(pd.DataFrame(charges), width='stretch', hide_index=True)

    # --- Everything the match could not settle ---
    # Quality/receipt status is not a matching outcome, so it is excluded here.
    st.subheader("Exceptions")
    exceptions = [
        {
            "Invoice": number,
            "": SEVERITY_ICON[e.severity],
            "Severity": e.severity,
            "Finding": e.message,
            "Note": e.suggested_action,
        }
        for number, r in results.items()
        for e in r.doc.exceptions
        if e.code not in NON_MATCHING_CODES
    ]
    if exceptions:
        st.dataframe(pd.DataFrame(exceptions), width='stretch', hide_index=True)
    else:
        st.caption("None.")
