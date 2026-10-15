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
| The web helper loops | a helper remembers nothing between tasks (a site that refused, a page read); no limit on searching instead of reading; links copied by hand into sources | LeetCode: 9 searches in a row for the same text in snippets (about 300 s) without reading the pages they kept listing, out of steps; the next two tasks went to leetcode.com again (refused again) and read algo.monster again; one source link mis-copied, refused |

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

### 2.10 The web helper

- **Pages are remembered for the chat.** A page read earlier in the chat comes back at once from memory,
  with when it was read; a site that refused (a bot check, blocked) is refused at once with the earlier
  reason. A web task starts with one line of each: "already read: ...; refused: leetcode.com (bot check)".
- **Read, don't keep searching.** At most 3 searches in a row without reading a page; the 4th returns the
  pages the searches found and nobody read, the most often listed first: read one, or finish with what you
  have. (web_search is for choosing pages, not answering from snippets.)
- **Sources by number.** Pages read are numbered in the results ([1], [2] ...); finish cites the numbers
  and the app writes the links - no link is copied by hand.
- **When the named source can't be read**, the answer says so and gives the text from the closest page
  that quotes it, named as that page.

### 2.11 Checks that stay - and nothing else

| Check | Kind | Way out |
|---|---|---|
| sandbox, downloads, deletes, changing the user's file | permission | the user's button |
| numbers in a helper's answer come from tools or runs | truth | fix and finish again |
| cited pages and files were read | truth | fix and finish again |
| a repeated failing call; invented numbers in a task | judgement | confirm: true |
| announced / claimed / asked to remember but nothing done | truth, one hidden nudge a turn | - (its text fixed: "was wrote") |

### 2.12 Interactive programs

- An `.html` file gets a live preview in a locked frame (scripts run; no network, no access to the app),
  in the chat and the file panel, with full screen: games, simulations, small tools.
- A Python program that asks for input runs in a console card started from its file: output streams, the
  user types lines. What happened in the session is kept, so the model can read it when asked.

### 2.13 The page

1. @ picker in the message box; @-ed files shown as chips in the sent message.
2. Pins: pin and unpin from the file panel, the @ menu and the chat header; pinned chips in the header.
3. File panel: folders and paths, a search box (the same find), versions and restore, agent.md at the top.
4. The live state (working, checking, writing) shown at the end of the reply, where the eye is; no lone
   blinking cursor.
5. Each file shown once per reply (charts, pictures, files).
6. Chart cards: native, animated, hover values, full screen, download PNG/CSV.
7. Videos open in the viewer like pictures, 16:9, with a full-screen button; links open in a new tab (they
   already do).
8. HTML preview card; Python console card (2.12).
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
web, many or long documents, tables and programs; tell a helper what is wanted, never how. The persona
may use a small visual touch when it helps - an emoji that fits a casual reply, bold for the key number -
never in every reply and never in serious or technical answers (measured in phase 5: emoji in at most 1
of 4 casual replies, none in serious ones). Nothing is
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
| 2. The helpers | 2.5, 2.6, 2.8, 2.9, 2.10 | on the 26B: the picture-name edit in 3 runs or fewer with a real font; the calculus problem solved by main with calculate, no helper; Stop then "continue" picks up the same file; a 10-run job without overflow; the LeetCode request in 3 searches or fewer, no second visit to a refused site, saying LeetCode couldn't be read |
| 3. Charts | 2.7 and its card | the Titanic showcase: every chart native, none shown twice, no PNG files unless a document needs one |
| 4. Finding and memory | 2.2, 2.3, find; notes moved into agent.md and notes/ | "how many lion images" in one call; a question on a pinned document answered without ask_knowledge; "remember X" edits agent.md; the snapshot is not in the saved history (test); context raised to 20k if the server holds it |
| 5. Descriptions and prompts | section 3 | routing probes on the 26B: maths to calculate; ML and statistics asks to predict, forecast, compare, find_patterns, analyze_text; documents to find, read and pins |
| 6. The page | 2.12, 2.13 (the rest) | each item checked in the browser, light and dark, phone width |
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

## 8. Progress

