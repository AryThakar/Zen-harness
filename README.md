# Zen - a local assistant

Zen is a chat assistant on an open model (Gemma 4 through llama-server - on this computer, or the 26B on a Kaggle
notebook reached through a local bridge). It talks with you and hands focused work to three helpers - one for the
web, one for reading many or long documents and earlier chats, and an analyst-programmer for your tables and
programs - each with its own tools, inside a **workspace**: your project, one folder of real files named by their
paths. What must be right - facts, numbers, files - is checked by code, not left to the model.

## What it does

- **Chat** with a persona and a standing memory - agent.md, one short file of your instructions and facts that
  Zen keeps in view in every chat, and that you and Zen both edit; every earlier conversation stays searchable.
- **Work with your files** - point at one with @ in the message box, or pin it to the chat so the parts that
  match each message come along; find files by name, by what is in them or by what a picture shows.
- **Research the web** - search, read pages (JavaScript sites through a real browser when needed), PDFs, Word and
  PowerPoint files, GitHub, YouTube and wikis; cite only pages it read; say what couldn't be read.
- **Do maths exactly** - arithmetic, equations, derivatives, integrals, limits and matrices with SymPy.
- **Analyse tables** - inspect and clean them, exact numbers by SQL, native charts (downloadable as pictures or
  numbers), whether a difference is real, forecasts, machine-learning models of what drives a column, segments,
  unusual values, what a text column says.
- **Write and run programs** - Python files in the workspace, run in a sandbox; run one yourself in a console and
  type its input while it runs; an HTML page runs in its card, sealed off from the network.
- **Make things** - Word documents and slides from Markdown with charts in them, pictures, diagrams.
- **Ask before acting** - downloads, deleting and changing your own files wait for your click.

How it works: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Who owns what, and every guard and check:
[docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md). How it was rebuilt, phase by phase, with what was measured:
[docs/REBUILD_PLAN.md](docs/REBUILD_PLAN.md).

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
(`-c`) is shared by the two slots, and the app's prompt limit follows each slot's size. The server can also run
elsewhere - the 26B runs on Kaggle, reached through a bridge at the same address (a new notebook session has a new
tunnel address: put it in the bridge's bridge.env and restart the bridge).

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
the API through its client, and every tool's contract and description against its schema. When the embedding
server is set up, one test also runs the real one. Measurements on the real model are scripts in `bench/` - each
phase's acceptance and 20 jobs (10 held out) - each saying what it measured; the results are recorded in
docs/REBUILD_PLAN.md.

## The first product

This repository started as an AI business analyst (FastAPI, DuckDB, Streamlit). It is kept, separate, in
[`legacy/`](legacy/README.md).
