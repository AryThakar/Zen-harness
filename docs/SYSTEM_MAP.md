# System map - after the rebuild (2026-10-15), the testing pass (2026-10-16) and the passes from the user's chats (to 2026-10-22)

Who owns what, and every guard, check and nudge, read from the code after the seven phases of
[REBUILD_PLAN.md](history/REBUILD_PLAN.md). How it fits together is in [ARCHITECTURE.md](ARCHITECTURE.md). The first map
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
| The model server's state | `LlamaCppProvider.health` - ready, loading or down, in plain words; and `location` - this computer, Kaggle (the place the Model settings give it) or a cloud | /status, the header's dot, the start screen |
| Zen-harness.exe's window | `desktop/Zen.cs` ZenWindow - WebView2 in Windows' frame without its title bar; the page draws the bar (`.winbar` in index.html: `app-region: drag`, the window's buttons, the top edge) and says its theme; Edge's app window when WebView2 is missing | the page only talks to it through `chrome.webview` messages |
| Where the model runs | `model_link.py` - Kaggle (address and password, or the notebook's Zen code), this PC (an address) or a cloud (address, key, model): tried first (llama-server's /props, which needs the password; a cloud's /models, which needs the key) and written in place; the file is `ZEN_MODEL_SETTINGS` (model.env; the Windows app's settings.env). `connection.py` makes the providers and the search model again when it changes | /model-settings (the page's own Origin only; a password or key never goes back), the dot by a chat's name, Settings - which open by themselves the first time |
| Search by meaning's place | `connection.py` - on the chat model's machine: the notebook's with Kaggle, this computer's with This PC or a cloud; one model file everywhere, so one model's vectors; where it isn't, keywords. `search_model.py` sets this computer's up: files already here used where they are, the rest downloaded (about 900 MB), each checked against its published SHA-256, a stopped download carried on | Settings' *Search by meaning* row (/search-model: the page's own Origin only) |
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
| main: pictures | a second generate_image in one turn is stopped (measured: two a turn, three turns running, for one "Dr. Stone image") | judgment | `confirm: true` when the user asked for more than one |
| main: a typed table | a helper task carrying ten or more numbers typed from web results, before the page's table was downloaded, is stopped: files download brings it in exact | truth | `confirm: true` |
| main: dictated method | a helper task naming libraries, code steps or a statistical method is stopped: what is wanted, never how | contract | `confirm: true` |
| main: numbers no result holds | a reply in a turn a helper worked in, with three or more numbers no tool returned and the user never gave: one hidden [App check] | truth | write it with the results' numbers, or call it an estimate |
| main: a file still broken | a reply after writing a page or program that still doesn't run (a page's script, Python that doesn't parse): one hidden [App check] | truth | fix it, or say plainly it doesn't work yet |
| main: the last step | the turn's last step has no tools and is told never to hand the user the work | serves the user | - |
| files: a page written | its script must parse; then it is opened headless (file_ops.written_code, browser.try_page) - controls clicked, keys pressed, 10 s at most - and an error, a canvas of one flat colour, or "Game Over" on show before any input comes back as broken (the words must lead the text: "Any key after game over to restart" is an instruction, not the game over - measured: it was quoted as one, and a working draw loop was rewritten four times) | truth | fix it before saying it works |
| files: no name | an edit, read or rename without a name is told which files changed last | contract | the next call |
| download: a raw host | an HTML file from raw.githubusercontent.com is a file, not a page | correctness | - |
| workspace: a chart as data | a chart file given where a table is wanted says it is a chart already made, and to chart the table it came from | contract | the table |
| runner: a finish sent back on the last step | one step more to fix it (measured: "ran out of steps", the answer lost, the task handed over twice more) | serves the model | - |
| runner: a short finish | a finish cut off by the length limit on the last step gets one more step for a short one | serves the model | - |
| runner: an answer and a question | a finish with both an answer and question_for_user is sent back: ask (no answer) or answer (no question); an offer of more goes in suggestion (measured: the Nobel prize found and cited, then "Would you like more details?" - the question paused the turn and the answer was never shown) | contract | the next finish |
| main: a currency sign the data never gives | a reply after a data helper, with "$" (or €, £, ₹, ¥) on numbers no data, task or user message has: one hidden [App check]; a helper answer its own check warned about the sign is no evidence (measured: the analyst kept "$40,060", main copied it) | truth | write the numbers without it |

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
  and the tools' own tests - 664 in all, no model needed; one test (marked real_browser) opens pages in the
  headless browser when it is installed.

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
- **A timing flake, found and fixed (2026-10-21)**: test_open_figures_are_saved_once failed only in busy full runs.
  Each project's runs rebuilt matplotlib's font cache (1-4 s per new project); past 5 s matplotlib says so on
  stderr, and the chart read as an error. The cache is now one for every project, and the notice is not an error.
- **The model is remote**: Qwen 3.6 runs on a Kaggle notebook through a quick tunnel (kaggle/); a new session gets
  a new address (its Zen code, pasted from the dot by a chat's name: no restart), and while it is down the page
  says so - nothing else can answer.
- **The analyst can over-work a simple question** - five forecasts and three charts for "what sells best, and what
  might next month look like", then scripts to combine them (2026-10-22). A finish sent back on its last step now
  gets a step to fix it, so the work is no longer lost; how much it does before finishing is the model's.
- **A game that needs fixing often stays broken** (2026-10-21, "make me a small snake game", 5 runs): 2 worked as
  first written (one waits for a key, as the stricter check now asks; one throws after the first death); 1 chased a
  false alarm of the old check (fixed); 2 broke further with each edit - old_text that doesn't match, an edit
  without its file name - and ended saying plainly it still didn't work. The check is right each time; fixing by
  small edits is this model's weak spot.
- **A rewrite after a hidden check can answer the check**: "You're right - my apologies" opened one, to a user who
  had said nothing; the two checks that ask for a rewrite now say the user sees only the new reply.
