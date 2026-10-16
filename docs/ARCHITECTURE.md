# Zen - architecture

How the assistant works, read from the code after the rebuild (2026-10-15) and the testing pass that followed
(2026-10-16: section 12). Why it is built this way, phase by
phase with what was measured, is in [REBUILD_PLAN.md](REBUILD_PLAN.md); who owns what, and every guard and check,
in [SYSTEM_MAP.md](SYSTEM_MAP.md).

## 1. The idea

Zen is a chat assistant on an open model through llama-server (Qwen 3.6 35B-A3B since 2026-10-16, Gemma 4 26B-A4B
before - section 12). A **workspace** is a project: one
folder of real files, each named by its path - the same name for the user, the model, the helpers, a script and
the search index - with its chats and one search index. Nothing crosses its edge except through a card the user
clicks (a download, a delete, changing the user's own file).

**main** talks with the user and does quick things itself; three **helpers** do focused work - the web, reading
many or long documents and earlier chats, and computing in the workspace - each with only its own tools, and
hand main one checked result. What the system guarantees - facts, file safety, honesty - is done by code; the
model is helped, never trusted with it.

```
browser (web/)  --HTTP + server-sent events-->  FastAPI (app/main.py, app/api/assistant.py)
                                                     |
                                       main's turn (app/assistant/main.py)  -- model slot 0
                                         7 tools of its own + 3 helpers
                     +----------------------+--------+---------------------+
                research_web          ask_knowledge          analyze_or_code      -- model slot 1
                web helper            knowledge helper       analyst-programmer   (app/assistant/workers/)
                     |                      |                      |
                app/tools/ - plain functions with direct tests, no model inside
                     |
   workspace folder (files/ by path, .zen/ registry, versions, reports) + search index + chat store
```

## 2. What runs

| Process | What it is | Who starts it |
|---|---|---|
| The app | `uvicorn app.main:app` - the API and the chat page on http://localhost:8000 | you |
| The chat model | llama-server (OpenAI-compatible) at `BIZ_LLAMACPP_URL`, two slots: 0 is main, 1 the helpers. Today Qwen 3.6 35B-A3B on Kaggle's two T4s, reached through `C:\zen-data\kaggle\bridge.py` (a local address forwarding to the notebook's tunnel; it names itself, so the page says the model runs on Kaggle); a local llama-server works the same way | you |
| Or a cloud model | for testing the harness with a stronger model: `BIZ_LLM_PROVIDER=gemini`, `nvidia` or `groq` and its key in `.env` (app/providers/cloud.py) - each free plan measured too small for the full tests (section 12) | you |
| The embedding server | llama-server with EmbeddingGemma and its picture projector, port 8070, this computer only | the app, on first use; stopped when it exits (app/rag/embed_server.py) |
| The browser | Playwright MCP (Edge, headless, no profile) for pages a plain read can't get | the app, on first use; closed when idle (app/tools/browser.py) |

Settings come from environment variables or the project's `.env` (app/config.py): the chat model's address,
the embedding server's files, Playwright MCP's folder, the data folder, thinking on or off, the assistant's and
the user's names. Without the embedding server, search falls back to fastembed's bge-small on the CPU; without
Playwright MCP, pages that need a browser are reported as unreadable. The prompt limit follows the model server
(its slot size from /props, less the room for the reply): 20,480 tokens a slot on Kaggle today.

## 3. The code

