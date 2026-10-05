# legacy - the first product: AI Business Analyst

Upload a sales export, explore it on a dashboard, run forecasts, segments, churn, basket and anomaly
models, search documents, and chat about it all (a LangGraph agent on Claude or a local llama-server).
It was this project's first product; the project is now the assistant in `app/`. It is kept here,
working and tested, and shares a few modules with the assistant (`app.config`, `app.providers`,
`app.rag`, `app.tools.sql_guard`, `app.tools.table_reader`).

    uvicorn legacy.main:app --port 8001            # API, docs at http://localhost:8001/docs
    set BIZ_API_URL=http://localhost:8001          # (PowerShell: $env:BIZ_API_URL = "...")
    cd legacy && streamlit run frontend/app.py     # its Streamlit UI
    python -m pytest legacy/tests                  # its tests
    python legacy/bench/t1_reading.py              # its analysis benchmark (t1-t8, summarize.py)

The full description, with screenshots, is in the repository's README (the "AI Business Analyst
(legacy)" part).
