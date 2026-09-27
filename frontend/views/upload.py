"""Upload: pick a file, check/edit the column mapping, load, see the report."""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

import client
import theme
from tables import rejected_frame

NOT_IN_FILE = ""
MAX_UPLOAD_MB = int(os.environ.get("BIZ_MAX_UPLOAD_MB", "100"))  # the API's limit (same setting)
STEPS = (
    ("1", "Drop in a file", "CSV or Excel straight from your POS, shop or spreadsheet - no tidying first."),
    ("2", "Check the columns", "Each column is matched to the standard format; change any match, keep extras "
                               "like salesperson or payment mode."),
    ("3", "Clean and load", "Dates, amounts and names are cleaned; rows that can't be read are listed with "
                            "the reason, never guessed."),
)
KIND_TITLES = {
    "required": "Required",
    "optional": "Optional – each one unlocks features",
    "input": "Totals – only if the file has no unit price or unit cost",
}


def render() -> None:
    theme.page_header("Upload data", "CSV or Excel exports from your shop, POS, Shopify, Tally or spreadsheet. "
                      "Messy files are fine - title rows, ₹ symbols and different date styles are handled.",
                      eyebrow="Data")

    if report := st.session_state.get("last_report"):
        _show_report(report)
        if st.button("Upload another file"):
            st.session_state.pop("last_report")
            st.session_state.pop("upload_preview", None)
            st.session_state["uploader_key"] = st.session_state.get("uploader_key", 0) + 1
            st.rerun()
        return

    file = st.file_uploader("Sales file", type=["csv", "txt", "tsv", "xlsx", "xlsm"], max_upload_size=MAX_UPLOAD_MB,
                            key=f"uploader_{st.session_state.get('uploader_key', 0)}")
    if file is None:
        st.session_state.pop("upload_preview", None)
        _how_it_works()
        return
    if st.session_state.get("upload_file_id") != file.file_id:  # a different file: forget its sheet
        st.session_state["upload_file_id"] = file.file_id
        st.session_state.pop("upload_sheet", None)

    preview = _preview(file)
    if preview is None:
        return
    _file_summary(preview, file)
    mapping = _mapping_editor(preview)
    try:
        check = client.check_mapping(mapping)  # the API owns the rules; the UI only shows them
    except client.ApiError as exc:
        st.error(str(exc))
        return
    _load_controls(preview, mapping, check)


def _how_it_works() -> None:
    cols = st.columns(3)
    for col, (number, title, text) in zip(cols, STEPS):
        with col, theme.card(f"step_{number}", stretch=True):
            st.html(f'<span class="bi-chip">Step {number}</span>')
            st.subheader(title)
            st.caption(text)
    st.caption("Needed in every file: a date, a product and either quantity with unit price or a line total. "
               "Order number, customer, cost, category, discount, location and channel each unlock more.")


def _preview(file, sheet: str | None = None) -> dict | None:
    sheet = st.session_state.get("upload_sheet") if sheet is None else sheet
    cache_key = (file.file_id, sheet)
    cached = st.session_state.get("upload_preview")
    if cached and cached[0] == cache_key:
        return cached[1]
    with st.spinner("Reading the file…"):
        try:
            preview = client.preview(file.name, file.getvalue(), sheet)
        except client.ApiError as exc:
            st.error(str(exc))
            return None
    st.session_state["upload_preview"] = (cache_key, preview)
    return preview


def _file_summary(preview: dict, file) -> None:
    facts = [f"**{preview['rows']:,}** rows", f"header on row {preview['header_row']}"]
    if preview["encoding"]:
        facts.append(f"encoding {preview['encoding']}")
    if preview["delimiter"] and preview["delimiter"] != ",":
        facts.append(f"separator {'tab' if preview['delimiter'] == chr(9) else repr(preview['delimiter'])}")
    st.markdown(" · ".join(facts))
    if preview["profile"]:
        st.success(f"Recognised format: **{preview['profile']}**")
    if len(preview["sheets"]) > 1:
        current = preview["sheet"] or preview["sheets"][0]
        sheet = st.selectbox("Sheet", preview["sheets"], index=preview["sheets"].index(current))
        if sheet != current:
            st.session_state["upload_sheet"] = sheet
            st.rerun()


