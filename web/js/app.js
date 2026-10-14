// Zen's page: the intro, the theme, projects and their chats, the start screen, the message box, and a
// reply as it streams (or as a saved chat replays through the same calls).

import { api, detail, KEYS, send, serverEvents, state, API } from "./api.js";
import { closeAsk, decision, openAsk } from "./ask.js";
import { CARD_TYPES, addRun, artifactCard, codeCard, finishAnswer, onDrawn, passagesEl, redrawDiagrams, resultsCard, webSourcesEl } from "./cards.js";
import { forgetCharts, redrawCharts } from "./charts.js";
import { attachedCount, closeAsked, closeDrawer, closeFile, drawerOpen, fileOpen, fillDrawer, forgetSent, hooks, offerFollowUps, openDrawer, openItem, point, sendFiles, sentFiles, takeSent, wireFiles } from "./files.js";
import { hooks as mentionHooks, wireMention } from "./mention.js";
import { hooks as pinHooks, pinsFromZen, renderPins } from "./pins.js";
import { activityEl, loadTools, traceEl } from "./steps.js";
import { $, addCaret, age, cap, closeDialog, confirmDialog, dialogOpen, h, load, markdown, plural, polish, prettyModel, promptDialog, save, showDialog, svg, toast, copyText, projectHue } from "./util.js";
import { wireViewer } from "./viewer.js";

const thread = $("#thread"), scroller = $("#scroller"), input = $("#input"), composer = $("#composer");

/* ═══ Theme: the system's light or dark until the user picks one ════════ */
const systemLight = matchMedia("(prefers-color-scheme: light)");
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (window.Chart) redrawCharts();
  redrawDiagrams();
}
applyTheme(load(KEYS.theme) || (systemLight.matches ? "light" : "dark"));
systemLight.addEventListener("change", e => { if (!load(KEYS.theme)) applyTheme(e.matches ? "light" : "dark"); });
$("#themeBtn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  save(KEYS.theme, next);
  applyTheme(next);
});

/* ═══ The intro: once a session, its sound keeping the animation's clock; a click or a key skips it ═══ */
function playIntro() {
  const intro = $(".intro");
  let seen = false;
  try { seen = sessionStorage.getItem("zen.intro") === "1"; sessionStorage.setItem("zen.intro", "1"); } catch {}
  if (!intro || seen || matchMedia("(prefers-reduced-motion: reduce)").matches) { intro?.remove(); document.body.classList.add("landed"); return; }
  intro.hidden = false;
  let sound = null;
  (async () => {
    try {
      const context = new AudioContext();
      const buffer = await context.decodeAudioData(await (await fetch("/static/sounds/startup.ogg")).arrayBuffer());
      await context.resume().catch(() => {});
      const [veil] = intro.getAnimations();
      const offset = (veil?.currentTime ?? Infinity) / 1000;
      if (!intro.isConnected || context.state !== "running" || !(offset < buffer.duration)) { await context.close(); return; }
      const source = context.createBufferSource(), gain = context.createGain();
      source.buffer = buffer;
      source.connect(gain).connect(context.destination);
      source.onended = () => context.close();
      source.start(0, offset);
      sound = { context, gain };
    } catch { /* a silent intro is still an intro */ }
  })();
  const done = () => { intro.remove(); removeEventListener("pointerdown", skip, true); removeEventListener("keydown", skip, true); };
  const skip = () => {
    document.body.classList.add("landed");
    if (sound) { sound.gain.gain.setTargetAtTime(0, sound.context.currentTime, .04); setTimeout(() => sound.context.close().catch(() => {}), 300); }
    done();
  };
  addEventListener("pointerdown", skip, true);
  addEventListener("keydown", skip, true);
  intro.addEventListener("animationend", e => { if (e.target === intro) { document.body.classList.add("landed"); done(); } });
}

/* ═══ The model's state ═════════════════════════════════════════════════ */
async function refreshStatus() {
  let s;
  try { s = await api("/status"); }
  catch { s = { ready: false, reachable: false, message: "Can't reach the server. Start it with: uvicorn app.main:app" }; }
  s.reachable ??= true;
  state.status = s;
  const chip = $("#modelState");
  chip.hidden = s.ready;
  chip.classList.toggle("down", !s.reachable);
  chip.textContent = s.reachable ? "Model starting" : "Server offline";
  chip.title = s.message || "";
  $("#here").title = s.ready ? `${prettyModel(s.model)}, running on this computer` : s.message || "";
  renderHello();
  clearTimeout(state.poll);
  if (!s.ready) state.poll = setTimeout(refreshStatus, 5000);  // wait for the model to come up
  updateComposer();
}

