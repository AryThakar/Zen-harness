# Target map - the assistant as one system

Step 2 of the system pass. docs/SYSTEM_MAP.md is how things are; this is how they will be. Decided
with the user on 2026-10-04 where marked **(agreed)**; the rest is the proposal to confirm before
step 3 (building) starts. The old "AI Business Analyst" code is legacy and out of scope (kept until
the project is confirmed; nothing of it is used or changed here).

## 1. The idea in one paragraph

A **workspace is a project and a sandbox (agreed)**: its files, tables, images, notes, chats and one
search index live inside it, and nothing crosses its edge except through a card the user clicks.
**main** talks with the user and does quick things; four **helpers** do focused work, each with a
small set of **comprehensive tools** (one tool per family, a `kind` argument, few arguments because
column roles and names are resolved by code) and the **fundamentals of its craft in its own prompt**
(agreed). A helper can **ask the user directly** and continue where it was (agreed). Everything
the model can get wrong but code can guarantee - names, permissions, numbers, claims, retries,
context - is guaranteed by **one shared contract per concern**. **Vision** (Gemma's own projector,
loaded) lets main and the coder look at images instead of guessing.

```
User <-> UI <-> API <-> main (slot 0) ----------------------------- one turn at a time
                        |-- direct: files, save_note, get_weather, generate_image, draw_diagram,
                        |           calculate, ask_user, view_image
                        `-- helpers (slot 1), each: own tools + own ask_user -> checked result + report
                            |-- research_web  -> web       : web_search, read_webpage
                            |-- ask_knowledge -> knowledge : search_files, recall, read_file
                            |-- analyze_data  -> data      : inspect_data, clean_data, query_data, make_chart,
                            |                                compare, forecast, predict, find_patterns,
                            |                                analyze_text, run_code, fix_code
                            `-- write_code    -> coder     : run_code, fix_code, read_file, view_image
```

## 2. Shared contracts (built first; every tool and helper uses them)

| Concern | The one rule | Replaces |
|---|---|---|
| **Names** | One resolver for every tool: an item is reachable by id or by name (any case, a copied list line like "data_1  sales.csv - ..." too). Not found -> "No item 'x'. Did you mean: data_1 (sales.csv), file_2 (a.py)?" - bare id and name, never a whole list line. | file_ops._exact/_close, sandbox._resolve, ids-only tools |
| **Errors** | One error shape: what failed, why, what to do next. A ToolError raised anywhere (also inside a helper) reaches the model as its message; only real bugs say "internal error". | the @tool wrapper vs registry.execute split |
| **Results** | Tool results keep facts and a `next` hint; ML/stats results add an honesty block: compared with what, tested how, how reliable, saved tables. Cards go to the UI, never repeated by the model. | per-tool shapes |
| **Permissions** | Cards for: download a file, import a table from a link, delete, replace the user's own file. Scripts get **no network** (an audit-hook block on sockets), so data enters and leaves only through those cards. Writes only inside the workspace (as now). | open network in scripts |
| **Asking the user** | `ask_user` for main **and the data and coder helpers** (agreed): buttons with the recommended option first + Skip (= the stated default) + free text; at most 2 questions per helper task, before the heavy work; never for what the user already said; a helper waits inside the running turn and continues (no restart). | question_for_user -> main -> re-delegate -> _apply_choice |
| **Checks** | Helpers: as now (sources, numbers, report sections, last run). **main** gets: numbers in its answer must appear in a tool result or the user's words (one nudge, then a warning on the answer); "done" claims (saved, ran, printed, added, created) need a successful tool call in the turn; a reply that announces an action and calls nothing gets one nudge. The coder's report is checked against what its last run printed. | none for main |
| **Repeats** | The repeat guard (same call, same failure) for main too. | helpers only |
| **Context** | A thinking budget per reply (so an answer is always left), and a retry without thinking after an empty reply; hidden nudges never count as turns; recall finds the current chat's cut turns too (inside the workspace); the chat's start time is in its snapshot. | 4,096 shared tokens; context.turn_starts; recall's exclusion |
| **Prompts** | Three layers, each rule in exactly one place: the **role prompt** (who you are, how an expert works - the fundamentals, when to ask), the **tool description** (what it does, one line per kind, when to pick it), the **tool result** (what to do next). | the same rule in up to four places |
| **Items** | Every image (uploaded, generated, drawn by a script, edited) is an `image` item with width and height; every item has a report line when a helper worked on it. | uploads stored as plain files; "image NonexNone" |

