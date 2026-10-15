// The work behind a reply: while it runs, the moon loader, what it is doing and a timer; then
// "Worked for 34s · 3 steps", which opens to each step, a helper's own steps inside it, and what they
// found (search results, pages read, tables, code).

import { api } from "./api.js";
import { favicon, tableEl } from "./cards.js";
import { cap, errorText, h, host, plural, svg } from "./util.js";

// How each tool and helper is named while it works ("Reading a page" / "Read a page"): defined with the
// tools on the server and loaded once at start (GET /assistant/tools), so the page keeps no list of its own.
const TOOLS = {};
export async function loadTools() { try { Object.assign(TOOLS, await api("/tools")); } catch {} }
export const isDelegate = name => Boolean(TOOLS[name]?.helper);
export const helperName = name => TOOLS[name]?.helper;
export function stepLabel(name, done, input) {
  const t = TOOLS[name], pair = t?.modes?.[input?.mode] || (t && [t.doing, t.done]);
  return (pair || [name.replace(/_/g, " "), name.replace(/_/g, " ")])[done ? 1 : 0];
}

export const LOADER = '<svg class="loader" viewBox="0 0 24 24" aria-hidden="true"><use href="#phase"/></svg>';
const clock = s => s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
const timer = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function code(text, lang) {
  const block = h("code", { class: lang ? `language-${lang}` : null }, text);
  if (lang && window.hljs) try { hljs.highlightElement(block); } catch {}
  return h("pre", { class: "code" }, block);
}
const fold = (title, body) => h("details", { class: "fold" }, h("summary", {}, title), body);

// What the step was asked: a helper's task as a quote, an expression or query as code, the rest as tags
function inputDetail(name, input) {
  if (!input || typeof input !== "object") return null;
  if (input.task || (name === "ask_knowledge" && input.question)) {
    return h("div", {}, h("div", { class: "quote" }, input.task || input.question),
      input.data ? h("div", { class: "tags" }, h("span", { class: "tag" }, `table: ${input.data}`)) : null);
  }
  if (input.expression) return code(input.expression);
  if (input.sql) return code(input.sql, "sql");
  if (name === "run_code" && input.code) return fold(`Code · ${plural(input.code.split("\n").length, "line")}`, code(input.code, "python"));
  if (name === "fix_code" && input.new_code != null) return fold(`Lines ${input.from_line}-${input.to_line}`, code(input.new_code, "python"));
  if (name === "read_webpage" && input.url) {
    return h("div", { class: "tags" }, h("span", { class: "tag", title: input.url }, host(input.url)), input.find ? h("span", { class: "tag" }, `looking for: ${input.find}`) : null);
  }
  if (name === "generate_image" && input.prompt) return h("div", { class: "quote" }, input.prompt);
  const tags = Object.entries(input).filter(([k, v]) => v != null && v !== "" && v !== false && !["title", "content", "mermaid", "options", "allow_text"].includes(k))
    .map(([k, v]) => {
      const text = Array.isArray(v) ? plural(v.length, k.replace(/s$/, "")) : typeof v === "object" ? JSON.stringify(v) : String(v);
      return h("span", { class: "tag", title: text }, `${k.replace(/_/g, " ")}: ${text.length > 60 ? text.slice(0, 60) + "…" : text}`);
    });
  return tags.length ? h("div", { class: "tags" }, tags) : null;
}

