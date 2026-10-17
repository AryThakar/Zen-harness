// What a turn shows besides its text: tables, pictures, diagrams, weather, files, reports, code
// runs and sources. They sit in the answer itself; the chart artifacts are in charts.js.

import { api, iconUrl, imageUrl, itemUrl, state } from "./api.js";
import { card, chartCard } from "./charts.js";
import { consoleEl } from "./console.js";
import { canView, isHtml, madeKind, openItem } from "./files.js";
import { openViewer } from "./viewer.js";
import { cell, codeBlocks, copyText, css, escapeHtml, fileSize, fileTile, h, host, markdown, nf0, plural, polish, svg } from "./util.js";

export const CARD_TYPES = new Set(["chart", "image", "diagram", "weather", "file", "report"]);
const stickHooks = [];
export const onDrawn = fn => stickHooks.push(fn);  // the thread keeps to the bottom as late drawings land
const drawnLate = () => stickHooks.forEach(fn => fn());

/* ═══ Tables ════════════════════════════════════════════════════════════ */
export function tableEl(columns, rows, limit) {
  const numeric = columns.map((_, i) => rows.length > 0 && rows.every(r => r[i] == null || typeof r[i] === "number"));
  const long = columns.map((_, i) => rows.some(r => typeof r[i] === "string" && r[i].length > 60));
  const body = rows.slice(0, limit ?? rows.length).map(r => h("tr", {}, r.map((v, i) => h("td", { class: numeric[i] ? "num" : long[i] ? "wrap" : null }, cell(v)))));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c, i) => h("th", { class: numeric[i] ? "num" : null }, c)))), h("tbody", {}, body)));
}
const sqlFold = sql => {
  const code = h("code", { class: "language-sql" }, sql);
  const fold = h("details", { class: "fold" }, h("summary", {}, "SQL"), h("pre", { class: "code" }, code));
  if (window.hljs) try { hljs.highlightElement(code); } catch {}
  return fold;
};
const notesEl = notes => h("ul", { class: "notes" }, notes.map(n => h("li", {}, typeof n === "string" ? n : JSON.stringify(n))));

export function tableCard(a) {
  const rows = a.rows || [], limit = 8;
  const { el } = card(a.title || "Table", a.note || plural(rows.length, "row"));
  el.classList.add("table-card");
  let wrap = tableEl(a.columns || [], rows, limit);
  el.append(wrap);
  if (rows.length > limit) {
    const more = h("button", { class: "more", type: "button" }, `Show all ${nf0.format(rows.length)}`);
    more.addEventListener("click", () => { const full = tableEl(a.columns, rows); wrap.replaceWith(full); wrap = full; more.remove(); });
    el.append(more);
  }
  if (a.notes?.length) el.append(notesEl(a.notes));
  if (a.sql) el.append(sqlFold(a.sql));
  return el;
}
export function resultsCard(tables) {
  if (tables.length === 1) return tableCard(tables[0]);
  const { el } = card("Results", plural(tables.length, "table"));
  el.classList.add("table-card");
  tables.forEach((a, i) => {
    const rows = a.rows || [];
    el.append(h("div", { style: i ? "margin-top:16px" : null },
      a.title && a.title !== "Query result" ? h("div", { class: "sub-t" }, a.title) : null,
      tableEl(a.columns || [], rows, 8),
      rows.length > 8 ? h("div", { class: "muted" }, `${rows.length - 8} more rows`) : null,
      a.notes?.length ? notesEl(a.notes) : null,
      a.sql ? sqlFold(a.sql) : null));
  });
  return el;
}

/* ═══ Pictures and diagrams ═════════════════════════════════════════════ */
function imageCard(a) {
  return h("div", { class: "card media enter" },
    h("img", { class: "zoomable", src: itemUrl(a.id), alt: a.prompt || "Generated image", width: a.width, height: a.height, loading: "lazy" }),
    h("div", { class: "media-foot" }, h("span", { title: a.prompt }, a.prompt || "")));
}

