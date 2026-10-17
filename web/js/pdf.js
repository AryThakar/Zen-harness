// A PDF in the file panel, drawn by PDF.js in Zen's own colours: its pages one under another on the panel's background
// (the browser's own viewer brought its grey and its toolbar into the app), and a floating bar - the page, zoom, and the
// pages' colours. In the dark theme the pages are dark too, following the app as it changes; the sun and moon switch
// them to their own colours and back. Pages are drawn as they come into view and let go far from it, so a 1,923-page
// specification opens at once, at a source tag's page. Where PDF.js can't load (offline), the browser's viewer stands in.

import { h, svg } from "./util.js";

const VERSION = "4.10.38";
const BASE = `https://cdn.jsdelivr.net/npm/pdfjs-dist@${VERSION}/build`;
const KEPT = 24;  // pages kept drawn: the ones further from the page in view are let go
const OWN = "zen.pdf.own";  // the user keeps the pages in their own colours in the dark theme
// the app's own sun and moon (index.html, the theme button), turning as the pages' colours do
const MODE_ICON = '<svg class="i ic-page" viewBox="0 0 24 24"><g class="moon"><path d="M20.37 13.47A8.5 8.5 0 1 1 10.53 3.63A7 7 0 0 0 20.37 13.47Z"/></g>'
  + '<g class="sun"><circle cx="12" cy="12" r="4"/><path d="M12 2.6v2M12 19.4v2M2.6 12h2M19.4 12h2M5.4 5.4l1.4 1.4M17.2 17.2l1.4 1.4M5.4 18.6l1.4-1.4M17.2 6.8l1.4-1.4"/></g></svg>';
let lib = null;

async function pdfjs() {
  if (!lib) {
    lib = await import(`${BASE}/pdf.min.mjs`);
    lib.GlobalWorkerOptions.workerSrc = `${BASE}/pdf.worker.min.mjs`;
  }
  return lib;
}

const fallback = (url, page, name) => h("iframe", { class: "pdf-view", src: url + (page ? `#page=${page}` : ""), title: name });

