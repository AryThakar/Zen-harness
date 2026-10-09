# Zen system plan - what we have, what we build

Written 2026-10-08, after moving the app to Gemma 4 26B-A4B (with the 12B as the smallest model that must
still work). It replaces docs/SYSTEM_MAP.md and docs/TARGET_MAP.md (written 2026-10-04 for a 4B model);
those two are merged into this one at clean-up. Nothing here is built yet: this is the map we agree on
first, then we build from it, stage by stage.

## 0. Why a new plan

The first design (Oct 4) was built around a 4B model's weak spots. Every time it failed, we added a guard:
hidden answer checks that read the reply's words, code that repairs badly formed output, strict report
shapes, one rule per measured failure in the prompts. With the 26B the model is no longer the weak part.
The guards now cause problems of their own (5 of the 6 hidden nudges in two chats this week were false
alarms), and features were
added one at a time, so the parts don't share one way of naming things, reporting results or describing
what they can do.

The goal now: one system where every part follows the same contracts, the model knows exactly what it
can do, new tools fit in without touching five files, and the product does things that feel complete
(a game you can play, a report you can download, a picture you can edit).

---

## The decision (frozen 2026-10-08)

**The shape stays:** main (Zen) talks to the user and runs the job; four helpers each do one kind of work;
all share one workspace. A helper starts fresh for every task, uses only its own tools and hands back one
result that code has checked. No new roles.

| Role | Its one job | Tools (✚ new, ✎ changed, ✖ removed) |
|---|---|---|
| **main** | Talk, quick things, plan and delegate, present results once | get_weather, generate_image ✎ (named files), view_image, draw_diagram, calculate, notes ✎ (save, update, list, delete), ask_user ✎ (question chains), files; delegates research_web, ask_knowledge, analyze_data, write_code |
| **web** | Answers from the internet, with dated sources | web_search ✎ (date filter), read_webpage ✎ (one status per read, wiki adapter), ask_user ✚, finish |
| **knowledge** | Answers from the user's documents, notes and past chats | search_files, recall, read_file, finish |
| **data** | Numbers and charts from the user's tables | the 9 data tools, run_code, fix_code, ask_user, finish; ✖ the "model" item kind |
| **coder** | Makes one working file | run_code ✎ (typed input for programs), fix_code, read_file, ask_user, finish; playable HTML in the viewer ✚ |

**Five rules that make it one app:**
1. Clear boundaries: numbers about a table → data; any file to keep (script, document, slides, edited
   picture) → coder; the internet → web; the user's own material → knowledge. main never does a helper's job.
2. One result shape for every helper: answer or report, sources or files made, warnings, next step.
3. One tool definition: name, description, who holds it, UI label, card, approval - in one place.
4. The workspace is the only link between roles: helpers never talk to each other; main chains them
   through items.
5. Code checks facts (sources read, numbers from tools, code ran, approvals); prompts only guide.

**Tier 1 - built now:** the foundations (section 10: one tool definition, one result shape, the item
contract, a turn trace, limits from the server, workers.py split, the capability list from the
contracts); notes list and delete; web: one status per read, the MediaWiki adapter, the date filter;
coder: its own module, one engine interface, typed input for programs; playable HTML in the viewer;
generated images named properly; the dead "model" kind removed; and a read-through of the whole codebase
(store, search index, providers, API, UI) for bugs, dead code and mismatches with these rules.

**Tier 2 - built when a real need shows up, each on its own:** the coder's sandbox panel with Edit & Run;
finished documents (Word, slides) with charts; Playwright MCP for the web helper (the reader fallback
first, click/type tools only if the fallback proves its worth); EmbeddingGemma 2 (13.0); the plan card.

**Order:** foundations and the read-through → the rest of Tier 1 → one evaluation (fixed tasks, a held-out
set, honest numbers) → README and clean-up. Tier 2 items enter only when chosen.

**Progress (2026-10-09, second update) - every item against this section's text:**

