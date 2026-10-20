# Zen harness

**A personal assistant that gets real work out of an open model you run yourself.** Zen talks with you and hands
focused work to three helpers - one for the web, one for your documents and earlier chats, and an analyst-programmer
for your tables and programs - inside a **project**: one folder of real files. What must be right - facts, numbers,
files, a game that actually starts - is checked by code before you see it, not left to the model.

It runs on **Qwen 3.6 35B-A3B** through llama.cpp: on your own GPU, or free on a Kaggle notebook's two T4s reached
through a small bridge (the notebook is in [`kaggle/`](kaggle/)).

![Zen answering a question about a year of cafe sales, with a chart and a forecast](docs/images/data.png)

**Demo video:** [docs/demo.mp4](docs/demo.mp4) - one session from start to finish, recorded on the real model.

## What it does

- **Chat** with a persona and a standing memory (`agent.md`: one short file of your instructions and facts, kept in
  view in every chat); every earlier conversation stays searchable.
- **Analyse tables** - inspect and clean them, exact numbers by SQL, live charts, whether a difference is real,
  forecasts, models of what drives a column, segments, unusual values, what a text column says.
- **Research the web** - search and read pages, PDFs, GitHub, YouTube and wikis; cite only pages it read; bring a
  page's table in as data rather than retyping it.
- **Work with your files** - point at one with `@`, or pin it to a chat; read long PDFs with sources you can click
  (`[Source: spec.pdf, p. 312]` opens the PDF at that page).
- **Write and run programs** - Python in a sandbox, or run it yourself in a console; a page or game it writes is
  tried in a real browser before it's called ready, and opens from its card.
- **Make things** - Word documents and slides from Markdown with charts in them, pictures, diagrams.
- **Ask before acting** - downloads, deleting and changing your own files wait for your click.