// url: the PDF (a blob URL); page: where it opens; onPage(n, total): the page in view, for the status line
export function pdfView(url, { page = null, name = "PDF", onPage = () => {} } = {}) {
  const pages = h("div", { class: "pdf-pages", tabindex: "0" });
  const at = h("input", { class: "pdf-at", type: "text", inputmode: "numeric", "aria-label": "Page", value: page || 1 });
  const total = h("span", { class: "pdf-total" }, "/ …");
  const btn = (icon, label, onclick, cls = "") => h("button", { class: `pdf-btn ${cls}`, type: "button", title: label, "aria-label": label, html: icon.startsWith("<") ? icon : svg(icon), onclick });
  const zoomLabel = h("button", { class: "pdf-zoom", type: "button", title: "Fit the width" }, "Fit");
  const sep = () => h("span", { class: "pdf-sep", "aria-hidden": "true" });
  const mode = btn(MODE_ICON, "", () => setOwn(!wrap.classList.contains("own")), "pdf-mode");
  const bar = h("div", { class: "pdf-bar", role: "toolbar", "aria-label": "Pages" },
    btn("chev", "Previous page", () => go(current - 1), "prev"), at, total, btn("chev", "Next page", () => go(current + 1)), sep(),
    btn("minus", "Zoom out", () => zoom(1 / 1.2)), zoomLabel, btn("plus", "Zoom in", () => zoom(1.2)),
    h("span", { class: "pdf-sep dark-only", "aria-hidden": "true" }), mode);
  const wrap = h("div", { class: "pdf-wrap" }, pages, bar);
  const setOwn = own => {
    wrap.classList.toggle("own", own);
    const label = own ? "Pages in the app's colours" : "Pages in their own colours";
    mode.title = label;
    mode.setAttribute("aria-label", label);
    try { localStorage.setItem(OWN, own ? "1" : ""); } catch {}
  };
  let own = false;
  try { own = !!localStorage.getItem(OWN); } catch {}
  setOwn(own);

  let doc = null, base = null, fit = true, factor = 1, scale = 1, boxes = [], drawn = new Map(), current = 1, frame = 0;
  const width = () => Math.max(200, pages.clientWidth - 32);
  const sizeBoxes = () => {
    scale = (fit ? width() / base.width : factor);
    zoomLabel.textContent = fit ? "Fit" : `${Math.round(scale * 100)}%`;
    for (const b of boxes) {
      const vp = b.size || base;
      b.el.style.width = `${Math.floor(vp.width * scale)}px`;
      b.el.style.height = `${Math.floor(vp.height * scale)}px`;
    }
  };
  const forget = n => {
    const d = drawn.get(n);
    if (!d) return;
    d.task?.cancel();
    boxes[n - 1].el.replaceChildren();
    drawn.delete(n);
  };
  const draw = async n => {
    if (drawn.has(n) || !doc) return;
    const mine = { scale };
    drawn.set(n, mine);
    try {
      const p = await doc.getPage(n);
      const box = boxes[n - 1];
      box.size = p.getViewport({ scale: 1 });
      if (mine.scale !== scale || drawn.get(n) !== mine) return;
      const vp = p.getViewport({ scale });
      box.el.style.width = `${Math.floor(vp.width)}px`;
      box.el.style.height = `${Math.floor(vp.height)}px`;
      const ratio = devicePixelRatio || 1;
      const canvas = h("canvas", { width: Math.floor(vp.width * ratio), height: Math.floor(vp.height * ratio) });
      canvas.style.width = `${Math.floor(vp.width)}px`;
      canvas.style.height = `${Math.floor(vp.height)}px`;
      const text = h("div", { class: "textLayer" });
      text.style.setProperty("--scale-factor", vp.scale);
      text.style.setProperty("--total-scale-factor", vp.scale);
      box.el.replaceChildren(canvas, text);
      mine.task = p.render({ canvasContext: canvas.getContext("2d"), viewport: vp, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null });
      await mine.task.promise;
      if (drawn.get(n) !== mine) return;
      try { await new lib.TextLayer({ textContentSource: p.streamTextContent(), container: text, viewport: vp }).render(); } catch {}  // only for selecting text
    } catch (e) {
      if (e?.name !== "RenderingCancelledException" && drawn.get(n) === mine) drawn.delete(n);
    }
  };
  // the page in the middle of the view; drawing the pages around it and letting go of the far ones
  const look = () => {
    frame = 0;
    if (!boxes.length) return;
    const mid = pages.scrollTop + pages.clientHeight / 2;
    let lo = 0, hi = boxes.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (boxes[m].el.offsetTop <= mid) lo = m; else hi = m - 1; }
    current = lo + 1;
    if (document.activeElement !== at) at.value = current;
    onPage(current, boxes.length);
    for (let n = Math.max(1, current - 2); n <= Math.min(boxes.length, current + 3); n++) draw(n);
    if (drawn.size > KEPT) [...drawn.keys()].filter(n => Math.abs(n - current) > KEPT / 2).forEach(forget);
  };
  const go = n => {
    n = Math.min(Math.max(1, Math.round(n) || 1), boxes.length);
    pages.scrollTop = boxes[n - 1].el.offsetTop - 12;
    look();
  };
  const zoom = by => {
    const keep = current;
    factor = Math.min(4, Math.max(.25, scale * by));
    fit = false;
    redraw(keep);
  };
  const redraw = keep => {
    [...drawn.keys()].forEach(forget);
    sizeBoxes();
    go(keep);
  };
  zoomLabel.addEventListener("click", () => { fit = true; redraw(current); });
  at.addEventListener("keydown", e => { if (e.key === "Enter") { go(Number(at.value)); at.blur(); } });
  at.addEventListener("blur", () => { at.value = current; });
  pages.addEventListener("scroll", () => { if (!frame) frame = requestAnimationFrame(look); }, { passive: true });
  pages.addEventListener("keydown", e => {
    if (e.key === "PageDown" || e.key === "PageUp") { e.preventDefault(); go(current + (e.key === "PageDown" ? 1 : -1)); }
  });
  let resized = 0;
  new ResizeObserver(() => {
    if (!base || !fit) return;
    clearTimeout(resized);
    resized = setTimeout(() => { if (Math.abs(width() / base.width - scale) > .01) redraw(current); }, 150);
  }).observe(pages);

  (async () => {
    try {
      const pdf = await pdfjs();
      doc = await pdf.getDocument({ url, isEvalSupported: false }).promise;
      base = (await doc.getPage(1)).getViewport({ scale: 1 });
      total.textContent = `/ ${doc.numPages}`;
      boxes = Array.from({ length: doc.numPages }, (_, i) => ({ el: h("div", { class: "pdf-page", "data-page": i + 1 }), size: null }));
      pages.replaceChildren(...boxes.map(b => b.el));
      requestAnimationFrame(() => { sizeBoxes(); go(page || 1); });
    } catch {
      wrap.replaceWith(fallback(url, page, name));  // offline, or a file PDF.js can't read: the browser's viewer
    }
  })();
  return wrap;
}
