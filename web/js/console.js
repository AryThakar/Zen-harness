// The console: a Python file of the workspace running with the user at the keyboard (REBUILD_PLAN 2.12). What it
// prints streams in as it is written; a line typed here goes to its input(). Started from the file - its card in the
// chat or the file panel - it keeps to one block: a head (what runs, for how long, Stop or Run again), the session,
// and the line to type in. What happened is kept with the file on the server, so Zen can read it when asked.

import { send, state } from "./api.js";
import { h, svg } from "./util.js";

const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

// item: the Python file ({id, name}); onEnd(files): what it made or changed, once it ends
export function consoleEl(item, { onEnd = () => {}, openFile = null } = {}) {
  const base = `/workspaces/${state.ws.id}`;
  const said = h("span", { class: "con-state" }, "Starting…");
  const time = h("span", { class: "con-time" });
  // a card's button: its word, or on a phone its icon (as cards.js's)
  const button = (icon, label, title) => h("button", { class: "btn-s", type: "button", title, "aria-label": label },
    h("span", { class: "btn-ic", html: svg(icon) }), h("span", { class: "btn-lbl" }, label));
  const stop = button("stop", "Stop", "Stop the program");
  const again = button("play", "Run again", "Run it again from the start");
  again.hidden = true;
  const out = h("pre", { class: "con-out", role: "log", "aria-live": "polite", "aria-label": `${item.name}: what it prints` });
  const field = h("input", { class: "con-field", type: "text", spellcheck: "false", autocomplete: "off", disabled: true,
    placeholder: "Type here, then Enter", "aria-label": `Type a line for ${item.name}` });
  const form = h("form", { class: "con-in" }, h("span", { class: "con-prompt", "aria-hidden": "true" }, "›"), field);
  const made = h("div", { class: "tags con-files", hidden: true });
  const el = h("div", { class: "console running" },
    h("div", { class: "con-head" }, h("span", { class: "con-dot", "aria-hidden": "true" }), h("b", {}, item.name), said, time,
      h("span", { class: "con-acts" }, stop, again)),
    out, form, made);

  let session = null, seen = 0, events = null, tick = null, t0 = 0, ended = false;
  const write = (text, cls) => {
    const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 24;  // keep to the bottom unless scrolled up
    const last = out.lastChild;
    if (last && last.className === (cls || "") && !cls) last.textContent += text;  // a print keeps to its own run of text
    else out.append(h("span", { class: cls || "" }, text));
    if (stick) out.scrollTop = out.scrollHeight;
    if (document.activeElement === field) form.scrollIntoView({ block: "nearest" });  // typing: its line stays in view
  };
  const follow = () => {
    events = new EventSource(`/assistant${base}/console/${session}?start=${seen}`);
    const on = (type, fn) => events.addEventListener(type, e => { seen++; fn(JSON.parse(e.data)); });
    on("out", d => write(d.text));
    on("err", d => write(d.text, "con-err"));
    on("in", d => write(`› ${d.text}\n`, "con-typed"));
    on("end", finish);
    events.onerror = () => {  // the stream dropped: follow again from where it was, unless it has ended
      events.close();
      if (!ended) setTimeout(follow, 1000);
    };
  };
  function finish(d) {
    ended = true;
    events?.close();
    clearInterval(tick);
    el.classList.remove("running");
    el.classList.toggle("failed", !d.stopped && d.exit_code !== 0);
    said.textContent = d.stopped ? `Stopped${d.stopped === "stopped" ? "" : ` - ${d.stopped.replace(/^stopped /, "")}`}`
      : d.exit_code === 0 ? "Finished" : `Ended with an error (exit ${d.exit_code})`;
    time.textContent = `${d.seconds} s`;
    field.disabled = true;
    field.placeholder = "It has ended";
    stop.hidden = true;
    again.hidden = false;
    const files = (d.files || []).filter(f => f.id);
    made.hidden = !files.length;
    made.replaceChildren(...files.map(f => openFile
      ? h("button", { class: "tag", type: "button", onclick: () => openFile(f) }, `${f.path}${f.updated ? " (updated)" : ""}`)
      : h("span", { class: "tag" }, f.path)));
    if (!out.textContent) write("It printed nothing.", "con-note");
    onEnd(files);
  }
  async function start() {
    ended = false; seen = 0;
    out.replaceChildren(); made.hidden = true;
    el.className = "console running";
    said.textContent = "Running";
    stop.hidden = false; again.hidden = true;
    field.disabled = false; field.placeholder = "Type here, then Enter"; field.value = "";
    try { session = (await send("POST", `${base}/items/${encodeURIComponent(item.id)}/console`)).session; }
    catch (e) { finish({ exit_code: 1, seconds: 0, stopped: null }); said.textContent = "Couldn't start"; write(e.message, "con-err"); return; }
    t0 = performance.now();
    time.textContent = "0:00";
    tick = setInterval(() => { time.textContent = clock((performance.now() - t0) / 1000); }, 1000);
    follow();
    field.focus({ preventScroll: true });
  }
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!session || ended) return;
    const line = field.value;
    field.value = "";
    try { await send("POST", `${base}/console/${session}/input`, { line }); }
    catch (err) { write(`${err.message}\n`, "con-err"); }
  });
  stop.addEventListener("click", () => session && send("POST", `${base}/console/${session}/stop`).catch(() => {}));
  again.addEventListener("click", start);
  start();
  return el;
}
