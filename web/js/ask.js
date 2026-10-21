// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Questions and permissions, asked in the message box. While Zen waits on the user the box becomes the
// question (it stays in view; what is typed is the answer), and once they decide it goes, leaving one
// line in the chat. Esc only ever skips or cancels; a delete is given by a click only, never a key.

import { helperName } from "./steps.js";
import { $, cap, escapeHtml, h, svg } from "./util.js";

const ASK = "Ask anything · @ points at a file";  // the message box's own placeholder (index.html), put back after a question

const DANGER = new Set(["delete_files", "replace_file"]);
let open = null;  // { card, onChoose }

const helperOf = card => helperName(card.via || card.tool || card.name);
function who(card) {
  if (card.approval) return "Needs your permission";
  const helper = helperOf(card);
  if (helper) return `The ${helper.toLowerCase()} needs you to decide`;
  return card.retry ? "Zen needs you" : "Zen asks";
}
const basename = url => { try { return decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || url); } catch { return url; } };
function action(approval) {
  const a = approval || {};
  if (a.action === "delete_files") return `delete ${(a.named || a.items || []).join(", ")}`;
  if (a.action === "replace_file") return `replace ${a.name || a.item || "the file"}`;
  const what = a.name || (a.url ? basename(a.url) : "the file");
  return `${a.action === "import_table" ? "import" : "download"} ${what}${a.host ? ` from ${a.host}` : ""}`;
}
function facts(approval) {
  const a = approval || {};
  return a.action === "delete_files" ? a.named || a.items || []
    : a.action === "replace_file" ? [a.name, a.item].filter(Boolean)
    : [a.host, a.type, a.size, a.url].filter(Boolean);
}

export function openAsk(card, { onChoose, onStop, stoppable }) {
  closeAsk(true);
  const box = $("#composer"), field = $("#input");
  const danger = DANGER.has(card.approval?.action), text = !card.approval && card.allow_text !== false;
  const skip = !card.approval && card.allow_skip !== false, options = card.options || [];
  open = { card, onChoose };
  box.classList.add("asking");
  box.classList.toggle("danger", danger);
  box.classList.toggle("no-text", !text);
  const icon = card.approval ? svg(danger ? "trash" : "lock") : '<svg class="ask-moon" aria-hidden="true"><use href="#mark"/></svg>';
  const key = i => card.approval ? (i === 1 ? '<kbd class="key">Esc</kbd>' : "") : i < 9 ? `<kbd class="key">${i + 1}</kbd>` : "";
  const panel = h("div", { class: "ask-panel", role: "group", "aria-label": who(card) },
    h("div", { class: "ask-top", html: `${icon}<span>${escapeHtml(who(card))}</span>` },
      stoppable ? h("button", { type: "button", class: "stop", onclick: onStop }, "Stop task") : null),
    h("div", { class: "ask-q" }, card.question || ""),
    card.approval && facts(card.approval).length ? h("div", { class: "facts" }, facts(card.approval).map(f => h("span", {}, f))) : null,
    h("div", { class: "ask-row" },
      options.map((o, i) => h("button", { type: "button", class: `opt${card.approval && i === 0 ? (danger ? " delete" : " allow") : ""}`,
        html: key(i) + escapeHtml(o), onclick: () => choose(o) })),
      skip ? h("button", { type: "button", class: "skip-link", html: 'Skip <kbd class="key">Esc</kbd>', onclick: () => choose(null) }) : null));
  box.prepend(panel);
  field.placeholder = text ? (options.length ? "Or type your own answer…" : "Type your answer…") : ASK;
  if (danger) panel.querySelectorAll(".opt")[1]?.focus();  // a delete never starts on its button
  else if (text) field.focus();
  else panel.querySelector(".opt")?.focus();
}

function choose(said) {
  if (!open) return;
  const { onChoose } = open;
  closeAsk();
  onChoose(said);
}

export function closeAsk(now = false) {
  const box = $("#composer"), panel = box.querySelector(".ask-panel");
  open = null;
  const reset = () => {
    panel?.remove();
    if (open) return;  // another question opened meanwhile
    box.classList.remove("asking", "danger", "no-text");
    $("#input").placeholder = ASK;
  };
  if (!panel || now) return reset();
  panel.classList.add("leaving");
  setTimeout(reset, 170);
}

// 1 / 2 pick an option of a question, Esc skips it (or cancels a permission); any other key types an answer
document.addEventListener("keydown", e => {
  if (!open || e.defaultPrevented) return;
  const { card } = open, field = $("#input"), inField = document.activeElement === field;
  if (e.key === "Escape") {
    if (card.approval) { e.preventDefault(); choose(card.options?.[1] ?? null); }
    else if (card.allow_skip !== false) { e.preventDefault(); choose(null); }
    return;
  }
  if (card.approval || inField || e.ctrlKey || e.metaKey || e.altKey || document.activeElement?.closest?.(".dialog-scrim, .viewer, .fileview")) return;
  if (/^[1-9]$/.test(e.key) && card.options?.[e.key - 1]) { e.preventDefault(); choose(card.options[e.key - 1]); return; }
  if (e.key.length === 1 && card.allow_text !== false) field.focus();
});

// The line a decision leaves in the chat
export function decision(card, said, allowed) {
  let mark, html;
  if (card.approval) {
    allowed ??= said != null && card.options?.length && said.trim().toLowerCase() === card.options[0].toLowerCase();
    [mark, html] = allowed ? ["ok", `Allowed: <b>${escapeHtml(action(card.approval))}</b>`] : ["no", `Declined: <b>${escapeHtml(action(card.approval))}</b>`];
  } else {
    const helper = helperOf(card), whom = helper ? `the ${helper.toLowerCase()}` : "Zen";
    [mark, html] = said == null
      ? ["skip", `Skipped. ${cap(whom)} went on with a sensible default`]
      : ["ok", `${helper ? `Answered the ${escapeHtml(helper.toLowerCase())}` : "You answered"}: <b>${escapeHtml(said)}</b>`];
  }
  const paths = { ok: "check", no: "cross", skip: "skip" };
  return h("div", { class: `record ${mark}`, html: `${svg(paths[mark])}<span>${html}</span>` });
}
