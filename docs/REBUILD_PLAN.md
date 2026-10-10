# The rebuild plan - frozen 2026-10-10

Why: the parts were built one at a time and never shared a model of the user's things. A file has a
different name for each reader (the user, main, a helper, a script, the search index); there are eight
ways to create an item, each with its own naming; a chart exists in three forms; memory is a separate
kind of thing (notes) pinned by date; work done by a helper is lost when it stops. The model was then
blamed - and patched around - for what the design did to it.

This plan replaces those parts with a few building blocks that every reader shares. It covers every
concern raised so far (chats of 2026-10-03 to 2026-10-10, docs/SYSTEM_MAP.md). **After it, work is an
addition on these blocks or measured tuning - not another redesign.** A problem found later belongs to
one block and is fixed there. What the model still gets wrong after it is measured and stated as a
limit of the model.

## 1. Why things went wrong - the causes, with evidence

| What the user saw | Cause in the design | Evidence |
|---|---|---|
| Files not "one file": wrong names, duplicates, scripts can't find them | ids (image_27), names and the script folder are three different things; 8 creation paths name things differently | the helper opened 'image_27.jpg' and crashed; 8 uploads named image.png; one screenshot became 3 items; titanic.csv twice |
| Can't point at a workspace file in the chat | nothing in the message box refers to stored files | - |
| Main can't find things | files: list (15 a page) or an exact name; search exists but only the knowledge helper has it | "how many lion images": 2 of 4 pages read, answered 2; the answer was 5 |
| Notes not usable | a separate kind and tool; the 5 *latest* pinned, by date | a standing preference falls out of view after 5 newer notes |
| Code rewritten again and again | one throw-away scratch script per task; fix_code fenced to errors; old scripts stay whole in the context | panda: 8 full rewrites, 15 min; math: 5 rewrites then 16,388 > 16,384 tokens, all lost |
| Work lost on Stop or failure | a stopped call is dropped from main's history; a failed helper returns only its error | the math answer was found 95 s in; main resent the task 3 times from zero |
| Doesn't know its environment | run_code's description doesn't say Windows, fonts, libraries | scripts looked for /usr/share/fonts; Windows fonts load fine (tested) |
| Charts twice, and as images | make_chart draws a live card *and* saves a PNG item; the answer embeds the PNG; grouped histograms refused, so matplotlib | 10 of 39 images are chart copies; the age histogram drawn by seaborn |
| Simple maths sent to a helper | calculate is arithmetic only: no solving, derivatives, integrals | 10 maths tasks sent to helpers since 3 Oct; the calculus problem took 8 min there |
| ML tools never used | described in our words, not the task's; main writes the method into the task ("train a Random Forest") | 37 chats: predict, forecast, compare, find_patterns, analyze_text used 0 times; run_code 24 |
| Answers fixed after the fact | each helper has its own result shape; the model writes facts the app knows (method, data used) | finish refused twice for a missing 'method' on a correct answer |
| A frozen cursor | a hidden check's state is shown at the top of the reply; the end blinks | a 48 s re-check looked like a hang; the user stopped it |
| A tool that cost more than it gave | plan: a checklist main must write before working | 31.6 s of thinking for a plan, then one call for its 3 steps |

Why each skipped tool was skipped, read from its description and prompt:
- **calculate**: "Maths problems: solve them yourself ... and check the numbers here" - but it cannot check
  the hard part (roots, integrals), so the honest route was a helper.
- **predict**: "Learn a column from the others" - it never says machine learning, classifier, accuracy,
  train/test, cross-validation, feature importance, gradient boosting, Random Forest. A task in those words
  matched run_code ("what none of these does") and the model's habit (scikit-learn).
- **compare / forecast / find_patterns / analyze_text**: the same - no "t-test, p-value, significance",
  "time series", "clustering, k-means, anomaly, market basket", "topics, sentiment, categorise".
- **analyze_or_code**: its task field ("what to do") invites a recipe; main wrote methods and code steps,
  and the helper followed them.
- **files / search_files / read_file / notes**: four tools over the same things, split by reader.

