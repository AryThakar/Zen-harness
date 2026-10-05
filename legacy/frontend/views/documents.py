"""Documents: the shop's policies, price lists, supplier terms and notes, which
the assistant searches and cites."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

import client
import theme

KIND_LABEL = {"pdf": "PDF", "docx": "Word", "txt": "Text", "md": "Markdown"}


def render() -> None:
    theme.page_header("Documents", "Add your policies, price lists, supplier terms, notes or logs. Ask AI searches "
                      "them - by keywords and by meaning - and says which file and page an answer came from. "
                      "Only the text is kept.", eyebrow="Knowledge")
    try:
        library = client.documents()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    status, docs = library["status"], library["documents"]
    _status(status)
    _upload()
    if not docs:
        st.info("No documents yet. Add your own above, or load the demo shop's sample documents.")
        if st.button("Load sample documents", key="docs_samples"):
            try:
                added = client.add_sample_documents()["added"]
            except client.ApiError as exc:
                st.error(str(exc))
                return
            st.toast(f"Added {len(added)} sample documents")
            st.rerun()
        return
    _search()
    st.subheader(f"In the library ({len(docs)})")
    for doc in docs:
        _document(doc)


def _status(status: dict) -> None:
    if not status["passages"]:
        return
    missing = status["passages"] - status["embedded"]
    if status["semantic_error"] or missing:
        reason = status["semantic_error"] or f"{missing} passages aren't embedded with {status['embed_model']} yet."
        cols = st.columns([5, 1], vertical_alignment="center")
        cols[0].warning(f"Search is using keywords only for some passages. {reason}")
        if cols[1].button("Retry embedding", key="docs_reindex", width="stretch"):
            try:
                done = client.reindex_documents()["embedded"]
                st.toast(f"Embedded {done} passages")
                st.rerun()
            except client.ApiError as exc:
                st.error(str(exc))
    else:
        st.caption(f"{status['documents']} documents · {status['passages']} passages · meaning-based search with "
                   f"{status['embed_model']}")


def _upload() -> None:
    with st.form("docs_upload", clear_on_submit=True, border=True):
        files = st.file_uploader("Add documents", type=["pdf", "docx", "txt", "md"], accept_multiple_files=True,
                                 max_upload_size=20,
                                 help="PDF, Word (.docx), text or Markdown, up to 20 MB each. A file with the "
                                      "same name as one in the library replaces it.")
        submitted = st.form_submit_button("Add to library", type="primary")
    if not (submitted and files):
        return
    for f in files:
        try:
            doc = client.add_document(f.name, f.getvalue())
        except client.ApiError as exc:
            st.error(f"{f.name}: {exc}")
            continue
        note = " (replaced the earlier version)" if doc["replaced"] else ""
        st.success(f"Added {f.name}: {doc['chunks']} passages{note}.")
        if not doc["semantic"]:
            st.warning("The embedding model couldn't be loaded, so this file is searchable by keywords only for now.")


def _search() -> None:
    query = st.text_input("Try a search", placeholder="e.g. can customers return opened phones?", key="docs_query")
    if not query:
        return
    try:
        result = client.search_documents(query)
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if result["note"]:
        st.caption(result["note"])
    for n, hit in enumerate(result["hits"], start=1):
        how = " + ".join(w for w, v in (("keywords", hit["keyword"]), ("meaning", hit["similarity"])) if v is not None)
        with theme.card(f"hit_{n}"):
            st.markdown(f"**{n}. {hit['source']}**")
            st.caption(f"Matched by {how}" + (f" · similarity {hit['similarity']:.2f}" if hit["similarity"] else ""))
            st.markdown(_md(hit["text"]))


def _document(doc: dict) -> None:
    size = f"{doc['pages']} page{'s' if doc['pages'] != 1 else ''} · " if doc["pages"] else ""
    uploaded = datetime.fromisoformat(doc["uploaded_at"])
    label = (f"{doc['filename']}  ·  {KIND_LABEL[doc['kind']]} · {size}{doc['words']:,} words · "
             f"{doc['chunks']} passages · added {uploaded:%d %b %Y}")
    with st.expander(label):
        if doc["title"] and doc["title"] != doc["filename"]:
            st.markdown(f"**{_md(doc['title'])}**")
        try:
            passages = client.document_passages(doc["doc_id"], limit=3)
        except client.ApiError as exc:
            st.error(str(exc))
            return
        for p in passages:
            where = " · ".join(x for x in (f"page {p['page']}" if p["page"] else "", p["section"] or "") if x)
            if where:
                st.caption(where)
            st.markdown(_md(p["text"][:500] + ("…" if len(p["text"]) > 500 else "")))
        if doc["chunks"] > len(passages):
            st.caption(f"… and {doc['chunks'] - len(passages)} more passages.")
        if st.button("Delete", key=f"docs_delete_{doc['doc_id']}"):
            try:
                client.delete_document(doc["doc_id"])
            except client.ApiError as exc:
                st.error(str(exc))
                return
            st.rerun()


def _md(text: str) -> str:
    return text.replace("$", "\\$")
