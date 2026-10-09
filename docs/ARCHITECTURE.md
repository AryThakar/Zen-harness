# Zen - architecture

How the assistant works today, read from the code (2026-10-09). The decisions behind it, what was measured
and what is still to do are in [SYSTEM_PLAN.md](SYSTEM_PLAN.md); the findings of the full read-through are in
[REVIEW_LOG.md](REVIEW_LOG.md).

## 1. The idea

Zen is a chat assistant on a local model (Gemma 4 through llama-server). A **workspace** is a project and a
sandbox: its files, tables, pictures, notes, chats and one search index live in its own folder, and nothing
crosses its edge except through a card the user clicks (a download, a delete, replacing the user's file).
**main** talks with the user and does quick things itself; four **helpers** do focused work, each with only
its own tools, and hand main a short checked result. What the system guarantees - facts, file safety,
honesty - is done by code; the model is helped, never trusted with it.

```
browser (web/)  ──HTTP + stream──►  FastAPI (app/main.py, app/api/assistant.py)
                                        │
                                   main turn loop (app/assistant/main.py) ── model slot 0
                                        │  9 tools of its own + 4 delegates
                     ┌──────────────┬───┴──────────┬──────────────┐
                 research_web   ask_knowledge   analyze_data    write_code      ── model slot 1
                 web helper     knowledge       data analyst    coder           (app/assistant/workers/)
                     │              │               │               │
                 app/tools/ - plain functions with direct tests, no model inside
                     │
   workspace folder (files, tables, pictures, notes, reports, sandbox) + search index + chat store
```

## 2. What runs

| Process | What it is | Who starts it |
|---|---|---|
| The app | `uvicorn app.main:app` - API and chat page on http://localhost:8000 | you |
| The chat model | llama-server (OpenAI-compatible) at `BIZ_LLAMACPP_URL`: local, or the 26B on Kaggle through its bridge. Slot 0 is main, slot 1 the helpers | you |
| The embedding server | llama-server with EmbeddingGemma 2 and its picture projector, port 8070, this computer only | the app, on first use; stopped when it exits (app/rag/embed_server.py) |
| The browser | Playwright MCP (Edge, headless, no profile) for pages a plain read can't get | the app, on first use; closed when idle (app/tools/browser.py) |

Settings come from environment variables or the project's `.env` (app/config.py): the chat model's address,
the embedding server's files (`ASSISTANT_EMBED_SERVER`, `ASSISTANT_EMBED_GGUF`, `ASSISTANT_EMBED_MMPROJ`),
Playwright MCP's folder (`ASSISTANT_PLAYWRIGHT_MCP`), the data folder, thinking on or off, the assistant's
and the user's names. Without the embedding server set up, search uses fastembed's bge-small on the CPU;
without Playwright MCP, there is no browser and such pages are reported as unreadable.

## 3. The code

| Folder | What is there |
|---|---|
| `app/api/` | the HTTP API: workspaces, chats, messages (streamed), answers to questions, uploads, items, the file panel's save and run, web icons |
| `app/assistant/` | main's turn loop (`main.py`), what every call sees (`context.py`), main's honesty checks (`checks.py`), every tool's schema and handler (`registry.py`), the prompts (`prompts.py`), the store (`store.py`) |
| `app/assistant/workers/` | the helpers: their contracts and profiles (`contracts.py`), the step loop (`runner.py`), the evidence trace and guards (`evidence.py`), result checks (`checks.py`) and repairs (`repairs.py`) |
| `app/tools/` | the tools - each a plain function returning one result shape (`core.py`), tested without a model |
| `app/rag/` | the document reader (`extract.py`), passages (`chunking.py`), embeddings (`embed.py`) and the embedding server (`embed_server.py`) |
| `app/providers/` | model providers: llama-server (`llamacpp.py`, with Gemma's text-written calls repaired in `gemma_calls.py`), Groq for testing, a scripted model for tests |
| `web/` | the chat page: `index.html`, `app.css`, and `js/` - `app.js` (the chat), `api.js`, `cards.js` (answer cards), `charts.js`, `steps.js` (helper steps, trace), `files.js` (file panel), `viewer.js`, `ask.js`, `util.js` |
| `tests/` | the tests (pytest, ~510) - tools directly, the loop with scripted models, the API, the real embedding server when it is set up |
| `bench/` | measurements behind decisions (search models, web ranking, page focus, the extractor gate, helper runs) |

## 4. A turn

A turn is the user's message, then model steps and tool calls until main answers - a LangGraph graph
(agent → tools → agent ... → end), streamed to the page; every message is saved as it happens (the store
is the history).

- **What main sees.** A system prompt that never changes (persona and how it works - so llama.cpp's prompt
  cache holds), the chat through a rolling window, and its tools. What the workspace holds and the pinned
  notes ride on the user's messages, append-only (context.py): the first message carries a snapshot,
  later ones only what changed. Each user message starts with its local time.
- **Fitting the slot.** Sizes are counted exactly by the model server. The oldest whole turns drop out,
  25% below the limit, and the cut stays put so the prompt's start stays the same; a turn that alone
  outgrows the slot has its older tool results shortened. Dropped turns stay findable through recall.
- **Questions.** ask_user pauses the turn: a card with options, a text box and Skip; the answer comes
  back as the call's result. A helper's question pauses in place - the helper keeps its work and waits.
- **Approvals.** Downloading, importing a table from a link, deleting and replacing the user's file are
  asked through a card (approval.py); on the button, code does the action - the model never does.
- **Honesty checks** (checks.py), each from a measured failure: a call that already failed is not
  repeated; a helper task carrying numbers no tool returned and the user never wrote is refused; a reply
  that announces an action without doing it, claims an action no tool did, says it will remember without
  saving, or writes a tool call as text gets one hidden nudge.
- **The plan card.** main can show its plan as a checklist; a step ticks when its tool's call ends well.
- **After the turn.** The turn is indexed for recall, the workspace's new pictures and tables are indexed,
  a new chat is named, and a trace is stored - main's steps and tokens, every tool call, each helper's
  steps and checks - opened from a button under the answer.

## 5. The helpers

| Helper | main calls | Its tools | Steps | Finishes with |
|---|---|---|---|---|
| web | research_web | web_search, read_webpage, ask_user | 12 | answer, sources, how each page was read, pages that failed, download links |
| knowledge | ask_knowledge | search_files, recall, read_file | 6 | answer, sources |
| data analyst | analyze_data (when a table exists) | inspect_data, clean_data, query_data, make_chart, compare, forecast, predict, find_patterns, analyze_text, run_code, fix_code, ask_user | 10 | save_report: answer, report kept on the table, files |
| coder | write_code | run_code, fix_code, read_file, ask_user | 10 | save: the file kept with its report, answer, files |

main knows each helper by its contract (contracts.py) - what it is for and what it hands back - never by
its tools. A helper runs on slot 1 with its own short prompt, a step limit and only its tools. The runner
(runner.py) stops a repeated call, a computation on a table nobody inspected and a seventh page of one site
before they run; the result is checked by code before main sees it - web sources must be pages it read,
knowledge sources ones a tool returned, every number in a data answer must be in a tool result, the coder's
report must be complete and its code must have run. A failed check gets one chance to fix; after that the
result goes back with a warning. Evidence fields small models get wrong are set right from the trace first
(repairs.py). Charts, tables and pictures go straight to the page, not through the summary. The coder sees
pictures - what its script drew, an image it reads - in its own conversation.

A **report** is kept on the item a helper worked on (`<item>.report.md`, reports.py): its own sections, then
facts the app writes itself (whether the run passed, what it made), so it can't claim what didn't happen.
The next task on that item starts from it.

## 6. The tools

main's own: **files** (list, find, read, write, update, rename, delete, download - one job per mode; Word and
slides are written from Markdown, with pictures), **notes** (save, list, delete - the AI's short memory, the
latest pinned in view), **calculate** (exact arithmetic), **draw_diagram** (Mermaid), **generate_image**,
**view_image** (Gemma's vision, one question about one picture), **get_weather**, **ask_user**, **plan**.

The helpers': **web_search** and **read_webpage** (section 8); **search_files**, **recall**, **read_file**
(section 7); the data tools - **inspect_data** (a first look and what could be cleaned), **clean_data**,
**query_data** (one read-only DuckDB query, time-limited), **make_chart** (7 kinds, computed by code, also
kept as a picture), **compare** (is a difference real: tests, A/B), **forecast**, **predict** (learn a column,
what drives it), **find_patterns** (segments, unusual values, items together), **analyze_text** (themes,
labels, similar texts by meaning); and **run_code** / **fix_code** (a helper's one scratch script, run in the
sandbox).

Every tool returns one shape: a status, a result for the model and an artifact for the page; a tool error
becomes a result with its message, never a crash (core.py). One resolver turns any name the model uses
(an id, a file name) into an item.

## 7. Workspace, store and search

- **Store** (store.py, one SQLite file): workspaces, chats (with the rolling window's cut and a waiting
  question) and every message, including the page's cards and each turn's trace.
- **Workspace folder** (workspace.py): every item has an id by kind - `data_1` (a table, kept as Parquet),
  `image_1`, `file_1`, `note_1` - a name, a parent when it was made from another item, and its report.
  One naming rule for everything made from an item ("sales - cleaned"). The `sandbox/` folder is where
  scripts run.
- **Search index** (search_index.py, one per workspace): every passage stored once - documents,
  file reports, notes, chats, pictures, tables and web pages read - searched by keywords (FTS5, BM25) and by
  meaning (embeddings), the two rankings fused by rank (RRF). Keywords must always work: without the
  embedding model, search says it used keywords only.
- **The embedding model**: EmbeddingGemma 2 on llama-server - text and pictures in one space of 768 numbers,
  so a typed question finds a picture. Each model keeps its own prompts and cut-offs (embed.py, measured on
  held-out questions about the user's own workspaces); a passage records which model embedded it, and a
  new model re-embeds each workspace in the background when it is opened.
- **What is kept current, and when**: documents when added or changed; notes when saved; each chat turn
  when it ends; pictures (by what they show) and tables (name, columns, a few values) after every turn,
  upload and run; pages the web helper read, for 30 days.
- **search_files** returns the best passages of documents, reports, pictures and tables, then - searched
  apart, never in their place - the 1-2 best notes and pages read before (dated, marked as that day's
  text). **recall** finds earlier turns of every chat in the workspace.

## 8. The web

`web_search` asks public search engines through ddgs, DuckDuckGo first, a second tier only when the first
finds nothing; results are de-duplicated and ordered by the engines' rank fused with meaning. `recent`
uses the engines' own time filter.

`read_webpage`, one pipeline for every link:
1. **Address check** - public addresses only, every redirect checked again; this computer is out of reach.
2. **Site adapters** - GitHub (repository, folder, file), YouTube (title, channel, length, description),
   MediaWiki wikis including Wikipedia and Fandom, through their APIs.
3. **By what the link is** - a PDF, Word or PowerPoint file goes through the one document reader; a data
   file shows its columns and first rows, with how to bring it in as a table; an image says it is one.
4. **A page** - menus, footers, cookie notices and pop-ups out, the parts a streamed (React) page sends
   late put back in place, files it links to listed.
5. **The browser** - only when the page is blocked or gives too little text; its HTML goes through the
   same extraction.
6. **Long pages** are cut to 12,000 characters; with `find`, the opening plus the lines about those words.
7. **One status** for every read: ok, cut, partial, blocked, not found, unreachable or unsupported - with
   what to try next.

Reliability: a pause between pages of one site, at most 6 pages of one site per task, pages read cached for
an hour, bounded retries on temporary errors only. Safety: an honest browser (no stealth, no CAPTCHA
solving, no logins), no tool that clicks or types, page text treated as data, not instructions.

## 9. Running code

`run_code` runs a script in a separate Python process inside the workspace's `sandbox/` folder (sandbox.py):
30 s by default (at most 120), a clean environment with no secrets, output cut to 10,000 characters, typed
input for programs that ask. Tables are read as copies; files it writes become items (CSV/Parquet a table,
PNG/JPEG a picture). An audit hook refuses reading outside the workspace, writing outside the sandbox,
starting programs and the network. It is a limit for one user's own assistant, not a security boundary.
All runs go through one engine interface (`sandbox.ENGINE`), so a real sandbox can replace it in one
place. The file panel can edit a text file and run a Python file with typed input, under the same rules.

## 10. The page

One chat page (web/): workspaces and chats on the left; answers with their cards (charts, tables, pictures,
diagrams, code, sources, web pages, questions, the plan); each helper's steps shown while it works; the
file panel for the workspace's items (view, download, edit and run); a trace button under each answer.
Libraries come pinned from a CDN (marked, DOMPurify, Chart.js, KaTeX, Mermaid). The API refuses
state-changing requests whose Origin is another site, so a web page the browser opens can't drive it.

## 11. Tests and measurements

`python -m pytest` runs the app's tests (no model needed: tools directly, the loop and helpers with
scripted models, the API through its client; the embedding test uses the real server when it is set up).
`requirements.lock` holds the exact versions the tests passed with. Decisions that needed numbers were
measured once and the scripts kept in `bench/` - the embedding models (`search_models.py`), web ranking
(`web_rank.py`), focusing pages by meaning (`web_focus.py`), the text extractor gate (`web_extract.py`).