## 3. main

Role: talk with the user, answer what it knows, do quick things itself, hand focused work to a helper
with a complete task, relay the helper's checked result briefly.

| Tool | Change |
|---|---|
| files | one reader for main and helpers (paging by lines with numbers for code, reports first); `start=0` read as 1; rename to the same name is refused; the resolver from section 2 |
| save_note, get_weather, draw_diagram | as now |
| generate_image | as now, then a quick `view_image` check when the prompt asked for something specific (text, a date, a count) before saying it is done |
| calculate | + a maths path: derivatives, integrals, equations, limits by sympy (exact), so "check with calculate" is possible for calculus |
| ask_user | as now |
| **view_image (new)** | look at an image item and answer a question about it (section 8) |
| research_web, ask_knowledge, analyze_data, write_code | descriptions name what each helper can do (e.g. analyze_data: "forecasts, predictions and what drives them, groups, unusual days, bought-together, text themes, charts") so questions reach the right helper |

Prompt: shorter - role, how to delegate, the honesty rules once; tool-specific rules move into the
tool descriptions.

## 4. Data helper (agreed shape)

12 tools + save_report: **data** inspect_data (+ column roles: date, entity, order, item, amount,
quantity, price, text; asked when unsure), clean_data, query_data (also plain arithmetic);
**visuals** make_chart (line with several series, grouped/stacked bar, scatter with colour and trend,
histogram, box, heatmap, pie up to 5); **ML/stats** compare (difference, correlation, association,
A/B), forecast (+ trend and seasonality, per group), predict (any target, drivers with direction, leak
check, scores saved; replaces train_model and predict), find_patterns (segments, unusual, together);
**DL** analyze_text (themes, classify, similar - local embeddings + Gemma naming); **code** run_code,
fix_code (custom cleaning and what no tool does; no longer advertised for ML or charts); ask_user
(e.g. chart kind when the task doesn't say). calculate and guide leave its toolset (arithmetic in
SQL; fundamentals in its prompt). Every tool works on data shape and roles, not a business domain.

Built (step 3.4, 2026-10-04): as above, with one change - roles are not asked about. They are
defaults shown by inspect_data, every result names the columns it used, and any tool takes a column
by name; asking would cost a question on most tables for a default that is right 21 times in 24 on
held-out files. Instead inspect_data asks the one thing only the user knows about rows: whether exact
repeats without an id are copies. Measurements: docs/ASSISTANT_PLAN.md, step 3.4.

## 5. Coder helper