| Folder | What is there |
|---|---|
| `app/api/` | the HTTP API: status, workspaces, chats, messages and answers (streamed), pins, items, uploads, the file panel's edit, find, versions and restore, chart pictures and CSV, the console, web icons |
| `app/assistant/` | main's turn (`main.py`), what every call sees and how it fits the slot (`context.py`), the pinned files' snapshot (`snapshot.py`), main's checks (`checks.py`), every tool's schema, description and handler (`registry.py`), the prompts and nudges (`prompts.py`), the chat store (`store.py`) |
| `app/assistant/workers/` | the helpers: their contracts and profiles (`contracts.py`), the step loop (`runner.py`), the evidence and the guards before a call (`evidence.py`), the result checks (`checks.py`) |
| `app/tools/` | the tools, each a plain function returning one result shape (`core.py`); the workspace (`workspace.py`), file types in one place (`filetypes.py`), agent.md (`agent_md.py`), the sandbox (`sandbox.py`), runs and the console (`run.py`, `console.py`), the search index (`search_index.py`) |
| `app/rag/` | the document reader (`extract.py`), passages (`chunking.py`), embeddings (`embed.py`) and the embedding server (`embed_server.py`) |
| `app/providers/` | model providers: llama-server (`llamacpp.py`, with Gemma's text-written calls read in `gemma_calls.py`); a cloud's OpenAI-compatible API (`cloud.py`) - Google's Gemini API, NVIDIA Build, Groq - for testing; a scripted model for tests |
| `web/` | the chat page: `index.html`, `app.css` and `js/` - `app.js` (the chat), `api.js`, `cards.js` (answer cards), `charts.js`, `steps.js` (the work behind a reply, the trace), `files.js` (the file panel), `console.js`, `viewer.js`, `mention.js` (@), `pins.js`, `ask.js`, `util.js` |
| `tests/` | pytest, 628 tests - tools directly, the turn and the helpers with scripted models, the API through its client, the contracts of every tool and description; no model needed |
| `bench/` | measurements on the real model: each phase's acceptance (`phase2.py`, `phase4.py`, `phase5.py`), the 20 jobs (`combos.py`), main's probe suites (`main/`), the search and web measurements behind earlier decisions |

## 4. A turn

A turn is the user's message, then model steps and tool calls until main answers (at most 8 tool rounds) - a
LangGraph graph (agent -> tools -> agent ... -> end), streamed to the page; every message is saved as it happens
(the store is the history).

- **What main sees.** A system prompt that never changes while the app runs - the persona, the routing list
  (what main does itself, what goes to which helper), how to read what the app adds - followed by agent.md as it
  is now (section 7). Then the chat through a rolling window, and its tools. What the workspace holds rides on
  the user's messages, append-only: the first message a snapshot of the newest files, later ones only what
  changed. Each user message starts with its local time. Files the user pointed at with @ are named on their
  message ("[Files with this message: ...]").
- **Pins and @: the snapshot.** For the turn being answered, the files pinned to the chat and those pointed at
  with @ are matched to the message - an outline, the passages that match, a table's sample rows (labelled as a
  sample), code's functions with their lines, a picture's caption - at most 6,000 characters, attached to this
  turn's message only and never stored, like thinking (snapshot.py). The model reads more with files read.
- **Fitting the slot.** Sizes are counted exactly by the model server, the snapshot included. The oldest whole
  turns drop out, to 25% below the limit, and the cut stays put so the prompt's start - and the server's cache -
  stays the same. A turn that alone outgrows the slot shortens, in order: its older tool results, its older
  calls' long texts (a file's whole text: the file keeps it), then its newest results; past that it stops with a
  plain message - never sent over the limit. Dropped turns stay findable through recall.
- **Questions.** ask_user pauses the turn: a card in the message box with options, a text box and Skip; the
  answer comes back as the call's result. A helper's question pauses it in place - it keeps its work and waits.
- **Approvals.** Downloading, deleting and changing the user's own file are asked through a card (approval.py);
  on the button, code does the action - the model never does.
- **Guards and checks.** A guard before a call judges it and says why it stopped it; a judgment guard runs the
  call when the model sends it again with `confirm: true` - a call that already failed in this turn; a helper
  task carrying numbers no tool returned and the user never wrote (confirmed - sample data the user asked for -
  it runs, labelled as made up). Guards for safety and permission (the sandbox, the approvals) never take a
  confirm. After a reply, checks only advise, once per turn, with a hidden [App check]: a reply that announces
  an action without doing it, claims one no tool did, says it will remember without writing agent.md, or says it
  saved a file that isn't in the workspace.
- **Cut off by the length limit.** A reply cut off in its text gets another step; one that thought until the
  limit and said nothing asks the user to try again quickly. One cut off while it wrote a call (a whole file in
  `content`) is told so, with the call's size, and how to write a long file in parts - not to think less (measured:
  a helper told only "cut off" was cut off three times running, 3.5 minutes each); the same again ends the turn
  with a plain message. A helper ends with what it has after the same call is cut twice, or three cut-offs of any
  kind in a row.
