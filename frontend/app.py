"""Streamlit UI for the AI Business Analyst.

    uvicorn app.main:app            # the API, in one terminal
    streamlit run frontend/app.py   # the UI, in another
"""

from __future__ import annotations

import streamlit as st

import client
import theme
from views import assistant, dashboard, documents, explore, insights, upload

PAGES = {  # name -> (render, Material icon)
    "Dashboard": (dashboard.render, "space_dashboard"),
    "Insights": (insights.render, "insights"),
    "Ask AI": (assistant.render, "auto_awesome"),
    "Upload data": (upload.render, "upload_file"),
    "Documents": (documents.render, "description"),
    "Explore data": (explore.render, "table_view"),
}

st.set_page_config(page_title="AI Business Analyst", page_icon=":material/monitoring:", layout="wide")
theme.inject()

with st.sidebar:
    theme.brand()
    page = st.radio("Page", list(PAGES), key="page", label_visibility="collapsed",
                    format_func=lambda name: f":material/{PAGES[name][1]}: {name}")
    try:
        h = client.health()
        chat = client.chat_status()
        theme.status(h["sales_rows"], h["uploads"], chat["model"] if chat["ready"] else "not set up")
    except client.ApiError as exc:
        st.error(str(exc))

PAGES[page][0]()
