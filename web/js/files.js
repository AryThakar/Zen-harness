// The project's files: the Files & data panel (everything in the project), the file panel beside the chat
// (view only: code like an editor, Markdown as a page, text, tables as a grid, HTML in a frame that runs
// nothing, PDFs in the browser's viewer) and uploads (drop, paste or pick), each showing its progress.

import { api, itemUrl, KEYS, rowsUrl, state, uploadFile } from "./api.js";
import { openViewer, viewerOpen } from "./viewer.js";
import { $, count, ext, escapeHtml, fileSize, fileTile, h, isPicture, load, markdown, modelTile, nf2, noteTile, plural, polish, save, svg, codeBlocks, copyText } from "./util.js";

export const hooks = { thread: null, stick: () => {}, loadItems: async () => {}, submit: () => {}, hideHello: () => {}, updateComposer: () => {} };

/* ═══ What a file is, and whether it can be viewed here ═════════════════ */
const CODE = { py: "python", js: "javascript", mjs: "javascript", ts: "typescript", sql: "sql", json: "json", css: "css", sh: "bash",
  ps1: "powershell", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", xml: "xml", r: "r", java: "java", c: "c", cpp: "cpp", h: "c",
  go: "go", rs: "rust", rb: "ruby", ipynb: "json" };
const TEXT = new Set(["txt", "log", "csv", "tsv", "rst", "tex"]);
const LANG = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", sql: "SQL", json: "JSON", css: "CSS", bash: "Shell",
  powershell: "PowerShell", yaml: "YAML", ini: "TOML", xml: "XML", r: "R", java: "Java", c: "C", cpp: "C++", go: "Go", rust: "Rust", ruby: "Ruby",
  markdown: "Markdown", plaintext: "Plain text" };
const DOT = { python: "#3b7ddd", javascript: "#e5c07b", typescript: "#3b7ddd", sql: "#c678dd", json: "#d19a66", markdown: "#8e8e8e", plaintext: "#8e8e8e", xml: "#e8784a" };
function shape(item) {
  if (item.kind === "table") return "table";
  if (item.kind === "note") return "md";
  if (item.kind === "image") return "image";
  const e = ext(item.name || item.id);
  if (CODE[e]) return "code";
  if (e === "md" || e === "markdown") return "md";
  if (e === "html" || e === "htm") return "html";
  if (e === "pdf") return "pdf";
  if (TEXT.has(e)) return "text";
  return null;
}
export const canView = item => !!shape(item);
const LIMIT = 4 << 20;  // larger text files are downloaded, not shown

/* ═══ The file panel ════════════════════════════════════════════════════ */
const fv = { item: null, mode: "preview", text: null, table: null, blob: null };
export const fileOpen = () => document.body.classList.contains("viewing");