let mermaid = null, seq = 0;
const diagrams = [];  // each one drawn, so a theme change can draw it again
async function drawMermaid(box, code, failed) {
  try {
    mermaid ??= (await import("https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.esm.min.mjs")).default;
    const dark = document.documentElement.dataset.theme !== "light";
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true, theme: "base",
      flowchart: { curve: "basis", padding: 14, useMaxWidth: false },  // its own size; the card fits it (app.css .diagram)
      themeVariables: { darkMode: dark, fontFamily: css("--font"), fontSize: "14px", primaryColor: css("--bubble"), primaryTextColor: css("--ink"),
        primaryBorderColor: css("--data"), lineColor: css("--soft"), secondaryColor: css("--surface"), tertiaryColor: css("--surface"),
        edgeLabelBackground: css("--surface") } });
    await mermaid.parse(code);  // first: a failed render leaves Mermaid's error picture in the page
    const { svg: drawing } = await mermaid.render(`mmd-${++seq}`, code);
    box.innerHTML = drawing;  // strict mode sanitises it
    if (!diagrams.some(d => d.box === box)) diagrams.push({ box, code });
    drawnLate();
  } catch (e) {
    const why = String(e?.message || e || "").split("\n").filter(Boolean).slice(0, 3).join("\n");
    box.replaceChildren(h("div", { class: "diagram-fail" }, h("span", { class: "df-ic", html: svg("alert") }),
      h("div", { class: "df-text" }, h("b", {}, "This diagram couldn't be drawn"), h("small", {}, "Its code has a mistake; the code is just below."),
        why ? h("pre", { class: "code" }, why) : null)));
    failed?.();
  }
}
export function redrawDiagrams() {
  for (let i = diagrams.length - 1; i >= 0; i--) {
    if (!diagrams[i].box.isConnected) diagrams.splice(i, 1);
    else drawMermaid(diagrams[i].box, diagrams[i].code);
  }
}
function diagramParts(source, title, kind) {
  const box = h("div", { class: "diagram zoomable", title: "Click to open larger" }, h("p", { class: "muted" }, "Drawing…"));
  const code = h("details", { class: "fold" }, h("summary", {}, "Mermaid code"), h("pre", { class: "code" }, source));
  const { el } = card(title || "Diagram", kind);
  el.append(box, code);
  drawMermaid(box, source, () => code.setAttribute("open", ""));
  return el;
}
const diagramCard = a => diagramParts(a.mermaid, a.title, a.kind);

// A finished answer: ```mermaid is drawn (the model wrote a diagram instead of calling draw_diagram),
// other code blocks get colours and a copy button, and an image of this project named by its id shows.
export function finishAnswer(root) {
  root.querySelectorAll("pre > code.language-mermaid").forEach(block => {
    block.parentElement.replaceWith(diagramParts(block.textContent, "Diagram", "flowchart"));
  });
  codeBlocks(root);
  const reply = root.closest(".reply");
  // what the reply's cards already show (cards sit in its flow, between its words)
  const shown = new Set([...(reply?.querySelectorAll(".flow > :not(.answer) img") || [])].map(i => i.getAttribute("src")));
  const charts = new Set([...(reply?.querySelectorAll(".flow > [data-item], .flow > :not(.answer) [data-item]") || [])].map(c => c.dataset.item));
  root.querySelectorAll("img").forEach(img => {
    const id = (img.dataset.item || img.getAttribute("src") || "").trim();
    if (/\.chart\.json$/i.test(id)) { answerChart(img, id, charts, reply); return; }  // a chart file: drawn live, once
    if (img.dataset.item || /^image_\d+$/.test(id)) { answerPicture(img, id, shown); return; }  // a project picture
    img.addEventListener("error", () => img.remove(), { once: true });  // a picture that doesn't load leaves no broken icon
  });
  root.querySelectorAll(":not(pre) > code").forEach(span => {
    const id = span.textContent.trim();
    if (/^image_\d+$/.test(id) && state.items.some(i => i.id === id)) {
      span.replaceWith(h("img", { class: "zoomable answer-pic", src: itemUrl(id), alt: id, loading: "lazy" }));
    }
  });
}

