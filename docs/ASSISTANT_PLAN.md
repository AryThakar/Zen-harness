# Assistant plan: workers, files, memory, context

The agreed design from the 2026-10-03 discussion, kept in one place so nothing is missed while
building. Items marked **(to confirm)** wait for the user's yes.

## Principles

- **The system guarantees, the model is helped.** Safety, honesty of facts and file handling are
  done by code (checks, approvals, limits), never left to the model's good behaviour.
- **Tools and system first, prompting later.** Fix and freeze tools now; model-behaviour findings
  are parked (section 10) for the testing and prompting phase.
- **Measure before claiming.** Every step ends with tests; behaviour is measured on the real model
  with `bench/main/tool_probe.py` and the worker benchmark, not assumed.
- **Simple over autonomous.** Stateless workers, one file per coder task, one latest report per item.

## 1. Context and the KV cache

Any text that changes in the system prompt makes llama.cpp re-read everything after it (the whole
chat). Today the workspace listing (30 items), pinned notes and the "earlier turns not shown" note
sit in the system prompt and break the cache whenever the workspace changes.

- The system prompt becomes fully static: `main`'s instructions + tool specs. Always cached.
- Changing context is **appended to user messages**, the way the time stamp already is:
  - the chat's first message carries a small snapshot: the 5 most recent items (one line each, with
    their report line) + "N more: files(list)";
  - later messages carry only what changed since the last one ("[Workspace: added parabola.py]");
    most messages carry nothing;
  - when old turns are dropped (the prefix changes anyway), a fresh snapshot goes on the first kept message.
- When a long chat is cut down, the user's standing instructions ("always answer in metric") are kept
  instead of dropped (Unsloth's instruction-pin idea; our own code).
- The 5 latest notes are pinned automatically (section 3) and travel the same way: in the first
  message's snapshot, then "[Pinned notes now: ...]" only on the message after one changes.
- Thinking: earlier turns' thinking is already never stored or sent back (verified: only the answer
  text is collected, `app/providers/llamacpp.py:226`), as Gemma requires. Keep it that way;
  add a test that pins it.
- Measure cache reuse before/after from llama-server's own numbers (cached prompt tokens per request).

## 2. The `files()` tool for `main`

One tool, `mode` + its input. Replaces `import_table`, `download_file`, `delete_files`.

| mode | input | does |
|---|---|---|
| `list` | `page` | ~15 items per page: name, kind, report line; ends with "more: page=2" or "end of list" |
| `find` | `name` | fuzzy match: exists or not, plus close names (corrects invented names) |
| `read` | `name`, `start` | default: the item's report (notes/documents: their text); with `start`: content, 50 lines, "next: start=51" |
| `write` | `name`, `content` | a file `main` writes itself (report, summary, list, CSV/JSON); `.docx` names are converted from Markdown by code (python-docx); `.md`/`.txt` are indexed for search |
| `delete` | `name` | approval card (as today) |
| `download` | `link` | approval card; GitHub files of any type, data tables from any public site |

- A missing input gets an exact error ("read needs name").
- Overwrite rule (default taken; the user can change it): writing an existing name answers "exists:
  pick another name or replace"; replacing a file the AI made is allowed, replacing a file the user
  uploaded or downloaded asks for approval (no versions, so the original would be lost).
- Notes are not files: `files()` doesn't list, read or delete them (section 3).
- Approvals stay as built: the action runs by code after the user's click (`app/tools/approval.py`).

## 3. Notes: the AI's short memory, kept apart from files

- `save_note` saves short notes (a few lines), written in the third person. Saving with the same
  title updates the note; the model mostly reuses titles, so notes update naturally.
- **Not part of the file system**: `files()` doesn't show them.
- **The 5 latest notes are pinned automatically** (by last save or update), no approval needed: they
  are always in `main`'s view, each shown up to ~300 characters. The UI shows these 5 live pinned
  notes, so the user always sees what the AI keeps in view. Older notes are found by search.
