"""Explore: browse sales rows, upload history, column meanings and saved formats."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import client

PAGE_SIZE = 100


def render() -> None:
    st.header("Explore data")
    sales_tab, uploads_tab, columns_tab, profiles_tab = st.tabs(["Sales rows", "Uploads", "Columns", "Saved formats"])
    with sales_tab:
        _sales()
    with uploads_tab:
        _uploads()
    with columns_tab:
        _columns()
    with profiles_tab:
        _profiles()


def _sales() -> None:
    cols = st.columns([3, 2, 2])
    q = cols[0].text_input("Search product, order or customer", key="sales_q")
    kind = cols[1].selectbox("Rows", ["All", "Sales only", "Returns only"], key="sales_kind")
    sort = cols[2].selectbox("Sort by", ["order_date", "revenue", "profit", "quantity"], key="sales_sort",
                             format_func=lambda s: s.replace("_", " ").capitalize())
    returns = {"All": None, "Sales only": False, "Returns only": True}[kind]
    try:
        total = client.sales(q=q, returns=returns, limit=1)["total"]
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if not total:
        st.caption("No rows match.")
        return
    pages = (total - 1) // PAGE_SIZE + 1
    page = st.number_input(f"Page (of {pages:,})", min_value=1, max_value=pages, value=1, key="sales_page")
    data = client.sales(q=q, returns=returns, sort=sort, desc=True, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    st.caption(f"{total:,} rows")
    df = pd.DataFrame(data["rows"], columns=data["columns"])
    if "order_date" in df:
        df["order_date"] = pd.to_datetime(df["order_date"])
    st.dataframe(df, hide_index=True, width="stretch", height=520)


def _uploads() -> None:
    try:
        uploads = client.uploads()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if not uploads:
        st.caption("No uploads yet.")
        return
    table = pd.DataFrame(uploads)[["upload_id", "filename", "profile_name", "rows_total", "rows_loaded",
                                   "rows_rejected", "rows_duplicate", "created_at"]]
    table["created_at"] = pd.to_datetime(table["created_at"])
    table["profile_name"] = table["profile_name"].fillna("—")
    table.columns = ["Upload", "File", "Format", "Rows", "Loaded", "Rejected", "Duplicates", "Uploaded at"]
    st.dataframe(table, hide_index=True, width="stretch")

    ids = [u["upload_id"] for u in uploads]
    names = {u["upload_id"]: f"#{u['upload_id']} – {u['filename']}" for u in uploads}
    chosen = st.selectbox("Details for", ids, format_func=names.get, key="upload_choice")
    detail = client.upload_detail(chosen)
    for w in detail["warnings"]:
        st.info(w)
    if detail["rejected_reasons"]:
        st.markdown("**Rejected:** " + ", ".join(f"{r} ({n})" for r, n in detail["rejected_reasons"].items()))
        page = client.rejected(chosen, limit=500)
        rows = [{"Row": r["row_number"], "Reason": r["reasons"], **r["raw"]} for r in page["rows"]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.expander("Column mapping used"):
        st.json(detail["mapping"])

    with st.expander("Undo this upload"):
        st.write(f"Deletes the {detail['rows_loaded']:,} rows loaded by upload #{chosen}.")
        sure = st.checkbox("I understand", key=f"undo_sure_{chosen}")
        if st.button("Delete upload", disabled=not sure, key=f"undo_{chosen}"):
            try:
                client.delete_upload(chosen)
            except client.ApiError as exc:
                st.error(str(exc))
                return
            st.success(f"Upload #{chosen} deleted.")
            st.rerun()


def _columns() -> None:
    try:
        columns = client.schema()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    st.caption("What each column means. The AI copilot reads these descriptions, so describe your custom columns.")
    table = pd.DataFrame([{
        "Column": c["column_name"], "Type": c["data_type"], "Kind": c["kind"],
        "Description": c["description"] or "", "Examples": ", ".join(map(str, c["sample_values"])),
    } for c in columns])
    edited = st.data_editor(table, hide_index=True, width="stretch", key="schema_editor",
                            disabled=["Column", "Type", "Kind", "Examples"])
    changed = edited[edited["Description"] != table["Description"]]
    if st.button("Save descriptions", disabled=changed.empty):
        try:
            for row in changed.itertuples(index=False):
                client.describe_column(row.Column, row.Description or None)
        except client.ApiError as exc:
            st.error(str(exc))
            return
        st.success(f"Saved {len(changed)} description(s).")
        st.rerun()


def _profiles() -> None:
    try:
        profiles = client.profiles()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    st.caption("Saved column mappings. A file whose columns match a saved format loads without questions.")
    for p in profiles:
        fields = ", ".join(f"{field} ← {src}" for field, src in p["mapping"]["fields"].items())
        cols = st.columns([5, 1])
        cols[0].markdown(f"**{p['name']}**{' · built-in' if p['builtin'] else ''}  \n{fields}")
        if not p["builtin"] and cols[1].button("Delete", key=f"del_profile_{p['name']}"):
            client.delete_profile(p["name"])
            st.rerun()
