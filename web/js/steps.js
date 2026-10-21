// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// The work behind a reply, in the reply's own order: each run of tool steps is a short list where it happened,
// between the model's words (nothing moves later), with a helper's own steps inside its step and what they found
// (search results, pages read, tables, code). The reply's head, on top, says how the turn is and for how long, and
// shows or hides the work; when the turn is done each step folds to its line, and a click opens it again.

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
  const label = (pair || [name.replace(/_/g, " "), name.replace(/_/g, " ")])[done ? 1 : 0];
  // the file it is about, by name ("Changed game_2048.html", not "Changed a file" four times over)
  const file = typeof input?.name === "string" && input.name.trim().split("/").pop();
  return file && /\ba file\b/.test(label) ? label.replace(/\ba file\b/, file) : label;
}

export const LOADER = '<svg class="loader" viewBox="0 0 24 24" aria-hidden="true"><use href="#phase"/></svg>';
// A call being written (the model writes a whole file into it - a long one takes minutes; the timer shows it goes on)
const size = n => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
// (a call without a file - a search, a task for a helper - is the model still thinking, as far as the user can tell)
const composing = d => d.file ? `Writing ${d.file}${d.chars > 400 ? ` · ${size(d.chars)} characters` : ""}…` : "Thinking…";
// rounded first: 239.6 s is "4m 00s", not "3m 60s"
const clock = t => { const s = Math.round(t); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; };
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
  if (a.type === "file") return h("div", {}, title(`${a.action === "updated" ? "Changed" : "Wrote"} ${a.name}`));
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