/* ═══ Projects (workspaces on the server) ═══════════════════════════════ */
const initial = name => (name.trim()[0] || "P").toUpperCase();
// The address keeps the open project and chat (#project=…&chat=…): a reload or a saved link lands there
const place = () => new URLSearchParams(location.hash.slice(1));
function mark() {
  const at = state.ws ? `#project=${state.ws.id}${state.chat ? `&chat=${state.chat.id}` : ""}` : " ";
  if (location.hash !== at.trim()) history.replaceState(null, "", at.trim() || location.pathname);
}
async function loadWorkspaces(prefer) {
  state.workspaces = await api("/workspaces");
  const id = [prefer, place().get("project"), load(KEYS.ws)].find(w => w && state.workspaces.some(x => x.id === w));
  if (id) await selectWorkspace(id);
  else showProjects();
}

// "used today", "used yesterday", "used 3 days ago", then the date
function used(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return "";
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(t).setHours(0, 0, 0, 0)) / 864e5);
  return days <= 0 ? "used today" : days === 1 ? "used yesterday" : days < 7 ? `used ${days} days ago`
    : `used ${new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" })}`;
}

function showProjects() {
  detach();
  closeAsk(true);
  closeFile();
  closeDrawer();
  document.body.classList.add("picking");
  const list = $("#projectList");
  const card = w => {
    const open = () => selectWorkspace(w.id);
    const more = h("button", { class: "icon-btn p-more", type: "button", title: "More", "aria-label": `More for ${w.name}`, html: svg("more") });
    more.addEventListener("click", e => { e.stopPropagation(); projectMenu(more, w); });
    const stats = h("div", { class: "p-stats" }, w.chats ? plural(w.chats, "chat") : "No chats yet");
    const last = h("div", { class: "p-last" });
    const el = h("div", { class: "project", role: "button", tabindex: "0", onclick: open, onkeydown: e => { if (e.key === "Enter" && e.target === el) open(); } },
      h("div", { class: "p-top" }, h("span", { class: "tile", style: `--hue: ${projectHue(w.id)}` }, initial(w.name)), more),
      h("div", { class: "p-name", title: w.name }, w.name), stats, last,
      h("div", { class: "p-used" }, cap(used(w.updated_at))));
    // what's inside: its files and its latest chat, filled in once they arrive
    Promise.all([api(`/workspaces/${w.id}/items`).catch(() => null), api(`/workspaces/${w.id}/chats`).catch(() => null)]).then(([items, chats]) => {
      if (items) stats.textContent = [w.chats ? plural(w.chats, "chat") : "No chats yet", items.length ? plural(items.length, "file") : ""].filter(Boolean).join(" · ");
      if (chats?.length) last.replaceChildren(h("span", { html: svg("chat") }), h("span", { class: "t" }, chats[0].title));
    });
    return el;
  };
  const add = h("button", { class: "project new", type: "button", onclick: newProject },
    h("span", { class: "p-plus", html: svg("plus") }), h("div", { class: "p-name" }, "New project"),
    h("div", { class: "p-stats" }, "Its own chats, files and notes"));
  list.replaceChildren(...state.workspaces.map(card), add);
  $("#projectsLede").textContent = state.workspaces.length ? "Pick up where you left off, or start something new." : "Start your first project: its chats, files and notes stay together.";
  setTimeout(() => (list.querySelector(".project") || add).focus({ preventScroll: true }), 50);
}
// A project's menu: rename, delete (the card itself opens it)
let menuEl = null;
function closeMenu() { menuEl?.remove(); menuEl = null; document.removeEventListener("pointerdown", outsideMenu, true); }
function outsideMenu(e) { if (!menuEl?.contains(e.target)) closeMenu(); }
function projectMenu(button, w) {
  if (menuEl) { closeMenu(); return; }
  const item = (icon, label, run, cls = "") => h("button", { class: `menu-item ${cls}`, type: "button", role: "menuitem",
    onclick: () => { closeMenu(); run(); } }, h("span", { html: svg(icon) }), label);
  menuEl = h("div", { class: "menu", role: "menu" }, item("pencil", "Rename", () => renameWorkspace(w)), item("trash", "Delete project", () => deleteWorkspace(w), "danger"));
  document.body.append(menuEl);
  const r = button.getBoundingClientRect();
  menuEl.style.top = `${Math.min(r.bottom + 6, innerHeight - menuEl.offsetHeight - 8)}px`;
  menuEl.style.left = `${Math.max(8, r.right - menuEl.offsetWidth)}px`;
  setTimeout(() => document.addEventListener("pointerdown", outsideMenu, true));
}
async function newProject() {
  const asked = promptDialog("New project", "A project keeps its own chats, files, tables and notes together.", "", "Create");
  $("#dialogInput").placeholder = "Project name, like Q4 sales review";
  const name = await asked;
  $("#dialogInput").placeholder = "";
  if (name?.trim()) createProject(name.trim());
}
async function createProject(name) {
  try { const ws = await send("POST", "/workspaces", { name }); await loadWorkspaces(ws.id); }
  catch (e) { toast(e.message, true); }
}
async function renameWorkspace(w) {
  const name = await promptDialog("Rename project", "", w.name);
  if (!name || name === w.name) return;
  try { await send("PATCH", `/workspaces/${w.id}`, { name }); state.workspaces = await api("/workspaces"); showProjects(); }
  catch (e) { toast(e.message, true); }
}
async function deleteWorkspace(w) {
  if (!await confirmDialog(`Delete “${w.name}”?`, "All its chats, files, tables, notes and images are removed. This can't be undone.", "Delete")) return;
  try { await api(`/workspaces/${w.id}`, { method: "DELETE" }); } catch (e) { toast(e.message, true); return; }
  if (load(KEYS.ws) === w.id) save(KEYS.ws, null);
  toast(`Deleted ${w.name}`);
  state.workspaces = await api("/workspaces");
  showProjects();
}

