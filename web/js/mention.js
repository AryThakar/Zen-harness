// @ in the message box (docs/history/REBUILD_PLAN.md 2.2): a file of the project pointed at, so it goes with this message
// without being uploaded again - typed "@sal" lists the files whose names hold it; the one picked becomes a chip in the
// message box, like an attachment, and is sent with the message (the model is told which files, and gets what in
// them matches). Each row can also pin its file to the chat.

import { state } from "./api.js";
import { isPinned, togglePin } from "./pins.js";
import { $, fileTile, h, plural, svg } from "./util.js";

export const hooks = { point: () => {} };
const SHOWN = 8;
let menu = null, hits = [], at = 0, range = null;


// The "@word" just before the caret: where it starts and ends, and the word
function word() {
  const el = $("#input"), before = el.value.slice(0, el.selectionStart);
  const m = /(^|\s)@([^\s@]*)$/.exec(before);
  return m ? { start: el.selectionStart - m[2].length - 1, end: el.selectionStart, text: m[2].toLowerCase() } : null;
}

const rank = (item, q) => {
  const name = (item.name || "").toLowerCase(), base = name.split("/").pop();
  return item.memory ? 3 : name.startsWith(q) ? 0 : base.startsWith(q) ? 1 : 2;  // agent.md last: it is always in view
};

function about(i) {
  if (i.memory) return "Zen's memory for this project · always in view";
  if (i.kind === "table") return `Table · ${plural(i.rows ?? 0, "row")}`;
  if (i.kind === "image") return i.caption || "Picture";
  if (i.pages) return `Document · ${plural(i.pages, "page")}`;
  return /\.chart\.json$/i.test(i.name || "") ? "Chart" : "File";
}

function update() {
  range = state.ws ? word() : null;
  if (!range) return close();
  const q = range.text;
  hits = state.items.filter(i => !q || (i.name || "").toLowerCase().includes(q))
    .sort((a, b) => rank(a, q) - rank(b, q)).slice(0, SHOWN);
  at = Math.min(at, Math.max(0, hits.length - 1));
  render();
}

function render() {
  if (!menu) {
    menu = h("div", { class: "mention", role: "listbox", "aria-label": "Files of this project" });
    $("#composer").append(menu);
  }
  if (!hits.length) {
    menu.replaceChildren(h("div", { class: "mention-empty" }, state.items.length ? "No file's name has that in it" : "No files in this project yet"));
    return;
  }
  menu.replaceChildren(h("div", { class: "mention-head" }, "Point at a file: it goes with this message"), ...hits.map((item, n) => {
    const pinnedNow = isPinned(item.id);
    return h("div", { class: `mention-row${n === at ? " on" : ""}`, role: "option", "aria-selected": String(n === at),
                      onmousedown: e => { e.preventDefault(); choose(item); }, onmousemove: () => { if (at !== n) { at = n; render(); } } },
      fileTile(item.name || ""),
      h("div", { class: "mention-text" }, h("b", {}, item.name), h("small", {}, about(item))),
      h("button", { class: `mention-pin${pinnedNow ? " on" : ""}`, type: "button", html: svg("pin"),
                    title: pinnedNow ? "Unpin from this chat" : "Pin to this chat: what in it matches each message goes with it",
                    "aria-label": `${pinnedNow ? "Unpin" : "Pin"} ${item.name}`,
                    onmousedown: e => { e.preventDefault(); e.stopPropagation(); togglePin(item).then(render); } }));
  }));
}

function choose(item) {
  const el = $("#input");
  let before = el.value.slice(0, range.start);
  const after = el.value.slice(range.end);
  if (/^\s/.test(after)) before = before.replace(/\s+$/, "");  // no double space where the @word was
  el.value = before + after;  // the @word goes: the file is a chip now
  el.setSelectionRange(before.length, before.length);
  close();
  hooks.point(item);
  el.dispatchEvent(new Event("input"));  // the message box resizes
}

function close() { menu?.remove(); menu = null; hits = []; at = 0; }

export function wireMention() {
  const el = $("#input");
  el.addEventListener("input", update);
  el.addEventListener("click", update);
  el.addEventListener("blur", () => setTimeout(close, 120));
  el.addEventListener("keydown", e => {  // capture: before the message box's Enter sends
    if (!menu) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); e.stopImmediatePropagation();
      at = (at + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % Math.max(1, hits.length);
      render();
    } else if ((e.key === "Enter" || e.key === "Tab") && hits.length) {
      e.preventDefault(); e.stopImmediatePropagation();
      choose(hits[at]);
    } else if (e.key === "Escape") {
      e.preventDefault(); e.stopImmediatePropagation();
      close();
    }
  }, true);
}