- **What the page sees while it works.** The steps (and a helper's own steps inside them) at the top of the
  reply; once the reply has text, what is happening now ("Checking the answer...") at its end. A call being
  written shows its file and size as they grow ("Writing game.html · 3.2 KB..."), from the stream.
- **After the turn.** The turn is indexed for recall, the workspace's new files are indexed, a new chat is
  named, and a trace is stored - main's steps and tokens, every tool call, each helper's steps and checks -
  opened from a button under the answer.

## 5. The helpers

| Helper | main calls | Its tools | Steps | Finishes with |
|---|---|---|---|---|
| web | research_web (task) | web_search, read_webpage, ask_user | 12 | submit_result: answer, caveats, sources (the numbers of the pages it read), download links |
| knowledge | ask_knowledge (question) | files (find, read), recall | 6 | submit_result: answer, caveats, sources |
| analyst-programmer | analyze_or_code (task, files) | inspect_data, clean_data, query_data, make_chart, compare, forecast, predict, find_patterns, analyze_text, files (find, read, write, edit), run, ask_user | 12 | finish: answer, caveats - kept as the report of the table it worked on |

main knows each helper by its contract (contracts.py) - what it is for, what it hands back, when not to use it -
never by its tools. A helper runs on slot 1 with its own short prompt, a step limit and only its tools; its task
opens with the time, the workspace, agent.md and main's task - not the user's message (given the user's words, a
helper did the parts main had given to others). **A helper acts or finishes**: every step requires a tool call
(llama-server's tool_choice 'required') and the last step offers only its finish, so its result always comes
through the finish call - nothing is read out of prose.

**One result envelope.** The model writes only what only it knows - the answer, its caveats, and when needed a
suggestion or a question only the user can answer; the app adds what it saw - what ran, the files made or
changed, the pages read. Before main sees it, code checks it: a web answer must cite pages it read (by number),
with file links in downloads; knowledge sources must be ones a tool returned; every number in the
analyst-programmer's answer must be in a tool result or a run's output. A failed check names the problem and gets
one chance; after that the result goes back with a warning. A Stop, a model failure or a full context keeps the
work done so far as the result.

**Guards in the runner**: a repeat of a call that worked gets its earlier result back (nothing runs); a call that
failed is stopped with why and runs when confirmed; a table nobody inspected is inspected first; a choice only the
user can make (inspect_data's question, e.g. day first or month first) is asked, and its cleaning step applied by
code; a fourth web search in a row is refused until a page is read; at most 6 pages of one site in a task; at most
2 questions to the user in a task; cut-offs in a row as in section 4.

**Programs are files.** The analyst-programmer writes a program with files write, runs it with run, changes it
with files edit and runs it again; the file is the state, so the next task - or the same task after a Stop - goes
on from it. One it writes only to get the answer goes under `work/` with the tables it writes along the way (the
user's decision, 2026-10-15); a program the task asks for goes where the task says. The page keeps `work/` out of
the way: inside the helper's step in the chat, and in a closed "Zen's working files" section of the file panel. Each run keeps the file's report: what it says it does, then facts the app writes (passed or failed,
what it printed, what it made). It sees pictures - what its script drew, an image it reads or the task names - in
its own conversation.

## 6. The tools

Each description follows one template - what it does (in the words a user would use, method names included),
what it returns, when to use it, when not and which tool instead - and every field says what it holds, with an
example. A test reads everything each model reads and checks that every tool, mode and field it names is one that
model has (tests/test_tool_contracts.py).

main's own: **files** (list, find, read, write, edit, rename, delete, download, pin, unpin; Word and slides are
written from Markdown, with charts and pictures), **calculate** (exact maths with SymPy: arithmetic, equations,
derivatives, integrals, limits, sums, matrices - run in the sandbox with a time limit), **draw_diagram**
(Mermaid), **generate_image**, **view_image** (the model's vision, one question about one picture),
**get_weather**, **ask_user**; and its three helpers.

The helpers': **web_search** and **read_webpage** (section 8); **recall** (earlier chats); the data tools -
**inspect_data** (a first look: types, roles, problems, suggested cleaning, questions for the user),
**clean_data** (dropping rows with a gap in any column may not remove most of a table: refused, naming the mostly
empty columns), **query_data** (one read-only DuckDB query over tables named by their files, time-limited),
**make_chart** (line, area, bar, scatter, histogram, box, heatmap, pie - computed from all the rows, kept as a
chart file; also_y draws more number columns beside y, one line or bar group each), **compare** (is a difference, correlation, association or A/B result real), **forecast** (trend,
season, the next periods with a range, against a baseline), **predict** (a model of one column from the others:
tested against a baseline, its drivers and their direction, drawn as a chart file), **find_patterns** (segments, unusual periods or
rows, items bought together), **analyze_text** (themes, labels, similar texts, by meaning); **files** in each
helper's own modes; **run** (a Python file of the workspace, in the sandbox).

Every tool returns one shape: a status, a result for the model and an artifact for the page; a tool error
becomes a result with what is wrong, what is valid and the next call - never a crash (core.py). One resolver turns
any name the model uses (a path in any case, a copied listing line, a key) into a file.

## 7. Workspace, store and search

- **The workspace folder** (workspace.py): `files/` holds every file at its path ('sales.csv',
  'charts/revenue.chart.json'); `.zen/` holds what each file is (registry.json: kind, path, who brought it, what it
  was made from), a table's typed copy, the last 10 versions of each file, the reports and the run folders. One
  write path (put, and adopt for a file a script wrote) keeps paths unique, writes atomically, keeps the version
  before a change and recognises an upload that is already there. Every text a model reads names files by path;
  the internal keys (data_3, image_7, file_2) stay inside the app.
- **agent.md** (agent_md.py): the project's standing memory, one short Markdown file at the top of the workspace -
  the user's standing instructions, preferences and facts. It is in main's system prompt every turn (and opens each
  helper's task), never in the history; the user edits it in the file panel, the model with files write. Capped
  at 2,000 characters; it can't be deleted or renamed, and scripts can't change it. The model server reprocesses
  the prompt only when it changes (measured: a normal turn reads 1-600 new tokens, the turn after a change about
  4,200 once).
- **Pins** are kept on the chat (store.py); a picture pasted with a name that says nothing is named from its
  caption when it arrives (caption.py), and the caption is searchable.
- **The store** (store.py, one SQLite file): workspaces, chats (with the rolling window's cut, a waiting question
  and the pins) and every message, including the page's cards and each turn's trace.
- **The search index** (search_index.py, one per workspace): documents' passages, code, charts, tables (name,
  columns, values), pictures (caption, prompt and the picture itself), reports, chat turns and web pages read -
  searched by keywords (FTS5, BM25) and by meaning (EmbeddingGemma: text and pictures in one space, so a typed
  question finds a picture), the two rankings fused by rank. Keywords always work: without the embedding model,
  search says it used keywords only.
- **find** (files.find_files) - the model's files find and the file panel's search box: each file once, best
  first, with the part that matched; "images" or "code" pick the kind instead of matching words; how many is the
  count of files. **recall** finds earlier turns of every chat in the workspace.

## 8. The web

`web_search` asks public search engines through ddgs, DuckDuckGo first, a second tier only when the first finds
nothing; results are de-duplicated and ordered by the engines' rank fused with meaning. `recent` uses the
engines' own time filter.

`read_webpage`, one pipeline for every link:
1. **Address check** - public addresses only, every redirect checked again; this computer is out of reach.
2. **Site adapters** - GitHub (repository, folder, file), YouTube (title, channel, length, description), MediaWiki
   wikis including Wikipedia and Fandom, through their APIs.
3. **By what the link is** - a PDF, Word or PowerPoint file goes through the one document reader; a data file
   shows its columns and first rows; an image says it is one.
4. **A page** - menus, footers, cookie notices and pop-ups out, the parts a streamed page sends late put back in
   place, files it links to listed.
5. **The browser** - only when the page is blocked or gives too little text; its HTML goes through the same
   extraction.
6. **Long pages** are cut to 12,000 characters; with `find`, the opening plus the parts about those words.
7. **One status** for every read - ok, cut, partial, blocked, not found, unreachable or unsupported - with what
   to try next, and the number the page is cited by.

A chat remembers what its helpers read and which sites refused: the same page again costs nothing, a refusing site
is not tried again (the helper reads one other page and says so). A pause between pages of one site, pages cached
for an hour, bounded retries on temporary errors only. Safety: an honest browser (no stealth, no CAPTCHA solving,
no logins), no tool that clicks or types, page text treated as data, not instructions.

## 9. Running code

`run` runs a Python file of the workspace in a separate Python process, in the workspace's `files/` folder, so
files open by their names (sandbox.py): 30 s by default, at most 120; a clean environment with no secrets
(PATH, the temp folder, matplotlib's Agg, and WINDIR so fonts are found by file name); output cut to 10,000
characters. Files it writes are the workspace's - a new file is registered, a file the assistant made gets a new
version; changing one of the user's own files is refused, with the advice to write the result under a new name
(the user is asked when main changes one); CSV and Parquet become tables, a chart() call writes a chart file the
page draws (`from chart import chart` and `import zen` work too). An audit hook refuses reading outside the workspace and
Python's installation, writing outside the workspace's files, deleting or moving them, starting programs and the
network. It is a limit for one user's own assistant, not a security boundary; all runs go through one engine
interface (`sandbox.ENGINE`), so a real sandbox can replace it in one place.

**The console** (console.py) is the same program started by the user from its file - in the file panel or on its
card in the chat: what it prints streams to the page as it is written, each line the user types goes to its
input(). Stop, Run again, Close (which stops a program still running); at most 15 minutes, 5 with nothing typed. When it ends, its files are registered and the
session - what it printed and what the user typed - is kept in the file's report, so the model can read it.

## 10. The page

One chat page (web/): projects and chats on the left; answers with their cards - native charts (hover values, full
screen, download as PNG or CSV), tables, pictures, diagrams, a file's card (an .html file runs in it; a Python
file has Run, opening the console), sources (a video opens in the viewer at 16:9), questions; the work behind each
reply (steps, a helper's steps, the trace); the live state at the end of a reply; a dot in the header for the
model's state (ready, loading, unreachable - said in plain words) and where it runs (this computer, Kaggle or a
cloud). The file panel: paths, a search box (find), the
Memory section with agent.md, pins, earlier versions and restore, editing text files, previews (Markdown, HTML in a
sealed frame with no network, PDF, tables, charts, code). The message box: @ to point at a file or pin it, pinned
files as chips in the header.

Libraries come pinned from a CDN (marked, DOMPurify, Chart.js, KaTeX, highlight.js, and Mermaid when a diagram
is drawn). The API refuses
state-changing requests whose Origin is another site, so a page the browser opens can't drive it; stored files are
served with a sandboxing policy, and only pictures inline.

## 11. Tests and measurements

`python -m pytest` runs the app's tests with no model: tools directly, the turn and the helpers with scripted
models, the API through its client, and the contract tests (every tool refuses a missing field and a wrong file
saying so; everything a model reads names only tools, modes and fields it has). `requirements.lock` holds the exact
versions the tests passed with.

On the real model (`bench/`, llama-server running): each phase's acceptance - `phase2.py` (the helpers),
`phase4.py` (find, pins, agent.md), `phase5.py` (routing probes, a dev and a held-out half) - and `combos.py`, 20
jobs of 3-6 tools each from new stores with fixed data: 10 for finding what fails, 10 held out and run once. Their
reports are kept beside them; REBUILD_PLAN.md section 8 records the results as they came.

## 12. The testing pass (2026-10-16)

The user's live chats were read turn by turn and every failure sorted into the app's or the model's: the app's
were fixed once (the cut-off loop, the cleaning that kept 34 of 344 rows, a script's `import chart`, a chart of two
columns, predict's chart, the console's close, a long write that looked frozen, helpers' working files in the chat,
the page saying "this computer" of a model on Kaggle); the model's are recorded in SYSTEM_MAP.md section 5.

To tell harness failures from model limits, the same tests ran on a second model. Free cloud plans were measured
too small (Gemini's free tier: 20 requests a day per Flash model; NVIDIA Build's free endpoints refused one model
and stalled on another; Groq: 8,000 tokens a minute), so the second model is Qwen 3.6 35B-A3B on the same Kaggle
setup - Gemma's exact server settings, without the model's built-in MTP (with MTP and the IQ4_XS file it wrote 10.6
words a second; without, with Q4_K_S, 47.9). Results, one run each (`bench/models/`):

| | Gemma 4 26B-A4B (2026-10-15) | Qwen 3.6 35B-A3B (2026-10-16) |
|---|---|---|
| Routing probes, held out | 18/19, 551 s | 18/19, 810 s |
| 10 dev jobs | 8/10, 1,071 s | 8/10, 1,039 s |
| 10 held-out jobs | 8/10, 492 s | 8/10, 614 s |

The same score. Qwen passed the two jobs Gemma failed in every run (a slide deck given to the wrong helper; a live
exchange rate in an exact sum) and failed four that Gemma passed - two with right answers (a strict check; a built
tool skipped for SQL), one tool skipped for a matplotlib picture, one file not written as a checklist. Today's fixes
were in Qwen's run only. Qwen became the working model.
