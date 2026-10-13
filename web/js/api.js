// The server: its JSON calls, uploads with progress, and the server-sent events a turn streams.

export const API = "/assistant";

// What the page holds: the project (a workspace on the server), its chats and items, the open chat,
// the running turn and a question waiting for the user.
export const state = {
  status: null, workspaces: [], ws: null, chats: [], chat: null, items: [],
  busy: false, controller: null, pending: null, stick: true, opening: 0,
};
export const KEYS = { ws: "zen.project", chat: "zen.chat", theme: "zen.theme", fileWidth: "zen.file-width" };

async function detail(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map(d => d.msg).join("; ");
  } catch {}
  return `${response.status} ${response.statusText}`;
}
export { detail };

export async function api(path, options = {}) {
  let r;
  try { r = await fetch(API + path, options); } catch { throw new Error("Can't reach the server. Is it running?"); }
  if (!r.ok) throw new Error(await detail(r));
  return r.status === 204 ? null : r.json();
}
export const send = (method, path, body) =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });

export function uploadFile(path, file, onProgress) {  // XHR, for upload progress
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + path);
    xhr.responseType = "json";
    xhr.upload.onprogress = e => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status < 300) return resolve(xhr.response);
      const d = xhr.response?.detail;
      reject(new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join("; ") : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Can't reach the server."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export async function* serverEvents(response) {
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      const data = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield [event, JSON.parse(data.join("\n"))];
    }
  }
}

export const itemUrl = id => `${API}/workspaces/${state.ws.id}/items/${encodeURIComponent(id)}/file`;
export const pngUrl = id => `${API}/workspaces/${state.ws.id}/items/${encodeURIComponent(id)}/png`;  // a chart file's picture
export const rowsUrl = (id, limit = 500) => `/workspaces/${state.ws.id}/items/${encodeURIComponent(id)}/rows?limit=${limit}`;
export const iconUrl = (page, favicon) => `${API}/web/icon?page=${encodeURIComponent(page)}${favicon ? `&favicon=${encodeURIComponent(favicon)}` : ""}`;
export const imageUrl = url => `${API}/web/image?url=${encodeURIComponent(url)}`;