// flow: the reply's blocks in order (app.js) - a step goes into the list of steps just above, or starts a new list
// after the model's words or a card.
// The reply's head, on top, is the turn's one status: while it works, the moon, how the turn is (thinking, writing,
// working, waiting for you) and a timer; when it is done, "Worked for 2m 26s · 6 steps". A click on it shows or hides
// the work - the steps and the words written between them - in place; what the turn made (files, pictures, pages) and
// its answer always stay. The work is open while it runs and folds when the answer is done; a stopped or failed turn
// stays open. Each running step's own row says what it does (its dot pulses, its words shimmer) - the head never repeats
// it (measured: "Writing 2048.html…" twice, one under the other) - and only while the model thinks, with nothing else
// moving, a small moon at the end says it is still at work. When the turn ends, every step still running is settled
// (stopped, or didn't finish): nothing stays live after Stop.
export function activityEl(flow) {
  const say = h("span", { class: "say shimmer" }, "Thinking…");
  const time = h("span", { class: "timer" }, "0:00");
  const head = h("button", { class: "act-head live", type: "button", "aria-expanded": "true" },
    h("span", { class: "spin", html: LOADER }), say, time, h("span", { class: "chev-wrap", html: svg("chev", "i chev") }));
  const tail = h("div", { class: "tail", hidden: true, html: LOADER, "aria-hidden": "true" });
  // the timer stands still while the turn waits for the user's answer: only Zen's working time counts
  const t0 = performance.now();
  let waited = 0, waitStart = null, words = false, live = true, touched = false;
  const unpause = () => { if (waitStart != null) { waited += performance.now() - waitStart; waitStart = null; } };
  const tick = setInterval(() => { time.textContent = timer(((waitStart ?? performance.now()) - t0 - waited) / 1000); }, 1000);
  const tell = text => {  // the head's words slide to their next state
    if (say.textContent === text) return;
    say.textContent = text;
    say.classList.remove("swap"); void say.offsetWidth; say.classList.add("swap");
  };
  const steps = new Map(), lists = [];
  let list = null, helperActions = 0, composed = null;  // composed: the call main was writing when the turn ended without steps
  const place = el => {
    if (!list || flow.lastElementChild !== list) { list = h("div", { class: "steps enter" }); lists.push(list); flow.append(list); }
    list.append(el);
  };
  const row = (step, cls, label) => { step.el.className = `step ${cls}`; step.label.textContent = label; };
  const busy = () => [...steps.values()].some(s => !s.done);
  // the head is there from the turn's start to its end - never hidden and shown again, which pushed the words below it
  // (measured: gone at the first word, back at the first step); the moon at the end only while nothing else moves
  // the moon at the end: only while the model thinks with nothing else moving, and only where the head can't be seen -
  // never a second moon right under the first (measured: "Thinking… 0:04" with a lone moon below it)
  let headSeen = true;
  const sync = () => {
    head.classList.toggle("has-steps", steps.size > 0);
    tail.hidden = !live || words || busy() || waitStart != null || headSeen || !flow.children.length;
  };
  if ("IntersectionObserver" in window) {
    const seen = new IntersectionObserver(([e]) => {
      headSeen = e.isIntersecting;
      if (!live) seen.disconnect();
      sync();
    }, { rootMargin: "-56px 0px 0px 0px" });  // under the glass top bar counts as out of view
    seen.observe(head);
  }
  const fold = open => {
    const reply = flow.parentElement;
    reply?.classList.toggle("folded", !open);
    head.setAttribute("aria-expanded", String(open));
  };
  head.addEventListener("click", () => {
    if (!steps.size) return;
    touched = true;
    fold(flow.parentElement?.classList.contains("folded"));
  });
  // a done step opens and folds on its line (its task, tags, a helper's steps); a failed step's reason stays in view
  flow.addEventListener("click", e => {
    const title = e.target.closest(".steps.done .step > .step-title");
    if (title && !e.target.closest("a")) title.parentElement.classList.toggle("open");
  });
  const summary = (seconds, stopped) => {
    const all = [...steps.values()], failed = all.filter(s => s.el.classList.contains("err")).length;
    // a plain answer "thought"; a turn that took steps "worked"
    const did = all.length ? "Worked" : "Thought";
    const took = seconds ? (stopped ? `Stopped after ${clock(seconds)}` : `${did} for ${clock(seconds)}`) : stopped ? "Stopped" : did;
    return { said: [took, all.length ? plural(all.length, "step") : null].filter(Boolean).join(" · "),
      more: [took, all.length ? plural(all.length, "step") : null, helperActions ? plural(helperActions, "helper action") : null,
        failed ? `${failed} didn't work` : null].filter(Boolean).join(" · ") };
  };
  // the turn's end: what still runs is settled (how: "stopped" or "didn't finish"), and the head says how it went
  const settle = (how, out) => {
    clearInterval(tick);
    live = false;
    for (const step of steps.values()) {
      if (step.sub && !step.sub.done) {
        step.sub.done = true;
        step.sub.el.className = "sub off";
        step.sub.title.textContent = `${stepLabel(step.sub.name, false)} · ${how}`;
      }
      if (!step.done) { step.done = true; row(step, "off", `${stepLabel(step.name, false, step.input)} · ${how}`); }
    }
    for (const l of lists) {
      l.classList.add("done");
      for (const s of l.querySelectorAll(":scope > .step")) {
        if ([...s.children].some(c => !c.matches(".step-title, .step-note.err") && !(c.matches(".subs") && !c.children.length))) s.classList.add("more");
      }
    }
    head.classList.remove("live", "waiting");
    say.classList.remove("shimmer");
    say.textContent = out.said;
    head.title = out.more;
    sync();
  };

  const view = {
    head, tail, steps,
    running(text) { unpause(); head.classList.remove("waiting"); say.classList.add("shimmer"); tell(text); sync(); },
    checking() { view.running("Checking the answer…"); },  // a hidden check sent the model back: say so, not a stuck reply
    // the model's words: flowing (the caret shows the work) or stopped (thinking again)
    words(on) { words = on; if (on) view.running("Writing…"); else if (live && waitStart == null && !busy()) view.running("Thinking…"); else sync(); },
    writing() { view.words(true); },
    // main writing a call: the file it writes, and how far it got (a long one takes minutes - no row shows it yet)
    composing(d) { composed = d; words = false; view.running(composing(d)); },
    start(d) {
      if (d.name === "ask_user") return null;  // its question is asked in the message box
      const step = { id: d.id, name: d.name, delegate: isDelegate(d.name), done: false, sub: null, tables: [], code: null, input: d.input };
      step.label = h("span", { class: "step-label" }, `${stepLabel(d.name, false, d.input)}…`);
      step.title = h("div", { class: "step-title" }, step.label, step.delegate ? h("span", { class: "helper" }, helperName(d.name)) : null);
      step.subs = h("div", { class: "subs" });
      step.el = h("div", { class: "step run enter" }, step.title, inputDetail(d.name, d.input), step.subs);
      steps.set(d.id, step);
      place(step.el);
      words = false;
      view.running("Working…");
      return step;
    },
    end(d) {
      const step = steps.get(d.id) || view.start({ id: d.id, name: d.name, input: {} });
      if (!step) return;
      if (d.status === "stopped") {  // the user stopped it (a saved chat): still, not failed
        step.done = true;
        row(step, "off", `${stepLabel(d.name, false, step.input)} · stopped`);
        if (step.sub && !step.sub.done) { step.sub.done = true; step.sub.el.className = "sub off"; }
        sync();
        return;
      }
      const failed = !["ok", "partial"].includes(d.status);
      step.done = true;
      row(step, failed ? "err" : "ok pop", failed ? `${stepLabel(d.name, false, step.input)} · didn't work` : stepLabel(d.name, true, step.input));
      if (failed) step.el.insertBefore(h("div", { class: "step-note err" }, errorText(d.summary)), step.subs);
      else if (step.delegate) {
        try {
          const r = JSON.parse(d.summary);
          if (r.warnings?.length) step.el.insertBefore(h("div", { class: "step-note" }, `Flagged: ${r.warnings.join("; ")}`), step.subs);
        } catch {}  // a long result is cut for the UI; nothing more to show
      }
      if (step.sub && !step.sub.done) view.subEnd(step, { status: "ok" });
      if (!busy()) view.running("Thinking…");  // the model reads the results next
      sync();
    },
    wait(id) {
      const step = steps.get(id);
      if (step) row(step, "run", `${stepLabel(step.name, false)} · waiting for your answer`);
      head.classList.add("waiting");
      say.classList.remove("shimmer");
      waitStart ??= performance.now();
      tell("Waiting for your answer, in the message box");
      sync();
    },
    answered(id) {
      const step = steps.get(id);
      if (step) { step.done = true; row(step, "ok pop", `${stepLabel(step.name, false)} · you answered`); }
      sync();
    },
    resumed(id) {  // a helper got its answer and goes on
      const step = steps.get(id);
      if (step) { step.done = false; row(step, "run", `${stepLabel(step.name, false)}…`); }
      view.running(step ? "Working…" : "Thinking…");
    },
    worker(step, e) {
      if (e.event === "tool") {
        const title = h("div", { class: "step-title" }, `${stepLabel(e.name, false, e.input)}…`);
        step.sub = { name: e.name, done: false, evidence: false, title, code: e.input?.code,
          el: h("div", { class: "sub run enter" }, title, inputDetail(e.name, e.input)) };
        step.subs.append(step.sub.el);
        helperActions++;
        view.running("Working…");
      } else if (e.event === "writing") {  // a helper writing a call (a script, a long answer): its row shows it
        view.running(composing(e));
      } else if (e.event === "result" && step.sub) {
        view.subEnd(step, e);
      } else if (e.event === "check") {
        const ok = !e.problems?.length;
        step.subs.append(h("div", { class: `sub enter ${ok ? "ok" : "err"}` },
          h("div", { class: "step-title" }, ok ? "Checked the answer" : e.retry ? "Checked the answer · fixing it" : "Checked the answer · flagged"),
          ok ? null : h("div", { class: "step-note err" }, e.problems.join("; "))));
        view.running(ok ? "Working…" : "Fixing the answer…");
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
      view.running("Working…");
    },
    evidence(step, a) {
      const into = step.sub && !step.sub.evidence ? step.sub : null;
      const node = evidenceEl(a, !into, into?.code);
      if (!node) return;
      if (into) { into.evidence = true; into.el.append(node); }
      else { step.subs.append(h("div", { class: "sub ok enter" }, node)); helperActions += 1; }
    },
    // The turn is done: the head says how it went and the work folds away (unless the user opened or closed it, or
    // nothing came after the steps to read). seconds: a live turn's length, or its trace's for a saved chat.
    // Returns {said, more, shown}: shown - the head says it, so the line under the reply doesn't again.
    finish(seconds, answered = true) {
      const out = summary(seconds, false);
      settle("didn't finish", out);
      head.hidden = !seconds && !steps.size;  // an old saved answer with no trace: nothing to say
      if (steps.size && answered && !touched) fold(false);
      return { ...out, shown: true };
    },
    // stopped, or failed: it stays open; with no step, a call it was writing says how far it got
    stop(seconds, how = "stopped") {
      const worked = seconds ?? ((waitStart ?? performance.now()) - t0 - waited) / 1000;
      const out = summary(worked, how === "stopped");
      if (!steps.size && composed?.file) out.more = `${out.more} · ${composing(composed).replace(/…$/, "")} didn't finish`;
      settle(how, out);
      return { ...out, shown: true };
    },
  };
  sync();
  return view;
}