Tier 1
- Done: one tool definition (labels served to the UI); one helper contract (Delegate) and one result shape;
  workers.py split into a package; the slot size from llama-server; notes list and delete; web: a status per
  read, the MediaWiki route, `recent`, the web helper asks the user (in place) and has 12 steps, page text
  is data not instructions, one result per page (tracking tags, www and #parts ignored), a pause between
  pages of one site, at most 6 pages of one site per task, read pages cached for an hour; the coder's typed
  input, its one engine (sandbox.ENGINE - a real sandbox plugs in there) and one run at a time per sandbox;
  playable HTML in a sealed frame; question chains (measured: a quiz of 10 questions runs to its end - each
  answer starts the turn's rounds afresh); the item contract (one naming rule for every kind, workspace.py);
  the turn trace, stored and shown in the UI (a button under each answer opens how it ran).
- The read-through: every source file read whole (docs/REVIEW_LOG.md - 13 findings, each fixed or decided,
  the fixes tested; the high one, a turn too big for main's slot, crashed before).
- Versions: requirements.lock holds every library's tested version; the UI's libraries are pinned.
- Decided not to build: a separate capability list - main reads every tool's and helper's own description,
  and a second list would be a second copy to fall out of step.
- Downloaded, its gate next: trafilatura (kept only if it beats today's extractor on saved pages).
- Waiting for the model (Kaggle): the prompt review (section 8), measuring the guessing checks with the trace
  (section 9), the thinking policy, and the evaluation.

Tier 2
- Done: the coder's sandbox panel (a text file edited and saved in the file panel, a Python file run with
  typed input, its output and the files it made shown - checked in the browser); finished documents (every
  chart also kept as a picture; Word and slides written from Markdown with pictures and charts in them); the
  plan card (main's plan as a checklist that ticks when its tools' calls end).
- Done: Playwright MCP behind read_webpage (the MCP Python SDK 2.3, Playwright MCP 0.0.83 with the installed
  Edge): a page that is blocked or gives too little text is opened in the browser and its HTML goes through
  the same extraction. Measured: a Kaggle dataset page, 23 words read plainly, 909 through the browser
  (12.7 s); a page the browser can't improve keeps its plain read; a local address is refused.
- Done: EmbeddingGemma 2 replaced bge-small for search by meaning (13.0 below): served by llama-server b11515,
  which the app starts itself; documents, reports, notes, chats, pictures (by what they show) and tables
  (by name, columns and values) in one index; each workspace re-indexed in the background when first
  opened. Measured on 76 held-out questions about the user's own workspaces: both models find every right
  passage; bge-small's scores for unrelated questions overlap its right ones (it answered 3 of 5 unrelated
  questions), EmbeddingGemma's don't (0 of 5). Next in 13: web pages by meaning (13.1).
- Not built, by the decision above: the five click/type tools - only if the fallback proves too little.

Next: the docs merge (SYSTEM_MAP, TARGET_MAP and ASSISTANT_PLAN into this plan and ARCHITECTURE), the
downloads above once approved, then the evaluation on the 26B, then the README.

Sections 1-13 below are the detail behind this decision; where they differ from it, this section wins.

---

## 1. The roles today

Two model slots on one llama-server: slot 0 is **main**, slot 1 runs **one helper at a time**. A helper
starts fresh for every task: it gets the task, the workspace listing and the pinned notes, works with its
own tools, and hands back one structured result.

| Role | Job | Its tools | Step limit | Finishes with | Checked by code |
|---|---|---|---|---|---|
| **main** (Zen) | Talks with the user, does quick things, delegates | 8 own + 4 delegates | 8 tool rounds a turn | a reply | 6 hidden checks (section 3.4) |
| **web** | Searches and reads pages | web_search, read_webpage | 6 | submit_result: answer, sources, download links | sources must be pages it read; links must come from a page |
| **knowledge** | Answers from documents, notes, past chats | search_files, recall, read_file | 6 | submit_result: answer, sources | sources must be ones a tool returned |
| **data** | Analyses tables | 9 data tools + run_code, fix_code, ask_user | 10 | save_report: question, data used, method, results, caveats | every number must appear in a tool result; table inspected first |
| **coder** | Writes or changes one file | run_code, fix_code, read_file, ask_user | 10 | save: the file + its report | the code ran; the report's claims match the code and its output |

---

## 2. Every tool today (28, plus 3 finish tools)

### 2.1 main's own tools

| Tool | What it does | Limits and known issues | Verdict |
|---|---|---|---|
| get_weather | Now + up to 7 days, UV, air quality, local time; place or the computer's location | Open-Meteo; IP location can be wrong behind a VPN | Keep |
| generate_image | A picture from a description (Pollinations, needs the user's key), with a vision self-check | Saved under the first 80 characters of the prompt, no extension - the coder couldn't find `image_19.png`. Can't edit an existing picture | Change: proper names; editing (section 6) |
| view_image | Asks the model's vision part one question about one image | Rough for exact numbers. An attached picture always needs this extra call | Keep; measure sending attachments straight to the 26B |
| draw_diagram | Mermaid diagrams, common mistakes mended | - | Keep |
| calculate | Exact arithmetic, safe evaluator | Fixed function list (hyperbolic added this week) | Keep |
| save_note | Saves or updates a note by title; the 5 newest are pinned in every chat | No way to list, change or delete a note ("forget that" has no tool) | Change: full notes tool |
| ask_user | One question with option buttons; the turn waits | One question per call; a quiz takes one round per question (8 rounds a turn) | Keep; lift the round limit for question chains |
| files | list, find, read, write, update, rename, delete, download | 8 modes in one tool (fine for main). Downloads only GitHub files and data tables. Delete / download / replacing the user's own file ask first | Keep |

### 2.2 main's delegates and the helpers' tools

| Delegate → helper | Helper's tools | Limits and known issues | Verdict |
|---|---|---|---|
| research_web → web | web_search (public engines, no key), read_webpage (3 MB, 12,000 characters; GitHub through its API; YouTube description only; PDF text) | Gave a made-up raw GitHub link once (404) | Keep |
| ask_knowledge → knowledge | search_files (keywords + meaning, documents and notes), recall (earlier chats), read_file | Answers only what the passages say | Keep |
| analyze_data → data (only when a table exists) | inspect_data, clean_data, query_data (DuckDB SQL), make_chart (7 kinds), compare (tests, A/B), forecast, predict (ML), find_patterns (segments, unusual, together), analyze_text (themes, labels, similar), run_code, fix_code | No chart from numbers typed in the chat (needs a table). predict doesn't keep a trained model, though the workspace has a "model" kind for one - nothing ever creates it | Keep; drop the dead "model" kind |
| write_code → coder | run_code / fix_code (one scratch script, sandbox: 30-120 s, no network, workspace only, 26 libraries), read_file | A program that waits for keyboard input can't be run, so the coder saved its test script as the game. HTML is shown with scripts blocked, so a web page or game can't be played. One file per task | Change: input for programs; playable HTML (section 6) |

### 2.3 Finish tools

submit_result (web, knowledge), save_report (data), save (coder): each helper's one way to hand in its
result. Each has its own shape.

---

## 3. The systems around the tools today

1. **Workspace.** One folder per project; every item has an id: `data_N` table, `image_N`, `file_N`,
   `note_N`, `model_N` (unused). One resolver turns an id, a name or a copied listing line into the id.
   Items can carry a report (what a helper did with them).
2. **Search and memory.** One index (SQLite keywords + local embeddings) for documents, notes and past
   chats. Every finished turn is indexed for recall. 5 newest notes pinned in every chat.
3. **Approvals.** Download, delete and replacing the user's own file always ask with a button; code
   carries the action out, never the model.
4. **Checks on main** (hidden nudges, one per turn): a repeated failed call is refused; a helper task with
   made-up numbers is refused; a reply that **announces** an action, **claims** one, ignores a
   **remember** request, or **writes a tool call as text** is sent back. The first two read evidence;
   the next three guess intent from the reply's words - that is where the false nudges come from.
5. **Guards in the helpers:** repeated calls refused, a table inspected before use, a user's choice never
   guessed, "steps left" hints, one chance to fix a failed check, and repair code for badly formed
   output (a result written as JSON text, as markup, a bare tool name, a missing save).
6. **Context.** The workspace and notes ride on the user's messages so the cached prompt stays valid;
   old turns are dropped when a 16k slot fills, and recall can still find them. The slot size is a
   constant in the code, not read from the server.
7. **Turn limits and thinking.** 8 tool rounds a turn; thinking is on for every step, so a one-line reply
   can take 30-60 s on the 12B.
8. **UI.** Live steps with a timer, cards (chart, image, diagram, weather, note, file, report), questions
   and permissions inside the message box, a file viewer (code, Markdown, tables, PDF, HTML without
   scripts). Step labels are written out in `web/js/steps.js`, separately from the tools.

---

## 4. What is wrong at the system level

Not single bugs - the reasons single bugs keep appearing.

1. **Guards built for a 4B model.** The intent-guessing checks misfire on normal replies, and the repair
   code fixes mistakes the 26B may no longer make - nobody knows, because nothing records which guard
   fired.
2. **No shared contracts.**
   - Names: a generated picture is named after its prompt with no extension; a script's output is
     named by the script; the "model" kind exists but is never made.
   - Results: the four helpers hand back four different shapes.
   - Labels: adding a tool means editing the registry, a helper profile, the UI's labels and sometimes
     the cards - five places.
3. **main doesn't know what its helpers can deliver.** It knows each delegate by one paragraph, so when
   asked "what tools does the analyst have?" it invented a "visualize" tool. (Copying the helpers' tools
   into main would break the design: main should know *what it can ask for*, not *how the helper does
   it*.)
4. **Capability gaps** - things users ask for that the system can't do as a whole:
   - a program or game that needs input; a web page or game that runs;
   - editing an existing picture (text, crop, blur, combine);
   - a finished deliverable - a report, Word document or slide deck with the charts in it;
   - managing memory - list notes, change one, forget one, "what do you know about me";
   - a chart from numbers typed in the chat;
   - a multi-step job planned and shown as a plan ("find a dataset, analyse it, write a report");
   - an exact answer to "what can you do?".
5. **No record of what a turn did.** To find this week's pauses we rebuilt the timing from the bridge's
   log. Failed turns (model unreachable) leave no trace in the chat. Limits that belong to the server
   (slot size) are constants.

---

## 5. The target: principles

1. **Code guarantees facts, the model makes judgements.** Permissions, numbers backed by a tool, sources
   really read, files really saved and run: code. Routing, wording, planning, when to ask: the model.
   No prompt rule for what code can enforce, and no code that guesses intent from words unless it is
   measured to be right.
2. **One contract per concern, used by everything:** item names, tool results, helper results, errors,
   approvals, UI labels.
3. **Roles stay separate.** main plans and talks; helpers do focused work in their own context and hand
   back a result in one shared shape. main knows each helper by its **contract** - what to ask, what
   comes back, what it can't do - never by its tools.
4. **One source of truth.** Each tool is defined once, with everything about it (schema, handler, who
   holds it, its UI labels, its card, whether it needs approval). The UI and the "what can you do"
   answer are generated from that, so they can't drift.
5. **Measured, not assumed.** Every turn leaves a trace (steps, times, tokens, thinking, guards fired).
   A guard that never fires on the 26B goes; a new one comes in only with a measured failure.
6. **Easy to extend.** A new tool = one definition + its tests. A new helper = one contract + one prompt.
7. **Designed on the 26B, still working on the 12B.** The 12B run is a check, not the target.

---

## 6. The target capability map

What the user wants → who does it → what they get. NEW = not possible today.

| The user wants | Who | With | The user gets |
|---|---|---|---|
| A talk, an explanation, writing, maths | main | itself (+ calculate for numbers) | a reply |
| Current facts, news, prices, a link read | web | search, read | a dated answer with sources |
| Their documents, notes, earlier chats | knowledge | search, recall, read | an answer with sources |
| To keep, change or forget something; "what do you know about me" | main | notes tool: save, update, list, delete (NEW: list, delete) | the note card |
| Their files: open, write, rename, delete, download | main | files | file cards; permission buttons |
| Data from the web into a table | main → web → files | the web helper's download links, then download with approval | a new table |
| Anything about a table | data | 9 data tools | charts, tables, a report kept on the table |
| A chart from numbers in the chat (NEW) | main | numbers written as a small table, then the analyst's chart | a chart card |
| A script that runs | coder | run_code / fix_code | a file card with its report |
| A program that needs input (NEW) | coder | run_code with typed input (e.g. the moves of a game) | the real program, tested by playing it once |
| A web page or game to use (NEW) | coder | an HTML file | the file viewer runs it, isolated from the app |
| A new picture | main | generate_image | an image card, properly named |
| A picture changed (NEW) | coder | image work in its script, the picture named in the task | a new image; the original kept |
| What a picture shows | main | view_image (or the picture sent straight to the model - to measure) | a reply |
| How something works, drawn | main | draw_diagram | a diagram card |
| Weather, the time somewhere | main | get_weather | a weather card |
| A choice, a quiz, a short form | main | ask_user, a chain of questions | buttons in the message box |
| A finished report, Word document or slide deck with charts (NEW) | data or coder | charts rendered as pictures from the same chart specs, then the document | a file to download |
| A multi-step job (NEW) | main | a plan shown as a card, worked through step by step | the plan with each step ticked off |
| "What can you do?" (NEW) | main | a capability list generated from the tool and helper contracts | an exact answer |

---

## 7. Tool changes

**Keep as they are:** get_weather, draw_diagram, calculate, ask_user (limit change only), files, web_search,
read_webpage, search_files, recall, read_file, inspect_data, clean_data, query_data, make_chart, compare,
forecast, predict, find_patterns, analyze_text, run_code, fix_code.

**Change:**
- generate_image: items named with a short title and their extension.
- save_note → a notes tool: save, update, list, delete (delete asks first).
- run_code / fix_code: optional typed input for the program.
- The HTML viewer: run scripts in an isolated frame (no access to the app or the network).
- view_image: measure the 26B reading attached pictures directly; keep the tool if it isn't better.
- The four finish tools: one shared result shape (answer, sources or files made, warnings, what to do next),
  each helper adding only its own fields.

**Add:**
- Chart rendering to an image from the same chart spec, so documents and slides can include charts.
- A plan card for multi-step jobs (main writes the plan; the UI ticks off steps).
- A capability list, generated from the contracts.

**Remove:**
- The "model" item kind (never created).
- Guards and repairs that the trace shows never fire on the 26B (decided after measuring, section 9).

---

## 8. Prompts

The three layers stay - role prompt, tool description, tool result - with each rule in exactly one of
them. What changes:

- **main's prompt** gets a short "how you work" part for a capable model: plan a multi-step job before
  starting it, give helpers complete tasks, combine tools (download → analyse → chart → report), and
  present results once. Its knowledge of the helpers comes from their contracts.
- **Tool descriptions** follow one template: what it does, when to use it, when not to, what comes back.
- **Helper prompts** keep their craft blocks, reviewed for the 26B.
- **Out:** rules that existed only for 4B failures and that a contract, a check or a better tool now
  covers. Every removal is checked against the evaluation (section 11), not assumed.

---

## 9. Checks

| Check | Today | Target |
|---|---|---|
| Numbers in data answers come from tool results | code | keep |
| Web and knowledge sources were really read or returned | code | keep |
| The coder's code ran; its report matches the code and output | code | keep |
| Approvals for download, delete, replacing the user's file | code | keep |
| The same failed call repeated | code | keep |
| A helper task with made-up numbers | code | keep |
| A tool call written as text | code, from the reply's form | keep (it reads the form, not intent) |
| Announced an action, claimed one, ignored "remember" | code guessing from words | measure on the 26B with the turn trace: keep only what still catches real failures, with its misfire rate known |

---

## 10. Making it scale (the code)

1. **One tool definition.** Name, description, input schema, handler, who holds it (main / which helpers),
   UI labels (doing / done), card type, approval. The registry holds them; the UI gets labels from the
   server instead of its own list.
2. **One helper contract.** What to ask, what comes back, limits. It generates the delegate's
   description and the "what can you do" list.
3. **One item contract.** Kind, id, display name, file name with extension, where it came from, its
   report.
4. **A turn trace**, stored with each turn: steps, durations, tokens, thinking length, guards and repairs
   that fired, errors - shown in a developer panel in the UI, and used by the evaluation. A turn that
   fails (model unreachable) is kept with its error.
5. **Limits from the server.** Slot size read from llama-server, not a constant. A thinking policy per
   step (for example: think on the first step, less when only wording the answer) - measured first.
6. **workers.py split** (1,018 lines today) into the runner, the contracts, the checks and the repairs.

---

## 11. Build order

Each stage ends with your review before the next starts.

1. **This map** - agreed.
2. **Foundations** - tool definition, item contract, result contract, turn trace, limits from the server.
   Done when: every test passes, the app behaves as before, and the trace shows each turn.
3. **Tools, role by role** - main, web, knowledge, data, coder: each tool's use cases, its gaps from
   section 7, tests that call it directly (no model).
4. **New capabilities** - the ones you pick from section 6.
5. **Prompts and checks** - rewritten against the contracts; checks kept or dropped using the trace.
6. **Evaluation, once** - about 40 real tasks covering the capability map (single tools and chains), plus
   10 held back for scoring only; run on the 26B, then the 12B. What fails goes back to the map, not
   into patches.
7. **UI polish, clean-up, packaging** - merge the old docs, README, GitHub, the exe.

---

## 12. Decisions for you

1. **Minimum model:** design on the 26B, the 12B must still work - confirm.
2. **New capabilities for this round:** which of section 6's NEW rows. My suggestion for first: playable
   programs and web pages, picture editing, finished deliverables, notes management, the capability list;
   the plan card after them.
3. **Guessing checks:** measure them with the trace, then keep or drop - agree?
4. **Thinking:** measure "think on the first step only" against "always" for speed and quality.
5. **Context:** read the slot size from the server; on Kaggle the 26B leaves room for 32k per conversation
   instead of 16k.
6. **This week's three fixes** (raw-JSON reply, bold question, "I'll try"): commit them before stage 2.

---

## 13. Role checklists

Each role's target as a checklist: **[x]** exists today and stays, **[ ]** to build. A role is done when
every box is ticked and its acceptance test passes.

### 13.0 Foundation for every role: one embedding model (EmbeddingGemma 2)

**Decision (the user, 2026-10-08):** replace bge-small with **EmbeddingGemma 2, the full model** - text,
code, images, audio and video in one space of 768 numbers - so one index holds every kind of item and one
question finds any of them (a typed question can find a matching image or a screenshot).

- [x] Where it runs: **this PC** - an RTX 3050 Laptop GPU (bfloat16 works there, unlike the T4, where
      float16 gave NaN), 15.7 GB of RAM; Google gives ~567 MB for the full model, quantised. Search,
      indexing and recall then keep working when Kaggle is off, with no second tunnel.
- [x] Served by llama-server (`--embeddings`, plus its multimodal projector) - the same engine as the LLM.
      Build b11515 (CUDA 13.4) with the Q8_0 model (296 MB) and projector (529 MB), in C:\zen-data. The app
      starts it on first use (app/rag/embed_server.py: this computer only, port 8070; the GPU with the
      layers fitted to free memory, else the CPU) and stops it when it exits; one already answering with
      the same model file is used as it is.
- [x] Gate: llama-server must return image vectors. Passed (2026-10-09), checked by search instead of
      against transformers (no second download): 15 of 15 captions found their own picture first among 15,
      look-alikes included (three lions, two pandas, three line charts); the same picture twice gives the
      same vector. 0.27 s a picture.
- [x] Text and images first (what the app holds today); audio and video are switched on the day the app
      accepts them - same space, so no re-index then.
- [x] Measured before switching (bench/search_models.py; the questions are user data, kept out of git):
      76 held-out questions about the user's own workspaces (67 with a right passage, 9 about nothing in
      them), the cut-off chosen on half and scored on the other half. Both models put a right passage
      first for 33 of 33 (EmbeddingGemma once second by meaning alone); unrelated questions answered by
      meaning: bge-small 3 of 5, EmbeddingGemma 0 of 5. Over all 76: EmbeddingGemma's right passages
      scored 0.712 or more, unrelated at most 0.631 (cut-off 0.68); bge-small's overlapped (0.575 / 0.688).
      Pictures: own caption 0.623-0.81, unrelated questions at most 0.604 (cut-off 0.64). Speed: 21.7
      passages a second when re-indexing, 0.04 s a question. analyze_text's labels (a business's own
      categories): 56% / 45% against bge-small's 53% / 29%.
- [x] One full re-index (vectors from two models can't be mixed); the index records which model made each
      vector. Each workspace is re-indexed in the background when it is first opened (the user's largest,
      254 passages and 19 pictures, within a minute of opening); search meanwhile uses keywords for what waits.
- [ ] Every item kind embedded: chats, notes, documents (chunks), reports, tables (name, columns, a few
      values) and pictures (the picture itself) - done; web pages read (13.1) - not yet; audio and video
      when the app accepts them. Search stays hybrid: keywords (FTS5) + vectors, fused (RRF).

### 13.1 Web (frozen 2026-10-08)

**Tier 1 now:** A (statuses, routing, the fast path), B (shape, `recent`), D (MediaWiki), E, F and G's
way of working. **Tier 2, when chosen:** C (the browser) and everything that needs 13.0 (focus and
ranking by meaning).

**Goal:** one general web helper that handles most of what users ask of the web, with a few deliberate
edge cases - not a tool that grows a patch after every test.

**The rule that keeps it general:** every link goes through the same steps (check → adapter or fast
fetch → browser if needed → extract → focus → report). A new failure becomes a test case and a fix to one
of those general steps - never a rule for one site. The only per-site code allowed is an **adapter for a
site's official API**, one file each.

**What users ask of it (the majority it must handle):**
1. Current facts and news ("latest", "who won", "when does it return", prices) - search, read 1-3 pages.
2. A link the user gives - an article, docs, a GitHub repo or file, a YouTube video, a PDF.
3. A dataset or file to download - found on a page, then downloaded with the user's approval.
4. Docs and how-tos - sometimes several pages of one site.
5. Lists and tables on pages - rankings, schedules, specs, episode lists.
6. Comparing sources - products, opinions, conflicting reports.

**What was measured to choose this design (2026-10-08):**
- Today's helper: 43 of 58 research calls fully answered (74%). Failures: pages blocked to the plain
  fetch (403), answers from search snippets instead of read pages, data that isn't public, unclear tasks.
- Search is not the weak part: 72 of 72 stored searches returned links, and fresh ones (a Wikipedia
  snippet 11 days old for "latest One Piece episode"). ddgs stays.
- Playwright MCP (Microsoft, Apache 2.0) with the installed Edge, headless: read a JavaScript-only page,
  a news article, BBC, Wikipedia and Kaggle (where Crawl4AI got a bot check). Blocked: Fandom
  (Cloudflare check, 403) and Amazon after 2-3 quick visits. Google, Bing and DuckDuckGo all refused a
  search from it on the first query - so the browser never searches.
- Correction: Crawl4AI's earlier Fandom read came from its default `--disable-blink-features=
  AutomationControlled`, a flag that hides the automation - stealth, so it does not count. An honest
  browser is blocked by Fandom; Fandom is reached through its wiki API only.
- The browser is the second step, not the first: 2-10 s a page against under 1 s, and a full page
  snapshot is 12,000-18,700 tokens (Wikipedia, Amazon, Kaggle, BBC) against ~3,000 of clean text.
  `find` answers in 150-1,700 tokens. All 25 of its tools cost ~5,400 tokens of schemas; the five below
  ~1,600.

**The design**
```
main ── research_web(task) ──► web helper (slot 1)
  tools it sees
    web_search(query, site?, recent?)   ddgs tiers: DuckDuckGo, Brave, Mojeek, Wikipedia → Google, Yahoo, Startpage
    read_webpage(url, find?)            the reading pipeline below → clean Markdown + one status
    browser_open / browser_find / browser_click / browser_type / browser_screenshot
                                        Playwright MCP behind our wrapper (one session per task, noise stripped)
    ask_user(question)                  when the task is unclear
    submit_result(...)                  the shared result shape
  reading pipeline (code, not the model)
    1 address check (public only, every redirect)
    2 site adapter when one fits (GitHub, YouTube, MediaWiki/Fandom) or a document (PDF, Word, PowerPoint)
    3 fast fetch → trafilatura → Markdown (tables kept)
    4 blocked / bot page / too little text → the browser opens it → its HTML → the same extraction
    5 long page → focus by meaning (13.0), the opening always kept
    6 status: ok | cut | blocked | not found | unsupported, with what to try next
    7 saved to the page cache and the index (address, date, how it was read)
```

**A. One reading pipeline (read_webpage)**
- [x] Only public addresses; every redirect checked again (the computer's own services stay out of reach)
- [x] Size limits (3 MB download, 12,000 characters of text)
- [ ] Route by what the link is: a web page → the steps above; PDF / Word / PowerPoint → one document
      converter (the same one the knowledge role uses); CSV / Excel → "a data file: download it as a table";
      an image → "an image: download or look at it"
- [ ] Fast path: a plain fetch, then main-text extraction (trafilatura) to Markdown - tables kept as rows,
      menus, footers and pop-ups out, garbled characters repaired (measured on Fandom), link addresses out
      of the text but kept as a list
- [x] Browser fallback (C) **only when** the fast path is blocked, gets a bot page or too little text - one
      general test, not a list of sites; the page's HTML goes through the same extraction, never the
      snapshot
- [x] Focus a long page on the question (today: by words)
- [ ] Focus by meaning and headings (13.0), always keeping the page's opening
- [x] Page age (published / updated date) for "latest" questions
- [ ] Every read ends in one status: **ok**, **cut** (long page, focused), **blocked** (bot check, login
      or paywall - detected by general signals), **not found**, or **unsupported** - each with what to try
      next

**B. Search (web_search)**
- [x] Public search engines through ddgs, no key, DuckDuckGo first; a second tier only when the first finds
      nothing - kept
- [ ] `recent` (day / week / month / year) for "latest" questions, passed to the engines' own time filter
- [ ] Results in one shape (title, link, snippet, site, date when known), duplicates removed, ranked by
      meaning against the task (13.0) so the best 1-3 are read first

**C. Browser (new) - Playwright MCP**
- [x] One MCP client in Zen (the official Python SDK); another MCP server later is settings, not code
- [x] Playwright MCP pinned (0.0.83), the installed Edge, headless, an in-memory profile, no stealth; one
      browser for the app, started on first use and closed when idle
- [ ] Five tools for the helper - open, find, click, type, screenshot. `open` returns the title, status and
      the start of the page's clean text (its HTML through the same extraction as A - not nothing, not the
      whole snapshot); the code lines MCP adds are stripped; the session stays open for the whole task
- [ ] Browser downloads off: a click on a download button never saves a file - the link is offered through
      the normal approved download. Gate: checked by clicking Kaggle's Download button; if Playwright MCP
      can't turn downloads off, they land in a quarantine folder that is emptied at once and reported
- [ ] Screenshots go to the helper as an image (Gemma 4 sees them) and to the user as a card
- [ ] Never offered to the model: running code in the page, cookies and storage, file upload, proxies

**D. Site adapters (official APIs) - one interface, one file each**
- [x] GitHub (repository, folder, file)
- [x] YouTube (title, channel, length, date, description)
- [ ] Wikipedia and any MediaWiki wiki, including Fandom (blocked to both the plain fetch and the browser)
- [ ] Adding one later = one file + its tests; nothing else changes

**E. Safety**
- [x] Cite only pages actually read; download links only ones a page gave (checked by code)
- [x] Downloads only with the user's approval
- [x] The browser can't reach this computer: every address checked before it opens, local addresses
      blocked in the browser (Playwright's own list is "not a security boundary"), and the app's API
      refuses requests that come from a web page
- [x] Page text marked as untrusted: instructions inside a page can't steer the helper
- [ ] Honest identity, no stealth, no proxies, no CAPTCHA solving; a blocked page is reported, and another
      source is tried
- [ ] Nothing that changes a site: no submitting, buying, posting or signing in from this role. Code can't
      judge every click, so the real guard is what a click can reach: the browser holds no logins, the
      helper holds only its task (never the user's files or notes), and every page and click shows in the
      steps

**F. Reliability**
- [x] Bounded retries on temporary errors only (rate limits, server errors)
- [x] The same failed page or search is not repeated
- [ ] Page cache in the index: hours for news and "latest", days for docs - a repeat costs nothing and
      the knowledge role can recall pages read before. Web pages are their own kind: always shown with
      address and date, ranked below the user's own files, and dropped after 30 days unless saved, so
      old web text never crowds out the user's documents or passes as a current fact
- [x] Per-site pacing: a pause between pages of one site and a cap per task (Amazon checked us after 2-3
      quick visits)
- [ ] Versions pinned (ddgs, trafilatura, Playwright MCP, the MCP SDK); an update every 1-3 months, kept
      only if the acceptance test passes

**G. The web helper's way of working (prompt + runner) - after the tools are frozen**
- [x] Official and primary sources before third-party articles; dated answers for "latest"
- [ ] A loop: plan 1-3 searches → read the best 1-3 pages → gaps? search again (bounded) → answer; when
      sources disagree, say so
- [ ] The browser only when reading fails or the task needs clicking, a form or a screenshot
- [ ] ask_user when the task is unclear (which shop, which country, which version)
- [ ] Step budget 6 → 12; page texts already used shrink to their key lines, so it fits 16k context (more
      if decision 12 raises the slot)
- [ ] The shared result shape: answer, sources (address, title, date, how read), a table when the task
      compares things, download links, what failed and why, what to try next

**H. Not handled, by design**
Getting past bot checks, CAPTCHAs, paywalls or logins; searching through the browser; scraping rules for
single sites; site-wide crawling; buying, posting or signing in.

**Build order (web)**
1. Reader: trafilatura fast path, statuses, tables, the document route.
2. MCP client + Playwright MCP + the safety wrapper; the browser fallback inside read_webpage.
3. The five browser tools, screenshots, pacing, the page cache.
4. MediaWiki adapter; search shape and `recent`.
5. EmbeddingGemma 2 (13.0) → focus by meaning, results ranked by meaning, web pages in the index.
6. Prompt and runner: the loop, ask_user, the budget, the result shape.
7. Acceptance test.

**Gates: the open questions, each with its answer chosen in advance**
Each is measured once during the build; the result picks a branch already written here, so no gate
reopens the design.

| Gate | Measured how | Pass | Fail |
|---|---|---|---|
| trafilatura vs today's extractor | the saved pages of the acceptance set | trafilatura replaces the text step (our page card, dates and file links stay) | today's extractor stays; trafilatura is not added |
| Browser downloads off | click Kaggle's Download button | downloads refused in the browser | quarantine folder, emptied at once |
| Image vectors from llama-server | embed 10 pictures, compare with transformers | llama-server for everything | transformers on the RTX 3050 (bfloat16) |
| EmbeddingGemma 2 vs bge-small | held-out questions from the user's chats | switch and re-index | keep bge-small, nothing re-indexed |
| 12 steps fit 16k context | the acceptance run | keep 16k | raise the worker slot to 32k (decision 12) |

**Known costs, accepted:** Playwright MCP needs Node (installed here; a packaged app must ship it); a
browse task takes minutes on the 26B (the acceptance run records the time of each task).

**Acceptance test**
- [ ] About 25 real links (the user's chats + one of each kind in A), about 10 research questions, the 4
      blocked cases (Fandom, Google search, Amazon, Kaggle download) and 3 browser tasks (a JavaScript
      page, a dataset page's file list, a product comparison on a shop that allows it). Code tests run
      offline from saved copies; the live run checks updates. Half the questions are held out and measured
      once, at the end.
- [ ] Target: at least 90% of questions whose answer is public fully answered from pages actually read
      (baseline 74%), every blocked page reported as blocked, tables intact.

**When it is done, the web helper can:** search with dates; read any public page, JavaScript sites
included; read PDFs, Office files, GitHub, YouTube and wikis; keep tables; open, click, type and take
screenshots to dig into a site; compare pages in a table; find data files and offer them for download with
approval; ask when a task is unclear; remember pages it read; and say plainly what was blocked and what to
try instead. It can't: get past bot walls or logins, search Google through a browser, do long shopping
sessions on Amazon, or find data that isn't public.
