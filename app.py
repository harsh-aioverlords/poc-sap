"""AP Three-Way Match — single-page Streamlit PoC.

Upload three PDFs (PO, Invoice, GRN) -> extract line items with OpenAI ->
compare quantities -> append a result row to an on-page table. State lives in
st.session_state for the session; no database. See plan.md.
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from extractor import extract_document, pdf_to_text
from matcher import three_way_match

load_dotenv()

st.set_page_config(page_title="AP Three-Way Match", layout="wide")
st.title("AP Three-Way Match")
st.caption("Upload a PO, Invoice, and GRN — extract, compare quantities, and see the verdict.")

if not os.getenv("OPENAI_API_KEY"):
    st.error(
        "OPENAI_API_KEY is not set. Copy `.env.example` to `.env`, add your key, "
        "and restart the app (`streamlit run app.py`)."
    )
    st.stop()

if "results" not in st.session_state:
    st.session_state.results = []

# --- Uploaders ------------------------------------------------------------
col_po, col_inv, col_grn = st.columns(3)
with col_po:
    po_file = st.file_uploader("Purchase Order (PDF)", type=["pdf"], key="po")
with col_inv:
    inv_file = st.file_uploader("Invoice (PDF)", type=["pdf"], key="inv")
with col_grn:
    grn_file = st.file_uploader("Goods Receipt Note (PDF)", type=["pdf"], key="grn")

all_uploaded = bool(po_file and inv_file and grn_file)

run = st.button("Run match", type="primary", disabled=not all_uploaded)
if not all_uploaded:
    st.info("Upload all three PDFs to enable the match.")

# --- Run ------------------------------------------------------------------
if run:
    try:
        with st.spinner("Extracting documents and comparing…"):
            po = extract_document(pdf_to_text(po_file), "po")
            invoice = extract_document(pdf_to_text(inv_file), "invoice")
            grn = extract_document(pdf_to_text(grn_file), "grn")
            row = three_way_match(po, invoice, grn)
        st.session_state.results.append(row)
        if row["status"] == "matched":
            st.success("Matched ✅")
        else:
            st.warning(f"Mismatch — {row['reason']}")
    except Exception as exc:  # surface API/parse errors without crashing the page
        st.error(f"Something went wrong during extraction/matching: {exc}")

# --- Results table --------------------------------------------------------
st.subheader("Results")
results = st.session_state.results

if not results:
    st.write("No runs yet.")
else:
    def _status_cell(status: str) -> str:
        return "🟢 matched" if status == "matched" else "🔴 mismatch"

    table = pd.DataFrame(
        [
            {
                "PO": r["po_number"],
                "Invoice": r["invoice_number"],
                "GRN": r["grn_number"],
                "Vendor": r["vendor"],
                "Status": _status_cell(r["status"]),
                "Reason": r["reason"],
            }
            for r in results
        ]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)

    latest = results[-1]
    with st.expander("Per-item breakdown (latest run)", expanded=True):
        detail = pd.DataFrame(latest["line_detail"])
        if detail.empty:
            st.write("No line items compared.")
        else:
            st.dataframe(detail, use_container_width=True, hide_index=True)

    if st.button("Clear table"):
        st.session_state.results = []
        st.rerun()