// What a helper's tool showed, inside its step (shown: the code its step already shows, not repeated)
function evidenceEl(a, titled, shown) {
  const title = text => titled ? h("div", { class: "step-title" }, text) : null;
  const link = (url, text, fav) => h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer", title: text },
    favicon(url, fav), h("span", {}, text || url), h("span", { class: "host" }, host(url)));
  if (a.type === "links") {
    return h("div", {}, title(`Searched the web: “${a.query}”`), h("div", { class: "links" }, (a.items || []).slice(0, 5).map(r => link(r.url, r.title, r.favicon))));
  }
  if (a.type === "webpage") return h("div", {}, title("Read a page"), h("div", { class: "links" }, link(a.url, a.title, a.favicon)));
  if (a.type === "table") {
    const rows = a.rows || [];
    const t = tableEl(a.columns || [], rows, 6);
    t.classList.add("mini-wrap");
    return h("div", {}, title(a.title), titled ? null : h("div", { class: "step-note" }, a.title), t,
      rows.length > 6 ? h("div", { class: "step-note" }, `${rows.length - 6} more rows`) : null);
  }
  if (a.type === "code") {
    return h("div", {}, title("Ran Python"), a.code === shown ? null : fold(`Code · ${plural(a.code.split("\n").length, "line")}`, code(a.code, "python")),
      a.output ? code(a.output.length > 1500 ? a.output.slice(0, 1500) + "\n…" : a.output) : null,
      a.errors ? h("div", { class: "step-note err" }, a.errors.trim().split("\n").at(-1)) : null);
  }
  if (a.type === "sources") {
    const names = [...new Set((a.items || []).map(i => i.source || i.title || host(i.url || "")))];
    return h("div", {}, title(a.query ? `Searched for “${a.query}”` : "Found sources"),
      h("div", { class: "tags" }, names.slice(0, 6).map(n => h("span", { class: "tag" }, n))));
  }
  return null;
}

// How a reply ran, for a developer: the server's trace of each part of the turn (a question to the user ends a
// part) - main's model steps with their tokens, every tool call, each helper's steps, tools and checks, the
// hidden checks that fired and the error, if any. Facts measured by the server, not the model's account.
export function traceEl(traces) {
  const secs = v => v == null ? "" : `${Number(v).toFixed(1)} s`;
  const usage = u => [u?.output_tokens, u?.prompt_tokens, u?.cached_tokens, u?.tokens_per_second ? Math.round(u.tokens_per_second) : null];
  const USAGE = ["tokens out", "prompt", "cached", "tok/s"];
  const table = (columns, rows) => { const t = tableEl(columns, rows); t.classList.add("mini-wrap"); return t; };
  const title = text => h("div", { class: "trace-title" }, text);
  return h("div", { class: "trace" }, traces.map((t, i) => {
    const steps = t.steps || [], tools = t.tools || [], helpers = t.helpers || [];
    const head = [`${cap(t.ended || "ran")} after ${secs(t.seconds)}`, plural(steps.length, "model step"), plural(tools.length, "tool call"),
      helpers.length ? plural(helpers.length, "helper task") : null].filter(Boolean).join(" · ");
    return h("div", { class: "trace-part" },
      h("div", { class: "trace-head" }, traces.length > 1 ? `Part ${i + 1}: ${head}` : head),
      t.error ? h("div", { class: "step-note err" }, t.error) : null,
      steps.length ? [title("Main model"), table(["#", "at", "took", "stop", "calls", ...USAGE],
        steps.map((s, j) => [j + 1, secs(s.at), secs(s.seconds), s.stop, (s.calls || []).join(", ") || "—", ...usage(s.usage)]))] : null,
      tools.length ? [title("Tool calls"), table(["at", "tool", "status", "took"],
        tools.map(c => [secs(c.at), c.name, c.refused ? `refused: ${String(c.refused).slice(0, 90)}` : c.status, secs(c.seconds)]))] : null,
      helpers.map(w => [title(`Helper: ${w.worker}`),
        w.steps?.length ? table(["at", "stop", "calls", ...USAGE], w.steps.map(s => [secs(s.at), s.stop, (s.calls || []).join(", ") || "—", ...usage(s.usage)])) : null,
        w.tools?.length ? table(["at", "tool", "status", "characters back"], w.tools.map(c => [secs(c.at), c.name, c.status, c.chars])) : null,
        (w.checks || []).map(c => h("div", { class: `step-note${c.problems?.length ? " err" : ""}` },
          `${secs(c.at)} · result checked: ${c.problems?.length ? c.problems.join("; ") + (c.retry ? " (sent back to fix)" : "") : "passed"}`))]),
      t.checks?.length ? [title("Hidden checks that fired"), h("ul", { class: "notes" }, t.checks.map(c => h("li", {}, c)))] : null);
  }));
}

