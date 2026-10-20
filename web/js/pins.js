// Files pinned to the chat (docs/history/REBUILD_PLAN.md 2.2): what in them matches each message goes with every turn - the
// server builds that snapshot for the turn and keeps none of it. Shown as chips in the chat header; pinned and unpinned
// there, in the file panel and in the @ menu - and by Zen (files pin), whose change arrives as a "pins" artifact.

import { send, state } from "./api.js";
import { $, fileTile, h, svg, toast } from "./util.js";

export const hooks = { open: () => {}, ensureChat: async () => state.chat, changed: () => {} };
export const pinned = () => state.chat?.pins || [];
export const isPinned = id => pinned().includes(id);

export async function setPins(ids) {
  const chat = await hooks.ensureChat();  // a new chat is made for its first pin
  if (!chat) return;
  try {
    const r = await send("PUT", `/chats/${chat.id}/pins`, { pins: ids });
    chat.pins = r.pins.map(p => p.id);
  } catch (e) { toast(e.message, true); }
  renderPins();
  hooks.changed();
}

export const togglePin = item => setPins(isPinned(item.id) ? pinned().filter(i => i !== item.id) : [...pinned(), item.id]);

export function pinsFromZen(pins) {  // Zen pinned or unpinned a file
  if (!state.chat) return;
  state.chat.pins = (pins || []).map(p => p.id);
  renderPins();
  hooks.changed();
}

// Shown twice, one at a time: beside the title on a wide screen, and on a phone in a row of their own above the message
// box - the bar had no room there (measured: a chip cut in half, its unpin out of reach, the title down to "Game ...")
export function renderPins() {
  const items = pinned().map(id => state.items.find(i => i.id === id)).filter(Boolean);
  const chip = i => h("span", { class: "pin-chip" },
    h("button", { class: "pin-open", type: "button", title: `${i.name} is pinned: what in it matches each message goes with it`,
                  onclick: () => hooks.open(i) }, fileTile(i.name), h("span", { class: "pin-name" }, i.name)),
    h("button", { class: "pin-x", type: "button", title: "Unpin", "aria-label": `Unpin ${i.name}`, html: svg("cross"),
                  onclick: () => togglePin(i) }));
  for (const box of [$("#pins"), $("#pinsDock")]) {
    box.replaceChildren(...items.map(chip));
    box.hidden = !items.length;
  }
}
