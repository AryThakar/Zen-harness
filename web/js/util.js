// Small shared pieces: building elements, icons, numbers and dates, Markdown with maths, code blocks,
// dialogs and toasts.

export const $ = (sel, root = document) => root.querySelector(sel);

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;  // only for trusted markup (icons, sanitised markdown)
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}

// Line icons, 24 units, drawn with the page's stroke (svg.i)
const ICONS = {
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  cross: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6.5A2.5 2.5 0 0 0 13.5 4h-7A2.5 2.5 0 0 0 4 6.5v7A2.5 2.5 0 0 0 6.5 16H8"/>',
  speak: '<path d="M5 10v4h3l4 4V6L8 10z"/><path d="M16 9a4 4 0 0 1 0 6"/>',
  hush: '<path d="M5 10v4h3l4 4V6L8 10z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  down: '<path d="M12 5v14M6.5 13.5 12 19l5.5-5.5"/>',
  download: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
  expand: '<path d="M14 4.5h5.5V10M19.5 4.5 13 11M10 19.5H4.5V14M4.5 19.5 11 13"/>',
  doc: '<path d="M7 3.5h6.5L18 8v12.5H7z"/><path d="M13 3.5V8h5M10 12.5h5M10 16h5"/>',
  note: '<path d="M6 3.5h12a1.5 1.5 0 0 1 1.5 1.5v14.5l-4-3H6A1.5 1.5 0 0 1 4.5 15V5A1.5 1.5 0 0 1 6 3.5z"/><path d="M8.5 8.5h7M8.5 12h4.5"/>',
  trash: '<path d="M4.5 7h15M10 11v6M14 11v6"/><path d="m6.5 7 .8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12M9.5 7V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2"/>',
  pencil: '<path d="M16.5 4.5a2 2 0 0 1 2.8 2.8L8.5 18.1 5 19l.9-3.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M6 12h12"/>',
  fit: '<path d="M4.5 9V5.5a1 1 0 0 1 1-1H9M15 4.5h3.5a1 1 0 0 1 1 1V9M19.5 15v3.5a1 1 0 0 1-1 1H15M9 19.5H5.5a1 1 0 0 1-1-1V15"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  skip: '<path d="M5 12h11M12 7l5 5-5 5M20 6v12"/>',
  chart: '<path d="M4 19V9M10 19V5M16 19v-7M21 19H3"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5S9.6 6.1 12 3.5z"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="m20.5 16-5-5-8 8.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.6v2M12 19.4v2M2.6 12h2M19.4 12h2M5.4 5.4l1.4 1.4M17.2 17.2l1.4 1.4M5.4 18.6l1.4-1.4M17.2 6.8l1.4-1.4"/>',
  chat: '<path d="M5.5 5h13A2 2 0 0 1 20.5 7v8.5a2 2 0 0 1-2 2H11l-4.5 3.5v-3.5h-1a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/>',
  terminal: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m7.5 9.5 3 2.5-3 2.5M12.5 15h4"/>',
  alert: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5.5M12 16.3v.2"/>',
  more: '<path d="M6 12h.01M12 12h.01M18 12h.01" style="stroke-width:3"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10"/>',
  code2: '<path d="m8.5 8-4.5 4 4.5 4M15.5 8l4.5 4-4.5 4M13.3 5.5l-2.6 13"/>',
  flow: '<rect x="3.5" y="4" width="7" height="5" rx="1.5"/><rect x="13.5" y="15" width="7" height="5" rx="1.5"/><path d="M7 9v4.5a2 2 0 0 0 2 2h4.5"/>',
  trace: '<path d="M3 12h4l2.5-6.5 5 13 2.5-6.5h4"/>',
};
export const svg = (name, cls = "i") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