async function selectWorkspace(id) {
  detach();
  state.ws = state.workspaces.find(w => w.id === id);
  save(KEYS.ws, id);
  document.body.classList.remove("picking");
  $("#projectTile").textContent = initial(state.ws.name);
  $("#projectTile").style.setProperty("--hue", projectHue(state.ws.id));
  $("#projectName").textContent = state.ws.name;
  closeFile();
  await Promise.all([loadChats(), loadItems()]);
  const saved = [place().get("chat"), load(KEYS.chat)].find(c => c && state.chats.some(x => x.id === c));
  if (saved) await openChat(saved);
  else newChat();
}
function leaveProject() {
  save(KEYS.ws, null);
  save(KEYS.chat, null);
  state.ws = null; state.chat = null; state.pending = null;
  mark();
  api("/workspaces").then(list => { state.workspaces = list; showProjects(); }).catch(e => toast(e.message, true));
}

/* ═══ Chats ═════════════════════════════════════════════════════════════ */
let query = "";
async function loadChats() {
  try { state.chats = await api(`/workspaces/${state.ws.id}/chats`); } catch (e) { toast(e.message, true); }
  renderChats();
  clearTimeout(state.chatPoll);  // a turn running in a chat this page is not following: notice when it ends
  if (state.chats.some(c => c.running && !(state.busy && c.id === state.chat?.id))) state.chatPoll = setTimeout(loadChats, 3000);
}

