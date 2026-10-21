// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// The viewer: a picture, a diagram, a chart or a video, full size over the chat (which blurs behind it). It grows
// from where the thing sat; scroll zooms where the pointer is, drag moves it, a click zooms in and back,
// a click beside it closes; x, Esc, or the download button at the top right. A video plays at 16:9 with its
// own controls and a full-screen button, and doesn't zoom.

import { h, svg } from "./util.js";

const V = { el: null, stage: null, item: null, s: 1, x: 0, y: 0, fit: 1, w: 0, h: 0, from: null, charts: [], url: null };
const parts = () => {
  V.el ??= document.querySelector(".viewer");
  V.stage ??= V.el.querySelector(".viewer-stage");
  V.item ??= V.el.querySelector(".viewer-item");
};
export const viewerOpen = () => { parts(); return !V.el.hidden; };

function place(animate) {
  V.item.classList.toggle("anim", !!animate);
  V.item.style.transform = `translate(${V.x}px, ${V.y}px) scale(${V.s})`;
  V.item.style.setProperty("--s", V.s);  // corners stay the same size on screen at any zoom
  V.stage.classList.toggle("zoomed", V.s > V.fit * 1.02);
  V.el.querySelector(".pct").textContent = `${Math.round(V.s * 100)}%`;
}
export function fitView(animate) {
  const r = V.stage.getBoundingClientRect();
  V.s = V.fit;
  V.x = (r.width - V.w * V.s) / 2;
  V.y = (r.height - V.h * V.s) / 2 - 26;  // clear of the zoom bar below
  place(animate);
}
function zoomAt(s, cx, cy, animate) {
  s = Math.min(Math.max(V.fit * 6, 4), Math.max(V.fit * .5, s));
  V.x = cx - (cx - V.x) * (s / V.s);
  V.y = cy - (cy - V.y) * (s / V.s);
  V.s = s;
  place(animate);
}
export function zoomBy(k) { const r = V.stage.getBoundingClientRect(); zoomAt(V.s * k, r.width / 2, r.height / 2, true); }