- **Phase 1 - files: done (2026-10-10).** One folder per workspace (files/ by path, .zen/ for the registry,
  typed table copies, versions, reports, notes); one write path (Workspace.put / adopt) with unique paths,
  identical uploads recognised, atomic writes and the last 10 versions; tables are their CSV or Excel files;
  scripts run in the folder and open files by name, change the assistant's files (version kept) and never
  the user's; every result a model reads names files by path (workspace.present, at ToolResult.for_model -
  one place for every tool); SQL takes file names; one index sync for documents, pictures and tables, also
  before a search; the file panel shows paths, who brought a file, its earlier versions and a restore.
  Tests: 586 pass (tests/test_workspace_files.py checks one path for every reader, duplicates, versions, the
  index following the folder, no key in any result a model reads, and the move of the first layout). The
  four real workspaces were moved (99 files, none lost; backup in claude-cloud-backups/assistant-before-phase1).
  Moved to phase 4: naming a pasted picture from its caption - it needs the vision model, and its caption is
  what find searches.
- **Phase 2 - the helpers: done (2026-10-14).** A program is a file the helper writes, runs (run) and edits
  (files edit) - the file is the state; one result envelope (the model writes answer, caveats, sources; the
  app adds what ran, the files made or changed, the last run's output); calculate does symbolic maths (SymPy in
  the sandbox); a Stop, a model failure or a full context keeps the work done; the web helper remembers the
  pages and refusing sites of the chat, must read after 3 searches in a row, cites pages by number; a table's
  question is asked once. Acceptance on the 26B (bench/phase2.py), as measured:
  - picture name: pass - one write, one run, 135 s, fonts named by file. The first runs exposed a sandbox bug:
    no WINDIR in the run's environment, so every named font fell back to the default (6 runs, 8 min); fixed.
  - calculus: pass - main with calculate, no helper, 104 s (before: 8 min through a helper).
  - Stop then "continue": pass - the same file carried on.
  - the 10-run job: not exercised - the model made all four changes in one program and one run; the
    overflow path is covered by a test without the model only.
  - LeetCode: the helper meets the bar (3 searches in a row at most - the 4th was refused and it read;
    LeetCode fetched once; its answer and caveat say the page was blocked). Not met at the user's end: main's
    reply dropped that caveat, and called NeetCode's version LeetCode's; 9-13 min chasing the exact quote on
    mirrors. Both moved to phase 5 (main passes a helper's caveats and sources on; a refused page needs one
    mirror, not a hunt for its exact words).
- **Phase 3 - charts as files: done (2026-10-14).** make_chart, forecast, find_patterns and a script's chart()
  each write one chart file (charts/<title>.chart.json) the page draws live; its picture is drawn only for a
  document or a download (cached per drawing version); the same chart again is its next version; histograms
  by group and area charts. Acceptance on the 26B: the Titanic showcase passed (95 s) - three native charts
  from make_chart after inspect_data, three chart files, no picture files. Checked in the browser (light,
  dark, phone width, the side panel beside a narrow chat): each chart shows once, moved to where the answer
  explains it; its picture downloads. Found there and fixed: the box chart's picture had its axis titles
  swapped; a starter chip offered to summarise a chart file; on touch the open-larger button covered the first
  value; a chart placed in an answer took the answer's heading spacing. Restyled after review: bars grow from
  one baseline with the value at the tip (not a progress track), download and open-larger in one cluster, a
  chart file fills the side panel (its download is the picture), and a chart tile in the file icons' style.