export function load(key) { try { return localStorage.getItem(key); } catch { return null; } }
export function save(key, value) { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {} }
export const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/* ═══ Numbers, dates, text ══════════════════════════════════════════════ */
export const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
export const nf2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
export const count = v => v == null ? "—" : nf0.format(v);
const PREFIX_UNITS = /^[₹$€£¥]$/;
function withUnit(text, unit) {
  if (!unit) return text;
  return PREFIX_UNITS.test(unit) ? `${unit}${text}` : unit === "%" ? `${text}%` : `${text} ${unit}`;
}
export function formatter(unit) {
  return {
    full: v => v == null ? "—" : withUnit(nf2.format(v), unit),
    axis: v => withUnit(Math.abs(v) >= 1e4 ? compact.format(v) : nf2.format(v), unit),
  };
}
const isDate = v => typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function dateLabel(s, long) {
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  return long ? `${MONTHS[m - 1]} ’${String(y).slice(2)}` : `${d} ${MONTHS[m - 1]}`;
}
export function xLabels(values) {
  if (!values.length || !values.every(isDate)) return values.map(v => v == null ? "" : String(v));
  const span = (Date.parse(values.at(-1).slice(0, 10)) - Date.parse(values[0].slice(0, 10))) / 864e5;
  return values.map(v => dateLabel(v, span > 150));
}
export function cell(v) {
  if (v == null) return "";
  if (typeof v === "number") return Number.isInteger(v) ? nf0.format(v) : nf2.format(v);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (isDate(v)) return v.replace("T", " ").replace(/ 00:00:00$/, "").slice(0, 16);
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}
export function fileSize(bytes) {
  if (bytes == null) return "";
  return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}
export const ext = name => ((name || "").includes(".") ? name.split(".").pop() : "").toLowerCase();
// A file's icon: a sheet of paper with its corner folded, the kind's mark and its extension in the kind's colour
const TILES = { csv: "csv", tsv: "csv", xlsx: "csv", xls: "csv", xlsm: "csv", pdf: "pdf", md: "md", markdown: "md", txt: "md", log: "md",
  py: "py", js: "py", ts: "py", sql: "py", json: "py", css: "py", html: "doc", htm: "doc", docx: "doc", doc: "doc",
  png: "img", jpg: "img", jpeg: "img", webp: "img", gif: "img" };
export const isPicture = name => TILES[ext(name)] === "img";
const LINES = '<path d="M10.5 14h11M10.5 17.3h11M10.5 20.6h7"/>';
const MARKS = {
  csv: '<rect x="9.5" y="12.5" width="13" height="10.5" rx="1.8"/><path d="M9.5 16h13M9.5 19.5h13M13.8 16v7"/>',
  py: '<path d="m13 14.3-3.3 3.4 3.3 3.4M19 14.3l3.3 3.4-3.3 3.4"/>',
  md: LINES, pdf: LINES, doc: LINES,
  img: '<rect x="9.5" y="12.5" width="13" height="10.5" rx="1.8"/><circle cx="13.3" cy="16" r="1.2"/><path d="m22.5 20.8-3.6-3.6-7.2 5.6"/>',
  chart: '<path d="M11.4 22.8v-4.6M16 22.8v-9.4M20.6 22.8v-6.6"/>',  // the PDF's lines stood up: bars rising
};
const sheet = (kind, tag) => `<svg class="fdoc" viewBox="0 0 32 40" aria-hidden="true">` +
  `<path class="pg" d="M7 1.5h12.6l8.9 8.9v25.1a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-31a3 3 0 0 1 3-3z"/>` +
  `<path class="fold" d="M19.6 1.5v6.4a2.5 2.5 0 0 0 2.5 2.5h6.4z"/><g class="mk">${MARKS[kind]}</g>` +
  `<text x="16" y="33.4" text-anchor="middle">${escapeHtml(tag)}</text></svg>`;
export function fileTile(name, label) {
  const chart = /\.chart\.json$/i.test(name);  // a chart file: drawn live, not read as JSON
  const type = chart ? "chart" : ext(name), kind = chart ? "chart" : TILES[type] || "md";
  const tag = (label || type || "file").slice(0, chart ? 5 : 4).toUpperCase();
  return h("span", { class: `ficon k-${kind}`, title: tag, html: sheet(kind, tag) });
}
// A pinned note: a notepad sheet with its yellow top, like a note on the desk
export const noteTile = () => h("span", { class: "ficon k-note", title: "Note", html:
  `<svg class="fdoc" viewBox="0 0 32 40" aria-hidden="true"><rect class="pg" x="4" y="2" width="24" height="36" rx="3.5"/>` +
  `<path class="band" d="M4 9.5V5.5A3.5 3.5 0 0 1 7.5 2h17A3.5 3.5 0 0 1 28 5.5v4z"/>` +
  `<path class="rule" d="M8.5 15.5h15M8.5 20h15M8.5 24.5h15M8.5 29h9"/></svg>` });