// media (a video): { embed } for a web video's player, or { src, name } for a video file; title for both
export async function openViewer(source, kind, media = {}) {
  parts();
  closeViewer(true);
  V.from = source;
  V.item.replaceChildren();
  V.el.classList.toggle("is-video", kind === "video");
  let title = "", download = null, filename = "", upscale = 2;
  if (kind === "video") {
    V.el.hidden = false;  // laid out, so the stage can be measured
    const r = V.stage.getBoundingClientRect();
    const w = Math.max(240, Math.min(1280, r.width - 80, (r.height - 120) * 16 / 9));
    const player = media.embed
      ? h("iframe", { src: `${media.embed}?autoplay=1`, title: media.title || "Video", allowfullscreen: true,
          allow: "autoplay; encrypted-media; picture-in-picture; fullscreen", referrerpolicy: "strict-origin-when-cross-origin" })
      : h("video", { src: media.src, controls: true, autoplay: true, playsinline: true });
    V.item.append(h("div", { class: "viewer-video", style: `width: ${w}px` }, player));
    V.w = w; V.h = w * 9 / 16; upscale = 1;
    title = media.title || media.name || "";
    if (media.src) { download = media.src; filename = media.name || ""; }
  } else if (kind === "image") {
    const img = h("img", { src: source.currentSrc || source.src, alt: source.alt || "" });
    await img.decode().catch(() => {});
    V.item.append(img);
    V.w = img.naturalWidth || 800; V.h = img.naturalHeight || 600;
    title = source.closest(".media")?.querySelector(".media-foot span")?.textContent || source.alt || "";
    download = img.src;
  } else {
    const panel = h("div", { class: "viewer-panel" }), card = source.closest(".card");
    title = card?.querySelector("h4")?.textContent || "";
    upscale = 1;
    V.el.hidden = false;  // laid out, so the panel can be measured
    if (kind === "diagram") {
      const drawing = source.querySelector("svg").cloneNode(true);
      panel.append(drawing);
      V.url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(drawing)], { type: "image/svg+xml" }));
      download = V.url; filename = "diagram.svg";
    } else {  // a chart: the whole block again, its canvases drawn afresh at the larger size
      const clone = card.cloneNode(true);
      clone.querySelectorAll(".expand").forEach(b => b.remove());
      panel.style.width = `${Math.min(1100, V.stage.getBoundingClientRect().width - 120)}px`;
      panel.append(clone);
    }
    V.item.append(panel);
    if (kind === "chart") {
      const originals = [...card.querySelectorAll(".plot")], copies = [...panel.querySelectorAll(".plot")];
      V.charts = copies.map((p, i) => {
        const make = originals[i]?._make;
        p.querySelector("canvas")?.remove();
        if (!make) return null;
        const canvas = h("canvas");
        p.append(canvas);
        const config = make();
        return new Chart(canvas, { ...config, options: { ...config.options, animation: false } });
      }).filter(Boolean);
      if (V.charts[0]) { download = V.charts[0].toBase64Image(); filename = "chart.png"; }
    }
    V.w = panel.offsetWidth; V.h = panel.offsetHeight;
  }
  V.el.querySelector(".viewer-title").textContent = title;
  V.el.querySelector(".v-full").hidden = kind !== "video";
  const link = V.el.querySelector(".v-download");
  link.hidden = !download;
  if (download) { link.href = download; link.download = filename; }

  V.el.hidden = false;
  const r = V.stage.getBoundingClientRect();
  V.fit = Math.min((r.width - 80) / V.w, (r.height - 140) / V.h, upscale);
  // start exactly over what was clicked, then grow into place (from the middle when nothing was)
  const at = source?.getBoundingClientRect() ?? { left: r.width / 2, top: r.height / 2, width: 1 };
  V.s = at.width / V.w; V.x = at.left - r.left; V.y = at.top - r.top;
  place(false);
  void V.item.offsetWidth;
  requestAnimationFrame(() => { V.el.classList.add("show"); fitView(true); });
  document.addEventListener("keydown", keys, true);
  V.el.querySelector(".v-close").focus({ preventScroll: true });
}

export function closeViewer(now = false) {
  parts();
  if (V.el.hidden) return;
  document.removeEventListener("keydown", keys, true);
  const done = () => {
    V.el.hidden = true;
    V.el.classList.remove("show");
    if (V.el.classList.contains("theater")) theater(false);
    V.charts.forEach(c => c.destroy()); V.charts = [];
    if (V.url) { URL.revokeObjectURL(V.url); V.url = null; }
    V.item.replaceChildren();
  };
  if (now === true) return done();
  const r = V.stage.getBoundingClientRect(), at = V.from?.getBoundingClientRect();  // back into its place, if it's on screen
  if (at && at.bottom > 0 && at.top < innerHeight && at.width) {
    V.s = at.width / V.w; V.x = at.left - r.left; V.y = at.top - r.top;
    place(true);
  }
  V.el.classList.remove("show");
  setTimeout(done, 300);
}

const playing = () => V.el.classList.contains("is-video");
let fullIcon = null;  // the button's own icon (index.html), back when the player is
function theater(on) {
  V.el.classList.toggle("theater", on);
  const full = V.el.querySelector(".v-full");
  fullIcon ??= full.innerHTML;
  full.innerHTML = on ? svg("fit") : fullIcon;
  full.setAttribute("aria-label", on ? "Back to the player's size" : "Full screen");
  full.title = on ? "Back to the player's size" : "Full screen";
}
function keys(e) {
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeViewer(); }
  else if (playing()) return;  // a video's own keys (space, arrows) stay its own
  else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.4); }
  else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomBy(1 / 1.4); }
  else if (e.key === "0") { e.preventDefault(); fitView(true); }
}