export async function openItem(item, from) {
  const kind = shape(item);
  if (kind === "image") {  // pictures open in the viewer
    const img = from?.tagName === "IMG" ? from : Object.assign(new Image(), { src: itemUrl(item.id), alt: item.name || item.id });
    if (!from) { await img.decode().catch(() => {}); }
    return openViewer(img, "image");
  }
  if (!kind) { location.href = itemUrl(item.id); return; }
  closeDrawer();
  fv.item = item; fv.mode = "preview"; fv.text = fv.table = null;
  if (fv.blob) { URL.revokeObjectURL(fv.blob); fv.blob = null; }
  const name = item.name || item.title || item.id;
  const icon = item.kind === "note" ? noteTile() : fileTile(item.kind === "table" ? `${name}.csv`.replace(/\.csv\.csv$/, ".csv") : name);
  $("#fvIcon").replaceWith(Object.assign(icon, { id: "fvIcon" }));
  $("#fvName").textContent = name;
  $("#fvMeta").textContent = "Loading…";
  $("#fvModes").hidden = !(kind === "md" || kind === "html");
  $("#fvBody").replaceChildren(h("p", { class: "muted", style: "padding:20px" }, "Loading…"));
  $("#fvStatus").replaceChildren();
  $("#fvTabs").hidden = true;
  document.body.classList.add("viewing");
  try {
    if (kind === "table") {
      fv.table = await api(rowsUrl(item.id));
    } else {
      const r = await fetch(itemUrl(item.id));
      if (!r.ok) throw new Error("Couldn't open the file");
      if (kind === "pdf") fv.blob = URL.createObjectURL(new Blob([await r.arrayBuffer()], { type: "application/pdf" }));
      else {
        const size = Number(r.headers.get("content-length")) || item.bytes || 0;
        if (size > LIMIT) throw new Error(`It's ${fileSize(size)}: download it to read it`);
        fv.text = await r.text();
      }
    }
  } catch (e) {
    $("#fvBody").replaceChildren(h("p", { class: "muted", style: "padding:20px" }, e.message));
    $("#fvMeta").textContent = item.id;
    return;
  }
  if (fv.item !== item) return;  // another file was opened meanwhile
  const bytes = fv.text != null ? new Blob([fv.text]).size : item.bytes;
  $("#fvMeta").textContent = [item.id, kind === "table" ? `${count(fv.table.total_rows)} rows × ${fv.table.columns.length} columns` : fileSize(bytes), "view only"].filter(Boolean).join(" · ");
  const link = $("#fvDownload");
  link.href = kind === "table" ? URL.createObjectURL(new Blob([toCsv(fv.table)], { type: "text/csv" })) : itemUrl(item.id);
  link.download = kind === "table" ? `${name.replace(/\.(csv|xlsx?|tsv)$/i, "")}.csv` : name;
  show();
}

function show() {
  const item = fv.item, kind = shape(item), body = $("#fvBody");
  const lang = kind === "code" ? CODE[ext(item.name)] : kind === "md" ? "markdown" : kind === "html" ? "xml" : "plaintext";
  const source = kind === "code" || kind === "text" || (fv.mode === "source" && (kind === "md" || kind === "html"));
  document.querySelectorAll("#fvModes button").forEach(b => b.classList.toggle("on", b.dataset.mode === fv.mode));
  $("#fvTabs").hidden = !source;
  $("#fvTabName").textContent = item.name || item.title || item.id;
  $("#fvDot").style.background = DOT[lang] || "#8e8e8e";
  body.replaceChildren();
  body.scrollTop = 0;
  let status;
  if (kind === "table") {
    const t = fv.table;
    body.append(gridView(t.columns, t.rows));
    status = ["Table", `${count(t.total_rows)} rows`, plural(t.columns.length, "column"), t.rows.length < t.total_rows ? `first ${count(t.rows.length)} shown` : null];
  } else if (kind === "pdf") {
    body.append(h("iframe", { class: "pdf-view", src: fv.blob, title: item.name }));
    status = ["PDF"];
  } else if (source) {
    body.append(editorView(fv.text, lang));
    status = [LANG[lang], plural(fv.text.replace(/\n$/, "").split("\n").length, "line"), fileSize(new Blob([fv.text]).size), "UTF-8"];
  } else if (kind === "md") {
    const page = h("div", { class: "md-view", html: markdown(fv.text) });
    polish(page);
    codeBlocks(page);
    body.append(page);
    status = ["Markdown", "preview"];
  } else {
    body.append(h("iframe", { class: "html-view", sandbox: "", srcdoc: fv.text, title: item.name }));  // runs nothing, reaches nothing
    status = ["HTML", "preview", "scripts off"];
  }
  $("#fvStatus").innerHTML = status.filter(Boolean).map(s => `<span>${escapeHtml(s)}</span>`).join("") + "<span>View only</span>";
}

function editorView(text, lang) {
  const content = text.replace(/\n$/, ""), lines = content.split("\n"), LH = 13 * 1.7, TOP = 12;
  const code = h("code"), cur = h("div", { class: "cur-line" });
  if (lang !== "plaintext" && window.hljs && content.length < 400_000 && hljs.getLanguage(lang)) {
    code.innerHTML = hljs.highlight(content, { language: lang, ignoreIllegals: true }).value;
  } else code.textContent = content;
  const ed = h("div", { class: "editor" }, cur, h("div", { class: "gutter" }, lines.map((_, i) => i + 1).join("\n")), h("pre", {}, code));
  ed.addEventListener("mousemove", e => {  // the line under the pointer, lit
    const i = Math.max(0, Math.min(lines.length - 1, Math.floor((e.clientY - ed.getBoundingClientRect().top - TOP) / LH)));
    cur.style.top = `${TOP + i * LH}px`;
  });
  return ed;
}