// A project's colour: one of Apple's system hues, fixed by its id (a rename keeps it)
const HUES = [211, 26, 140, 280, 4, 186, 330, 241];  // in the order given: neighbours differ most
// given once, the first time a project shows (a free one while any is free), then kept on this computer
export function projectHue(id) {
  let kept = {};
  try { kept = JSON.parse(load("zen.project-hues")) || {}; } catch {}
  if (kept[id] == null) {
    const taken = new Set(Object.values(kept));
    let n = 7;
    for (const c of String(id)) n = (n * 31 + c.charCodeAt(0)) >>> 0;
    kept[id] = HUES.find(hue => !taken.has(hue)) ?? HUES[n % HUES.length];
    save("zen.project-hues", JSON.stringify(kept));
  }
  return kept[id];
}
export const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export function host(url) { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } }
export function prettyModel(name) {  // "gemma-4-E4B-it-qat-UD-Q4_K_XL" -> "Gemma 4 E4B"
  const base = (name || "").replace(/\.gguf$/i, "").replace(/[-_](it|instruct|chat)([-_].*)?$/i, "").replace(/[-_]Q\d.*$/i, "");
  return base.replace(/[-_]+/g, " ").replace(/^\w/, c => c.toUpperCase()) || "Model";
}
export function errorText(summary = "") {
  try {
    const r = JSON.parse(summary), e = r.error;
    return (typeof e === "object" ? e?.message : e) || r.message || summary;
  } catch {  // results are cut for the UI: take the message out of the cut JSON
    const m = summary.match(/"message":\s*"((?:[^"\\]|\\.)*)("?)/);
    if (!m) return summary.split("\n")[0];
    try { return JSON.parse(`"${m[1]}"`) + (m[2] ? "" : " …"); } catch { return m[1]; }
  }
}
// How long ago, for grouping the chat list
export function age(iso) {
  const t = Date.parse(iso), now = new Date(), day = 864e5;
  if (Number.isNaN(t)) return "Older";
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (t >= start) return "Today";
  if (t >= start - day) return "Yesterday";
  if (t >= start - 7 * day) return "Previous 7 days";
  if (t >= start - 30 * day) return "Previous 30 days";
  return "Older";
}

/* ═══ Markdown → safe HTML, with maths ══════════════════════════════════ */
// Maths as the model writes it: $$…$$ and \[…\] as blocks, $…$ and \(…\) inline. A single $ follows
// pandoc's rule - no space after the opening $, none before the closing one, and no digit after it -
// so "$5 and $10" stays text while "$\rightarrow$" and "($Q_{next}$)" become maths.
const tex = (text, displayMode) => katex.renderToString(text, { displayMode, throwOnError: false, output: "htmlAndMathml" });
const MATH = { extensions: [
  { name: "mathBlock", level: "block",
    start: src => { const i = src.search(/\$\$|\\\[/); return i < 0 ? undefined : i; },
    tokenizer(src) {
      const m = /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])[^\S\n]*(?:\n|$)/.exec(src);
      if (m) return { type: "mathBlock", raw: m[0], text: (m[1] ?? m[2]).trim() };
    },
    renderer: t => `<div class="math">${tex(t.text, true)}</div>` },
  { name: "mathInline", level: "inline",
    start: src => { const i = src.search(/\$|\\\(/); return i < 0 ? undefined : i; },
    tokenizer(src) {
      let m = /^\$\$([\s\S]+?)\$\$/.exec(src);
      if (m) return { type: "mathInline", raw: m[0], text: m[1].trim(), display: true };
      m = /^\\\(([\s\S]+?)\\\)/.exec(src) || /^\$(?![\s$])((?:\\.|[^\\$\n])*?[^\s\\])\$(?!\d)/.exec(src);
      if (m) return { type: "mathInline", raw: m[0], text: m[1] };
    },
    renderer: t => tex(t.text, !!t.display) },
] };
let mathOn = false;
export function markdown(text) {
  if (window.marked && window.DOMPurify) {
    if (window.katex && !mathOn) { marked.use(MATH); mathOn = true; }
    return DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: true }));
  }
  return escapeHtml(text).replace(/\n/g, "<br>");
}
// Tables scroll on their own, number cells line up right, links open outside
export function polish(root) {
  root.querySelectorAll("table").forEach(t => {
    if (!t.parentElement.classList.contains("table-wrap")) t.replaceWith(h("div", { class: "table-wrap" }, t.cloneNode(true)));
  });
  root.querySelectorAll("td").forEach(td => {
    if (/^[\s₹$€£¥+\-−(]*[\d.,]+\s*(%|[KMB]|x)?\)?\s*$/.test(td.textContent)) td.classList.add("num");
  });
  root.querySelectorAll("a[href]").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
}
// The caret goes after the last thing written: down the last child while it is an element, so text after a
// bold word ("**sm_90**.") keeps it at the end, into the deepest paragraph, item, heading or bold on the way
const lastNode = el => { let n = el.lastChild; while (n?.nodeType === 3 && !n.textContent.trim()) n = n.previousSibling; return n; };
export function addCaret(root) {
  let node = root, into = root;
  for (let next = lastNode(node); next?.nodeType === 1 && !["TABLE", "PRE", "DIV"].includes(next.tagName); next = lastNode(node)) {
    node = next;
    if (["P", "LI", "H1", "H2", "H3", "H4", "STRONG", "EM"].includes(node.tagName)) into = node;
  }
  into.append(h("span", { class: "caret" }));
}