- **Automatic pruning** when a note is saved: if the notes pass a limit on count (100), total length
  (50,000 characters) or age (a year since last update), the oldest (by last update) are removed
  until 20% of the limit is free. By code, no approval (they are the AI's own memory). Limits are
  settings, tuned later.
- **No automatic recall (auto-RAG)**: a small model over-trusts injected text, so a search match
  pushed into the prompt becomes a confident wrong answer. (The 5 pinned notes are not a search
  match: they are the AI's own latest notes, always the same set until one is saved.)

## 4. Search: relevance picks, time orders (Unsloth's approach)

Today: keyword (BM25) and meaning (embeddings) rankings fused by reciprocal rank fusion - no time at all.

- **Relevance alone picks** the results; time never decides what comes back, so a recent but
  unrelated passage can't get in.
- **Shown oldest to newest with dates**, under a header (two or more results) that a later one
  supersedes an earlier one ("supersedes", not "is the answer", so "what was it originally" still works).
- **Ties fetched from both ends**: when many passages score the same (a shared subject word), the
  candidates come half from the oldest end and half from the newest, so the newest is never cut off.
- Applies to earlier chats and notes, not documents.
- **Notes are their own search**: short notes lose to long passages in a shared ranking, so they are
  ranked separately and the best 1-2 matching notes are added after the document passages, never
  competing for the top results.

## 5. Workers: stateless, file + latest report

Any workspace item (code file or table) can carry one `report.md`, its latest only, replaced each
time a worker works on it. It is stored on the item, never as a separate workspace file (so it can
never be fed back in as work: no circle).

### The report
- **System facts** (the worker can't write them): item, date, last run passed/failed, output
  excerpt, images/outputs made, time.
- **Worker knowledge**, fixed sections, detailed (cap about 400 words):
  - coder: Purpose · How it works (functions, flow) · Inputs and outputs · How to run / change it · Limits and next steps
  - data: Question · Data used and cleaning decisions (e.g. "use data_4") · Method · Results · Caveats and next steps
- **Checks** (one chance to fix): sections present; coder: the functions named exist in the code;
  data: every number appears in a tool result.
- Indexed for `ask_knowledge`; its first line is the item's line in lists.

### Coder: one file per task
- Tools: `run_code(code)` (write the whole scratch file and run), `fix_code(from_line, to_line,
  new_code)` (change lines and run again), `read_file(item)` (a file and its report), `save(name,
  report)` (finish).
- A failed run shows the error with the numbered lines around it.
- A task that names a file: the file is loaded into the scratch file, its report goes first.
- `save` writes the scratch file under the name (same name = replaced), stores the report, links the
  last run's outputs (images; CSVs as tables). The saved code must have run cleanly, or the facts say "fails".
- `main` gets the report + facts, never the code. The chat gets a file card automatically: name,
  report (folded), code viewer with copy/download, output images shown, other outputs listed.

### Data analyst: scratch pad and a report on the table
- Tools: the 8 tested data tools, `run_code`/`fix_code` on a throwaway scratch file, `guide()`.
- A task that names a table: that table's report comes first (earlier decisions, which table to use).
- Ends with `save_report(report)` on the table the user named; it mentions derived tables. Nothing
  else persists except tables its tools made.

### Guides
- A detailed data analyst guide (and a smaller coder guide), written as "how an expert thinks":
  each section = when this applies · how to think (steps, questions to ask) · which tool / tested
  code · how to check the result · common mistakes (from measured failures) · a short worked example.
- `guide(query=...)` returns up to 3 hits as ID + title + one line, with "read one with
  guide(id='...')"; `guide(id=...)` returns that section (~300-500 words).
- A miss (below a score threshold) answers "nothing matches" + the guide's topic titles + "try one of
  these words, or go ahead with your own tools".
- At task start the system shows the 2-3 most relevant titles and IDs (~50 tokens).
- At most ~4 guide calls per task; then the tool says to use the tools.
- Every code block in a guide is run by a test.

Built as (step 8):
- The books are Markdown (`app/guides/data.md`, `coder.md`); each section has an id, a title, a
  `when:` line (shown in hits and suggestions) and `words:` (the words people use for it, for search
  only), then Think / Do / Check / Mistakes / Example. Examples use fixed example tables (orders,
  customers, site events), named in a footer the tool adds.
- Found three ways, because the guide is never pushed into the context: (1) the tool's description
  lists every section id (static, so it stays cached - Unsloth's catalog idea); (2) the task's
  opening names the 2-3 sections that match the task; (3) `guide(query)` searches. An exact id or
  title returns the section itself.
- Search: keywords (BM25 over id, title, words, when, body, code, weighted; word endings stripped;
  two-word phrases only from the authored fields; typo matching; a fixed boost for a word in a
  section's id/title/words) fused by rank with meaning (the app's bge-small embeddings of each
  section's title, when-line and words; weight 0.5, so keywords lead). A query misses when the best
  keyword section holds under a third of its words and no section is similar enough (0.6).
  Without the embedding model, keywords alone.
- Guide text is never evidence: its example numbers can't pass the numbers check. At most 4 guide
  calls per task.
- Code blocks: `sql` runs through query_data, `call` (a tool name + JSON) through the real tool,
  `python` through run_code, all on the example tables (a fresh copy per section); `html` is parsed;
  `text` blocks are examples. 62 data + 13 coder blocks run.
- Search measured (tests/test_guides.py): tuning sets 131 data + 50 coder queries (tuned on: 100%
  top-3, 99-100% first); held-out set 2, written before meaning search and never tuned on: data 92%
  in the top 3 / 82% first (keywords alone 88% / 80%), coder 100% / 100%. The first held-out set
  showed the overfit (data 80% / 78% with keywords) and led to the general fixes, not to aliases.
- No guide example may reproduce a benchmark task's answer (the coder's F30, 1229 primes and 380
  days were replaced).

### Limits
| | now | new |
|---|---|---|
| coder steps | 8 | 14 |
| data analyst steps | 10 | 16 |
| web / knowledge steps | 6 | 8 |
| script time | 30 s, max 120 s | 60 s, max 300 s |

Plus a time budget per task (~6 min) with a "time nearly up" note. Final numbers set from the
benchmark's measured times.

## 6. `main`

- Tools (11): `files`, `save_note`, `get_weather`, `generate_image`, `draw_diagram`, `calculate`,
  `ask_user`, `research_web`, `ask_knowledge`, `analyze_data(task, data?)`, `write_code(task, file?)`.
- Never paste or retype code: answer from the report; `files(read)` for the content when the report
  can't answer, or ask the coder with the file name.

## 7. Removals and simplifications

- Scripts writing stored files in place, and file versions (built on 2026-10-03): removed. The one
  way to change a file is the coder's `save` (or `files(write)`). Script writes are that run's outputs.
- `import_table`, `download_file`, `delete_files` folded into `files()`.
- Workspace listing and pinned notes out of the system prompt (section 1); pinning by the user's
  approval replaced by automatic pinning of the 5 latest notes (the pin button and `pin` input go).
- The coder's code no longer in its result to `main`; `calculate` off the coder's tools.

## 8. Build order (each step ends with tests)

1. Static system prompt; workspace snapshot/changes and the 5 pinned notes appended to user
   messages; standing instructions kept when a chat is cut; cache reuse measured.
2. Reports on items (storage, list line, indexing); remove script in-place writes and versions.
3. `files()` tool (list/find/read/write/delete/download) replacing the three tools; `.docx` writing.
4. Notes: automatic pinning of the 5 latest (live in the UI), automatic pruning, separate notes
   search; search shown oldest to newest with the supersede header, ties fetched from both ends.
5. Scratch-file tools (`run_code`, `fix_code`) with numbered error context.
6. Coder: `save` + report checks + linked outputs; `main` handoff without code; file card in the UI (browser check).
7. Data analyst: report fed in, `save_report`, checks.
8. Guides + `guide()` + the recipe test; auto-suggested titles at task start.
9. Limits and time budget; worker benchmark updated (coder judged on saved file + report + outputs) and run.
10. Prompting phase on the real model (section 10).

## 9. Done on 2026-10-03 (kept)

- Sandbox: matplotlib and a broad library set installed (the `sandbox` extra); scripts run as
  `script.py` (true line numbers); open figures saved after `plt.show()`; new workspaces run
  scripts; a missing module is said once; `n_jobs=-1` works; files outside the workspace can't be
  changed or deleted (16 ways tested); stored items can't be deleted by scripts.
- Approvals: download (GitHub), table import (any site), delete - card with the action's button and
  Cancel, run by code after the click; tested end to end.
- Weather `days` counts days ahead (1 = through tomorrow); the time stamp is the current time.
- Code blocks have a copy button; pinned-notes header says whose words they are.
- `bench/main/tool_probe.py` + `suites.py`: real-model probes through the real turn.

### Progress

- **Step 1 done** (2026-10-03): static system prompt; workspace snapshot/changes and the 5 pinned
  notes on user messages; standing instructions kept after a cut; pinned notes automatic (5 latest,
  shown live in the library), pruning by count/length/age. Measured (`bench/main/cache_probe.md`):
  after a workspace change the old layout re-read the whole chat (2,330-3,253 tokens), the new one
  only the new message (503-536); turns 2-6 read 9,345 vs 2,511 tokens.
- **Step 3 done** (2026-10-03): `files()` (list/find/read/write/delete/download) replaces
  `import_table`, `download_file`, `delete_files`; `main`'s snapshot shows the 5 newest items + "N
  more: files(mode='list')"; `.docx` from Markdown; replacing the user's own file asks first; a
  written file shows as a card (View, Download). Any file type can be written (only Python runs in
  the sandbox). Real model through the real turn (`bench/main/probe_files_run1.md`, `run2.md`):
  list 2/2 (the request that failed 8 times in the user's chat), find 2/2, read 2/2, write 2/2,
  .docx 2/2, delete and download 2/2 each paused for approval.
- **Step 2 done** (2026-10-03): each item can carry its latest `report.md` (beside the item, never an
  item; `Workspace.set_report`), its first line shown in lists and the library, searchable as "the
  report on X"; `files(read)` gives the report first, content with `start`. `files()` gained `update`
  (whole text or lines start-end; the user's own files ask first) and `rename`; `write` only makes new
  files. Scripts no longer change stored files (writing to a stored id is refused); versions removed.
  The app's bracketed notes are stripped from replies (the model copied one). Real model, 8 modes,
  22 runs (`probe_files_eight.md`): list, find, read, write, update (asked for the user's file), line
  update (once via the coder), rename, delete, download, questions answered from report lines - all
  right; the Word case named the file .md 3 of 6 times (parked).
- **Step 4 done** (2026-10-03): notes are searched apart and added after the document passages
  (best 1-2, real matches only, oldest first); earlier chats and notes come back oldest first with
  "a later one supersedes an earlier one"; ties in the ranking keep the newest and the oldest (both
  ends fetched, Unsloth's approach). Pinned notes are citable ("note 'Title' (note_1)") and count as
  evidence for the knowledge helper. Knowledge worker benchmark on the real model: 6/7 before the
  pinned-note fix (it answered from a pinned note but cited the header), 7/7 after.
- **Step 5 done** (2026-10-03): `run_code` (write the whole scratch script and run) and `fix_code`
  (change lines from-to and run again) replace `run_python` in both helpers; a failed run shows the
  numbered lines around the line the error points at; the scratch script is new for every task.
  Real model: coder 7/7 (c_fix fixed the bug with fix_code), data 10/11 then 11/11. h_forecast had
  the model building dates by hand although inspect_data said forecast combines year + quarter: the
  forecast tool's `date` now says so too, and a script querying `data_1` through duckdb gets "use
  table('data_1')". It also exposed a crash from step 4 - the helpers' trace read any result's
  "notes" as search notes, and forecast/make_chart/clean_data/train_model return plain-text notes -
  fixed and tested; h_forecast then 3/3.
- **Step 6 done** (2026-10-03): the coder finishes with `save(name, purpose, how_it_works,
  inputs_outputs, how_to_run, limits, code?)`; the app writes the report's facts (last run passed or
  failed, what it printed, what it made) and links the run's outputs to the file. Python is always run
  before it is kept; other languages are kept as written ("not run"). main gets the report, never the
  code; the chat shows the file card (View, Download, output images, the report folded). `write_code`
  takes `file` (loaded into the scratch script, its report first); a code file the coder reads becomes
  its scratch script; saving over the user's own file waits for their approval. If the coder ends
  without save but its script ran, the app keeps the script (flagged). Coder benchmark 8 tasks (new
  c_edit changes the user's stats.py): 7/8, then c_fix 4/4 after the read-becomes-scratch fix;
  h_emails once ended without save (now kept by the safety net). main + real coder end to end
  (`probe_coder_e2e.md`): new script, change squares.py to cubes (same file), Rust (kept, not run)
  2/2 each, no code pasted; "what does squares.py do?" retyped code from memory once (parked).
- **Step 7 done** (2026-10-04): the data analyst finishes with `save_report(question, data_used,
  method, results, caveats)`; the app adds facts (table, tables made, charts, tools run) and keeps the
  report on the table the task was about (`analyze_data`'s data, else the first table it inspected);
  the next task on that table starts from it. Every section must be written and every number in
  data used / results / caveats must come from a tool result (one chance to fix). main gets the
  results as its answer plus the report; the chat shows a report card. A plain-text answer gets one
  nudge to save_report, then is still checked and used, with a note that no report was kept.
  Shared report layout in `app/tools/reports.py`. Real model: data benchmark 10/11, h_corr failed on
  "a 95% range" - stats_test called it range_95, so the 95 was invisible to the numbers check; it now
  says "confidence": "95%" (h_corr 3/3). main + real analyst end to end (`probe_analyst_e2e.md`):
  total sales and busiest day 2/2 each, report kept, card shown. (The model and API servers had been
  stopped by the desktop app overnight; restarted.)
- **Step 8 built, not frozen** (2026-10-04): guides for the data analyst (30 sections) and the coder
  (13), the guide tool, task-start suggestions, every recipe run by tests/test_guides.py (151 tests).
  Search on held-out queries: data 92% in the top 3, 82-85% first; coder 100%. Real model: data
  benchmark 10/11 (as before), coder 8/8 (was 7/8). Harder tasks (pizza): 7/12 with the first guide vs
  8/12 without; after writing each trap into the SQL as a comment and adding answer-shaped recipes,
  10/12 - on the tasks the revision was made from, so not a clean measure. Fresh tasks on other data
  (cdnow, coffee): 1/18 with the guide, 3/18 without - most misses come from the tasks' own definitions
  and inspect_data's suggestions (dropping copies that are real purchases), not the guide; to be
  redone in the system pass. The model reads exactly one section, nearly always the first one the
  opening names. clean_data now reads steps written as {op: {...}} (the analyst wrote them that way and
  the validator's message told it nothing). Gemma's vision projector is loaded (audio removed, 230 MB,
  2.5 s for a chart, read correctly).
- **Direction from 2026-10-04 (the user's decision)**: stop the benchmark-and-patch loop. Order:
  (1) map the current system - docs/SYSTEM_MAP.md; (2) design one coherent target map, the whole
  system as one piece (view_image included); (3) fixes, restructuring, prompts and tools from that
  map; (4) UI; (5) clean-up: GitHub, docs, code, bugs found while testing. Benchmarks become a final
  check, not the driver. The target design is docs/TARGET_MAP.md (shared contracts, main, the four
  helpers with comprehensive tools and their own ask_user, vision, build order).
- **System pass, step 3.0-3.2 done** (2026-10-04; docs/TARGET_MAP.md section 11):
  - One resolver for item names (Workspace.find/resolve): every tool takes an id, a name or a copied
    listing line; registry.execute resolves every `data`, `model` and read_file `file` input; "not
    found" gives the closest items as "id (name)", a wrong kind says what reads it.
  - A ToolError raised anywhere keeps its message (helpers no longer show "internal error").
  - files: start=0 reads from line 1; a rename to the same name is refused.
  - Every image is an image item with its size (uploads were plain files; script images "NonexNone").
  - Scripts have no network (DNS and connections refused by the audit hook; a socket pair inside the
    process still works); requests/bs4 no longer offered to scripts.
  - GitHub raw+blob links repaired; long repository listings say "10 of N".
  - A finish call written as markup comes off a helper's plain answer.
  - Hidden nudges never start the context window; recall finds the current chat's cut turns; after a
    cut the snapshot says when the chat started.
  - Thinking (the user's choice, option C): no budget - a cut mid-thought can break the model's
    work, and only 2 of 323 messages thought until the limit. A reply that thinks until the limit and
    says nothing shows a card; "Try again, answer quickly" retries that one call with a 2,048-token
    thinking budget, Skip ends the turn. Measured on such a question (hard integral, 4,096-token
    replies): no budget -> 4,093 tokens of thinking, no answer; "think briefly" in the message -> cut
    off mid-answer; 2K budget -> thinking cut mid-sentence, then a complete answer in 45 s (wrong -
    a 4B model's maths limit, as with thinking off). Each reply's thinking length is now kept in its
    stored usage.
  - main's checks (app/assistant/checks.py): the same failed call isn't run twice in a turn; a helper
    task with 3+ numbers no tool returned and the user never gave is refused; a reply that announces
    an action without a tool call, or claims one when no tool did anything, gets one hidden nudge.
    On the 188 replies stored so far the announcement detector catches the 9 real ones and nothing
    else (tuned on that set; to be confirmed on new chats).
  - Tests: 739 pass (tests/test_contracts.py and tests/test_main_checks.py new).
- **Step 3.3 done** (2026-10-04): the data and coder helpers ask the user in place (ask_user, at
  most 2 a task, the recommended option first): the card shows in the chat, the helper waits inside
  the running turn and goes on with its work (main.Turn.ask, Live.follow stops a request at the
  waiting question, POST /answer hands the answer over and streams the rest); skipped or unanswered
  (10 min) -> the recommended option, said in the report. inspect_data's questions (date order) are
  asked by the runner itself and the picked option's cleaning step applied by code. Real model in the
  browser: "total sales in February 2025?" on day-first dates -> card -> Day first -> data_2 cleaned
  -> 636 (correct), one delegation; a reload shows the card answered. 741 tests pass.
- **Step 3.4 done** (2026-10-04): the data helper's 11 tools + ask_user + save_report, each proven by
  direct calls on 12 real public tables (coffee, pizza, CDNOW, Online Retail 525k rows, 8 seaborn/R
  tables) before any model run:
  - Column roles (app/tools/roles.py): date, order, item, entity, amount, quantity, price, text, from
    generic column words confirmed by the values; the defaults the other tools use, shown by
    inspect_data with how money combines ("no amount column: quantity * price"). Tuning tables 24/25;
    held-out files 16/24 on the first run, 21/24 after generic fixes (no longer held out); the misses
    left are gaps, not wrong defaults.
  - inspect_data: codes (ids, invoice and stock codes, leading zeros) are never suggested as numbers;
    rows that repeat exactly are dropped only when an id repeats with them - otherwise the user is
    asked (titanic would have lost 107 real passengers).
  - query_data does the arithmetic (calculate left the helper); exact decimals come back as numbers.
  - make_chart: line/bar/scatter split by a group, stacked bars, scatter trend line, box, heatmap,
    pie; matched pandas on real data; the UI draws every kind (checked in the browser).
  - compare (was stats_test) + A/B: Newcombe range and z-test equal statsmodels; A/A false alarms
    6.8% of 400 (5% expected); a 5% -> 6% lift found 88% of the time (theory 87%).
  - forecast + trend (Mann-Kendall/Theil-Sen, ported) + seasonality (peak/low) + vs a year earlier +
    by group: false alarms 3% on noise, 10% on random walks, planted trend found 97%; flights: rising,
    peak July. Year + month-name columns are combined.
  - predict (train_model + predict in one): scores saved out of sample (their AUC equals the reported
    one), drivers with a direction from partial dependence (diamond clarity no longer confounded by
    size), leaks named for yes/no and category copies (titanic 'alive').
  - find_patterns: segments (CDNOW: one-time vs repeat buyers), unusual periods/rows (pizza closed
    days, retail Christmas, diamonds' bad table/depth values), together (shuffled baskets 0 pairs;
    first-half pairs hold in the second half 9/10).
  - analyze_text (bge-small): similar strong (top 10 all relevant); themes readable on product
    names; classify 70% on held-out labels with a meaning, 29-53% on a business's own codes (stated
    in its result).
  - The data guide left the helper: its craft is a fundamentals block in its prompt (prompt + 13 tool
    schemas = 3,926 tokens). The guide files stay dormant until the clean-up step (never committed:
    deleting them is the user's call); the coder keeps its guide until step 3.5.
  - Real model, one task per ability (a smoke check, not tuned on): 6/8 right on the first run; the 2
    failures were tool-side (Gemma's quoted keys inside clean_data steps - now unquoted where calls
    are read - and the inspect_data suggestions above). Second run: 8/8 right answers; its two
    detours were made unnecessary in the tools - compare(difference) on 2 groups and a yes/no outcome
    runs the A/B test, and make_chart ignores a by that repeats x.
  - Tests: 766 pass.
- **Step 3.5 done** (2026-10-04): the coder.
  - One reader (app/tools/read_file.read) for main's files(read) and the helpers' read_file: lines from
    `start`, code numbered, documents with [page N], the report first (alone for main); an image or a
    table read says which tool reads it (view_image; the data tools or table('data_1')).
  - Vision: view_image(image, question) - Gemma's own projector on llama-server, the helpers' slot,
    thinking off, images at most 1,024 px. Measured on a real chart: 3-4 s and about 140 image tokens a
    question; it named the 3 lines and the legend and read a peak as "roughly 600" (622): every answer
    says it is rough for exact numbers. Wired through ToolContext.look, so main gets it in step 3.6.
  - Image editing: a script opens an image by its id (Image.open('image_3')) and saves the changed one
    as a new image item; a run that draws or edits a picture points to view_image, and save sends the
    coder back once if it never looked (seen: a chart saved unseen).
  - The report is checked against the code and what the runs printed: a number in its purpose, how it
    works or inputs/outputs that is in neither (nor the task, nor a tool result) gets one chance to fix.
    On 14 real reports those sections had none; change examples are not checked (one legitimate
    'print(fibonacci(45))').
  - The coder's guide left too: its craft is a fundamentals block in its prompt (prompt + 6 tool
    schemas = 1,864 tokens); the guide hooks are gone from the runner, the registry and the benchmark;
    the books stay dormant until clean-up.
  - Real model, one task per ability: 5/5 right (primes, a fix to the user's file - its save asked the
    user first, as designed - a chart from a table, an image captioned and widened and checked with
    view_image, an HTML page kept without running); the chart was saved unseen, now enforced, re-run:
    looked first. Tests: 770 pass.
- **Step 3.6 done** (2026-10-04): vision for main.
  - main has view_image (on the helpers' slot, idle while main works); its prompt says it can't see
    images otherwise and should look before describing one. A mix-up (an image given as a file or a
    table) points to view_image.
  - generate_image looks at its own picture and returns what it shows, its text quoted, with "compare
    this with what the user asked". On images generated earlier in the user's chats: the requested
    overlay text was read back exactly; the "JK flip-flop diagram" was seen as "a bright square with a
    stylized silhouette of a person" - the miss main could not see before. 1.5 s a look; a failed look
    never costs the image.
  - Image detail (--image-min-tokens / --image-max-tokens, .claude/launch.json): a fixed set of 5 test
    images with 10 known answers (chart, text, table, receipt, shapes): default (100-190 tokens an
    image) 7/10; max 560 alone changed nothing (the images are below it) 8/10; min 280 (about 310
    tokens) 8/10 twice; min 560 (about 600) 7/10 and slower. The two misses that stay are the model's
    limits (two near-equal bars compared, one letter of small text), not detail. Kept: min 280 / max
    560 - no measurable gain over the default on this set, chosen so small screenshots aren't read from
    ~100 tokens, at no speed cost; the cap bounds large photos.
  - Real app, end to end: a receipt attached, "what is the total, and which item cost the most?" ->
    main called view_image itself -> 38.47 and Coffee beans 500g at 12.99 (both right), 9 s.
  - Why a tool, not the image in main's input (the user asked): one path for every image (attached,
    generated, drawn or edited mid-task), ~50 tokens of answer in main's 16K window instead of ~300 of
    image in every later turn, a visible step, and a tool result the numbers checks can use. Putting an
    attached image straight into that message's input is to be measured in the UI step.
- **Step 3.7 done** (2026-10-05): the web and knowledge helpers, from their real record in the user's
  chats (research_web 39 runs: 5 answers with warnings, 1 failure - stopped by the user; ask_knowledge
  8 runs: 1 warning).
  - read_webpage gives a page's age, worked out by code ("updated 2026-03-02, 7 months ago" - from
    its published / modified dates), and the files the page links to that main can download (data
    tables from any site, GitHub files). The web prompt: 'latest' means this year's date, the
    official releases page, each page's age, and "as of" in the answer.
  - Downloads: the web helper's result has `downloads` - links to files the task wants, each one a
    tool returned (else refused once); a file link written into the answer is moved there by code;
    main gets them with "files(mode='download', link=...): the user approves it first". It never
    downloads itself.
  - Pages it read are credited as sources when it lists none (3 of the 5 web warnings).
  - Knowledge: recall finds the current chat's cut turns (3.2), read_file is the one reader (3.5);
    its one warning was an answer its sources didn't support - the check doing its job, left as is.
  - Real model + live web: "latest Python" -> 3.14.8, released 30 September 2026, from python.org's
    own page; "Iris CSV link" -> first in the answer only (now moved by code), re-run: two GitHub links
    in downloads, and main's files(download) turns one into "Download iris.csv (2.7 KB) from GitHub
    into this workspace?"; a GitHub file's version -> 2.34.2 with its source. Tests: 775 pass.
- **Step 3.8 done** (2026-10-05): the prompts as one system. `app/assistant/prompts.py` holds every
  role prompt (main, web, knowledge, data, coder) and the runners' nudges (cut off, steps left, fix
  and answer again, announced / claimed but not done, "the user sees this card"). Three layers, each
  rule in one of them: the role prompt (who, how an expert works, how to read the app's notes, the
  honesty rules), the tool description (what it does, when to pick it over another), the tool result
  (what to do next, only when it applies). Found by reading all of it - rules that were in two or
  three places, each now in one:
  - main's prompt repeated its tools' descriptions (files' modes and Allow button, save_note, calculate,
    ask_user, view_image): those rules now live only in the descriptions; routing rules too (diagram vs
    image, maths vs write_code, own files before the web: in draw_diagram/generate_image, calculate and
    write_code, ask_knowledge). "Don't do it another way" after a refusal is in the refusal's result only;
    "give the helper the task again" in the helper's result only.
  - The script functions (table(), open(), path()), "files the script writes are kept" and "no
    internet" were in the coder's prompt, run_code's description and its field: now run_code's
    description only. Inserting with fix_code: its field only. Changing a given file: said with the file.
    Looking at a drawn picture: the run's result and the save check. The report's sections: the save
    fields only.
  - The data prompt's chart-kind list duplicated make_chart's description (kept there); SELECT
    arithmetic without a table moved to query_data's description; "use an earlier report's decisions"
    is said where the report is shown. ask_user's "when" is the helper ask_user's description only.
  - Web: "snippets are not enough" (prompt, description, result: kept in the result), "read again
    with find" (the cut page's result), "quote only this page" (dropped from every page read: the prompt
    says it), downloads (the field and the page's file list). Knowledge: "answer only from these
    passages" dropped from search_files' result (the prompt says it).
  - Two parked items handled where they belong: save_note's description says a note is the only way to
    keep something for later chats ("I'll remember that" without a call, in the probe), and the claim
    nudge says plainly that nothing was done (it had been answered "the action was performed").
    research_web's description: not for explaining what main already knows.
  - Fixed prompt sizes (exact, llama-server): main 2,804 -> 2,470 tokens, web 807 -> 700, knowledge
    574 -> 545, data 3,922 -> 3,829, coder 1,862 -> 1,616.
  - tests/test_prompts.py: no sentence reaches one model twice (its prompt, its tools' descriptions and
    fields); the runners read prompts.py. Tests: 777 pass.
  - Real model, main's probe suites before / after (2 runs a case, helpers as stand-ins; tuning cases,
    so a regression check, not a score): 103 -> 108 of 126 runs. Better: pin 1 -> 2, save the code as a
    file 1 -> 2, ask on a vague request 1 -> 2, list all files 1 -> 2, a file's report 0 -> 1, no image
    for a description 1 -> 2 (its miss before was the server under load). Worse: "the states of an
    online order" drawn as a flowchart, not a state diagram (2/2 -> 0/2; with draw_diagram's kinds first
    again, 1/3). Same: weather, routing 20/20, import 5/6 (cases updated to the files tool first).
    Helpers' smoke checks: data 8/8, coder 4/5 (the image-caption task, as before), web 3/3.
  - "Explain how X works" went to research_web 0/2 before and after; "answer what you already know
    yourself" moved from research_web's description into main's role: 1/3. Left there, not chased.
  - Probe cases brought up to date (bench/main/suites.py): import_table and set_pin no longer exist,
    the date check used a fixed day.
  - main's persona (the user's request): main's prompt opens with who it is - Zen (ASSISTANT_NAME),
    talking with Arya (ASSISTANT_USER_NAME, in .env; none = "the user") - its manner (warm, direct,
    answer first, match the user's tone, a few words and a fix when wrong, its view when asked), what
    it says it is (a local open model, not ChatGPT or Claude), and how it takes a failure (try once
    more another way, else say plainly what didn't work and what could; never raw errors or JSON). The
    rule for a helper's failed trail is that one rule now. Probe suite "persona" (bench/main/suites.py):
    12/12 replies right (one counted as a miss by a too-narrow pattern: "I wasn't able to find ...",
    since widened) - greets Arya by name, says it is Zen on a local open model and not ChatGPT, an
    unknown place or a missing file explained in plain words with no raw error, "you're wrong" -> one
    line and 144, an upset user met kindly. Names 7/8 (the miss: "I will remember that" with no note,
    as before). main's fixed prompt: 2,470 -> 2,700 tokens. Tests: 777 pass.
  - "Remember X" (the user's rule, made one place): main's prompt says notes are its memory between
    chats - asked to remember, note, or keep something for every chat, it calls save_note; and a
    check backs it (checks.py): the user asked to remember and the reply saved nothing -> one hidden
    nudge to call save_note. Found on the way: with the persona, a plain bracketed nudge was answered
    as if the user had said it ("You are right to point that out, Arya ... I've corrected that") - an
    apology and no tool call. The nudges now start with [App check], and main's prompt says to act on
    one, not reply to it. Real model: notes cases 10/10 (names 1/2 and metric 0/2 before), persona
    spot checks 3/3. Tests: 779 pass.
- **Clean-up before the UI** (2026-10-05, the user's order: clean the backend, prompts, comments,
  commit, then the UI; the held-out evaluation, step 3.9, waits until after the UI):
  - Every tool's description says when to pick it in the user's words ("Use it when the user asks
    about the weather ... / for a picture ... / what files they have ..."), the user's notes rule made
    the pattern for all tools. Every hidden app message starts with [App check].
  - The guides are deleted (app/guides, app/tools/guide.py, their tests): no helper used them since
    3.4/3.5; the one test that matters - each helper's craft is in its prompt - is in test_prompts.py.
    ToolResult.text (prose results, only the guide used it) is gone.
  - The legacy "AI Business Analyst" moved to legacy/ (git mv, history kept): its agent, API,
    analytics, db, ingestion, ml, document library, Streamlit UI, benchmark and tests, runnable as
    `uvicorn legacy.main:app --port 8001` (legacy/README.md). app/ is the assistant only; what both
    use has its own home - app/providers (the model providers; Claude went with legacy),
    app/tools/sql_guard.py, app/tools/table_reader.py, app/rag (chunking, embedding, extraction,
    ranking). app/main.py serves the assistant alone. Tests: 631 pass (the guides' ~150 went with
    them).
  - Docstrings made true (main.py, workers.py, the package overviews); one unreferenced helper
    (roles.role_column) removed.
  - Real model after all of it (one run a case, 24 cases across routing, weather, diagrams, images,
    files, persona): 22/24. "Explain how forecasts are made" answered directly; the circuit request
    didn't reach generate_image - but its reply held the call written as JSON text, shown as it was: the
    call healer (app/providers/gemma_calls.py) now reads that form too. Misses: a file deleted after
    a find (right, the case wants delete first), and the order-states diagram written as Mermaid in the
    reply instead of draw_diagram (parked, as below).
  - A read-through of the whole backend in the order a request flows (the user's order: read the code
    and workflows, not only the tests): API, turn loop, store, workspace, main's tools, the helper
    runner, the sandbox, web, knowledge and data tools, the provider. Found and fixed, each with a test:
    1. The Workspace object is shared by the turn and the API's requests, unguarded: without a lock,
       8 threads making 25 items each crashed 5 runs of 5 (two registry saves meeting on Windows) and
       lost items - an upload during a script's run could do it. Every registry change and walk now
       holds its lock.
    2. files wrote text under a binary name (.pdf, .xlsx, .png ...): a broken file with a good name.
       Refused, also for update and rename (before the user is asked to approve).
    3. clean_data's outliers with half the values equal (IQR 0): the fences sat on that value and
       remove_outliers dropped every other row. Now none are found, and the log says why.
    4. The SQL guard's default allowlist was the legacy product's tables: now the caller's, required;
       legacy keeps its own (legacy/agent/sql_guard.py).
    5. HTTP clients opened per call and never closed (weather; the UI's favicons and preview images).
    6. A message typed while a helper waits in place went to a new turn and was refused: it is the
       answer now (the page already sent it to /answer).
    7. Wording: an [App check] says "tell the user" when it can't be done - "don't reply to it" said
       the opposite; the data prompt's "Its roles" named no tool.
    8. Tables a script writes are marked as the assistant's, like its images and files.
    Known and left: the public-address check resolves a name once (a hostile DNS could answer
    differently on the connection - local, single-user app); view_image on the helpers' slot replaces
    that slot's cached prompt (two slots); the sandbox folder keeps old scripts' files. Tests: 635 pass.
- **Helpers see pictures directly** (2026-10-05, the user's question: the model takes images as input -
  why a tool?). Checked on the real server first: an image in a user message inside a tool conversation
  is accepted, the token count includes it (+290 for a chart) and the next call reuses the cache (2,092
  tokens cached, 11 new). On the step 3.6 set (10 known answers): in the coder's own conversation 9/10
  (the miss: "1284" for "1,284"), the separate view_image call 8/10. With view_image still among its tools
  the coder called it 8 times of 10 instead of reading the picture it had - so it left the helpers.
  - The runner shows the coder what its script drew, an image it read with read_file and an image the
    task names (by id or name), as images in its conversation with a one-line note (prompts.PICTURES);
    at most 2 in view (context.fewer_images), older ones named. Only when the server has vision
    (LlamaCppProvider.sees_images: /props). The "never looked at it" save check went with view_image.
  - main keeps view_image: a long chat would carry an image through every later turn.
  - Real coder, end to end: the chart task 24 s, clean (69 and 92 s before); the image-caption task 30 s
    (298 and 566 s before, with 4 view_image calls) - but it named its save after the picture
    ('passengers_modified.png') and the save was refused. A save named after a picture or other binary
    file now keeps the script as name.py (the picture is kept already): re-run 41 s, clean, image 1600 px
    wide with the caption, script and report saved. Tests: 639 pass.
- From the user's own test chat: `main` refused 8 times to list workspace files ("I can't list
  files"), misled by its line "You can't read, list or change files on the user's computer"; the line
  now says what is allowed. The real fix is `files(list)` (step 3). The coder can't run non-Python
  code (Rust): step 6 lets it save such a file without running it, said plainly in the facts.

## 10. Parked for the prompting phase (measured, not fixed)

- "Make a Word document": the file was named .md 3 of 6 times (with a note to convert it).
- The knowledge helper sometimes cites a pinned note as an extra source next to the real one (h_bulk).
- main retyped a file's code from memory when asked what it does (1 of 2), and repeats the coder's
  report in its answer (with "image_1.png" as text) instead of a short summary.
- The coder sometimes ends in prose ("here is the save") instead of calling save, even after the nudge.
- The analyst's report title (question) is often a long sentence; it is also the table's list line.
- Refusing a direct, harmless order to call a tool ("use the coder to list the files"), and writing
  the opposite into the helper's task (8 turns in the user's test chat).

- "remember X" often answered "I saved a note" without calling `save_note` (4/6 runs). After step
  3.8: fiscal 2/2, metric 2/2, names 1/2 - the miss says "I will remember that", a promise the
  claim check does not catch (it reads past tense only). Fixed in 3.8 (remember rule + its check): 10/10.
- "explain how ..." questions go to `research_web` (3/3) instead of answering directly. After 3.8: 1/3.
- "Draw an image of a JK flip flop circuit" -> generate_image (0/2 before and after 3.8), although its
  description says not for circuits: an explicit "image" outweighs it.
- "who am I talking to" ignores the pinned name (2/3). After 3.8: 2/2, and "what is my/your name" 2/2.
- "what's the weather" sometimes asks now-or-forecast instead of checking (1/3).
- Maths: long numeric chains done in the head or in one long expression (gain ratio 0/2).