## 2. The building blocks

### 2.1 Files: one folder, one path per file, for everyone

- The workspace is a real folder. **A file's path is its only name** - in the file panel, an @ in the
  message box, main's calls, a helper's calls, a script (`open('panda.jpg')`, `pd.read_csv('sales.csv')`),
  search results, reports and answers. No model-facing ids (data_1, image_27) anywhere.
- **One write path.** Every way a file arrives or changes - upload, download, generated image, a tool's
  table, a chart, a script's output, an edit - goes through one function that: keeps names unique,
  recognises identical content (an identical upload is the existing file: "already here as x"), writes
  atomically, keeps the previous version, updates the search index, records lineage (made from what, by
  whom) and shows the card. Nothing else writes to the workspace.
- **Names**: an upload keeps its name; a picture that arrives unnamed (pasted "image.png") is named from a
  one-line caption the vision model writes (the caption is kept and indexed); a generated image is named
  from a short title, not the first 80 characters of its prompt.
- **Tables are their CSV or Excel files**; the fast copy the data tools use is a hidden cache. A tool that
  makes a table writes a new CSV beside its source (`titanic-cleaned.csv`), with its lineage.
- **Versions**: an edit or overwrite keeps the previous version (the last 10); the file panel restores one.
- **Folders** are allowed; charts go in `charts/`. `agent.md` sits at the top.
- **Permissions are unchanged**: scripts work inside the workspace only, can't change a file the user
  brought (that is files(edit), which asks), can't delete, and have no internet; deleting and downloading
  ask with a button.

### 2.2 Pointing at files, pins, and agent.md

- **@ (one message)**: the message box offers the workspace's files and folders; the message carries the
  file's path, so a file is asked about again without uploading it again. For that turn the model gets the
  file's line and, for a document, the passages that match the question.
- **Pins (every turn of a chat)**: the user pins a file from the file panel, the @ menu or the chat header;
  the model pins with files(mode='pin'). Pinned files show as chips in the chat header.
- **The snapshot**: on each turn, the passages of the pinned (and @-ed) files that best match the user's
  message are attached to *that turn only* - never saved in the history, retrieved fresh each turn, within
  a budget (about 1,500 tokens). A table contributes its columns and matching rows; a picture its caption;
  code the matching lines.
- **agent.md** replaces notes: one short Markdown file (capped at 2,000 characters) always in view - the
  user's standing instructions, preferences and facts about them. "Remember X" is an edit of agent.md
  (files, mode='edit'); the user edits it in the file panel. Existing notes are moved into it (standing
  ones) or into `notes/` as ordinary files (the rest, searchable). The notes tool and the note kind go.

### 2.3 Search and the knowledge base

- **The knowledge base is the index of the files** - plus earlier chats and pages read in the last 30
  days. It is never a second copy: the one write path keeps it in step (added, changed, renamed, deleted).
  Files are the things; the knowledge base is how anyone finds things inside them.
- One hybrid search (keywords, plus the Gemma embeddings for text *and* pictures): document passages,
  table columns and values, picture captions and the pictures themselves, code, chart titles.
- **files(mode='find', query)** returns matching files - path, one line about each, the best passage - for
  main and every helper ("how many lion images" is one call). **recall** stays for what was said in chats.
- **ask_knowledge** stays for reading many or long documents in its own context (a 40-page summary, a
  comparison across files); a quick lookup is main's find, read or a pin.

### 2.4 The tools after the rebuild

Main - its own (7) and its helpers (3):

| Tool | Its one job |
|---|---|
| files | the workspace: list, find, read, write, edit, rename, delete, download, pin, unpin |
| calculate | exact maths: arithmetic, equations (exact and numeric), derivatives, integrals, limits, sums, matrices - SymPy, run in the sandbox with a time limit |
| get_weather | weather and air quality |
| generate_image | a new picture from a description |
| view_image | look at a picture |
| draw_diagram | Mermaid diagrams: how something works or is built |
| ask_user | one question when the request could mean different things |
| research_web | helper: the web |
| ask_knowledge | helper: reading many or long documents |
| analyze_or_code | helper: tables, analysis, ML and programs |

