// Charts: the analyst's chart artifacts, drawn in the answer itself (no boxes). One series takes the data
// colour; groups take the series colours. Each chart can open larger in the viewer.

import { addExpand } from "./viewer.js";
import { css, formatter, h, nf2, plural, xLabels, cap, escapeHtml } from "./util.js";

export function rgba(color, alpha) {
  const hex = color.replace("#", "");
  const n = parseInt(hex.length === 3 ? hex.replace(/./g, c => c + c) : hex, 16);
  return `rgba(${n >> 16 & 255}, ${n >> 8 & 255}, ${n & 255}, ${alpha})`;
}
const SERIES = () => [1, 2, 3, 4, 5, 6].map(i => css(`--s${i}`));
const DATA = () => css("--data");

export function card(title, sub, legend) {
  const el = h("div", { class: "card enter" },
    h("div", { class: "card-head" }, h("div", {}, h("h4", {}, cap(title || "Chart")), sub ? h("div", { class: "sub-t" }, sub) : null), legend));
  return { el, body: el };
}
function legendFor(names, colors) {
  return h("div", { class: "legend", html: names.map((n, i) =>
    `<span><i style="background:${colors[i % colors.length]}"></i>${escapeHtml(String(n))}</span>`).join("") });
}

function chartOptions(fmt, { xNumeric = false, xTitle, yTitle, stacked = false } = {}) {
  const soft = css("--soft"), line = css("--line");
  const title = text => text ? { display: true, text, color: soft, font: { size: 11 } } : undefined;
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 600, easing: "easeOutQuart" },
    interaction: { mode: xNumeric ? "nearest" : "index", intersect: false },
    layout: { padding: { top: 6, right: 4 } },
    scales: {
      x: { ...(xNumeric ? { type: "linear" } : {}), stacked, grid: { display: xNumeric, color: line }, border: { color: line }, title: title(xTitle),
           ticks: { color: soft, maxRotation: 0, autoSkipPadding: 18, font: { size: 11 }, ...(xNumeric ? { callback: v => nf2.format(v) } : {}) } },
      y: { stacked, grid: { color: line, drawTicks: false }, border: { display: false }, title: title(yTitle),
           ticks: { color: soft, padding: 8, maxTicksLimit: 6, font: { size: 11 }, callback: v => fmt.axis(v) } },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--muted"),
        borderColor: css("--line"), borderWidth: 1, padding: 10, cornerRadius: 10, displayColors: false, boxPadding: 4,
        titleFont: { weight: "600", size: 12.5 }, bodyFont: { size: 12.5 },
        callbacks: { label: c => c.raw == null ? null : `${c.dataset.label}: ${fmt.full(typeof c.raw === "object" ? c.raw.y : c.raw)}` },
        filter: c => c.raw != null && !c.dataset.hideInTooltip,
      },
    },
  };
}

// Every drawn chart, so a theme change can draw it again in the new colours
const drawn = [];
export function plot(host, make) {
  if (!window.Chart) { host.replaceWith(h("p", { class: "muted" }, "Charts need an internet connection to load the first time.")); return; }
  host._make = make;  // the viewer draws it again, larger, from this
  const canvas = h("canvas", { role: "img", "aria-label": make().label || "Chart" });
  host.append(canvas);
  requestAnimationFrame(() => {
    Chart.defaults.font.family = css("--font");
    drawn.push({ host, chart: new Chart(canvas, make()) });
  });
  addExpand(host);
}
export function redrawCharts() {
  for (let i = drawn.length - 1; i >= 0; i--) {
    const d = drawn[i];
    d.chart.destroy();
    if (!d.host.isConnected) { drawn.splice(i, 1); continue; }
    const canvas = d.host.querySelector("canvas");
    d.chart = new Chart(canvas, { ...d.host._make(), options: { ...d.host._make().options, animation: false } });
  }
}
export function forgetCharts() {  // the thread was cleared
  for (const d of drawn.splice(0)) d.chart.destroy();
}
const plotHost = (cls = "") => h("div", { class: `plot ${cls}`.trim() });