function renderChats() {
  const list = $("#chatList"), shown = state.chats.filter(c => !query || c.title.toLowerCase().includes(query));
  if (!shown.length) { list.replaceChildren(h("div", { class: "chat-empty" }, query ? "No chats match" : "No chats yet")); return; }
  const groups = new Map();
  for (const c of shown) { const g = age(c.updated_at); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
  list.replaceChildren(...[...groups].flatMap(([group, chats]) => [h("div", { class: "label" }, group), ...chats.map(c => {
    const del = h("button", { class: "del", type: "button", title: "Delete chat", "aria-label": `Delete chat ${c.title}`, html: svg("trash") });
    del.addEventListener("click", e => { e.stopPropagation(); deleteChat(c); });
    return h("div", { class: `row chat-row${state.chat?.id === c.id ? " on" : ""}`, role: "button", tabindex: "0", title: c.title,
      onclick: () => openChat(c.id), onkeydown: e => { if (e.key === "Enter" && e.target === e.currentTarget) openChat(c.id); } },
      h("span", { class: "t" }, c.title),
      c.running ? h("span", { class: "live", title: "Answering…" }) : c.pending ? h("span", { class: "needs", title: "Waiting for your answer" }, "?") : null, del);
  })]));
}

function setTitle() {
  $("#chatTitle").textContent = state.chat?.title || "New chat";
  document.title = state.chat ? `${state.chat.title} · Zen` : "Zen";
  mark();
}

function newChat() {
  detach();
  closeAsk(true);
  state.chat = null;
  state.pending = null;
  save(KEYS.chat, null);
  clearThread();
  renderPins();
  showHello(true);
  renderChats();
  setTitle();
  updateComposer();
  closeSide();
  input.focus();
}

async function openChat(id, again = false) {
  if (!again && state.chat?.id === id && !document.body.classList.contains("empty")) { closeSide(); return; }  // already open
  detach();
  closeAsk(true);
  const ticket = ++state.opening;  // a newer click wins
  let data;
  try { data = await api(`/chats/${id}/messages`); }
  catch (e) { toast(e.message, true); await loadChats(); return; }
  if (ticket !== state.opening) return;
  state.chat = data.chat;
  const pins = data.chat.pins || [];  // as stored: a replayed "pins" card says what they were then
  state.pending = null;
  save(KEYS.chat, id);
  clearThread();
  const rows = data.messages, live = rows.filter(m => m.live);
  showHello(!rows.length);
  if (rows.length) {
    // A turn still running (the page reloaded, or the chat was left mid-answer): show what is stored
    // before it, then follow the turn from its start.
    const continues = data.running && !live.some(m => m.role === "user");  // answering a question
    let view = replay(rows.filter(m => !m.live), data.chat, continues);
    if (data.running) {
      if (continues && view) {
        const said = live.find(m => m.role === "tool");
        view.resume(said?.skipped ? null : said?.answer ?? null, said?.id);
      } else {
        const user = live.find(m => m.role === "user");
        if (user) addUser(user.text, user.files);
        view = assistantView();
      }
      run(`/chats/${id}/stream`, null, view);
    }
  }
  state.chat.pins = pins;
  renderPins();
  renderChats();
  setTitle();
  updateComposer();
  closeSide();
  requestAnimationFrame(() => stick(true));
}

async function deleteChat(c) {
  if (!await confirmDialog(`Delete “${c.title}”?`, "Its messages are removed and it is no longer found by recall. This can't be undone.", "Delete")) return;
  try { await api(`/chats/${c.id}`, { method: "DELETE" }); } catch (e) { toast(e.message, true); return; }
  if (state.chat?.id === c.id) newChat();
  await loadChats();
}

// Search: the row becomes a field that filters the list as you type
function openSearch() {
  $("#searchRow").hidden = true;
  $("#searchBox").hidden = false;
  $("#searchInput").focus();
}
function closeSearch() {
  query = "";
  $("#searchInput").value = "";
  $("#searchBox").hidden = true;
  $("#searchRow").hidden = false;
  renderChats();
}
const searching = () => !$("#searchBox").hidden;

/* ═══ The project's items ═══════════════════════════════════════════════ */
async function loadItems() {
  try { state.items = await api(`/workspaces/${state.ws.id}/items`); } catch { state.items = []; }
  const n = state.items.length;
  $("#filesBadge").hidden = !n;
  $("#filesBadge").textContent = n > 99 ? "99+" : n;
  $("#filesCount").textContent = n || "";
  renderHello();
  renderPins();  // their names, as the files are now
  if (drawerOpen()) fillDrawer();
}

/* ═══ The start screen ══════════════════════════════════════════════════ */
function showHello(on) { document.body.classList.toggle("empty", on); if (on) renderHello(); }
const hideHello = () => document.body.classList.remove("empty");
function renderHello() {
  const s = state.status, user = s?.user;
  $("#helloText").textContent = `What can I help with${user ? `, ${user}` : ""}?`;
  const notice = $("#helloNotice");
  notice.hidden = !s || s.ready;
  if (s && !s.ready) notice.replaceChildren(h("b", {}, s.reachable ? "The model isn't ready yet" : "The server isn't reachable"), s.message || "");
  const tables = state.items.filter(i => i.kind === "table" && !i.parent);
  const files = state.items.filter(i => i.kind === "file" && !/\.chart\.json$/i.test(i.name || ""));  // a chart is no read
  const ideas = [];
  if (tables[0]) ideas.push(["chart", `What stands out in ${tables[0].name}? Show a chart`]);
  if (files[0]) ideas.push(["doc", `Summarise ${files[0].name}`]);
  ideas.push(["chart", "Analyse a table"], ["globe", "What's new in the latest llama.cpp release?"], ["sun", "What's the weather like this week?"],
             ["image", "Create an image of a cosy reading corner"], ["flow", "Draw a flowchart of how a web request is served"]);
  $("#starters").replaceChildren(...ideas.filter(([, t]) => t !== "Analyse a table" || !tables.length).slice(0, 4).map(([icon, text]) =>
    h("button", { class: "starter", type: "button", html: svg(icon), onclick: () => text === "Analyse a table" ? $("#fileInput").click() : submit(text) }, text)));
}

/* ═══ The thread ════════════════════════════════════════════════════════ */
function clearThread() { forgetCharts(); forgetSent(); thread.replaceChildren(); }
const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
function stick(force = false) { if (force || state.stick) scroller.scrollTop = scroller.scrollHeight; }
scroller.addEventListener("scroll", () => {
  state.stick = nearBottom();
  $("#jump").classList.toggle("show", !state.stick && !document.body.classList.contains("empty"));
}, { passive: true });
$("#jump").addEventListener("click", () => { scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }); state.stick = true; });
onDrawn(() => stick());

function addUser(text, files = null) {  // files: those a saved message was sent with
  hideHello();
  const el = h("div", { class: "user enter" }, files?.length ? sentFiles(files) : null, text ? h("p", {}, text) : null);
  thread.append(el);
  return el;
}