function gridView(columns, rows) {
  const id = columns.map(c => /(^|_)id$/i.test(c));  // ids stay as written; other numbers line up right, one count of decimals each
  const num = columns.map((_, j) => !id[j] && rows.length && rows.every(r => r[j] == null || typeof r[j] === "number"));
  const dec = columns.map((_, j) => Math.min(4, Math.max(0, ...rows.map(r => typeof r[j] === "number" ? (String(r[j]).split(".")[1] || "").length : 0))));
  const show = (v, j) => v == null ? "" : typeof v !== "number" || id[j] ? String(v)
    : v.toLocaleString(undefined, { minimumFractionDigits: dec[j], maximumFractionDigits: dec[j] });
  return h("table", { class: "grid-view" },
    h("thead", {}, h("tr", {}, h("th", { class: "rn" }, "#"), columns.map((c, j) => h("th", { class: num[j] ? "num" : null }, c)))),
    h("tbody", {}, rows.map((r, i) => h("tr", {}, h("td", { class: "rn" }, i + 1), r.map((v, j) => h("td", { class: num[j] ? "num" : null }, show(v, j)))))));
}
const toCsv = t => [t.columns, ...t.rows].map(r => r.map(v => v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v).join(",")).join("\n") + "\n";

export function closeFile() { document.body.classList.remove("viewing", "fv-full"); }

function wireFilePanel() {
  $("#fvClose").addEventListener("click", closeFile);
  $("#fvWide").addEventListener("click", e => {
    const wide = document.body.classList.toggle("fv-full");
    e.currentTarget.title = wide ? "Back to the side" : "Full width";
    e.currentTarget.setAttribute("aria-label", e.currentTarget.title);
  });
  $("#fvCopy").addEventListener("click", e => copyText(fv.table ? toCsv(fv.table) : fv.text ?? "", e.currentTarget));
  document.querySelectorAll("#fvModes button").forEach(b => b.addEventListener("click", () => { fv.mode = b.dataset.mode; show(); }));
  // its width: drag the left edge (the chat keeps 380px, the panel 360px), arrows nudge, double-click resets
  const MIN = 360, CHAT = 380, handle = $(".fv-resize");
  const width = (w, remember = true) => {
    const side = Math.max(0, $(".side").getBoundingClientRect().right), max = Math.max(MIN, innerWidth - side - CHAT);
    if (w == null) { document.body.style.removeProperty("--fv-w"); save(KEYS.fileWidth, null); return; }
    w = Math.round(Math.min(max, Math.max(MIN, w)));
    document.body.style.setProperty("--fv-w", `${w}px`);
    handle.setAttribute("aria-valuenow", w);
    if (remember) save(KEYS.fileWidth, w);
  };
  let from = null;
  handle.addEventListener("pointerdown", e => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    from = { x: e.clientX, w: $(".fileview").getBoundingClientRect().width };
    document.body.classList.add("resizing");
  });
  handle.addEventListener("pointermove", e => { if (from) width(from.w + (from.x - e.clientX), false); });
  const done = () => { if (!from) return; from = null; document.body.classList.remove("resizing"); width($(".fileview").getBoundingClientRect().width); };
  handle.addEventListener("pointerup", done);
  handle.addEventListener("pointercancel", done);
  handle.addEventListener("dblclick", () => width(null));
  handle.addEventListener("keydown", e => {
    const now = $(".fileview").getBoundingClientRect().width;
    if (e.key === "ArrowLeft") { e.preventDefault(); width(now + 24); }
    else if (e.key === "ArrowRight") { e.preventDefault(); width(now - 24); }
    else if (e.key === "Home" || e.key === "Enter") { e.preventDefault(); width(null); }
  });
  const saved = Number(load(KEYS.fileWidth));
  if (saved) width(saved, false);
  addEventListener("resize", () => { const w = parseFloat(document.body.style.getPropertyValue("--fv-w")); if (w) width(w, false); });
}