// A file of the project an answer names as a picture (![caption](panda_edited.jpg), or a key of an old chat): the
// picture there - or nothing, when the reply already shows it as a card, or it isn't a picture of this project.
async function answerPicture(img, name, shown) {
  const block = img.parentElement?.tagName === "P" && img.parentElement.childNodes.length === 1 ? img.parentElement : img;
  const want = name.toLowerCase();
  const find = () => state.items.find(i => i.id === name || (i.name || "").toLowerCase() === want);
  let item = find();
  if (!item) { try { state.items = await api(`/workspaces/${state.ws.id}/items`); } catch {} item = find(); }  // made this turn
  if (!item || item.kind !== "image") { block.remove(); return; }
  const src = itemUrl(item.id);
  if (shown.has(src)) { block.remove(); return; }
  shown.add(src);
  Object.assign(img, { src, alt: img.alt || item.name, className: "zoomable answer-pic", loading: "lazy" });
  img.addEventListener("error", () => block.remove(), { once: true });
}

// A chart file an answer names (![caption](charts/sales.chart.json)): the chart shows there, once - the reply's own
// card moved to that spot, so each chart sits above the line that explains it (measured: the cards above, the lines
// below with a gap for each chart), or drawn there when the reply has no card for it.
async function answerChart(img, name, charts, reply) {
  const spot = chartSpot(img);
  const find = () => state.items.find(i => i.name === name || i.name?.toLowerCase() === name.toLowerCase());
  let item = find();
  if (!item) { try { state.items = await api(`/workspaces/${state.ws.id}/items`); } catch {} item = find(); }
  if (!item) { spot.remove(); return; }
  if (charts.has(item.id)) {
    const card = reply?.querySelector(`.flow > [data-item="${CSS.escape(item.id)}"], .flow > :not(.answer) [data-item="${CSS.escape(item.id)}"]`);
    card ? spot.replaceWith(card) : spot.remove();  // no card left: named twice, it is already shown
    return;
  }
  charts.add(item.id);
  try {
    const spec = await (await fetch(itemUrl(item.id))).json();
    spot.replaceWith(chartCard({ type: "chart", ...spec, id: item.id }));
  } catch { spot.remove(); }
}

// The chart's own place: out of its paragraph (with the line break beside it), just before the paragraph's text.
function chartSpot(img) {
  const p = img.parentElement;
  if (p?.tagName !== "P") return img;
  for (const side of [img.previousSibling, img.nextSibling]) if (side?.nodeName === "BR") side.remove();
  const spot = h("div", {});
  p.before(spot);
  img.remove();
  if (!p.textContent.trim() && !p.querySelector("img")) p.remove();
  return spot;
}