function metaEl({ seconds, first, stats, answer, stopped }, textEl) {
  const parts = [];
  if (seconds) parts.push(`${seconds.toFixed(1)} s`);
  if (first != null) parts.push(`first word ${first.toFixed(1)} s`);
  const tokens = stats.reduce((n, s) => n + (s.output_tokens || 0), 0);
  const gen = stats.reduce((t, s) => t + (s.tokens_per_second ? (s.output_tokens || 0) / s.tokens_per_second : 0), 0);
  if (tokens && gen) parts.push(`${(tokens / gen).toFixed(0)} tok/s`);
  const el = h("div", { class: "meta enter" });
  if (answer) {
    const copy = h("button", { class: "icon-btn", type: "button", title: "Copy", "aria-label": "Copy the answer", html: svg("copy") });
    copy.addEventListener("click", () => copyText(answer, copy));
    el.append(copy);
    if ("speechSynthesis" in window) {
      const speak = h("button", { class: "icon-btn", type: "button", title: "Read aloud", "aria-label": "Read the answer aloud", html: svg("speak") });
      speak.addEventListener("click", () => {
        if (speechSynthesis.speaking) { speechSynthesis.cancel(); return; }
        const say = new SpeechSynthesisUtterance(textEl.textContent);
        say.onend = say.onerror = () => (speak.innerHTML = svg("speak"));
        speak.innerHTML = svg("hush");
        speechSynthesis.speak(say);
      });
      el.append(speak);
    }
  }
  if (stopped) el.append(h("span", { class: "stopped" }, "Stopped"));
  if (parts.length) el.append(h("span", { class: "stats", title: tokens ? `${tokens} tokens written` : null }, parts.join(" · ")));
  return el;
}

// "[Source: ComicBook.com]" as the model writes it: shown as a small source pill
const SOURCE_TAG = /\[(?:Source|Sources):\s*([^\]\n]{1,80})\]/gi;