/* ═══ The Files & data panel ════════════════════════════════════════════ */
export const drawerOpen = () => document.body.classList.contains("drawer-open");
export function openDrawer() { closeSide(); fillDrawer(); document.body.classList.add("drawer-open"); $("#drawerClose").focus(); }
export function closeDrawer() { document.body.classList.remove("drawer-open"); }
const closeSide = () => document.body.classList.remove("side-open");
const itemsOf = kind => state.items.filter(i => i.kind === kind);

export function fillDrawer() {
  $("#drawerSub").textContent = state.ws ? `In ${state.ws.name} · ${plural(state.items.length, "item")}` : "";
  const body = $("#drawerBody");
  if (!state.items.length) {
    body.replaceChildren(h("div", { class: "drawer-empty" }, "Nothing here yet. Add spreadsheets, documents or any file, or ask Zen for charts, pictures and notes."));
    return;
  }
  const act = (label, onclick, title) => h("button", { class: "act", type: "button", title, onclick }, label);
  const row = (icon, name, meta, ...actions) => h("div", { class: "item" }, icon, h("div", {}, h("div", { class: "nm", title: name }, name), h("div", { class: "mt", title: meta }, meta)), ...actions);
  const from = i => i.parent ? ` · from ${i.parent}` : "";
  const sections = [
    ["Tables", itemsOf("table"), i => row(fileTile(/\.(csv|tsv|xlsx?|xlsm)$/i.test(i.name || "") ? i.name : "table.csv"), i.name, `${i.id} · ${count(i.rows)} rows × ${i.columns} columns${from(i)}`,
      act("View", () => openItem(i)))],
    ["Documents & files", itemsOf("file"), i => row(isPicture(i.name) ? h("img", { class: "zoomable", src: itemUrl(i.id), alt: i.name, loading: "lazy" }) : fileTile(i.name || ""), i.name,
      `${i.id} · ${i.passages != null ? `${i.pages ? plural(i.pages, "page") : plural(i.words || 0, "word")} · searchable` : fileSize(i.bytes)}${i.report ? ` · ${i.report}` : ""}`,
      canView(i) ? act("View", () => openItem(i)) : h("a", { class: "act", href: itemUrl(i.id), download: i.name || "" }, "Download"))],
    ["Pinned notes", itemsOf("note"), i => row(noteTile(), i.title || i.name, `saved ${i.saved || ""} · kept in view`,
      act("View", () => openItem(i)))],
    ["Images", itemsOf("image"), i => {
      const thumb = h("img", { class: "zoomable", src: itemUrl(i.id), alt: i.name || i.id, loading: "lazy" });
      return row(thumb, i.name || i.id, `${i.id}${i.width ? ` · ${i.width}×${i.height}` : ""}${from(i)}`, act("View", () => openViewer(thumb, "image")));
    }],
    ["Models", itemsOf("model"), i => row(modelTile(), i.name,
      `${i.id}${i.target ? ` · predicts ${i.target}` : ""}${i.metric ? ` · ${i.metric} ${typeof i.score === "number" ? nf2.format(i.score) : i.score}` : ""}`)],
  ];
  body.replaceChildren(...sections.filter(([, list]) => list.length).flatMap(([title, list, make]) => [
    h("div", { class: "sec" }, h("span", {}, title), h("span", {}, String(list.length))), ...list.map(make)]));
}