Helpers:

| Helper | Tools |
|---|---|
| web | web_search, read_webpage, finish |
| knowledge | files (find, read), recall, finish |
| analyze_or_code | files (find, read, write, edit), run, inspect_data, clean_data, query_data, make_chart, compare, predict, forecast, find_patterns, analyze_text, ask_user, finish |

A delegate call is `task` (what the user wants and why - results, not methods or code) and `files` (the
paths the task is about: tables, pictures, documents, programs). It replaces the data and file fields.

### 2.5 How the analyst-programmer works

Like a developer in a project: write a file, run it, edit it, run it again.
- **files(mode='write')** a script (`scripts/age_by_class.py`); **run(path, input?)** runs it with the
  workspace as its folder; **files(mode='edit', path, old_text, new_text)** changes it - an exact, unique
  match, else refused with the closest lines (a wrong edit fails loudly instead of landing on wrong lines);
  a Python file that no longer parses says the line.
- The file *is* the state: it stays for the next task ("make the name bigger" edits the same file), a Stop
  loses nothing, and the context holds small edits instead of whole copies.
- **run's description states the environment**: Windows, Python and its libraries, fonts by file name
  (`ImageFont.truetype('segoeui.ttf', 96)`), no internet, files by their names, pandas prints in full.
- **The built tools come first**: they are tested (baselines, 95% ranges, leak checks, time-ordered
  tests). A method named in a task - Random Forest, t-test, k-means, ARIMA - is done by the tool built for
  that job. Code is for what no tool does.
- inspect_data's questions are asked once per table: an answer (including "your call") is kept on the table
  and on the tables made from it.

### 2.6 Results: one envelope for every helper

- **The model writes only what only it knows**: `answer` (required), `caveats` (optional), and for web and
  knowledge `sources` (which pages or files it used) and `downloads` (file links found).
- **The app writes everything it can see**: status, files made or changed, pages and files read, tools
  run, the last run's output, warnings. Main gets facts it can trust; the model is never refused for
  leaving out what the app already knows.

### 2.7 Charts: one chart file, drawn live

- make_chart writes a chart file (`charts/survival-by-sex.chart.json`): the data and how to draw it. The
  page draws it live - animated, values on hover, full screen, download as PNG or CSV.
- A PNG is made from that same file only when a document, slide or download needs one. No picture copies
  in the workspace.
- Scripts make native charts too: `chart(df_or_values, type, x, y, ...)` in the sandbox writes the same
  chart file (a formula plot is a line of computed points).
- make_chart adds what forced code before: histogram by group, area. matplotlib stays only for pictures
  that aren't charts (image processing, 3D, maps).
- The page shows each file once per reply: a chart card and the same chart in the answer are one.

### 2.8 Maths

calculate (2.4) lets main solve maths itself, step by step, with every number checked by the tool. A helper
is only for maths that needs a program (a simulation, a plot of many cases).

### 2.9 Never losing work

- A helper that is stopped, fails or runs out of steps returns what it did: the files it wrote or changed
  (on disk), its last run's output, the pages it read. On Stop, this is saved as the call's result, so the
  next turn starts from it.
- A helper whose next request would not fit is given its last step to finish with what it has; an
  oversized request is never sent.

### 2.10 Checks that stay - and nothing else

| Check | Kind | Way out |
|---|---|---|
| sandbox, downloads, deletes, changing the user's file | permission | the user's button |
| numbers in a helper's answer come from tools or runs | truth | fix and finish again |
| cited pages and files were read | truth | fix and finish again |
| a repeated failing call; invented numbers in a task | judgement | confirm: true |
| announced / claimed / asked to remember but nothing done | truth, one hidden nudge a turn | - (its text fixed: "was wrote") |

### 2.11 Interactive programs

- An `.html` file gets a live preview in a locked frame (scripts run; no network, no access to the app),
  in the chat and the file panel, with full screen: games, simulations, small tools.