/* ═══ Weather ═══════════════════════════════════════════════════════════ */
const WX = {
  sun: '<circle cx="12" cy="12" r="4.5" fill="var(--sun)"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4" stroke="var(--sun)" stroke-width="1.6" stroke-linecap="round"/>',
  partly: '<circle cx="9" cy="9" r="4" fill="var(--sun)"/><path d="M8 19h9.5a3.5 3.5 0 0 0 .4-6.98A5 5 0 0 0 8.3 11.6 3.7 3.7 0 0 0 8 19z" fill="var(--muted)"/>',
  cloud: '<path d="M7 18h10a4 4 0 0 0 .5-7.97A5.5 5.5 0 0 0 6.6 11 3.5 3.5 0 0 0 7 18z" fill="var(--muted)"/>',
  rain: '<path d="M7 15h10a3.5 3.5 0 0 0 .4-6.98A5 5 0 0 0 7.3 7.6 3.7 3.7 0 0 0 7 15z" fill="var(--muted)"/><path d="M9 17.5 8 20M13 17.5 12 20M17 17.5 16 20" stroke="var(--rain)" stroke-width="1.6" stroke-linecap="round"/>',
  storm: '<path d="M7 14h10a3.5 3.5 0 0 0 .4-6.98A5 5 0 0 0 7.3 6.6 3.7 3.7 0 0 0 7 14z" fill="var(--muted)"/><path d="m12.5 14-2 4h3l-1.5 3.5" stroke="var(--sun)" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  snow: '<path d="M7 15h10a3.5 3.5 0 0 0 .4-6.98A5 5 0 0 0 7.3 7.6 3.7 3.7 0 0 0 7 15z" fill="var(--muted)"/><circle cx="9" cy="18.5" r="1.1" fill="var(--rain)"/><circle cx="13" cy="20" r="1.1" fill="var(--rain)"/><circle cx="17" cy="18.5" r="1.1" fill="var(--rain)"/>',
  fog: '<path d="M4 9h16M6 13h14M4 17h12" stroke="var(--muted)" stroke-width="1.8" stroke-linecap="round"/>',
};
function wxIcon(condition = "") {
  const c = condition.toLowerCase();
  const key = /thunder|storm/.test(c) ? "storm" : /snow|sleet|ice/.test(c) ? "snow" : /rain|shower|drizzle/.test(c) ? "rain"
    : /fog|mist|haze/.test(c) ? "fog" : /partly|mostly sunny|few|scattered/.test(c) ? "partly" : /cloud|overcast/.test(c) ? "cloud" : "sun";
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${WX[key]}</svg>`;
}
function weatherCard(a) {
  const c = a.current || {}, air = a.air_quality || {};
  const { el } = card(a.place || "Weather", a.local_time ? `Local time ${a.local_time}` : null);
  const tags = [
    c.feels_like_c != null && `Feels like ${nf0.format(c.feels_like_c)}°`,
    c.humidity_pct != null && `Humidity ${nf0.format(c.humidity_pct)}%`,
    c.wind_kmh != null && `Wind ${nf0.format(c.wind_kmh)} km/h`,
    c.uv_index != null && `UV ${nf0.format(c.uv_index)}${c.uv ? ` · ${c.uv}` : ""}`,
    air.us_aqi != null && `Air ${nf0.format(air.us_aqi)}${air.category ? ` · ${air.category}` : ""}`,
  ].filter(Boolean);
  if (c.temperature_c != null || c.condition) {
    el.append(h("div", { class: "wx-now" }, h("span", { html: wxIcon(c.condition) }),
      c.temperature_c != null ? h("div", { class: "wx-temp" }, `${nf0.format(c.temperature_c)}°`) : null,
      h("div", {}, h("div", { class: "wx-cond" }, c.condition || ""), h("div", { class: "tags" }, tags.map(t => h("span", { class: "tag" }, t))))));
  }
  if (a.daily?.length) {
    el.append(h("div", { class: "wx-days" }, a.daily.map(d => h("div", { class: "wx-day" },
      h("span", {}, new Date(`${d.date}T12:00`).toLocaleDateString(undefined, { weekday: "short" })),
      h("span", { html: wxIcon(d.condition) }), h("span", {}, d.condition || ""),
      h("span", { class: "rain" }, d.rain_chance_pct != null ? `${nf0.format(d.rain_chance_pct)}%` : ""),
      h("span", { class: "rng", html: d.min_c != null && d.max_c != null ? `${nf0.format(d.min_c)}° – <b>${nf0.format(d.max_c)}°</b>` : "" })))));
  }
  if (a.warnings?.length) el.append(h("p", { class: "muted" }, a.warnings.join(" ")));
  return el;
}

/* ═══ Files, reports, code ═══════════════════════════════════════ */
const answerBlock = text => { const el = h("div", { class: "answer report", html: markdown(text || "") }); polish(el); return el; };
const lineCount = s => (s || "").trimEnd().split("\n").length;

// A card's button: its word, or on a phone just its icon (so a file's two actions keep to one line)
const action = (tag, icon, label, props) => h(tag, { class: "btn-s", title: label, "aria-label": label, ...props },
  h("span", { class: "btn-ic", html: svg(icon) }), h("span", { class: "btn-lbl" }, label));

/* What a helper's work leaves to look into - its report, its code, what the code printed - as tabs under
   its card: one opens at a time, in a panel with its own header and a copy button, and never wider than
   the chat. */
const RUN_ICONS = { report: "doc", code: "code", output: "terminal", error: "alert" };
function runPart(key, a) {
  if (key === "report") return { key, label: "Report", title: "Report", copy: a.report, body: () => answerBlock(a.report) };
  if (key === "code") return { key, label: "Code", count: plural(lineCount(a.code), "line"), title: "Python", copy: a.code, body: () => {
    const code = h("code", { class: "language-python" }, a.code.trimEnd());
    if (window.hljs) try { hljs.highlightElement(code); } catch {}
    return h("pre", { class: "code" }, code);
  } };
  if (key === "output") return { key, label: "Output", count: plural(lineCount(a.output), "line"), title: "Output", copy: a.output,
    body: () => h("pre", { class: "code out" }, a.output.trimEnd()) };
  return { key, label: "Error", title: "What went wrong", copy: a.errors, body: () => h("pre", { class: "code out err-out" }, a.errors.trimEnd()) };
}
function runEl(parts = [], open = null) {
  const tabs = h("div", { class: "run-tabs" }), inner = h("div", { class: "run-inner" });
  const el = h("div", { class: "run" }, tabs, h("div", { class: "run-panel" }, inner));
  let current = null;
  const show = (p, btn) => {
    const closing = current === p.key;
    tabs.querySelectorAll(".run-tab").forEach(b => b.setAttribute("aria-expanded", String(!closing && b === btn)));
    el.classList.toggle("open", !closing);
    if (closing) { current = null; return; }
    current = p.key;
    const copy = h("button", { class: "run-copy", type: "button", title: "Copy", html: svg("copy") + "<span>Copy</span>" });
    copy.addEventListener("click", () => copyText(p.copy, copy, svg("check") + "<span>Copied</span>"));
    inner.replaceChildren(h("div", { class: `run-box is-${p.key}` },
      h("div", { class: "run-head" }, h("span", { html: svg(RUN_ICONS[p.key]) }), h("b", {}, p.title), p.count ? h("small", {}, p.count) : null, copy),
      h("div", { class: "run-body" }, p.body())));
  };
  el.add = p => {
    const btn = h("button", { class: `run-tab is-${p.key}`, type: "button", "aria-expanded": "false" },
      h("span", { html: svg(RUN_ICONS[p.key]) }), p.label, p.count ? h("small", {}, p.count) : null);
    btn.addEventListener("click", () => show(p, btn));
    tabs.append(btn);
    if (p.key === open || p.key === "error") show(p, btn);
  };
  parts.forEach(el.add);
  return el;
}
const pictures = ids => ids.length ? h("div", { class: "outputs" }, ids.map(id => h("img", { class: "zoomable", src: itemUrl(id), alt: id, loading: "lazy" }))) : null;

function fileCard(a) {
  const verb = { updated: "Updated", saved: "Saved" }[a.action] || "Wrote";
  const el = h("div", { class: "card slim enter" }, fileTile(a.name || ""),
    h("div", { class: "what", html: `${verb} <b>${escapeHtml(a.name || "")}</b>${a.bytes != null ? `<small>${fileSize(a.bytes)}</small>` : ""}` }));
  const item = { id: a.id, kind: "file", name: a.name, bytes: a.bytes };
  if (/\.py$/i.test(a.name || "")) {  // a program: it runs here, the user typing while it runs
    el.append(action("button", "play", "Run", { type: "button", onclick: () => {
      el.querySelector(".console")?.remove();
      el.querySelector(".below").append(consoleEl(item, { openFile: f => openItem({ id: f.id, name: f.path, kind: madeKind(f) }) }));
    } }));
  }
  if (canView(item)) el.append(action("button", "eye", "View", { type: "button", onclick: () => openItem(item) }));
  el.append(action("a", "download", "Download", { href: itemUrl(a.id), download: a.name || "" }));
  // the coder's save: what its last run drew, then its report (what Zen answers from); its run joins later
  const run = runEl(a.report ? [runPart("report", a)] : []);
  el.append(h("div", { class: "below" }, pictures((a.outputs || []).filter(id => id.startsWith("image_"))), run));
  return el;
}

// A page Zen wrote - a game, a tool: a card with its picture as it opened (taken when it was tried, browser.try_page),
// its title and Open, which runs it in the file panel at a real window's size, with full screen there. Run inline in
// the chat instead, a page sized to its window (100vh, a canvas the window's size) was cut or blank, and every game
// kept running and sounding as the chat scrolled on (measured 2026-10-17).
function pageCard(a) {
  const item = { id: a.id, kind: "file", name: a.name, bytes: a.bytes };
  const open = () => openItem(item);
  const shot = h("button", { class: "page-shot", type: "button", title: `Open ${a.name}`, "aria-label": `Open ${a.name}` });
  const pic = h("img", { alt: "", loading: "lazy", src: `${itemUrl(a.id).replace(/\/file$/, "/preview")}?v=${a.bytes ?? ""}` });
  pic.addEventListener("error", () => { pic.remove(); shot.classList.add("none"); shot.innerHTML = svg("globe"); }, { once: true });
  shot.append(pic);
  shot.addEventListener("click", open);
  const title = h("b", {}, a.name || "");
  const what = h("div", { class: "what" }, title, h("small", {}, ["Page", a.bytes != null ? fileSize(a.bytes) : null]
    .filter(Boolean).join(" · ")));
  const el = h("div", { class: "card page-card enter" }, shot, h("div", { class: "page-side" }, what,
    h("div", { class: "page-acts" }, h("button", { class: "btn-s on", type: "button", onclick: open },
      h("span", { class: "btn-ic", html: svg("play") }), h("span", {}, "Open")),
      action("a", "download", "Download", { href: itemUrl(a.id), download: a.name || "" }))));
  // its own title, from the page (the file's name until then)
  fetch(itemUrl(a.id)).then(r => r.ok ? r.text() : "").then(text => {
    const named = /<title[^>]*>([^<]{1,80})<\/title>/i.exec(text)?.[1]?.trim();
    if (named) { title.textContent = named; title.title = a.name; }
  }).catch(() => {});
  return el;
}
function reportCard(a) {
  const el = h("div", { class: "card slim enter" }, fileTile(a.name || a.table || "table.csv"),
    h("div", { class: "what", html: `Report on <b>${escapeHtml(a.name || a.table || "")}</b><small>Kept with the table: the next analysis starts from it</small>` }));
  el.append(h("div", { class: "below" }, runEl([runPart("report", a)])));
  return el;
}
const runParts = a => [a.code && runPart("code", a), a.output && runPart("output", a), a.errors && runPart("error", a)].filter(Boolean);
// The coder's run, added to the card of the file it saved: one block instead of two
export function addRun(fileEl, a) {
  const run = fileEl.querySelector(".run");
  if (run) runParts(a).forEach(run.add);
}
export function codeCard(a) {
  const { el } = card("Python", a.errors ? "Finished with errors" : "Ran on this computer");
  el.classList.add("code-card");
  // a run on its own: what it printed is its result, so that tab starts open
  el.append(runEl(runParts(a), lineCount(a.output) <= 12 ? "output" : null));
  const files = (a.files || []).filter(f => f.id);
  const made = files.filter(f => madeKind(f) === "image"), others = files.filter(f => madeKind(f) !== "image");
  if (made.length) el.append(pictures(made.map(f => f.id)));
  if (others.length) {
    el.append(h("div", { class: "tags" }, others.map(f => {
      const item = { id: f.id, kind: madeKind(f), name: f.path };
      return canView(item) ? h("button", { class: "tag", type: "button", onclick: () => openItem(item) }, f.path)
        : item.kind === "file" ? h("a", { class: "tag", href: itemUrl(f.id), download: f.path }, `${f.path} ↓`)
        : h("span", { class: "tag" }, f.path);
    })));
  }
  return el;
}

export function artifactCard(a) {
  const make = { chart: chartCard, image: imageCard, diagram: diagramCard, weather: weatherCard,
                 table: tableCard, code: codeCard, file: fileCard, report: reportCard }[a.type];
  if (a.type === "file" && isHtml(a.name)) return pageCard(a);
  return make ? make(a) : null;
}

/* ═══ Sources ═══════════════════════════════════════════════════════════ */
function favicon(page, fav, cls = "fav") {
  const img = h("img", { class: cls, src: iconUrl(page, fav), alt: "", loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(h("span", { class: `${cls} letter` }, (host(page)[0] || "?").toUpperCase())), { once: true });
  return img;
}
export { favicon };

// "2026-06-28T02:06:28+00:00" -> "28 Jun 2026"; anything else as it came
const day = s => { const t = Date.parse(s); return s && /^\d{4}-\d{2}-\d{2}/.test(s) && !Number.isNaN(t) ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : s; };

export function webSourcesEl(items) {
  const videos = items.filter(i => i.video?.embed), pages = items.filter(i => !i.video?.embed);
  const wrap = h("div", { class: "sources" }, h("div", { class: "sources-label" }, "Sources"));
  for (const v of videos) {
    const thumb = h("div", { class: "vthumb" });
    if (v.image) thumb.append(h("img", { src: imageUrl(v.image), alt: "", loading: "lazy" }));
    const play = h("button", { type: "button", "aria-label": `Play ${v.title || "the video"}`, html: '<svg viewBox="0 0 24 24"><path d="M9 6.2v11.6l9.4-5.8z"/></svg>' });
    play.addEventListener("click", () => openViewer(thumb, "video", { embed: v.video.embed, title: v.title }));
    thumb.append(play);
    wrap.append(h("div", { class: "video" }, thumb, h("div", {},
      h("a", { class: "t", href: v.url, target: "_blank", rel: "noopener noreferrer" }, v.title || v.url),
      h("div", { class: "h" }, [v.author, v.video.length, day(v.published)].filter(Boolean).join(" · ")))));
  }
  if (pages.length) {
    wrap.append(h("div", { class: "sites" }, pages.map(p => h("a", { class: "site", href: p.url, target: "_blank", rel: "noopener noreferrer", title: p.description || p.title },
      favicon(p.url, p.favicon), h("div", {}, h("div", { class: "t" }, p.title || host(p.url)),
        h("div", { class: "h" }, [p.site || host(p.url), day(p.published)].filter(Boolean).join(" · ")))))));
  }
  return wrap;
}

export function passagesEl(items) {
  const unique = [...new Map(items.map(i => [i.source, i])).values()].slice(0, 6);
  const wrap = h("div", { class: "sources" }), row = h("div", { class: "docs" });
  let open = null;
  for (const item of unique) {
    const picture = /^image_\d+$/.test(item.ref || "");  // a picture found by what it shows: show the picture
    const chip = h("button", { class: "doc-chip", type: "button", "aria-expanded": "false", title: item.source, html: svg(picture ? "image" : "doc") }, item.source);
    chip.addEventListener("click", () => {
      const was = open?.chip === chip;
      open?.panel.remove(); open?.chip.classList.remove("on"); open?.chip.setAttribute("aria-expanded", "false"); open = null;
      if (was) return;
      const panel = h("div", { class: "passage" }, h("b", {}, item.source),
        picture ? h("img", { class: "zoomable passage-pic", src: itemUrl(item.ref), alt: item.text || item.ref }) : item.text);
      wrap.append(panel);
      chip.classList.add("on");
      chip.setAttribute("aria-expanded", "true");
      open = { chip, panel };
    });
    row.append(chip);
  }
  wrap.append(h("div", { class: "sources-label" }, "Sources"), row);
  return wrap;
}