- **Phase 4 - find, @, pins, agent.md: done (2026-10-14).** find searches every file by words and by meaning
  (documents, code, charts, tables, reports, pictures by their caption; "images", "py" and the like pick the
  kind instead of matching words); agent.md replaces notes - one capped file (2,000 characters) in main's
  system prompt, edited by the user in the file panel and by the model with files write, never deleted or
  renamed; a pin keeps a file in view in its chat and @ points at one for a single message - both reach the
  model as a snapshot (outline, passages for the message, a table's sample, code's functions with lines, a
  picture's caption; 6,000 characters) attached to the turn being answered and never stored; a pasted picture
  with a generic name is named from its caption. No move of old notes or chats (in development). Acceptance on
  the 26B (bench/phase4.py), as measured:
  - 6 lion pictures: pass - one find, all 6, the panda one said as such (43 s).
  - a pinned return policy: pass - answered from the snapshot with no tool call (9 s; 372 new prompt tokens,
    3,904 from cache).
  - "remember our fiscal year starts on 1 April", then a new chat: pass - one files write, the new chat knew it
    (28 s; 3 of 3 runs). Caching as designed: a normal turn reads 1-600 new tokens, the turn after agent.md
    changes reads the prompt again once (about 4,200).
  Found and fixed there: the model counted a table from the snapshot's sample rows (now labelled as a sample:
  counts come from analyze_or_code); a chart or picture named by its path didn't show in the answer; agent.md's
  instructions said edit while the model sends the whole new text (refused, then read, then edited: 3 calls) -
  the instructions now name the call and its field. The model server's slot is now 20,480 tokens (the app's
  prompt limit follows /props: 16,384 with 4,096 for the reply); generation speed unchanged (45-59 tokens/s).
  Tests: 595 pass. Moved to phase 5: a check that every mode and field a description or prompt names exists
  in that tool's schema - the agent.md failures were all instructions naming the wrong mode or field.