// The expand button a chart opens from (its own hover readouts stay usable)
export function addExpand(visual) {
  visual.classList.add("has-expand");
  const b = h("button", { class: "expand", type: "button", title: "Open larger", "aria-label": "Open larger", html: svg("expand") });
  b.addEventListener("click", e => { e.stopPropagation(); openViewer(visual, "chart"); });
  visual.append(b);
}

export function wireViewer() {
  parts();
  V.el.querySelector(".v-close").addEventListener("click", () => closeViewer());
  V.el.querySelector(".v-out").addEventListener("click", () => zoomBy(1 / 1.4));
  V.el.querySelector(".v-in").addEventListener("click", () => zoomBy(1.4));
  V.el.querySelector(".v-fit").addEventListener("click", () => fitView(true));
  // Full screen - or, where the browser refuses it (an embedded browser: "Permissions check failed", and the button did
  // nothing), the player fills the viewer instead; the button puts it back
  const full = V.el.querySelector(".v-full");
  full.addEventListener("click", () => {
    if (V.el.classList.contains("theater")) return theater(false);
    // (a request the browser neither grants nor refuses - measured with a real click - must not leave it dead)
    V.item.querySelector("iframe, video")?.requestFullscreen?.()?.catch(() => {});
    setTimeout(() => { if (!document.fullscreenElement) theater(true); }, 300);
  });
  V.stage.addEventListener("wheel", e => {
    if (playing()) return;
    e.preventDefault();
    const r = V.stage.getBoundingClientRect();
    zoomAt(V.s * Math.exp(-e.deltaY * .0016), e.clientX - r.left, e.clientY - r.top, false);
  }, { passive: false });
  const down = new Map();
  let start = null, moved = false, pinch = null;
  V.stage.addEventListener("pointerdown", e => {
    if (playing() && V.item.contains(e.target)) return;  // the player's own controls get the click
    down.set(e.pointerId, { x: e.clientX, y: e.clientY });
    V.stage.setPointerCapture(e.pointerId);
    if (down.size === 1) { start = { x: e.clientX, y: e.clientY, ox: V.x, oy: V.y, onItem: V.item.contains(e.target) }; moved = false; }
    if (down.size === 2) { const [a, b] = [...down.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s: V.s }; }
  });
  V.stage.addEventListener("pointermove", e => {
    if (!down.has(e.pointerId)) return;
    down.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = V.stage.getBoundingClientRect();
    if (pinch && down.size === 2) {
      const [a, b] = [...down.values()];
      zoomAt(pinch.s * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, false);
      moved = true;
      return;
    }
    if (!start) return;
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) moved = true;
    if (moved && V.s > V.fit * 1.02 && !playing()) {
      V.stage.classList.add("dragging");
      V.x = start.ox + e.clientX - start.x; V.y = start.oy + e.clientY - start.y;
      place(false);
    }
  });
  const up = e => {
    down.delete(e.pointerId);
    if (down.size < 2) pinch = null;
    if (down.size) return;
    V.stage.classList.remove("dragging");
    if (start && !moved) {
      const r = V.stage.getBoundingClientRect();
      if (!start.onItem) closeViewer();
      else if (playing()) {}  // a click on a video is the player's
      else if (V.s > V.fit * 1.02) fitView(true);
      else zoomAt(V.fit * 2.2, e.clientX - r.left, e.clientY - r.top, true);
    }
    start = null;
  };
  V.stage.addEventListener("pointerup", up);
  V.stage.addEventListener("pointercancel", up);
  addEventListener("resize", () => {
    if (V.el.hidden) return;
    const r = V.stage.getBoundingClientRect();
    V.fit = Math.min((r.width - 80) / V.w, (r.height - 140) / V.h, V.fit);
    fitView(false);
  });
  // pictures and diagrams open with a click
  document.addEventListener("click", e => {
    const img = e.target.closest("img.zoomable");
    if (img) { e.preventDefault(); openViewer(img, "image"); return; }
    const diagram = e.target.closest(".diagram.zoomable");
    if (diagram?.querySelector("svg")) openViewer(diagram, "diagram");
  });
}
