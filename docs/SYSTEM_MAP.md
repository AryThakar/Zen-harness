# System map - after the rebuild (2026-10-15) and the testing pass (2026-10-16)

Who owns what, and every guard, check and nudge, read from the code after the seven phases of
[REBUILD_PLAN.md](REBUILD_PLAN.md). How it fits together is in [ARCHITECTURE.md](ARCHITECTURE.md). The first map
(the read of 2026-10-10, before the rebuild) is in the git history.

The principle, the user's: **a fixed contract with small genuine variations (a path in any case, a copied listing
line); anything else is refused with what is wrong, what is valid and the next call; guards only for safety,
permission and truth.** Code never guesses what the model meant, never dead-ends it, never tells it something
false, never loses work. And since phase 5: a model that still misroutes once its contract is clear and
consistent is a limit to record, not a prompt to bend (section 5).

## 1. The parts

| Part | Files (lines) | Owns |
|---|---|---|
| Server | app/main.py (108), app/config.py (74), app/api/assistant.py (543) | the app, settings, the HTTP API |
| Turn | app/assistant/main.py (769), context.py (254), snapshot.py (129), checks.py (182), prompts.py (205), registry.py (674), store.py (284) | main's turn, what every call sees and how it fits, the pinned files' snapshot, main's checks, the prompts and nudges, every tool's contract and handler, the chat store |
| Helpers | app/assistant/workers/ contracts (185), runner (529), evidence (209), checks (132), __init__ (98) | the helpers' contracts and profiles, their step loop, the evidence and guards, the result checks |
| Providers | app/providers/ base (109), llamacpp (505), gemma_calls (268), cloud (136) with gemini (45), nvidia (34), groq (51), scripted (51) | model calls, streaming (a call's size as it is written), exact counting, the model server's state and where it runs, Gemma's text-written calls, a cloud's API for tests |
| Tools | app/tools/ - 40 modules (10,238) | each tool: one function, one result shape (core.py); the workspace, the sandbox, the console, the search index |
| Search | app/tools/search_index.py (335), files.py (404), memory.py (76), app/rag/ (714) | FTS5 + embeddings, documents, find, recall |
| Page | web/ (4,683) | the chat page; card types checked against what the server sends |

## 2. One owner per concept

| Concept | Owner | Everyone else |
|---|---|---|
| A file's name | its path - `Workspace.resolve` reads one (any case, a listing line, a key); `workspace.present` writes paths into every result a model reads (at `ToolResult.for_model`) | keys stay inside the app and the page's links |
| A file arriving or changing | `Workspace.put` (and `adopt`, for a file a script wrote in place): unique paths, atomic writes, the last 10 versions, identical uploads recognised | uploads, tools, runs and the console all go through it |
| What kind a file is | `filetypes.py` | tables, documents, pictures, code, what is editable as text |
| The project's memory | `agent_md.py` - in main's system prompt (main.py) and each helper's opening (context_block) | the file panel shows it first, as Memory |
| Pins and @ | the chat's pins (store.py), a message's files (main.record), the turn's snapshot (snapshot.py) | the page: pins.js, mention.js |
| Finding files | `files.find_files` | main's and the helpers' files find, the file panel's search box |
| A chart | its chart file (make_chart, a script's chart(), forecast, find_patterns); its picture and its numbers drawn from it on demand (chart_image: picture_of, table_of) | the page draws it live; documents take its picture |
| A tool's contract | `registry.py` (and the delegates in contracts.py): one description template, every field described | tests/test_tool_contracts.py checks every tool and everything each model reads |
| A helper's result | the envelope (`contracts._Out`; the app's facts added in runner._done) | main reads one shape from every helper |
| Fitting the slot | `context.py` (cut_turns, shorten_tool_results, shorten_call_args) and the provider's exact count | main and the runner use the same ladder |
| Running a program | `sandbox.py` (the set-up, the audit hook, the clean environment): run_python to its end, start for the console | run.py (the helper's run, the report), console.py (the user's session) |
| What a run left | `run.keep_report` - the facts, and a console session as it went | the next task and the model read the file's report |
| The model server's state | `LlamaCppProvider.health` - ready, loading or down, in plain words; and `location` - this computer, Kaggle (the bridge names itself, X-Model-Host) or a cloud | /status, the header's dot, the start screen |
| A helper's working files | `work/` - its scripts and in-between tables (the engineer's prompt and run's contract say so) | the page: inside the helper's step in the chat; a closed section of the file panel |
| How a tool is named while it works | `Tool.labels` in registry.py, served at /tools | the page keeps no list of its own |
| Instructions | prompts.py (roles, nudges), registry.py (tools), contracts.py (delegates), the tools' own results | each rule in one place (tests/test_prompts.py: no model reads a sentence twice) |

## 3. Guards, checks and nudges

| Where | What it does | Kind | Way out |
|---|---|---|---|
| sandbox (audit hook) | refuses reading outside the workspace, writing outside its files, changing the user's files, deleting or moving files, programs, the network | safety | none - "write under a new name"; data comes in through files download |
| approval.py | download, delete, changing the user's own file: a card, code acts on the button | permission | the user's click |
| agent_md | capped at 2,000 characters; never deleted or renamed | truth (it is sent every turn) | merge or drop lines; longer material in a file under notes/ |
| main: repeated failure | a call that already failed in this turn is stopped with why | judgment | `confirm: true` |
| main: invented numbers | a helper task with numbers no tool returned and the user never wrote is stopped | truth | `confirm: true` (sample data, labelled made up) |
| main: announce / claim / remember | a reply that announces an action without a call, claims one no tool did, or says it remembers without writing agent.md: one hidden [App check] a turn | truth | do it, or say plainly it wasn't done |
| main: a file named as made that isn't there | a reply saying Zen saved, wrote or made a file no file of that name is in the workspace: one hidden [App check] (found in the held-out run; replayed over 333 stored replies: none flagged) | truth | make it, or tell the user it wasn't made |
| main: cut-off reply, thought too long | another step to finish; a card to try again with a thinking budget | serves the model | - |
| main: over the slot | shorten older results, older calls' texts, newest results; else a plain stop | truth (never sent over) | ask for less, or a new chat |
| runner: not its tool, too many questions | refused, saying which tools it has / to decide with the recommended choice | contract | the next call |
| runner: a table nobody inspected | inspected first; the call runs after when nothing needs cleaning | correctness | - |
| runner: a choice only the user can make | asked in place; its cleaning step applied by code | permission | the user's answer |
| runner: a repeat | one that worked gets its earlier result back; one that failed is stopped with why | serves the model | `confirm: true` |
| runner: web searching without reading | a fourth search in a row is refused until a page is read | judgment | read a page, or `confirm: true` |
| runner: one site | at most 6 pages of one site in a task | politeness | read another site |
| runner: cut-offs in a row | the same call cut off twice, or three cut-offs of any kind: the helper ends with what it has (measured: three in a row, 3.5 minutes each, until the user stopped it) | serves the model | - |
| main and runner: a call cut off | told the call was too long, with its size, and to write a long file in parts - not to think less; main's second in a row ends the turn with a plain message | serves the model | a shorter call |
| clean_data: drop_missing over every column | refused when it would remove most of the rows, naming the mostly empty columns (measured: 34 of 344 rows kept, the answer built on them silently) | truth | name the columns, or drop the empty ones first |
| web: a refusing site | not tried again in the chat | politeness | one other page, said in the caveats |
| result checks | web: pages cited were read, file links in downloads; knowledge: sources a tool returned; analyst: every number in a tool result or a run | truth | one chance to fix, then a warning main passes on |
| runner nudges | cut off, no call, the conversation full, steps left, fix the finish, the pictures it is shown | serves the model | - |
| provider: Gemma's calls as text | a call written in Gemma's own syntax read as the call | format adapter | - |

## 4. Tests that hold the contracts

- `test_tool_contracts.py`: every tool refuses a missing field naming it, and a file that isn't there or of
  another kind saying so; everything each model reads (its prompt, the app's hidden messages, agent.md's lines, its
  tools and fields, and every string a tool returns) names only tools, modes and fields that model has; each tool
  with modes describes every mode and no other.
- `test_prompts.py`: no model reads a rule twice; each helper reads its craft in its own prompt.
- `test_workspace_files.py`: one path for every reader; identical uploads one file; versions; the index follows
  the folder; no key in any result a model reads.
- `test_console.py`, `test_pins_snapshot.py`, `test_assistant_main.py` (the slot never overflows, the snapshot is
  never stored), `test_main_checks.py` (each check from a measured failure, and the replies it must leave alone),
  and the tools' own tests - 628 in all, no model needed.

## 5. Known limits (measured, recorded - not prompted around)

Seen on Gemma 4 26B-A4B and, since 2026-10-16, on Qwen 3.6 35B-A3B (the same tests: ARCHITECTURE.md section 12) -
a limit both models show is the models' class, not one model:

- **main writes a method into a helper's task** ("analyze correlations between left and ..."); the analyst then
  tests columns one pair at a time instead of predict - the answer was right, but pairwise tests can mislead when
  columns move together (phase 5, 2 of 2 runs).
- **main sends a one-file question to ask_knowledge** before reading the file itself - right answer, one helper
  call more (phase 5, 2 of 2).
- **main can hand its own work to a helper**: a slide deck given to analyze_or_code, against its contract ("not
  for documents - Word, slides"), came back as one slide built in a script (phase 7, dev).
- **Both models write the method into a helper's task** (Qwen: "do correlation analysis", "use a time series
  method (e.g. Holt-Winters/ETS)"), and both analysts **skip a built tool** now and then (Qwen: a spike found with
  SQL instead of find_patterns - right answer; a Fibonacci chart drawn with matplotlib instead of a live chart).
- **Qwen: a casual remark can call a tool** ("rainy afternoon" -> get_weather), and emoji in 2 of 4 casual replies
  (the aim is at most 1).
- **A long file in one call**: a reply holds 4,096 tokens, thinking included (measured: a 2 KB game written by
  main took 3,425 of them, most of it thinking), so only a few KB fit one call; a longer one is now told, with its
  size, to be written in parts.
- **The analyst can skip a built tool for a script** - item pairs counted in a script instead of find_patterns,
  giving popularity rather than lift (phase 5 held-out). If this keeps happening, the answer is a library door to
  the analysis tools from scripts (REBUILD_PLAN section 7), not more wording.
- **The sandbox is not a security boundary**: a separate process with an audit hook, for one user's own
  assistant; a native library that opens files on its own is not covered.
- **A timing flake**: test_open_figures_are_saved_once failed once in a full run and passed the five runs since.
- **The model is remote**: the 26B runs on a Kaggle notebook through a quick tunnel; a new session gets a new
  address (bridge.env), and while it is down the page says so - nothing else can answer.
