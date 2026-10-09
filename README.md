# Zen - a local assistant

Zen is a chat assistant that runs on your own computer, on a local model (Gemma 4 through llama-server).
It talks with you and hands focused work to four helpers - one for the web, one for your documents and
notes, a data analyst and a coder - each with its own tools, inside a **workspace** that is your project
and its sandbox. What must be right - facts, numbers, files - is checked by code, not left to the model.

## What it does

- **Chat** with a persona, a memory of notes it keeps in view, and every earlier conversation searchable.
- **Research the web** - search, read pages (JavaScript sites through a real browser when needed), PDFs,
  Word and PowerPoint files, GitHub, YouTube and wikis; cite only pages it read; say what was blocked.
- **Answer from your files** - documents, notes, chats, pictures (found by what they show) and tables,
  searched by keywords and meaning (EmbeddingGemma 2).
- **Analyse tables** - inspect and clean them, exact numbers by SQL, charts, whether a difference is real,
  forecasts, what predicts a column, segments, unusual values, what a text column says.
- **Write and run code** - Python in the workspace's sandbox, checked by running it; programs that ask for
  input can be run with typed input; edit and run files yourself in the file panel.
- **Make things** - Word documents and slides from Markdown with charts in them, images, diagrams.
- **Ask before acting** - downloads, importing a table from a link, deleting and replacing your files wait
  for your click.

How it works: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Decisions, measurements and what is left:
[docs/SYSTEM_PLAN.md](docs/SYSTEM_PLAN.md).

## Setup

Python 3.11 or later (tested on 3.13), on Windows:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock -e .
```

`requirements.lock` holds the exact versions the tests passed with.

**The chat model.** A llama-server (llama.cpp) with a Gemma 4 model, listening at `BIZ_LLAMACPP_URL`
(default http://127.0.0.1:8080). The app needs two slots (`-np 2`: slot 0 is the conversation, slot 1
the helpers) and the model's vision projector (`--mmproj`, so it can look at pictures); the context
(`-c`) is shared by the two slots. The server can also run elsewhere - the 26B runs on Kaggle, reached
through a bridge at the same address.

**Search by meaning (optional, recommended).** A llama.cpp build that knows EmbeddingGemma 2 (b11515 or
later) and the model's two files (`embeddinggemma-2-Q8_0.gguf` and its `mmproj`, from
ggml-org/embeddinggemma-2-GGUF). Set the three settings below; the app starts the server itself and stops
it when it exits. Without them, search uses a small CPU model (bge-small, downloaded once).

**A real browser (optional).** Node and Microsoft Edge; install Playwright MCP into a folder of its own
(`npm install @playwright/mcp@0.0.83`) and set `ASSISTANT_PLAYWRIGHT_MCP` to that folder. Without it,
a page drawn by JavaScript is read only partly (its description), and a page that refuses plain readers
is reported as blocked.

## Settings

Environment variables, or lines in a `.env` file in the project folder (`NAME=value`; it is git-ignored):

| Setting | What it is |
|---|---|
| `BIZ_LLAMACPP_URL` | the chat model's llama-server (default `http://127.0.0.1:8080`) |
| `ASSISTANT_EMBED_SERVER` | llama-server's program, for the embedding server the app starts |
| `ASSISTANT_EMBED_GGUF`, `ASSISTANT_EMBED_MMPROJ` | EmbeddingGemma 2's model file and picture projector |
| `ASSISTANT_EMBED_URL` | where the embedding server listens (default `http://127.0.0.1:8070`) |
| `ASSISTANT_PLAYWRIGHT_MCP`, `ASSISTANT_BROWSER` | Playwright MCP's folder; the browser it drives (default `msedge`) |
| `POLLINATIONS_KEY` | for generating images (a key from enter.pollinations.ai) |
| `ASSISTANT_NAME`, `ASSISTANT_USER_NAME` | the assistant's name (default Zen) and yours |
| `ASSISTANT_THINKING` | the model thinks before answering: `on` (default) or `off` |
| `ASSISTANT_MAX_TOKENS` | a reply's limit, thinking included (default 4096) |
| `ASSISTANT_DATA_DIR` | where workspaces, chats and files are kept (default `data/assistant`) |
| `BIZ_MAX_UPLOAD_MB` | the largest upload (default 100) |
| `BIZ_LLM_PROVIDER`, `GROQ_API_KEY` | `groq` runs the app on a cloud model instead, for testing |

## Run

```bash
.venv/Scripts/uvicorn.exe app.main:app
```

Then open http://localhost:8000: make a workspace, upload files or just ask.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

No model is needed: the tools are tested directly, the conversation and the helpers with scripted models,
the API through its client. When the embedding server is set up, one test also runs the real one.
Measurements behind decisions are scripts in `bench/` (each says what it measured and what it found).

## The first product

This repository started as an AI business analyst (FastAPI, DuckDB, Streamlit). It is kept, separate, in
[`legacy/`](legacy/README.md).