// One reply, built from the stream (or from a saved chat, through the same calls)
function assistantView() {
  const root = h("div", { class: "reply" }), activity = activityEl();
  const cards = h("div", { class: "cards" }), answer = h("div", { class: "answer" }), foot = h("div", { class: "foot" });
  root.append(activity.el, cards, answer, foot);
  thread.append(root);
  let text = "", roundStart = 0, frame = 0, streaming = true, current = null, question = null, fresh = false;
  const stats = [], web = new Map(), passages = [], traces = [];
  let finished = false, traceBtn = null;
  // How the reply ran (the server's trace of each part of the turn): one small button beside the answer's
  // copy and read-aloud, opening the developer's view
  const traceButton = () => {
    if (!finished || !traces.length || traceBtn) return;
    traceBtn = h("button", { class: "icon-btn", type: "button", title: "How this answer ran: steps, times, tools, checks",
      "aria-label": "How this answer ran", html: svg("trace") });
    traceBtn.addEventListener("click", () => showDialog({ title: "How this answer ran", extra: traceEl(traces), ok: "Close", cancel: null, wide: true }));
    const meta = foot.querySelector(".meta");
    if (meta) meta.insertBefore(traceBtn, meta.querySelector(".stats, .stopped"));
    else foot.append(h("div", { class: "meta" }, traceBtn));
  };
  const paint = () => {
    frame = 0;
    answer.innerHTML = markdown(text.replace(SOURCE_TAG, (_, s) => `<span class="cite">${s.replace(/[<>&"]/g, "")}</span>`)
      .replace(/\{(image_\d+)\}/g, "![]($1)"))  // "{image_2}" written as a placeholder: shown as the picture (or dropped when it's on show already)
      .replace(/(<img\b[^>]*?)\bsrc="(image_\d+)"/g, '$1data-item="$2"');  // a workspace picture: resolved once the answer is done, not fetched as a page path
    polish(answer);
    if (streaming && text) addCaret(answer);
    stick();
  };
  const repaint = () => { if (!frame) frame = requestAnimationFrame(paint); };
  const show = el => {
    if (!el) return;
    // a picture a helper made shows once: with the first card that carries it (the save, or its run)
    const seen = new Set([...cards.querySelectorAll("img")].map(i => i.getAttribute("src")));
    el.querySelectorAll(".outputs img").forEach(img => { if (seen.has(img.getAttribute("src"))) img.remove(); });
    el.querySelectorAll(".outputs:empty").forEach(o => o.remove());
    el.querySelectorAll(".below:empty").forEach(o => o.remove());
    cards.append(el);
    stick();
  };
  const promote = step => {  // what a helper found, as cards once it is done
    if (!step) return;
    if (step.tables.length) show(resultsCard(step.tables.slice(-4)));
    if (step.code) step.fileCard ? addRun(step.fileCard, step.code) : show(codeCard(step.code));
    step.tables = [];
    step.code = null;
  };
  // A question: asked in the message box; where it was asked, the chat keeps the decision's line
  const ask = card => {
    const q = { card, slot: h("div", { class: "q-slot" }), decided: false };
    q.decide = (said, allowed) => {
      if (q.decided) return;
      q.decided = true;
      q.slot.replaceWith(decision(q.card, said, allowed));
    };
    show(q.slot);
    return q;
  };

  const view = {
    get question() { return question; },
    text(chunk) {
      if (fresh) {  // another reply in the same turn (after a check): its own paragraph, not glued on
        fresh = false;
        if (text.trim()) text = text.trimEnd() + "\n\n";
        roundStart = text.length;
      }
      if (!text.slice(roundStart)) activity.writing();
      text += chunk;
      repaint();
    },
    round() { fresh = true; },  // the model's step ended; what it writes next starts a new reply
    checking() { fresh = true; activity.checking(); repaint(); },
    toolStart(d) {
      if (text.slice(roundStart).trim()) text = text.trimEnd() + "\n\n";
      roundStart = text.length;
      const step = activity.start(d);
      if (step?.delegate) current = step;
      repaint();
    },
    toolEnd(d) {
      const step = activity.steps.get(d.id);
      if ("answer" in d || d.skipped) {  // a saved chat: this result is the user's answer to a question
        question?.decide(d.skipped ? null : d.answer);
        if (step) activity.answered(d.id);
      } else {
        if (question && !question.decided && question.card.approval) {  // a saved permission: allowed or not
          const m = /"allowed":\s*(true|false)/.exec(d.summary || "");
          question.decide(null, m ? m[1] === "true" : false);
        }
        activity.end(d);
      }
      if (step?.delegate) { promote(step); if (current === step) current = null; }
    },
    artifact(a) {
      if (a.type === "question") {
        question = ask(a);
      } else if (a.type === "answer") {  // the user's answer to a helper's question asked in place
        question?.decide(a.skipped ? null : a.answer);
      } else if (a.type === "pins") {  // Zen pinned or unpinned a file
        pinsFromZen(a.pins);
      } else if (a.type === "sources") {
        for (const item of a.items || []) item.url ? web.set(item.url, item) : passages.push(item);
        if (current && !(a.tool || "").endsWith("_worker")) activity.evidence(current, a);
      } else if (CARD_TYPES.has(a.type) || !current) {
        const el = artifactCard(a);
        if (a.type === "file" && current && el) current.fileCard = el;
        show(el);
      } else {  // a helper's working: shown in its step; its result tables and last code also as cards
        activity.evidence(current, a);
        if (a.type === "code") current.code = a;
        else if (a.type === "table" && !/^Columns of /.test(a.title || "")) current.tables.push(a);
      }
    },
    worker(e) { if (current) activity.worker(current, e); if (e.event === "turn" && e.usage) stats.push(e.usage); },
    trace(t) { if (t) { traces.push(t); traceButton(); } },
    stats(s) { stats.push(s); fresh = true; },
    ask(pending) {  // the turn pauses here until the user answers, in the message box
      streaming = false;
      paint();
      if (!question || question.decided) question = ask(pending);
      Object.assign(question.card, pending);
      openAsk(question.card, { stoppable: !!pending.inplace, onChoose: said => giveAnswer(said), onStop: stopWaiting });
      activity.wait(pending.call_id);
      stick();
    },
    resume(said, callId = state.pending?.call_id, inplace = state.pending?.inplace) {
      question?.decide(said);
      closeAsk();
      if (inplace) {  // a helper asked in place: its step goes on working
        activity.resumed(callId);
        streaming = true;
        return;
      }
      activity.answered(callId);
      const step = activity.steps.get(callId);
      if (step === current) current = null;
      streaming = true;
      activity.running("Thinking…");
    },
    finish(final, timing) {
      streaming = false;
      if (final != null && final !== "") text = final;
      cancelAnimationFrame(frame);
      paint();
      finishAnswer(answer);  // only once finished: a half-written diagram would fail to draw
      if (current) { promote(current); current = null; }
      activity.finish(timing?.seconds);
      if (web.size) foot.append(webSourcesEl([...web.values()]));
      if (passages.length) foot.append(passagesEl(passages));
      if (timing || text.trim()) foot.append(metaEl({ ...(timing || {}), stats, answer: text.trim() }, answer));
      finished = true;
      traceButton();
      stick();
    },
    error(message) {
      streaming = false;
      paint();
      activity.stop();
      foot.append(h("div", { class: "callout" }, h("b", {}, "This reply didn't finish."), message));
      finished = true;
      traceButton();
      stick();
    },
    stop(timing) {
      streaming = false;
      paint();
      activity.stop(timing?.seconds);
      foot.append(metaEl({ ...timing, stats, stopped: true, answer: text.trim() }, answer));
      finished = true;
      traceButton();
    },
  };
  return view;
}

