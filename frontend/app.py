"""Streamlit UI for the AI Business Analyst.

    uvicorn app.main:app            # the API, in one terminal
    streamlit run frontend/app.py   # the UI, in another
"""

from __future__ import annotations

import streamlit as st

import client
from views import assistant, dashboard, documents, explore, insights, upload

PAGES = {"Dashboard": dashboard.render, "Insights": insights.render, "Ask AI": assistant.render,
         "Upload data": upload.render, "Documents": documents.render, "Explore data": explore.render}

st.set_page_config(page_title="AI Business Analyst", layout="wide")

with st.sidebar:
    st.title("AI Business Analyst")
    page = st.radio("Page", list(PAGES), key="page", label_visibility="collapsed")
    st.divider()
    try:
        h = client.health()
        st.caption(f"{h['sales_rows']:,} sales rows from {h['uploads']:,} upload(s)")
    except client.ApiError as exc:
        st.error(str(exc))

PAGES[page]()