// onStatus(text): each new status, so the reply can also show it at its end (app.js, the live line)
export function activityEl(onStatus = () => {}) {
  const say = h("span", { class: "say shimmer" }, "Thinking…");
  const time = h("span", { class: "timer" }, "0:00");
  const head = h("button", { class: "act-head", type: "button", "aria-expanded": "false" }, h("span", { class: "spin", html: LOADER }), say, time,
    h("span", { class: "chev-wrap", html: svg("chev", "i chev") }));
  const body = h("div", { class: "act-body" });
  const el = h("div", { class: "activity enter running" }, head, body);
  // the timer stands still while the turn waits for the user's answer: only Zen's working time counts
  const t0 = performance.now();
  let waited = 0, waitStart = null;
  const unpause = () => { if (waitStart != null) { waited += performance.now() - waitStart; waitStart = null; } };
  let touched = false, tick = setInterval(() => { time.textContent = timer(((waitStart ?? performance.now()) - t0 - waited) / 1000); }, 1000);
  const setOpen = open => { el.classList.toggle("open", open); head.setAttribute("aria-expanded", open); };
  head.addEventListener("click", () => { touched = true; setOpen(!el.classList.contains("open")); });
  const tell = text => {  // the status text slides to its next state
    onStatus(text);
    if (say.textContent === text) return;
    say.textContent = text;
    say.classList.remove("swap"); void say.offsetWidth; say.classList.add("swap");
  };
  const steps = new Map();
  let helperActions = 0;

  const view = {
    el, steps,
    running(text) { unpause(); el.hidden = false; el.classList.remove("done", "waiting"); el.classList.add("running"); say.classList.add("shimmer"); tell(text); },
    checking() { view.running("Checking the answer…"); },  // a hidden check sent the model back: say so, not a stuck reply
    writing() { if (!steps.size) el.hidden = true; else { view.fold(); view.running("Writing…"); } },
    start(d) {
      if (d.name === "ask_user") return null;  // its question is asked in the message box
      const step = { id: d.id, name: d.name, delegate: isDelegate(d.name), done: false, sub: null, tables: [], code: null, input: d.input };
      step.title = h("div", { class: "step-title" }, `${stepLabel(d.name, false, d.input)}…`, step.delegate ? h("span", { class: "helper" }, helperName(d.name)) : null);
      step.subs = h("div", { class: "subs" });
      step.el = h("div", { class: "step run enter" }, step.title, inputDetail(d.name, d.input), step.subs);
      steps.set(d.id, step);
      body.append(step.el);
      view.running(`${stepLabel(d.name, false, d.input)}…`);
      if (step.delegate && !touched) setOpen(true);  // watch a helper work; it folds when the answer starts
      return step;
    },
    end(d) {
      const step = steps.get(d.id) || view.start({ id: d.id, name: d.name, input: {} });
      if (!step) return;
      const failed = !["ok", "partial"].includes(d.status);
      step.done = true;
      step.el.className = `step ${failed ? "err" : "ok pop"}`;
      step.title.firstChild.textContent = failed ? `${stepLabel(d.name, false, step.input)} · didn't work` : stepLabel(d.name, true, step.input);
      if (failed) step.el.insertBefore(h("div", { class: "step-note err" }, errorText(d.summary)), step.subs);
      else if (step.delegate) {
        try {
          const r = JSON.parse(d.summary);
          if (r.warnings?.length) step.el.insertBefore(h("div", { class: "step-note" }, `Flagged: ${r.warnings.join("; ")}`), step.subs);
        } catch {}  // a long result is cut for the UI; nothing more to show
      }
      if (step.sub && !step.sub.done) view.subEnd(step, { status: "ok" });
    },
    wait(id) {
      const step = steps.get(id);
      if (step) { step.el.className = "step run"; step.title.firstChild.textContent = `${stepLabel(step.name, false)} · waiting for your answer`; }
      el.hidden = false;
      el.classList.remove("done");
      el.classList.add("waiting", "running");
      say.classList.remove("shimmer");
      waitStart ??= performance.now();
      tell("Waiting for your answer, in the message box");
    },
    answered(id) {
      const step = steps.get(id);
      if (step) { step.done = true; step.el.className = "step ok pop"; step.title.firstChild.textContent = `${stepLabel(step.name, false)} · you answered`; }
    },
    resumed(id) {  // a helper got its answer and goes on
      const step = steps.get(id);
      if (step) { step.el.className = "step run"; step.title.firstChild.textContent = `${stepLabel(step.name, false)}…`; }
      unpause();
      el.classList.remove("waiting");
      say.classList.add("shimmer");
      tell(step ? `${stepLabel(step.name, false)}…` : "Thinking…");
    },
    worker(step, e) {
      if (e.event === "tool") {
        const title = h("div", { class: "step-title" }, `${stepLabel(e.name, false)}…`);
        step.sub = { name: e.name, done: false, evidence: false, title, code: e.input?.code,
          el: h("div", { class: "sub run enter" }, title, inputDetail(e.name, e.input)) };
        step.subs.append(step.sub.el);
        helperActions++;
        view.running(`${stepLabel(e.name, false)}…`);
      } else if (e.event === "result" && step.sub) {
        view.subEnd(step, e);
      } else if (e.event === "check") {
        const ok = !e.problems?.length;
        step.subs.append(h("div", { class: `sub enter ${ok ? "ok" : "err"}` },
          h("div", { class: "step-title" }, ok ? "Checked the answer" : e.retry ? "Checked the answer · fixing it" : "Checked the answer · flagged"),
          ok ? null : h("div", { class: "step-note err" }, e.problems.join("; "))));
        view.running(ok ? "Writing the answer…" : "Fixing the answer…");
      }
    },
    subEnd(step, e) {
      const sub = step.sub, failed = !["ok", "partial"].includes(e.status);
      sub.done = true;
      sub.el.className = `sub ${failed ? "err" : "ok"}`;
      // refused before running (wrong arguments), or ran and failed (a script that raised)
      sub.title.textContent = !failed ? stepLabel(sub.name, true)
        : e.status === "invalid_arguments" ? `${stepLabel(sub.name, false)} · didn't run` : `${stepLabel(sub.name, true)} · failed`;
      if (failed && e.summary) sub.el.append(h("div", { class: "step-note err" }, errorText(e.summary)));
      view.running(`${stepLabel(step.name, false)}…`);
    },
    evidence(step, a) {
      const into = step.sub && !step.sub.evidence ? step.sub : null;
      const node = evidenceEl(a, !into, into?.code);
      if (!node) return;
      if (into) { into.evidence = true; into.el.append(node); }
      else { step.subs.append(h("div", { class: "sub ok enter" }, node)); helperActions += 1; }
    },
    fold() { if (!touched) setOpen(false); },
    finish(seconds) {  // seconds: a live turn's length (a saved chat has none)
      clearInterval(tick);
      const all = [...steps.values()];
      if (!all.length) { el.remove(); return; }
      el.hidden = false;
      el.classList.remove("waiting", "running");
      el.classList.add("done");
      say.classList.remove("shimmer");
      const failed = all.filter(s => s.el.classList.contains("err")).length;
      tell([seconds ? `Worked for ${clock(seconds)}` : null, plural(all.length, "step"), helperActions ? plural(helperActions, "helper action") : null,
            failed ? `${failed} didn't work` : null].filter(Boolean).join(" · "));
      view.fold();
    },
    stop(seconds) { steps.size ? view.finish(seconds) : (clearInterval(tick), el.remove()); },
  };
  return view;
}