- A Python program that asks for input runs in a console card started from its file: output streams, the
  user types lines. What happened in the session is kept, so the model can read it when asked.

### 2.12 The page

1. @ picker in the message box; @-ed files shown as chips in the sent message.
2. Pins: pin and unpin from the file panel, the @ menu and the chat header; pinned chips in the header.
3. File panel: folders and paths, a search box (the same find), versions and restore, agent.md at the top.
4. The live state (working, checking, writing) shown at the end of the reply, where the eye is; no lone
   blinking cursor.
5. Each file shown once per reply (charts, pictures, files).
6. Chart cards: native, animated, hover values, full screen, download PNG/CSV.
7. Videos open in the viewer like pictures, 16:9, with a full-screen button; links open in a new tab (they
   already do).
8. HTML preview card; Python console card (2.11).
9. A model server that can't be reached (the tunnel down) is said in plain words, with a status dot; no raw
   error pages.
10. The plan card goes with the plan tool.

## 3. Tool descriptions and prompts

One pass over every description and prompt, after the tools above exist - not before, or it tunes tools
that are going away. Each description follows one template:

1. What it does - in the words a user or a task would use, method names included (predict: "train and test
   a machine-learning model - classification or regression, logistic/ridge vs gradient boosting,
   cross-validated or time-ordered, accuracy against a baseline, feature importance and direction").
2. What it returns.
3. When to use it.
4. When not, and which tool instead.
5. For run: the environment.

Each field says what its value holds, with an example. Main's prompt gets a short routing list: do it
yourself with your tools (talk, maths with calculate, files, diagrams, images, weather); a helper for the
web, many or long documents, tables and programs; tell a helper what is wanted, never how. Nothing is
tuned to a benchmark answer.

## 4. Removed

notes (tool and kind) - plan (tool and card) - run_code, fix_code, save_code and the scratch script -
read_file and search_files (into files) - the data and file fields of a delegate call (into files) - the
program check on file= - chart picture items - the method and data_used fields - model-facing ids.

## 5. Build order and acceptance

Each phase ends with its tests and its acceptance, and is committed before the next starts.

| Phase | Builds | Accepted when |
|---|---|---|
| 1. Files | 2.1, the file panel's paths, folders, versions; existing workspaces converted | the same path works from upload, @, main, a helper and a script; identical uploads are one file; the index matches the folder after every write (contract test); every existing item converted, none lost |
| 2. The helper on files | 2.5, 2.6, 2.8, 2.9 | on the 26B: the picture-name edit in 3 runs or fewer with a real font; the calculus problem solved by main with calculate, no helper; Stop then "continue" picks up the same file; a 10-run job without overflow |
| 3. Charts | 2.7 and its card | the Titanic showcase: every chart native, none shown twice, no PNG files unless a document needs one |
| 4. Finding and memory | 2.2, 2.3, find; notes moved into agent.md and notes/ | "how many lion images" in one call; a question on a pinned document answered without ask_knowledge; "remember X" edits agent.md; the snapshot is not in the saved history (test); context raised to 20k if the server holds it |
| 5. Descriptions and prompts | section 3 | routing probes on the 26B: maths to calculate; ML and statistics asks to predict, forecast, compare, find_patterns, analyze_text; documents to find, read and pins |
| 6. The page | 2.11, 2.12 (the rest) | each item checked in the browser, light and dark, phone width |
| 7. Measure | the 10 dev jobs, then the 10 held-out jobs once; ARCHITECTURE and SYSTEM_MAP rewritten | results recorded as they are, with the model's limits named |

## 6. Fixed decisions

- Paths, not ids. Edits by exact text. Native charts first. One envelope for helper results.
- Pins and @ attach a snapshot to the active turn only. agent.md is capped and always in view.
- Permissions as they are (2.1).
- The plan and notes tools are removed.

## 7. Not in this plan

Several users or accounts, cloud sync, voice, a phone app, a container sandbox (run's engine interface is
ready for one), new helper roles, editing pictures with the image model, fine-tuning, and a library door
to the analysis tools from scripts (only if phase 5 shows the tools are still skipped).