export function chartCard(a) {
  if (a.chart === "forecast") return a.series ? forecastGroupsCard(a) : forecastCard(a);
  if (a.chart === "box") return boxCard(a);
  if (a.chart === "heatmap") return heatmapCard(a);
  if (a.chart === "pie") return pieCard(a);
  if (a.chart === "unusual") return unusualCard(a);
  if (a.series) return seriesCard(a);
  if (a.chart === "scatter" && (a.groups || a.trend)) return scatterCard(a);
  const fmt = formatter(a.unit), x = a.x || [], y = a.y || [];
  if (a.chart === "bar" && x.length <= 24) return barsCard(a.title, x.map((label, i) => ({ label: String(label), value: y[i] })), fmt, a.y_label);
  const { el, body } = card(a.title, a.chart === "histogram" ? `${plural(y.reduce((s, v) => s + v, 0), "row")} in ${plural(y.length, "bin")}` : plural(x.length, "point"));
  const host = plotHost();
  body.append(host);
  plot(host, () => {
    const c = DATA();
    if (a.chart === "scatter") {
      return { type: "scatter", label: a.title, data: { datasets: [{ label: a.y_label || "y", data: x.map((v, i) => ({ x: v, y: y[i] })),
               pointRadius: x.length > 300 ? 2 : 3.5, backgroundColor: rgba(c, .6) }] },
               options: chartOptions(fmt, { xNumeric: true, xTitle: a.x_label, yTitle: a.y_label }) };
    }
    if (a.chart === "histogram" || a.chart === "bar") {
      const labels = a.chart === "histogram" ? x.map(v => nf2.format(v)) : xLabels(x);
      return { type: "bar", label: a.title, data: { labels, datasets: [{ label: a.y_label || "rows", data: y, backgroundColor: rgba(c, .8), borderRadius: 4,
               categoryPercentage: a.chart === "histogram" ? 1 : .8, barPercentage: a.chart === "histogram" ? .94 : .9 }] },
               options: chartOptions(a.chart === "histogram" ? formatter() : fmt, { xTitle: a.x_label, yTitle: a.y_label }) };
    }
    return { type: "line", label: a.title, data: { labels: xLabels(x), datasets: [{ label: a.y_label || a.title, data: y, borderColor: c, borderWidth: 2.2,
             tension: .3, spanGaps: false, pointRadius: y.length > 40 ? 0 : 2.5, pointHoverRadius: 5, pointBackgroundColor: c, fill: true,
             backgroundColor: ({ chart }) => { const { ctx, chartArea } = chart; if (!chartArea) return "transparent";
               const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom); g.addColorStop(0, rgba(c, .26)); g.addColorStop(1, rgba(c, 0)); return g; } }] },
             options: chartOptions(fmt, { yTitle: a.y_label }) };
  });
  return el;
}

function seriesCard(a) {
  const fmt = formatter(a.unit), series = a.series || [], names = series.map(s => s.name), line = a.chart === "line";
  const { el, body } = card(a.title, plural(names.length, "group"), legendFor(names, SERIES()));
  const host = plotHost();
  body.append(host);
  plot(host, () => {
    const colors = SERIES();
    const datasets = series.map((s, i) => line
      ? { label: s.name, data: s.y, borderColor: colors[i % 6], backgroundColor: colors[i % 6], borderWidth: 2, tension: .3, pointRadius: s.y.length > 40 ? 0 : 2.5 }
      : { label: s.name, data: s.y, backgroundColor: rgba(colors[i % 6], .85), borderRadius: 3 });
    const options = chartOptions(fmt, { xTitle: line ? undefined : a.x_label, yTitle: a.y_label, stacked: !!a.stacked });
    options.plugins.tooltip.displayColors = true;
    return { type: line ? "line" : "bar", label: a.title, data: { labels: xLabels(a.x || []), datasets }, options };
  });
  return el;
}

function scatterCard(a) {
  const fmt = formatter(a.unit), x = a.x || [], y = a.y || [], names = a.groups || [a.y_label || "points"];
  const tone = (n, g) => n === "other" ? css("--soft") : a.groups ? SERIES()[g % 6] : DATA();
  const { el, body } = card(a.title, plural(x.length, "point"), a.groups ? legendFor(names, names.map(tone)) : null);
  const host = plotHost();
  body.append(host);
  plot(host, () => {
    const datasets = names.map((n, g) => ({ label: n, data: x.map((v, i) => ({ x: v, y: y[i] })).filter((_, i) => !a.group || a.group[i] === g),
      pointRadius: x.length > 300 ? 2 : 3.5, backgroundColor: rgba(tone(n, g), .7) }));
    if (a.trend) datasets.push({ type: "line", label: "trend", data: a.trend.map(([tx, ty]) => ({ x: tx, y: ty })), borderColor: css("--soft"),
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, hideInTooltip: true });
    return { type: "scatter", label: a.title, data: { datasets }, options: chartOptions(fmt, { xNumeric: true, xTitle: a.x_label, yTitle: a.y_label }) };
  });
  return el;
}