// A code block: its language, a copy button, and an editor's colours
export function codeBlocks(root) {
  root.querySelectorAll("pre > code").forEach(block => {
    const pre = block.parentElement;
    if (pre.parentElement?.classList.contains("code-block")) return;
    const lang = (block.className.match(/language-(\S+)/) || [])[1] || "";
    if (window.hljs && lang !== "text" && (!lang || hljs.getLanguage(lang))) {  // no language: it guesses
      try { hljs.highlightElement(block); } catch {}
    }
    pre.classList.add("code");
    const copy = h("button", { type: "button", "aria-label": "Copy code", html: svg("copy") + "Copy" });
    copy.addEventListener("click", () => copyText(block.textContent, copy, svg("check") + "Copied"));
    const wrap = h("div", { class: "code-block" }, h("div", { class: "code-head" }, h("span", {}, lang === "text" ? "" : lang), copy));
    pre.replaceWith(wrap);
    wrap.append(pre);
  });
}

export async function copyText(text, button, done = svg("check")) {
  try { await navigator.clipboard.writeText(text); } catch { toast("Couldn't copy", true); return; }
  if (!button) return;
  const was = button.innerHTML;
  button.innerHTML = done;
  setTimeout(() => (button.innerHTML = was), 1400);
}

/* ═══ Dialogs and toasts ════════════════════════════════════════════════ */
export function showDialog({ title, text = "", value = null, extra = null, ok = "OK", cancel = "Cancel", danger = false, wide = false }) {
  const scrim = $("#dialog"), box = $("#dialogBox"), field = $("#dialogInput"), okBtn = $("#dialogOk"), cancelBtn = $("#dialogCancel");
  box.classList.toggle("wide", wide);
  $("#dialogTitle").textContent = title;
  $("#dialogText").textContent = text;
  $("#dialogText").hidden = !text;
  $("#dialogExtra").replaceChildren(...(extra ? [extra] : []));
  field.hidden = value == null;
  field.value = value ?? "";
  okBtn.textContent = ok;
  okBtn.className = danger ? "danger" : "primary";
  cancelBtn.hidden = !cancel;
  cancelBtn.textContent = cancel || "";
  scrim.classList.add("show");
  (value != null ? field : cancel ? cancelBtn : okBtn).focus();
  if (value != null) field.select();
  return new Promise(resolve => {
    const done = result => {
      scrim.classList.remove("show");
      okBtn.onclick = cancelBtn.onclick = scrim.onclick = field.onkeydown = null;
      resolve(result);
    };
    okBtn.onclick = () => done(value != null ? field.value.trim() : true);
    cancelBtn.onclick = () => done(value != null ? null : false);
    scrim.onclick = e => { if (e.target === scrim) cancelBtn.onclick(); };
    field.onkeydown = e => { if (e.key === "Enter") okBtn.onclick(); };
  });
}
export const confirmDialog = (title, text, ok) => showDialog({ title, text, ok, danger: true });
export const promptDialog = (title, text, value, ok = "Save") => showDialog({ title, text, value, ok });
export function dialogOpen() { return $("#dialog").classList.contains("show"); }
export function closeDialog() { const c = $("#dialogCancel"); c.hidden ? $("#dialogOk").click() : c.click(); }

export function toast(message, error = false) {
  const el = h("div", { class: `toast${error ? " err" : ""}`, role: error ? "alert" : "status" }, message);
  $("#toasts").append(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 3200);
}