| Tool | Change |
|---|---|
| run_code, fix_code | as now; no network; an image the run made is attached to the result so the coder sees what it drew |
| read_file | the one reader (section 3) |
| **view_image (new)** | look at an image item: a figure it made, or an image the user wants changed |
| **ask_user (new)** | e.g. which language or file format, or which of two readings, when the task leaves it open |
| save | as now (+ the report checked against the last run's output); an image saved by the coder becomes an image item; saving over the user's own image or file asks first |

Built (step 3.5, 2026-10-04): as above; an edited image is always a new image (replacing the
user's image in place is left out - nothing is lost, and the approval flow for it can come with the
UI step). Measurements: docs/ASSISTANT_PLAN.md, step 3.5.

Image editing (the user's idea): the coder opens an image by id in its script (Pillow), changes it
(text, size, colour, crop) and saves it - a new image, or in place with the user's approval - then
checks the result with view_image. HTML files it saves (a game, a page) get a "Preview" in the file
card (UI step).

## 6. Web helper

web_search, read_webpage. Fixes: raw+blob GitHub links repaired; a repository listing says "10 of N
files"; "latest"/"newest" questions take today's date from the opening; when it finds a downloadable
file it returns the link for main to download with the user's approval (it never downloads). No
ask_user: a web task's open points go back in the briefing (suggestion).

Built (step 3.7, 2026-10-05): as above; read_webpage also gives each page's age and the file links it
offers, the helper's result carries `downloads` (checked against what the tools returned), and pages it
read are credited when it lists no sources. Measurements: docs/ASSISTANT_PLAN.md, step 3.7.

## 7. Knowledge helper

search_files, recall, read_file - all inside the workspace. recall also finds the current chat's own
cut turns. No ask_user (it answers from what exists, or says it isn't there).

## 8. Vision - where it plugs in

How it runs: a vision call is one request to llama-server with the image and a question, on the
helpers' slot (idle while main works), returning text. Inside a helper's own task the image goes into
that helper's conversation instead. The projector stays loaded (230 MB, +330 MiB GPU; a chart read in
2.5 s); `--image-max-tokens` is tuned on real charts and photos in step 3.

| Where | What it does |
|---|---|
| main: `view_image(item, question)` | describe or answer about an image the user attached, a generated image, or a figure a script drew |
| main: after `generate_image` | check a requested detail is really in the picture before saying so |
| coder: run results + `view_image` | see the plot it drew; check an image it edited |
| UI: drop or paste an image | stored as an image item; main sees "[user attached image_3]" in the note and can look |
| data helper | not needed: make_chart returns the chart's numbers, which are what the answer needs |

Built (steps 3.5-3.6, 2026-10-04): all rows above except the UI's drop/paste (step 4). generate_image's
check runs inside the tool (the picture's description comes back with its result) rather than as a
separate call the model could skip. Detail: --image-min-tokens 280 --image-max-tokens 560, measured
(docs/ASSISTANT_PLAN.md, step 3.6).

## 9. User level (the UI step, after the code)

One project view: the workspace's files, tables, images, notes and chats together; helper questions
as cards inside the helper's step; a visible thinking indicator with elapsed time; image drop/paste
and image view; HTML preview in file cards; the chart kinds above; a clear "approved / refused" trail
on cards.

## 10. Code level (folded into each step, finished in the clean-up step)

- `app/assistant`: turn loop (main.py), helper runner (workers.py), context, store, and **prompts.py**
  holding every role prompt and nudge in one place.
- `app/tools`: grouped by family - workspace (files, notes, reports, resolver, approvals), data,
  ml, web, code (scratch, sandbox), media (image, diagram, vision), small (weather, calc, ask).
- `core.py`: ToolResult, ToolError, the result and error shapes, the resolver - imported by all.
- Docs: README and an architecture page describing this assistant; module docstrings kept true.
- Legacy product: untouched until the project is confirmed; then moved out of the app in the
  clean-up step, with the user's say-so. Done 2026-10-05: legacy/ (its own app, tests and benchmark);
  the shared pieces live in app/providers, app/rag, app/tools/sql_guard.py and table_reader.py. The
  tools stay one flat package (their family is in app/tools/__init__.py), not sub-packages.

## 11. Build order (step 3)

The user's rules for this step (2026-10-04): backend first - code cleaning, coherent prompts, synced
workflows, proper comments - **easy to hard**, then the UI, then docs and GitHub. Every tool gets
system help, and tools chain logically: when a result meets something another tool handles, the
result says which (a file read that finds an image points to view_image; the model never answers
"I can't read images").

0. Small fixes first: files start=0 and same-name rename; one resolver for names (copied list lines
   too); helper errors passed through; nudges not counted as turns; GitHub raw+blob links and "10 of
   N files"; finish-call markup stripped from plain answers; every image an image item with its size.
1. Shared contracts: resolver, errors, result shape, no network in scripts, image items. Tests.
2. main and context: thinking budget and retry, nudges not counted as turns, recall of the current
   chat, repeat guard, numbers / done-claim / announce-without-action checks.
3. Helpers' ask_user (pause in place) - used by data and coder.
4. Data helper: roles, make_chart kinds, compare, forecast + trend, predict, find_patterns,
   analyze_text; fundamentals prompt; tool tests on real data before any model run.
5. Coder: one reader, view_image, image editing, report-vs-output check.
6. Vision: view_image for main; generate_image check; token budget measured.
7. Web and knowledge fixes.
8. Prompts rewritten as one system (prompts.py), each rule in one place. Built 2026-10-05: role
   prompts and nudges in app/assistant/prompts.py, each tool's rules in its description, next steps in
   results (docs/ASSISTANT_PLAN.md, step 3.8).
9. One clean evaluation set (fresh tasks with unambiguous definitions, held out, 3 runs each) as the
   final check - not a driver. Moved after the UI (the user's order, 2026-10-05: clean-up and commit,
   then the UI).
Then step 4 (UI) and step 5 (clean-up).