// A saved chat, through the same view calls the live stream uses
function replay(messages, chat, keepOpen = false) {  // returns the last reply's view
  let view = null, last = "";
  const close = () => { if (view) view.finish(last); };
  for (const m of messages) {
    if (m.role === "user") {
      close();
      addUser(m.text, m.files);
      view = assistantView();
      last = "";
      continue;
    }
    if (!view) continue;
    if (m.role === "assistant") {
      view.round();
      if (m.text) { view.text(m.text); last = m.text; }
      for (const t of m.tools || []) view.toolStart(t);
    } else if (m.role === "tool") {
      view.toolEnd({ id: m.id, name: m.name, status: m.is_error ? "failed" : "ok", summary: m.summary,
        ...("answer" in m ? { answer: m.answer } : {}), ...(m.skipped ? { skipped: true } : {}) });
    } else if (m.role === "artifact") {
      view.artifact(m.artifact);
    } else if (m.role === "trace") {
      view.trace(m.trace);
    }
  }
  if (keepOpen) return view;  // the running turn goes on in it
  if (view && chat.pending) {
    state.pending = { ...chat.pending, view };
    view.ask(chat.pending);
  } else close();
  return view;
}

/* ═══ Talking ═══════════════════════════════════════════════════════════ */
async function submit(text) {
  text = text.trim();
  const files = attachedCount();
  if ((!text && !files) || state.uploading || state.busy && !state.pending) return;
  if (state.pending) return text ? giveAnswer(text) : undefined;
  if (text && state.status && !state.status.ready) { toast(state.status.message || "The model isn't ready yet.", true); return; }
  if (files) {  // the attached files go first, shown on the message as they upload
    input.value = "";
    autosize();
    const bubble = addUser(text);
    stick(true);
    state.uploading = true;
    updateComposer();
    const follow = await sendFiles(bubble).finally(() => { state.uploading = false; updateComposer(); });
    if (!text) return offerFollowUps(bubble, follow);
    return talk(text, bubble);
  }
  return talk(text);
}
// The open chat - made now when there is none yet (a first message, or a first pin)
async function ensureChat() {
  if (state.chat) return state.chat;
  try { state.chat = await send("POST", `/workspaces/${state.ws.id}/chats`); }
  catch (e) { toast(e.message, true); return null; }
  save(KEYS.chat, state.chat.id);
  state.chats.unshift(state.chat);
  renderChats();
  return state.chat;
}
// Sends the words (the user's message already shown when files went first)
async function talk(text, shown = null) {
  if (!await ensureChat()) return;
  if (!shown) {
    input.value = "";
    autosize();
    addUser(text);
  }
  stick(true);
  const files = takeSent();  // uploaded for this message, or alone just before it
  await run(`/chats/${state.chat.id}/messages`, files.length ? { text, files } : { text }, assistantView());
}

async function giveAnswer(said) {  // the waiting question's answer (null: skipped)
  const pending = state.pending;
  if (!pending || state.busy) return;
  pending.view.resume(said);
  state.pending = null;
  input.value = "";
  autosize();
  await run(`/chats/${state.chat.id}/answer`, { answer: said }, pending.view);
}

// Sends a message or an answer (body), or follows the chat's running turn (body null), into view
async function run(path, body, view) {
  const chatId = state.chat.id;
  setBusy(true);
  const controller = new AbortController();
  state.controller = controller;
  const started = performance.now();
  let first = null, ended = false, seen = false, closedBy = null;
  const timing = () => ({ seconds: (performance.now() - started) / 1000, first: first && (first - started) / 1000 });
  try {
    const r = await fetch(API + path, body === null ? { signal: controller.signal }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal });
    if (!r.ok) throw new Error(await detail(r));
    for await (const [event, data] of serverEvents(r)) {
      seen = true;
      if (event === "text") { first ??= performance.now(); view.text(data.text); }
      else if (event === "tool_start") view.toolStart(data);
      else if (event === "tool_end") view.toolEnd(data);
      else if (event === "artifact") view.artifact(data.artifact);
      else if (event === "worker") view.worker(data);
      else if (event === "stats") view.stats(data);
      else if (event === "checking") view.checking();
      else if (event === "question") { ended = true; closedBy = data.inplace ? "inplace" : event; state.pending = { ...data, view }; view.ask(data); }  // the stream ends here; the answer's request streams the rest
      else if (event === "done") { ended = true; closedBy = event; view.finish(data.answer, timing()); }
      else if (event === "stopped") { ended = true; closedBy = event; view.stop(timing()); }
      else if (event === "error") { ended = true; closedBy = event; view.error(data.message); }
    }
    // the part of the turn that just ended has its trace stored by now (a helper asking in place goes on: not yet)
    if (closedBy && closedBy !== "inplace") api(`/chats/${chatId}/traces`).then(all => view.trace(all.at(-1))).catch(() => {});
    if (!ended && body === null && !seen) {  // the turn ended meanwhile: show the chat as stored
      controller.detached = true;
      state.controller = null;
      setBusy(false);
      return openChat(chatId, true);
    }
    if (!ended) view.error("The connection closed before the answer finished. Please try again.");
  } catch (e) {
    if (controller.detached) return;  // the page moved on; the turn goes on without it
    if (e.name === "AbortError") view.stop(timing());
    else view.error(e.message === "Failed to fetch" ? "Lost the connection to the server." : e.message);
  } finally {
    if (!controller.detached) {  // a detached run must not touch what the page shows now
      if (state.controller === controller) state.controller = null;
      setBusy(false);
      await loadChats();
      if (state.chat?.id === chatId && !state.controller) {  // checked after the wait: the page may have moved on
        state.chat = state.chats.find(c => c.id === chatId) || state.chat;
        setTitle();
        updateComposer();
      }
      loadItems();
    }
  }
}