def _mapping_editor(preview: dict) -> dict:
    columns = preview["columns"]
    original = {c["name"]: c["original"] for c in columns}
    samples = {c["name"]: ", ".join(c["samples"]) for c in columns}
    suggested = preview["mapping"]
    by_field = suggested["fields"]
    file_id = preview["file_id"]

    st.subheader("1. Match your columns")
    options = [NOT_IN_FILE] + [c["name"] for c in columns]

    def label(name: str) -> str:
        if name == NOT_IN_FILE:
            return "— not in file —"
        example = f"  (e.g. {samples[name][:40]})" if samples[name] else ""
        return f"{original[name]}{example}"

    fields: dict[str, str] = {}
    for kind in ("required", "optional", "input"):
        group = [f for f in preview["fields"] if f["kind"] == kind]
        container = st.expander(KIND_TITLES[kind], expanded=kind != "input" or any(f["name"] in by_field for f in group))
        with container:
            grid = st.columns(3)
            for i, f in enumerate(group):
                default = by_field.get(f["name"], NOT_IN_FILE)
                help_text = f["description"] + (f" Unlocks: {', '.join(f['unlocks'])}." if f["unlocks"] else "")
                chosen = grid[i % 3].selectbox(
                    f["name"], options, index=options.index(default), format_func=label,
                    help=help_text, key=f"map_{file_id}_{f['name']}",
                )
                if chosen:
                    fields[f["name"]] = chosen

    custom, ignore, descriptions = _custom_columns(preview, set(fields.values()))

    st.subheader("3. Options")
    cols = st.columns(2)
    date_order = cols[0].radio(
        "Dates like 03/04/2025 mean", ["auto", "dayfirst", "monthfirst"],
        index=["auto", "dayfirst", "monthfirst"].index(suggested["date_order"]), horizontal=True,
        format_func={"auto": "Detect", "dayfirst": "3 April", "monthfirst": "March 4"}.get,
        key=f"date_order_{file_id}",
    )
    discount_type = cols[1].radio(
        "Discount values are", ["auto", "percent", "amount"],
        index=["auto", "percent", "amount"].index(suggested["discount_type"]), horizontal=True,
        format_func={"auto": "Detect", "percent": "Percentages", "amount": "Money"}.get,
        key=f"discount_type_{file_id}",
    )
    return {"fields": fields, "custom": custom, "ignore": ignore, "date_order": date_order,
            "discount_type": discount_type, "descriptions": descriptions}


def _custom_columns(preview: dict, used: set[str]) -> tuple[dict, list[str], dict]:
    st.subheader("2. Other columns")
    suggested = preview["mapping"]
    rest = [c for c in preview["columns"] if c["name"] not in used]
    if not rest:
        st.caption("Every column is matched.")
        return {}, [], {}
    st.caption("Keep extra columns (salesperson, payment mode, …) to filter and chart by them later. "
               "A short description helps the AI understand them.")
    table = pd.DataFrame({
        "File column": [c["original"] for c in rest],
        "Keep": [c["name"] not in suggested["ignore"] for c in rest],
        "Save as": [suggested["custom"].get(c["name"]) or c["default_name"] for c in rest],
        "Type": [c["custom_type"] or "auto" for c in rest],
        "Description": [suggested["descriptions"].get(suggested["custom"].get(c["name"], ""), "") for c in rest],
        "Example": [", ".join(c["samples"]) for c in rest],
    })
    edited = st.data_editor(
        table, hide_index=True, width="stretch",
        disabled=["File column", "Type", "Example"],
        key=f"custom_{preview['file_id']}_{'|'.join(c['name'] for c in rest)}",
    )
    custom, ignore, descriptions = {}, [], {}
    for c, row in zip(rest, edited.itertuples(index=False)):
        if row.Keep:
            name = str(row[2]).strip()  # checked by the API, which explains any problem
            custom[c["name"]] = name
            if row.Description:
                descriptions[name] = row.Description
        else:
            ignore.append(c["name"])
    return custom, ignore, descriptions


def _load_controls(preview: dict, mapping: dict, check: dict) -> None:
    st.subheader("4. Load")
    problems = check["problems"]
    for p in problems:
        st.error(p)
    if check["locked_features"]:
        st.caption("Not in this file: " + "; ".join(
            f"**{col}** ({', '.join(features)})" for col, features in check["locked_features"].items()))
    save_as = st.text_input("Remember this mapping as (optional)",
                            placeholder="e.g. My shop POS – next time it loads automatically",
                            key=f"save_as_{preview['file_id']}")
    if st.button("Clean and load", type="primary", disabled=bool(problems)):
        with st.spinner("Cleaning and loading…"):
            try:
                report = client.confirm(preview["file_id"], mapping, save_as.strip() or None, preview["profile"])
            except client.ApiError as exc:
                st.error(str(exc))
                return
        st.session_state["last_report"] = report
        st.rerun()


def _show_report(report: dict) -> None:
    if report["rows_loaded"]:
        st.success(f"Upload #{report['upload_id']} ({report['filename']}): {report['summary']}")
    else:
        st.warning(f"Upload #{report['upload_id']} ({report['filename']}): {report['summary']}")
    for w in report["warnings"]:
        st.info(w)
    if report["custom_columns"]:
        st.caption("Custom columns: " + ", ".join(f"{n} ({t})" for n, t in report["custom_columns"].items()))
    if report["rows_rejected"]:
        st.subheader("Rows that were not loaded")
        try:
            page = client.rejected(report["upload_id"], limit=200)
        except client.ApiError as exc:
            st.error(str(exc))
            return
        st.dataframe(rejected_frame(page["rows"]), hide_index=True, width="stretch")
        if page["total"] > len(page["rows"]):
            st.caption(f"Showing {len(page['rows'])} of {page['total']}. All are in Explore data → Uploads.")