/* ═══ Attachments ═══════════════════════════════════════════════════════ */
// Files wait in the message box until the message is sent (removable till then); on sending they go into
// the project, their progress shown on the message, and Zen hears of them with that message.
const waiting = [];
export const attachedCount = () => waiting.length;
const typeLine = file => `${(ext(file.name) || "file").toUpperCase()} · ${fileSize(file.size)}`;
function chip(entry, remove) {
  const { file, url } = entry;
  const el = h("div", { class: `chip${url ? " is-pic" : ""}`, title: file.name },
    url ? h("img", { class: "chip-thumb", src: url, alt: "" }) : fileTile(file.name),
    url ? null : h("div", { class: "chip-text" }, h("b", {}, file.name), h("small", {}, typeLine(file))));
  if (remove) el.append(h("button", { class: "chip-x", type: "button", title: "Remove", "aria-label": `Remove ${file.name}`, html: svg("cross"), onclick: remove }));
  return el;
}
function renderTray() {
  const tray = $("#tray");
  tray.replaceChildren(...waiting.map(entry => chip(entry, () => {
    waiting.splice(waiting.indexOf(entry), 1);
    if (entry.url) URL.revokeObjectURL(entry.url);
    renderTray();
    $("#input").focus();
  })));
  tray.hidden = !waiting.length;
  $("#composer").classList.toggle("has-files", waiting.length > 0);
  hooks.updateComposer();
}
export function addFiles(files) {
  if (!state.ws || !files.length) return;
  closeDrawer();
  for (const file of files) waiting.push({ file, url: isPicture(file.name) ? URL.createObjectURL(file) : null });
  renderTray();
  $("#input").focus();
}
// On sending: each waiting file uploads into the project, its progress on the message (bubble); returns
// what came of each - and the follow-ups worth offering when the message had no words
export async function sendFiles(bubble) {
  const entries = waiting.splice(0);
  renderTray();
  const row = h("div", { class: "user-files" });
  bubble.prepend(row);
  const marks = entries.map(entry => {
    const c = chip(entry), mark = h("span", { class: "chip-state" });
    c.classList.add("sending");
    c.style.setProperty("--p", "0");
    c.append(mark);
    row.append(c);
    return { c, mark };
  });
  hooks.stick(true);
  const follow = [];
  for (const [i, entry] of entries.entries()) {
    const { c, mark } = marks[i];
    try {
      const r = await uploadFile(`/workspaces/${state.ws.id}/files`, entry.file, p => {
        c.style.setProperty("--p", String(p));
        c.classList.toggle("reading", p >= 1);  // uploaded; the server is reading it
      });
      c.classList.remove("sending", "reading");
      c.classList.add("done");
      mark.innerHTML = svg("check");
      c.title = r.kind === "table" ? `${entry.file.name}: ${count(r.rows)} rows, saved as ${r.id}`
        : r.kind === "document" ? `${entry.file.name}: ${plural(r.passages, "passage")} searchable` : `${entry.file.name}: saved as ${r.id}`;
      if (r.kind === "table") follow.push(`Give me an overview of ${r.name}`);
      else if (r.kind === "document") follow.push(`Summarise ${r.name}`);
      if (r.note) bubble.after(h("div", { class: "user-note" }, r.note));
    } catch (e) {
      c.classList.remove("sending", "reading");
      c.classList.add("failed");
      mark.innerHTML = svg("cross");
      c.title = e.message;
      bubble.after(h("div", { class: "user-note err" }, `Couldn't add ${entry.file.name}: ${e.message}`));
    }
  }
  hooks.loadItems();
  return follow;
}
// A message that was only files: the next steps worth a click, under it
export function offerFollowUps(bubble, follow) {
  if (!follow.length) return;
  bubble.after(h("div", { class: "user-follow" }, follow.map(q => h("button", { class: "btn-s", type: "button", onclick: () => hooks.submit(q) }, q))));
  hooks.stick();
}

export function wireFiles() {
  wireFilePanel();
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerScrim").addEventListener("click", closeDrawer);
  $("#drawerAdd").addEventListener("click", () => $("#fileInput").click());
  $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", e => { addFiles([...e.target.files]); e.target.value = ""; });
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", e => { if (!hasFiles(e) || !state.ws) return; e.preventDefault(); depth++; $("#drop").classList.add("show"); });
  addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; $("#drop").classList.remove("show"); } });
  addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; $("#drop").classList.remove("show");
    addFiles([...e.dataTransfer.files]);
  });
  $("#input").addEventListener("paste", e => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
}

export { viewerOpen };