function stopTurn() {  // Stop: the server stops the turn (at its next token), then the stream is let go
  if (!state.controller) return;
  if (state.chat) fetch(`${API}/chats/${state.chat.id}/stop`, { method: "POST" }).catch(() => {});
  state.controller.abort();
  closeAsk();
}
function stopWaiting() {  // a helper waiting in place on the user: its turn is stopped, and the chat shown as stored
  if (!state.chat) return;
  const id = state.chat.id;
  state.pending = null;
  closeAsk();
  fetch(`${API}/chats/${id}/stop`, { method: "POST" }).catch(() => {}).finally(() => setTimeout(() => { if (state.chat?.id === id) openChat(id, true); }, 700));
}
function detach() {  // leaving the chat: its turn goes on, and opening the chat again follows it
  if (!state.controller) return;
  state.controller.detached = true;
  state.controller.abort();
  state.controller = null;
  setBusy(false);
}

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
  $("#sendBtn").title = busy ? "Stop" : "Send";
  $("#sendBtn").setAttribute("aria-label", busy ? "Stop" : "Send");
  updateComposer();
}
function updateComposer() {
  $("#sendBtn").disabled = state.uploading || (!state.busy && !input.value.trim() && !attachedCount());
}
function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  input.style.overflowY = input.scrollHeight > 200 ? "auto" : "hidden";  // a scrollbar only when it really overflows
}
composer.addEventListener("submit", e => {
  e.preventDefault();
  if (state.pending && input.value.trim()) submit(input.value);
  else if (state.busy) stopTurn();
  else submit(input.value);
});
input.addEventListener("input", () => { autosize(); updateComposer(); });
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); composer.requestSubmit(); }
});

/* ═══ The shell ═════════════════════════════════════════════════════════ */
function closeSide() { document.body.classList.remove("side-open"); }
$("#collapseBtn").addEventListener("click", () => document.body.classList.add("collapsed"));
$("#showSideBtn").addEventListener("click", () => { document.body.classList.remove("collapsed"); document.body.classList.toggle("side-open"); });
$("#sideScrim").addEventListener("click", closeSide);
$("#newChatSide").addEventListener("click", newChat);
$("#newChatTop").addEventListener("click", newChat);
$("#filesSide").addEventListener("click", openDrawer);
$("#filesTop").addEventListener("click", openDrawer);
$("#searchRow").addEventListener("click", openSearch);
$("#searchInput").addEventListener("input", e => { query = e.target.value.trim().toLowerCase(); renderChats(); });
$("#searchClose").addEventListener("click", closeSearch);
$("#leaveBtn").addEventListener("click", leaveProject);
$("#newProjectBtn").addEventListener("click", newProject);

// Esc, from the top layer down (the viewer and a question in the box take theirs first)
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  if (dialogOpen()) closeDialog();
  else if (drawerOpen()) closeDrawer();
  else if (fileOpen()) closeAsked();
  else if (searching()) closeSearch();
  else if (document.body.classList.contains("side-open")) closeSide();
  else if (state.busy) stopTurn();
});
setInterval(() => { if (!state.busy && state.ws) renderChats(); }, 60_000);  // "Today" turns into "Yesterday"

Object.assign(hooks, { thread, stick, loadItems, submit, hideHello, updateComposer });
wireViewer();
wireFiles();
wireMention();
mentionHooks.point = point;
Object.assign(pinHooks, { open: openItem, ensureChat, changed: () => { if (drawerOpen()) fillDrawer(); } });
playIntro();
(async () => {
  updateComposer();
  await Promise.all([refreshStatus(), loadTools()]);  // the tools' names before any chat is drawn
  try { await loadWorkspaces(); }
  catch (e) { showProjects(); toast(e.message, true); }
})();