| | |
|---|---|
| ![Web research with sources](docs/images/web.png) | ![A game made in the chat, as a card](docs/images/game.png) |
| ![The game open in the file panel](docs/images/game-open.png) | ![A reply's work opened: the steps between its words](docs/images/steps.png) |
| ![A picture](docs/images/picture.png) | ![A diagram](docs/images/diagram.png) |

On a phone it has its own layout:

<p>
<img src="docs/images/phone-projects.png" width="260" alt="Projects on a phone">
<img src="docs/images/phone-chat.png" width="260" alt="A chat on a phone">
</p>

## How it works

```
browser (web/)  --HTTP + server-sent events-->  FastAPI (app/)
                                                     |
                                     main's turn  -- model slot 0
                                       7 tools of its own + 3 helpers
                   +----------------------+--------+---------------------+
              research_web          ask_knowledge          analyze_or_code      -- model slot 1
              web helper            knowledge helper       analyst-programmer
                   |                      |                      |
              app/tools/ - plain functions with direct tests, no model inside
                   |
     project folder (files by path, versions, reports) + search index + chat store
```

- **main** answers and routes; each **helper** has only its own tools, a step limit and a contract, and hands back
  one checked result. A web answer must cite pages it read; every number in an analysis must come from a tool's
  result; a page must open, respond to its controls and draw something.
- **Guards before a call** stop what is unsafe or likely wrong and say why (with `confirm: true` as the way out
  where it is a judgment); **checks after a reply** send it back once, quietly, when it claims what no tool did.
- **A reply is one flow** in the order things happened - words, steps, cards - with a head on top that says what
  is going on and folds the work away when the answer is done.

The whole design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every guard, check and known limit:
[docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md). How it was built, phase by phase, with what was measured:
[docs/history/](docs/history/).

## Run it

### 1. The app

Python 3.11 or later (tested on 3.13), on Windows:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock -e .
copy .env.example .env
```

`requirements.lock` holds the exact versions the tests passed with. Fill in `.env` as you like (see Settings);
nothing in it is required to start.

### 2. The model - on Kaggle (free) or your own GPU

**On Kaggle**, with no GPU of your own:

1. Open [`kaggle/zen-qwen36-server.ipynb`](kaggle/zen-qwen36-server.ipynb) on Kaggle (*File → Import notebook*), set
   *Accelerator* to `GPU T4 x2` and *Internet* to `On`, and *Run All*. It downloads llama.cpp and the model (about
   21 GB) and prints an **address** and a **password**.
2. Copy `kaggle/bridge.env.example` to `kaggle/bridge.env` and paste them in.
3. Run the bridge: `.venv/Scripts/python.exe kaggle/bridge.py` - the model now answers at `127.0.0.1:8080`, as if it
   ran here.

**On your own GPU** (about 24 GB of GPU memory for this model), run llama-server with two slots and the vision
projector:

```bash
llama-server -m Qwen3.6-35B-A3B-UD-Q4_K_S.gguf --mmproj mmproj-F16.gguf -c 131072 -np 2 --no-kv-unified \
  -fa on -ctk q8_0 -ctv q8_0 --reasoning-format deepseek --temp 1.0 --top-p 0.95 --top-k 20 --presence-penalty 1.5 \
  --host 127.0.0.1 --port 8080
```

Slot 0 is the conversation, slot 1 the helpers; the app's prompt limit follows each slot's size.

### 3. Start

```bash
.venv/Scripts/uvicorn.exe app.main:app
```

Open http://localhost:8000, make a project, and ask - or start from
[`data/samples/cafe_daily_sales.csv`](data/samples/cafe_daily_sales.csv): *"What sells best, and what might next
month look like? Show me a chart."*

**Optional, recommended:**
- *Search by meaning* - a llama.cpp build that knows EmbeddingGemma 2 and its two files (`embeddinggemma-2-Q8_0.gguf`
  and its `mmproj`); set the three `ASSISTANT_EMBED_*` settings and the app starts it itself. Without them, search
  uses a small CPU model (bge-small, downloaded once).
- *A real browser* - Node and Microsoft Edge, with Playwright MCP in a folder of its own
  (`npm install @playwright/mcp`) named by `ASSISTANT_PLAYWRIGHT_MCP`. It reads pages drawn by JavaScript, and it
  tries the pages and games Zen writes before they are called ready.
- *Pictures* - a key from enter.pollinations.ai as `POLLINATIONS_KEY`.

## Settings

Environment variables, or lines in `.env` in the project folder (`NAME=value`; it is git-ignored):

| Setting | What it is |
|---|---|
| `BIZ_LLAMACPP_URL` | the chat model's llama-server (default `http://127.0.0.1:8080`) |
| `ASSISTANT_EMBED_SERVER` | llama-server's program, for the embedding server the app starts |
| `ASSISTANT_EMBED_GGUF`, `ASSISTANT_EMBED_MMPROJ` | EmbeddingGemma 2's model file and picture projector |
| `ASSISTANT_EMBED_URL` | where the embedding server listens (default `http://127.0.0.1:8070`) |
| `ASSISTANT_PLAYWRIGHT_MCP`, `ASSISTANT_BROWSER` | Playwright MCP's folder; the browser it drives (default `msedge`) |
| `POLLINATIONS_KEY` | for generating pictures |
| `ASSISTANT_NAME`, `ASSISTANT_USER_NAME` | the assistant's name (default Zen) and yours |
| `ASSISTANT_THINKING` | the model thinks before answering: `on` (default) or `off` |
| `ASSISTANT_MAX_TOKENS` | a reply's limit, thinking included (default 4096) |
| `ASSISTANT_DATA_DIR` | where projects, chats and files are kept (default `data/assistant`) |
| `BIZ_MAX_UPLOAD_MB` | the largest upload (default 100) |
| `BIZ_LLM_PROVIDER` | `gemini`, `nvidia` or `groq` runs the app on a cloud model instead, for testing the harness (default `llamacpp`) |
| `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY` | that cloud's key; `BIZ_GEMINI_MODEL`, `BIZ_NVIDIA_MODEL`, `BIZ_GROQ_MODEL` pick its model |

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

660 tests, no model needed: the tools directly, the conversation and the helpers with scripted models, the API
through its client, and every tool's contract and description against its schema. With Playwright MCP installed,
one test also opens pages in the real headless browser. Measurements on the real model are scripts in `bench/` -
each phase's acceptance and 20 jobs (10 held out) - with their reports beside them.

## Layout

| Folder | What is there |
|---|---|
| `app/assistant/` | main's turn, its checks, the prompts, every tool's contract, the chat store; `workers/` - the helpers |
| `app/tools/` | the tools, each a plain function; the project folder, the sandbox, the search index, the page check |
| `app/rag/`, `app/providers/`, `app/api/` | document reading and embeddings; the model providers; the HTTP API |
| `web/` | the chat page (no build step) |
| `kaggle/` | the notebook that serves the model on Kaggle, and the bridge |
| `tests/`, `bench/` | the tests; measurements on the real model |
| `docs/` | architecture, the system map, the build history, screenshots |
| `data/samples/` | small sample tables for trying it and for the tests |

## Status

A working personal project, built and tested on Windows with one user. Known limits are recorded, not hidden: the
model can still over-work a simple question or skip a built tool for a script, the sandbox is a limit for your own
assistant rather than a security boundary, and a Kaggle session ends after about 12 hours (a new one gets a new
address). See [docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md), section 5.