function boxCard(a) {
  const fmt = formatter(a.unit), boxes = a.boxes || [], names = a.x || [];
  const all = boxes.flatMap(b => [b.min, b.max, ...(b.outliers || [])]).filter(v => v != null);
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const pos = v => `${(100 * (v - lo) / span).toFixed(2)}%`;
  const { el, body } = card(a.title, `Median and middle half${boxes.length > 1 ? `, ${plural(boxes.length, "group")}` : ""}`);
  const rows = boxes.map((b, i) => h("div", { class: "boxrow" }, h("span", { class: "nm", title: names[i] }, names[i]),
    h("div", { class: "btrack", title: `min ${fmt.full(b.min)} · Q1 ${fmt.full(b.q1)} · median ${fmt.full(b.median)} · Q3 ${fmt.full(b.q3)} · max ${fmt.full(b.max)}` },
      h("div", { class: "wh", style: `left:${pos(b.min)};width:calc(${pos(b.max)} - ${pos(b.min)})` }),
      h("div", { class: "iqr", style: `left:${pos(b.q1)};width:calc(${pos(b.q3)} - ${pos(b.q1)})` }),
      h("div", { class: "med", style: `left:${pos(b.median)}` }),
      ...(b.outliers || []).map(o => h("div", { class: "out", style: `left:${pos(o)}` }))),
    h("span", { class: "val" }, fmt.full(b.median))));
  const visual = h("div", { class: "boxes" }, rows);
  body.append(visual, h("div", { class: "bscale" }, h("span", {}, fmt.axis(lo)), h("span", {}, fmt.axis(hi))));
  addExpand(visual);
  return el;
}

function heatmapCard(a) {
  const fmt = formatter(a.unit), values = a.values || [], cols = a.x || [], rows = a.rows || [];
  const flat = values.flat().filter(v => v != null), max = Math.max(...flat.map(Math.abs), 1), numbers = cols.length <= 8;
  const { el, body } = card(a.title, `${a.row_label || ""} × ${a.x_label || ""}`,
    h("div", { class: "legend" }, h("span", {}, "fewer"), h("i", { style: "width:60px;height:8px;border-radius:4px;background:linear-gradient(90deg,color-mix(in srgb,var(--data) 10%,transparent),var(--data))" }), h("span", {}, "more")));
  const head = h("tr", {}, h("th"), ...cols.map(c => h("th", {}, String(c))));
  const lines = rows.map((r, i) => h("tr", {}, h("th", { class: "row" }, String(r)), ...cols.map((c, j) => {
    const v = values[i]?.[j];
    return h("td", { title: `${r} · ${c}: ${fmt.full(v)}`, style: v == null ? "" : `background:color-mix(in srgb, var(--data) ${Math.round(8 + 82 * Math.abs(v) / max)}%, transparent)` },
      numbers && v != null ? fmt.axis(v) : "");
  })));
  const visual = h("div", { class: "heat" }, h("table", {}, h("thead", {}, head), h("tbody", {}, lines)));
  body.append(visual);
  addExpand(visual);
  return el;
}

function unusualCard(a) {
  const fmt = formatter(a.unit), x = a.x || [], flagged = new Set(a.flagged || []);
  const legend = h("div", { class: "legend", html: `<span><i style="background:var(--data)"></i>Actual</span><span><i class="dash"></i>Expected</span>` +
    `<span><i class="dot" style="background:var(--red)"></i>Unusual</span>` });
  const { el, body } = card(a.title, plural(flagged.size, "unusual " + (a.x_label || "period")), legend);
  const host = plotHost();
  body.append(host);
  plot(host, () => ({ type: "line", label: a.title, data: { labels: xLabels(x), datasets: [
    { label: "Unusual", data: x.map((d, i) => flagged.has(d) ? a.y[i] : null), showLine: false, pointRadius: 5, pointBackgroundColor: css("--red"), borderColor: css("--red") },
    { label: "Actual", data: a.y, borderColor: DATA(), borderWidth: 1.8, pointRadius: 0, tension: .2 },
    { label: "Expected", data: a.expected, borderColor: css("--soft"), borderWidth: 1.4, borderDash: [5, 4], pointRadius: 0, tension: .2 },
  ] }, options: chartOptions(fmt, { yTitle: a.y_label }) }));
  return el;
}