- **Phase 5 - descriptions and prompts: done (2026-10-14).** Every tool description in one order (what it does,
  in a user's words with method names; what it returns; when to use it; when not, and which tool instead; run:
  its environment), fields with examples; main's prompt has the routing list, a small visual touch (bold; an emoji
  rarely, never in serious or technical answers), and passes a helper's caveats and sources on as the helper named
  them; the web helper reads one other page for a site that refuses, and says so. The plan tool and its card are
  removed (and the note card notes left). A test reads everything each model reads - prompt, the app's hidden
  messages, agent.md's lines, tools and fields, and every string a tool returns - and checks that each tool, mode
  and field it names is one that model has (it found the sandbox still saying files(update)). Main reads 4,208
  tokens before any message (3,773 before), the analyst-programmer 5,800 (5,146).
  Fixed on the way (system, not wording): a turn whose tool results grew was sent over the prompt limit - now older
  results, then older calls' long texts, then the newest results are shortened, and past that the turn stops
  plainly; the pinned files' snapshot counts toward the limit; the token estimate counted a user's words twice;
  query_data crashed on a backslash-escaped quote (now: how to quote); analyze_text refused a table's only text
  column; ask_knowledge's description contradicted itself ("a file that may cover it: ask here").
  Acceptance on the 26B (bench/phase5.py; dev and held-out written together, different data, none of phase 7's):
  dev 16/19 at first; the held-out set once, after the fixes: 18/19 - maths 4/4 with calculate, statistics 4/5
  (predict, forecast, compare, analyze_text), documents 3/3 (files find or read; a pinned file answered with no
  tool), talk 1/1; emoji in 1 of 4 casual replies and 0 of 2 serious ones. The dev ticket and held-out feedback
  tables were rewritten with varied texts before the held-out run (11 and 8 distinct texts, under the 20 themes
  need: the data's fault, not routing).
  Flagged as the model's limits, not prompted around (a third wording round had begun to echo a probe's own
  words, and was taken back): main writes a method into a helper's task ("analyze correlations between left and
  ..."), and the helper then tests columns one pair at a time instead of predict - 2 of 2 runs, the answer right;
  main sends a one-file question to ask_knowledge before reading the file itself - 2 of 2, the answer right, one
  helper call more; the helper counted item pairs in a script instead of find_patterns - held-out, its list is
  popularity (bread with everything) where find_patterns on the same table finds the one real pair. If the tools
  keep being skipped, the library door from scripts (section 7) is the system's answer, not more wording.
  Tests: 605 pass.
- **Phase 6 - the page: done (2026-10-15).** What changed, each checked in the browser:
  - The live state at the end of the reply: once a reply has text, what the turn is doing ("Calculating...",
    "Checking the answer...") shows at its end; the caret only while words arrive (before: the state at the top,
    a lone caret blinking at the end - a 48 s re-check looked like a hang).
  - The model's state: a dot always in the header - green ready (the model in its tooltip), amber loading, red
    unreachable - checked every 30 s when ready, every 5 s otherwise, and right after a failed reply. A tunnel's
    error page (Cloudflare's 530 through the Kaggle bridge) is said in plain words, never shown (before: "llama-server
    answered 530; check its window", "Model starting", and 300 characters of HTML in a failed reply). Seen on a real
    outage the morning of 2026-10-15 (the Kaggle session had ended): red dot, "The model can't be reached right now".
  - Videos open in the viewer at 16:9 with a full-screen button, no zoom: a web video from a reply's sources (before:
    played inside its small card) and a video file of the workspace (before: not viewable).
  - An .html file runs in its card in the chat as well as in the file panel - the same sealed frame (its own origin;
    a fetch to example.com never left the page) - with full screen in both.
  - The console (2.12): Run, in the file panel or on a Python file's card, starts the program; what it prints
    streams in, the user types its input() lines; Stop, Run again; at most 15 minutes, 5 with nothing typed; what it
    writes is the workspace's; the session is kept in the file's report, so the model reads it when asked. The
    panel's run with the answers typed up front, and its endpoint, are gone: one way to run a program.
  - The file panel's search box: the model's own find, each file with the part that matched; folders in the paths.
  - A chart downloads as its numbers (CSV) beside its picture.
  - @ chips on a sent message and pinning from the @ menu (a chip in the header, the pin saved) - built in phase 4,
    checked here; the plan card went with the plan tool in phase 5.
  Light, dark and phone width (375 px, no sideways scroll): the console, the HTML card, the search and the video
  viewer. Found and fixed there: the viewer's "video" state class was the sources' video-row class (the viewer
  collapsed to nothing); the start screen offered to summarise agent.md; the console sent Windows' "\r\n"; a stray
  "null" under the search results; the typing line slipped below the panel's fold as output grew. The live line was
  checked with a scripted stream of a turn's events through the page's own code, the model being down; a real turn
  sees it in phase 7.
  Tests: 616 pass (test_open_figures_are_saved_once failed once in a full run and passed the five runs since -
  a timing flake to watch, not fixed).
- **Phase 7 - measure: done (2026-10-15).** The 20 jobs of bench/combos.py on the 26B (Kaggle, 20,480 tokens a
  slot), and ARCHITECTURE.md, SYSTEM_MAP.md and the README rewritten from the code.
  - The 10 dev jobs: 6/10 as first scored. Two failures were the jobs' own checks, not the app: agent.md - in every
    workspace since phase 4 - counted as a download the user had cancelled; and a note checked only in agent.md,
    where phase 4 gives notes two homes (agent.md for what is kept in view in every chat, a file of its own for the
    rest - "save it as a note" wrote travel_growth_note.md). Both checks corrected (memory_has / note_has), both
    jobs run again: pass. So 8/10. The two left: plan_forecast_deck - main gave the slide deck to analyze_or_code,
    against its contract ("not for documents - Word, slides"), which built one slide in a script (the model's
    routing; flagged); invoice_rupees - the rupee total exact (1,063,840.05), the USD total not said, which the
    job's check asks for (kept as it is).
  - The 10 held-out jobs, after those corrections (made from phase 4's design, before the run): 8/10. Not a first
    run: the same ten ran once on 2026-10-10, before the rebuild (9/10); none of their jobs were used for tuning
    since. pricing_changes - two file writes refused, a third worked, and the reply said "I have saved the detailed
    changes to changes.md": there was none (a rerun to see why passed in 25 s; the first run's errors were not
    kept). Found a gap in the claim check (only turns where no tool did anything): a reply that says Zen saved,
    wrote or made a file no longer passes when no file of that name is in the workspace - one hidden nudge
    (checks.missing_files; replayed over the 333 stored replies of the user's chats: 27 say Zen made a file, none
    flagged). budget_left - "how much is left if March spending was 4,350.75?" read as the March share (1,649.25,
    said as such) where the check wants the whole budget (7,649.25): an ambiguous question, answered the way the
    instructions ask (one reading, said).
  - Against the same jobs before the rebuild (2026-10-10: dev 7/10, held-out 9/10): no better, within the noise
    of single runs - the rebuild's measured gains are where these jobs don't look (each phase's acceptance:
    the lion count in one call, calculus with calculate, a pinned document answered without a helper, routing
    18/19 held-out). Slower: the dev jobs took 1,120 s in all (871 before), the held-out 492 s (341) - not
    analysed (the prompts are 400-650 tokens longer; Kaggle's sessions differ).
  Tests: 617 pass.