function pieCard(a) {
  const fmt = formatter(a.unit), x = a.x || [], y = a.y || [], total = y.reduce((s, v) => s + (v || 0), 0) || 1;
  const shades = () => x.map((n, i) => n === "other" ? css("--soft") : SERIES()[i % 6]);
  const { el, body } = card(a.title, a.y_label, legendFor(x.map((n, i) => `${n} ${Math.round(100 * y[i] / total)}%`), shades()));
  const host = plotHost("pie");
  body.append(host);
  plot(host, () => ({ type: "doughnut", label: a.title,
    data: { labels: x.map(String), datasets: [{ data: y, backgroundColor: shades(), borderColor: css("--bg"), borderWidth: 3 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "62%", animation: { duration: 600 }, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: c => `${c.label}: ${fmt.full(c.raw)} (${Math.round(100 * c.raw / total)}%)` } } } } }));
  return el;
}

function barsCard(title, items, fmt, sub) {
  const max = Math.max(...items.map(i => Math.abs(i.value) || 0), 1);
  const { el, body } = card(title, sub);
  const visual = h("div", { class: "hbars" }, items.map(i => {
    const fill = h("i");
    requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = `${Math.max(1.5, 100 * Math.abs(i.value || 0) / max)}%`; }));
    return h("div", { class: "hbar" }, h("span", { class: "nm", title: i.label }, i.label), h("div", { class: "track" }, fill), h("span", { class: "val" }, fmt.full(i.value)));
  }));
  body.append(visual);
  addExpand(visual);
  return el;
}

function forecastGroupsCard(a) {
  const fmt = formatter(a.unit), series = a.series || [];
  const labels = [...new Set(series.flatMap(s => [...(s.x || []), ...(s.forecast_x || [])]))].sort();
  const at = new Map(labels.map((l, i) => [l, i]));
  const { el, body } = card(a.title || "Forecast", `${plural(series[0]?.forecast_x?.length || 0, a.x_label || "period")} ahead · dashed = forecast`,
    legendFor(series.map(s => s.name), SERIES()));
  const host = plotHost();
  body.append(host);
  plot(host, () => {
    const colors = SERIES();
    const datasets = series.flatMap((s, i) => {
      const actual = Array(labels.length).fill(null), predicted = Array(labels.length).fill(null);
      (s.x || []).forEach((d, j) => { actual[at.get(d)] = s.y[j]; });
      if (s.x?.length) predicted[at.get(s.x.at(-1))] = s.y.at(-1);
      (s.forecast_x || []).forEach((d, j) => { predicted[at.get(d)] = s.forecast[j]; });
      return [{ label: s.name, data: actual, borderColor: colors[i % 6], borderWidth: 1.8, pointRadius: 0, tension: .3 },
              { label: `${s.name} (forecast)`, data: predicted, borderColor: colors[i % 6], borderWidth: 2.2, borderDash: [6, 4], pointRadius: 0, tension: .3, spanGaps: false }];
    });
    const options = chartOptions(fmt, { yTitle: a.y_label });
    options.plugins.tooltip.displayColors = true;
    return { type: "line", label: a.title, data: { labels: xLabels(labels), datasets }, options };
  });
  return el;
}

function forecastCard(a) {
  const fmt = formatter(a.unit), hx = a.x || [], hy = a.y || [], fx = a.forecast_x || [], fy = a.forecast || [];
  const n = hx.length, pad = k => Array(Math.max(0, k)).fill(null);
  const legend = h("div", { class: "legend", html:
    `<span><i style="background:var(--soft)"></i>Actual</span><span><i class="dash" style="color:var(--data)"></i>Forecast</span>` +
    `<span><i style="background:color-mix(in srgb,var(--data) 22%,transparent)"></i>80% range</span>` });
  const { el, body } = card(a.title || "Forecast", `${plural(fx.length, a.x_label || "period")} ahead`, legend);
  const host = plotHost();
  body.append(host);
  plot(host, () => {
    const c = DATA();
    return { type: "line", label: a.title, data: { labels: xLabels([...hx, ...fx].map(String)), datasets: [
      { label: "Low", data: [...pad(n), ...(a.low || [])], borderWidth: 0, pointRadius: 0, fill: false, hideInTooltip: true },
      { label: "Range", data: [...pad(n), ...(a.high || [])], borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: rgba(c, .16), hideInTooltip: true },
      { label: "Actual", data: [...hy, ...pad(fx.length)], borderColor: css("--soft"), borderWidth: 1.6, pointRadius: 0, tension: .3 },
      { label: "Forecast", data: [...pad(n - 1), ...(n ? [hy[n - 1]] : []), ...fy], borderColor: c, borderWidth: 2.2, borderDash: [6, 4], pointRadius: 0, tension: .3 },
    ] }, options: chartOptions(fmt, { yTitle: a.y_label }) };
  });
  return el;
}
