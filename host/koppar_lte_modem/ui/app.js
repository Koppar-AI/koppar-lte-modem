// Koppar LTE modem UI. Plain JS, no dependencies: talks to the daemon's
// /api on 127.0.0.1 and listens to its event stream. All text from the
// network (names, messages) goes into the page as text nodes, never HTML.
"use strict";
(() => {

// ---- token -------------------------------------------------------------------

function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
const params = new URLSearchParams(location.search);
let TOKEN = params.get("token") || store("koppar-token") || "";
const EMBEDDED = params.get("embedded") === "1";
if (params.get("token")) {
  store("koppar-token", TOKEN);
  history.replaceState(null, "", location.pathname + (EMBEDDED ? "?embedded=1" : "") + location.hash);
}

// ---- api ---------------------------------------------------------------------

class ApiError extends Error {}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Koppar-Token": TOKEN } };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new ApiError("the daemon is not answering"); }
  if (res.status === 401) { gate(); throw new ApiError("not authorised"); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) throw new ApiError((data && data.error) || `HTTP ${res.status}`);
  return data;
}
const GET = p => api(p);
const POST = (p, body = {}) => api(p, { method: "POST", body });
const DEL = p => api(p, { method: "DELETE" });

// ---- DOM ---------------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const ICONS = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  msg: "M4 5h16v11H9l-5 4z",
  phone: "M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  term: "M4 17l6-5-6-5M12 19h8",
  signal: "M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 4v16",
  bolt: "M13 2L3 14h9l-1 8 10-12h-9z",
  chip: "M7 7h10v10H7zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3",
  hangup: "M3 15c5-4 13-4 18 0l-2 3-4-1v-3c-2-.6-4-.6-6 0v3l-4 1z",
  mic: "M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3M5 11a7 7 0 0 0 14 0M12 18v3",
  rec: "M12 12m-6 0a6 6 0 1 0 12 0a6 6 0 1 0-12 0",
  grid: "M5 5h2v2H5zM11 5h2v2h-2zM17 5h2v2h-2zM5 11h2v2H5zM11 11h2v2h-2zM17 11h2v2h-2zM5 17h2v2H5zM11 17h2v2h-2zM17 17h2v2h-2z",
  min: "M6 12h12",
  del: "M21 5H9l-6 7 6 7h12a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1zM12 9l6 6M18 9l-6 6",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M4 21a8 8 0 0 1 16 0",
};
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name] || "");
  s.append(p);
  return s;
}
function toast(text, err) {
  const box = document.getElementById("toast");
  const t = h("div", { class: err ? "err" : "", text });
  box.append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3500);
}
async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(typeof ok === "function" ? ok(r) : ok); return r; }
  catch (e) { toast(e.message, true); return undefined; }
}

// ---- formatting --------------------------------------------------------------

const pad = n => String(n).padStart(2, "0");
function fmtTime(iso, withDate) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const today = new Date().toDateString() === d.toDateString();
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (today && !withDate) return t;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " " + t;
}
function fmtDur(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${pad(s % 60)}`; }
function nameOf(num) { return (num && S.contacts[num]) || num || "Unknown"; }
function avatar(num, cls = "") {
  const name = num && S.contacts[num];
  const c = name ? [...name][0] : /^[+\d]/.test(num || "") || !num ? null : [...num][0];
  return h("div", { class: "avatar " + cls, "aria-hidden": "true" }, c ? c.toUpperCase() : icon("user"));
}
// Mirrors the host's sms_parts(): which characters go as 7-bit, and the limits.
const GSM_SAFE = new Set("@$\n\r !\"#%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz");
function smsCount(text) {
  const ucs2 = [...text].some(c => !GSM_SAFE.has(c));
  const size = ucs2 ? text.length : [...text].length;     // JS length is UTF-16 units
  const limit = ucs2 ? 70 : 160;
  if (size <= limit) return { ucs2, size, parts: text ? 1 : 0, limit };
  let n = 2;
  for (;;) {
    const room = limit - ` (${n}/${n})`.length;
    const parts = Math.ceil(size / room);
    if (parts <= n) return { ucs2, size, parts, limit };
    n = parts;
  }
}
function describe(ev) {
  const d = ev.data || ev;
  switch (ev.event) {
    case "sms": return `Text from ${nameOf(d.number)}: ${d.text ?? d.error ?? ""}`;
    case "sms_sent": return `Sent to ${nameOf(d.number)}${d.by && d.by !== "ui" ? ` (${d.by})` : ""}: ${d.text}`;
    case "sms_failed": return `Text to ${nameOf(d.number)} failed: ${d.error}`;
    case "incoming_call": return `Incoming call from ${nameOf(d.number)}`;
    case "ring": return `Ringing${d.number ? " – " + nameOf(d.number) : ""}`;
    case "call_state": return `Call ${d.direction === "in" ? "from" : "to"} ${nameOf(d.number)}: ${d.state}`;
    case "call_ended": return `Call with ${nameOf(d.number)} ended`;
    case "call_audio": return `Call audio ${d.active ? "flowing" : "stopped"}`;
    case "network": return `Network: ${d.operator || "none"} ${d.tech || ""} ${d.band ? "· " + d.band : ""} (${d.state})`;
    case "ready": return `Modem ready, SIM ${d.sim}`;
    case "module_on": return "Module switched on";
    case "module_off": return "Module switched off";
    case "modem_disconnected": return "Modem left USB";
    case "ussd": return `USSD (${d.status}): ${d.text}`;
    case "ussd_sent": return `USSD sent: ${d.code}`;
    case "board_error": return `Board: ${d.message}`;
    default: return ev.event;
  }
}

// ---- state -------------------------------------------------------------------

const S = {
  view: "overview", state: null, live: false, contacts: {}, threads: [], sel: null, convo: [],
  events: [], evKind: "", evQuery: "", evSel: null, atlog: [], showPoll: false, history: [], histPos: -1,
  signal: [], hours: 6, metric: "rsrp", cell: null, scan: null, ussd: [], device: null, settings: null,
  scheduled: [], calls: [], dial: "", callStart: null, callMin: false, keypad: false, starred: false,
  query: "", draft: "", scheduleOn: false,
};
const VIEWS = [
  ["overview", "Overview", "home"], ["messages", "Messages", "msg"], ["calls", "Calls", "phone"],
  ["events", "Events", "list"], ["console", "Console", "term"], ["network", "Network", "signal"],
  ["automations", "Automations", "bolt"], ["device", "Device", "chip"],
];

// ---- top bar and nav ---------------------------------------------------------

function renderNav() {
  const nav = document.getElementById("nav");
  const unread = S.threads.reduce((a, t) => a + (t.unread || 0), 0);
  nav.replaceChildren(...VIEWS.map(([id, label, ic]) => h("button", {
    "aria-current": S.view === id ? "page" : null, onclick: () => go(id), title: label,
  }, icon(ic), h("span", { class: "label", text: label }),
     id === "messages" && unread ? h("span", { class: "badge", text: unread }) : null)));
  document.getElementById("side-foot").replaceChildren(
    h("div", {}, h("span", { class: "dot " + (S.live ? "good" : "bad") }), " ", S.live ? "Live" : "Reconnecting…"));
}
function bars(n) { return h("span", { class: "bars", "aria-label": `${n} of 4 bars` }, [1, 2, 3, 4].map(i => h("i", { class: i <= n ? "on" : "" }))); }
function renderTop() {
  const st = S.state || {};
  const net = st.network || {}, sig = st.signal || {};
  const chips = [
    h("span", { class: "chip" }, h("span", { class: "dot " + (st.modem ? "good" : "bad") }), st.modem ? "Modem" : "No modem"),
    h("span", { class: "chip" }, h("span", { class: "dot " + (st.sim === "READY" ? "good" : st.sim ? "warn" : "") }), "SIM " + (st.sim || "–")),
  ];
  if (net.operator || net.state) chips.push(h("span", { class: "chip" }, h("span", { class: "dot " + (net.registered ? "good" : "warn") }),
    [net.operator || net.state, net.tech, net.band].filter(Boolean).join(" · ")));
  if (sig.rssi != null || sig.bars != null) chips.push(h("span", { class: "chip", title: "signal" }, bars(sig.bars || 0),
    sig.rsrp != null ? `${sig.rsrp} dBm` : sig.rssi != null ? `${sig.rssi} dBm` : ""));
  const call = currentCall();
  if (call && S.callMin) chips.push(h("button", { class: "btn sm good", onclick: () => { S.callMin = false; renderCall(); } },
    icon("phone"), `${callLabel(call)} – return`));
  document.getElementById("top").replaceChildren(...chips, h("span", { class: "grow" }),
    h("button", { class: "btn sm", onclick: () => go("messages", "__new") }, "New message"),
    h("button", { class: "btn sm", onclick: () => go("calls") }, "Dial"));
}

// ---- navigation --------------------------------------------------------------

function go(view, arg) {
  S.view = view;
  if (view === "messages" && arg) S.sel = arg;
  if (fromHash()[0] !== view) history.replaceState(null, "", location.pathname + location.search + "#" + view);
  load(view).then(renderAll);
  renderAll();
  document.getElementById("view").focus({ preventScroll: true });
}
async function load(view) {
  try {
    if (view === "overview") { [S.signal] = await Promise.all([GET("/api/signal?hours=6")]); if (!S.events.length) S.events = await GET("/api/events?limit=200"); }
    if (view === "messages") { await loadThreads(); if (S.sel && S.sel !== "__new") S.convo = await GET("/api/messages?number=" + encodeURIComponent(S.sel)); if (S.starred) S.convo = await GET("/api/messages?starred=1"); }
    if (view === "calls") S.calls = await GET("/api/calls");
    if (view === "events") S.events = await GET("/api/events?limit=300" + (S.evKind ? "&kind=" + S.evKind : ""));
    if (view === "console") S.atlog = await GET("/api/atlog" + (S.showPoll ? "?poll=1" : ""));
    if (view === "network") { S.signal = await GET("/api/signal?hours=" + S.hours); if (!S.ussd.length) S.ussd = (await GET("/api/events?kind=ussd&limit=20")); }
    if (view === "automations") { S.settings = await GET("/api/settings"); S.scheduled = await GET("/api/scheduled"); }
    if (view === "device") S.device = await GET("/api/device");
  } catch (e) { if (!(e instanceof ApiError && e.message === "not authorised")) toast(e.message, true); }
}
async function loadThreads() { S.threads = await GET("/api/threads"); }
async function loadContacts() { S.contacts = Object.fromEntries((await GET("/api/contacts")).map(c => [c.number, c.name])); }
async function refreshState() {
  try { S.state = await GET("/api/state"); } catch (e) { return; }
  const call = currentCall();
  if (call && call.state === "active" && !S.callStart) S.callStart = Date.now();
  if (!call) { S.callStart = null; S.callMin = false; S.keypad = false; }
  renderTop(); renderCall();
  if (S.view === "overview") renderView();
}

// ---- views -------------------------------------------------------------------

function renderAll() { renderNav(); renderTop(); renderView(); renderCall(); }
function renderView() {
  const v = document.getElementById("view");
  const fn = { overview, messages, calls, events, console: consoleView, network, automations, device }[S.view];
  v.classList.toggle("flush", S.view === "messages");
  const keep = v.querySelector("[data-keep-scroll]");
  const top = keep ? keep.scrollTop : 0;
  v.replaceChildren(fn());
  const again = v.querySelector("[data-keep-scroll]");
  if (again && S.view !== "messages") again.scrollTop = top;
}

function overview() {
  const st = S.state || {}, net = st.network || {}, sig = st.signal || {}, info = st.info || {};
  const recent = S.events.slice(0, 10);
  return h("div", {},
    h("h1", { text: "Overview" }),
    h("p", { class: "lede", text: "Your phone line on this computer: network, texts, calls and everything the modem reports." }),
    h("div", { class: "grid" },
      h("section", { class: "card" }, h("h2", { text: "Signal" }),
        h("div", { class: "sig-big" }, bars(sig.bars || 0),
          h("div", {}, h("div", { class: "big", text: sig.rsrp != null ? `${sig.rsrp} dBm` : sig.rssi != null ? `${sig.rssi} dBm` : "–" }),
            h("div", { class: "muted small", text: sig.rsrp != null ? "RSRP (LTE reference signal)" : "RSSI" }))),
        h("dl", { class: "kv", style: { marginTop: "12px" } },
          h("dt", { text: "RSRQ" }), h("dd", { text: sig.rsrq != null ? `${sig.rsrq} dB` : "–" }),
          h("dt", { text: "SINR" }), h("dd", { text: sig.sinr != null ? `${sig.sinr} dB` : "–" }),
          h("dt", { text: "RSSI" }), h("dd", { text: sig.rssi != null ? `${sig.rssi} dBm` : "–" })),
        spark(S.signal.map(r => r.rsrp ?? r.rssi))),
      h("section", { class: "card" }, h("h2", { text: "Network" }),
        h("dl", { class: "kv" },
          h("dt", { text: "Operator" }), h("dd", { text: net.operator || "–" }),
          h("dt", { text: "Registration" }), h("dd", { text: net.state || "–" }),
          h("dt", { text: "Radio" }), h("dd", { text: [net.tech, net.band].filter(Boolean).join(" · ") || "–" }),
          h("dt", { text: "Modem" }), h("dd", { text: st.modem ? "connected" : "not connected" }))),
      h("section", { class: "card" }, h("h2", { text: "SIM and module" }),
        h("dl", { class: "kv" },
          h("dt", { text: "SIM" }), h("dd", { text: st.sim || "–" }),
          h("dt", { text: "Model" }), h("dd", { text: info.model || "–" }),
          h("dt", { text: "Firmware" }), h("dd", { class: "mono", text: info.revision || "–" }),
          h("dt", { text: "IMEI" }), h("dd", { class: "mono", text: info.imei || "–" }))),
      h("section", { class: "card" }, h("h2", { text: "Do something" }),
        h("div", { class: "stack" },
          h("button", { class: "btn primary", onclick: () => go("messages", "__new") }, "Send a text"),
          h("button", { class: "btn", onclick: () => go("calls") }, "Make a call"),
          h("button", { class: "btn", onclick: () => go("network") }, "USSD code, cell details, scan"),
          h("button", { class: "btn", onclick: () => go("automations") }, "Auto-reply, forwarding, webhooks")))),
    h("section", { class: "card", style: { marginTop: "14px" } },
      h("div", { class: "spread" }, h("h2", { text: "Recent activity" }), h("button", { class: "btn sm ghost", onclick: () => go("events") }, "All events")),
      recent.length ? h("ul", { class: "feed" }, recent.map(ev => h("li", {},
        h("span", { class: "when", text: fmtTime(ev.time) }), h("span", { class: "grow", text: describe(ev) }))))
        : h("div", { class: "empty", text: "Nothing yet. Events show up here as they happen." })));
}

function spark(vals) {
  const pts = vals.filter(v => v != null);
  if (pts.length < 2) return h("div", { class: "faint small", style: { marginTop: "10px" }, text: "The last hours show here as samples come in (every 15 s)." });
  return chart(vals.map((v, i) => ({ x: i, y: v })), { height: 60, axes: false });
}
function chart(points, { height = 220, axes = true, unit = "" } = {}) {
  const NS = "http://www.w3.org/2000/svg";
  const W = axes ? Math.max(320, (document.getElementById("view").clientWidth || 900) - 82) : 640, H = height, L = axes ? 40 : 2, R = 6, T = 8, B = axes ? 22 : 4;
  const ok = points.filter(p => p.y != null);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "chart");
  if (!axes) { svg.setAttribute("preserveAspectRatio", "none"); svg.style.height = height + "px"; }
  if (ok.length < 2) return svg;
  let lo = Math.min(...ok.map(p => p.y)), hi = Math.max(...ok.map(p => p.y));
  if (hi - lo < 4) { lo -= 2; hi += 2; }
  const x0 = Math.min(...ok.map(p => p.x)), x1 = Math.max(...ok.map(p => p.x));
  const X = x => L + (W - L - R) * (x - x0) / Math.max(1, x1 - x0);
  const Y = y => T + (H - T - B) * (1 - (y - lo) / (hi - lo));
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); svg.append(e); return e; };
  if (axes) {
    for (let i = 0; i <= 4; i++) {
      const y = lo + (hi - lo) * i / 4;
      el("line", { x1: L, x2: W - R, y1: Y(y), y2: Y(y), class: "grid-line" });
      el("text", { x: L - 6, y: Y(y) + 3, "text-anchor": "end", class: "axis" }).textContent = Math.round(y) + unit;
    }
    const first = points.find(p => p.label), last = [...points].reverse().find(p => p.label);
    if (first) el("text", { x: L, y: H - 6, class: "axis" }).textContent = first.label;
    if (last && last !== first) el("text", { x: W - R, y: H - 6, "text-anchor": "end", class: "axis" }).textContent = last.label;
  }
  let d = "", pen = false;
  for (const p of points) {
    if (p.y == null) { pen = false; continue; }
    d += (pen ? "L" : "M") + X(p.x).toFixed(1) + " " + Y(p.y).toFixed(1) + " ";
    pen = true;
  }
  el("path", { d, class: "l1", "vector-effect": "non-scaling-stroke" });
  return svg;
}

// ---- messages ----------------------------------------------------------------

function messages() {
  const q = S.query.toLowerCase();
  const threads = S.threads.filter(t => !q || (t.name || "").toLowerCase().includes(q) || t.number.includes(q) || (t.last_text || "").toLowerCase().includes(q));
  const list = h("div", { class: "list" },
    h("div", { class: "list-head" },
      h("div", { class: "row" },
        h("button", { class: "btn sm primary", onclick: () => { S.starred = false; go("messages", "__new"); } }, "New"),
        h("button", { class: "btn sm" + (S.starred ? " primary" : ""), onclick: async () => { S.starred = !S.starred; S.sel = null; await load("messages"); renderView(); } }, "★ Starred"),
        h("span", { class: "grow" }),
        h("button", { class: "btn sm", title: "Copy the texts on the SIM/module into this computer's archive", onclick: syncMessages }, "Sync")),
      h("input", { class: "input", type: "search", placeholder: "Search", value: S.query, "aria-label": "Search texts",
        oninput: e => { S.query = e.target.value; const pos = e.target.selectionStart; renderView(); const i = document.querySelector(".list-head input"); i.focus(); i.setSelectionRange(pos, pos); } })),
    h("div", { class: "threads" }, threads.length ? threads.map(t => h("div", {
      class: "thread" + (S.sel === t.number && !S.starred ? " sel" : "") + (t.unread ? " unread" : ""), role: "button", tabindex: "0",
      onclick: () => openThread(t.number), onkeydown: e => { if (e.key === "Enter") openThread(t.number); },
    }, avatar(t.number),
      h("div", { class: "grow" },
        h("div", { class: "spread" }, h("span", { class: "name", text: t.name || t.number }), h("span", { class: "faint small", text: fmtTime(t.last_time) })),
        h("div", { class: "last", text: (t.last_direction === "out" ? "You: " : "") + (t.last_text || "") })))) :
      h("div", { class: "empty", text: S.threads.length ? "No match." : "No texts yet. Received texts land here; press Sync to bring in what's already on the SIM." })));
  return h("div", { class: "split" }, list, convoPane());
}
async function openThread(number) { S.starred = false; S.sel = number; S.convo = await GET("/api/messages?number=" + encodeURIComponent(number)); await loadThreads(); renderNav(); renderView(); }
async function syncMessages() {
  const r = await act(() => POST("/api/messages/sync", { delete: false }));
  if (r) { toast(`${r.on_module} on the module, ${r.imported} new in the archive`); await load("messages"); renderView(); }
}
function convoPane() {
  if (S.starred) {
    return h("div", { class: "convo" }, h("div", { class: "convo-head" }, h("h2", { text: "Starred texts" })),
      h("div", { class: "bubbles" }, S.convo.length ? S.convo.map(m => bubble(m, true)) : h("div", { class: "empty", text: "Star a text with ☆ to keep it here." })));
  }
  if (!S.sel) return h("div", { class: "convo" }, h("div", { class: "empty", style: { margin: "auto" }, text: "Pick a conversation, or start a new one." }));
  const isNew = S.sel === "__new";
  const head = isNew
    ? h("div", { class: "convo-head" }, h("input", { class: "input", id: "to", placeholder: "To: +44 7700 900123", inputmode: "tel", "aria-label": "Recipient number", autofocus: true }))
    : h("div", { class: "convo-head" }, avatar(S.sel),
        h("div", { class: "grow" }, h("div", { style: { fontWeight: 600 }, text: nameOf(S.sel) }), S.contacts[S.sel] ? h("div", { class: "faint small mono", text: S.sel }) : null),
        h("button", { class: "btn sm", onclick: () => renameContact(S.sel) }, S.contacts[S.sel] ? "Rename" : "Add name"),
        /^\+?\d{3,}$/.test(S.sel) ? h("button", { class: "btn sm", onclick: () => dial(S.sel) }, icon("phone"), "Call") : null);
  let lastDay = "";
  const items = [];
  for (const m of S.convo) {
    const day = new Date(m.time).toDateString();
    if (day !== lastDay) { items.push(h("div", { class: "day", text: new Date(m.time).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "long" }) })); lastDay = day; }
    items.push(bubble(m, false));
  }
  const bubbles = h("div", { class: "bubbles" }, isNew ? h("div", { class: "empty", text: "Type the number above, then your text." }) : items.length ? items : h("div", { class: "empty", text: "No texts." }));
  requestAnimationFrame(() => { bubbles.scrollTop = bubbles.scrollHeight; });
  return h("div", { class: "convo" }, head, bubbles, composer(isNew));
}
function bubble(m, showWho) {
  return h("div", { class: "bubble " + m.direction, title: m.status ? `status: ${m.status}` : "" },
    showWho ? h("div", { class: "small", style: { fontWeight: 600, opacity: .8 }, text: m.direction === "in" ? nameOf(m.number) : "To " + nameOf(m.number) }) : null,
    m.text,
    h("div", { class: "meta" },
      m.status && m.status !== "sent" && m.direction === "out" ? h("span", { text: m.status }) : null,
      h("span", { text: fmtTime(m.time) }),
      h("button", { class: "star" + (m.starred ? " on" : ""), "aria-label": m.starred ? "Unstar" : "Star", title: m.starred ? "Unstar" : "Keep this text (star)",
        onclick: async () => { await act(() => POST(`/api/messages/${m.id}/star`, { on: !m.starred })); m.starred = !m.starred; renderView(); } }, m.starred ? "★" : "☆")));
}
function composer(isNew) {
  const ta = h("textarea", { class: "input", placeholder: "Text message", rows: "2", "aria-label": "Message", value: S.draft,
    oninput: e => { S.draft = e.target.value; updCount(); },
    onkeydown: e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } } });
  const count = h("span", { class: "counter" });
  const when = h("input", { class: "input", type: "datetime-local", style: { width: "auto" }, "aria-label": "Send at" });
  function updCount() {
    const c = smsCount(S.draft);
    count.className = "counter" + (c.ucs2 ? " ucs2" : "");
    count.textContent = S.draft ? `${c.size}/${c.limit}${c.parts > 1 ? ` · ${c.parts} texts` : ""}${c.ucs2 ? " · Unicode (accents/emoji): 70 per text" : ""}` : "Enter to send, Shift+Enter for a new line";
  }
  updCount();
  async function send() {
    const to = isNew ? (document.getElementById("to").value || "").replace(/[\s.()-]/g, "") : S.sel;
    const text = S.draft.trim();
    if (!to || !text) { toast(to ? "Write something first" : "Who to? Type a number", true); return; }
    if (S.scheduleOn) {
      if (!when.value) { toast("Pick when to send it", true); return; }
      const r = await act(() => POST("/api/scheduled", { number: to, text, due: new Date(when.value).getTime() / 1000 }), "Scheduled");
      if (r) { S.draft = ""; S.scheduleOn = false; renderView(); }
      return;
    }
    ta.disabled = true;
    const r = await act(() => POST("/api/messages/send", { number: to, text }), r => `Sent${r.references.length > 1 ? ` as ${r.references.length} texts` : ""}`);
    ta.disabled = false;
    if (r) { S.draft = ""; S.sel = to; await load("messages"); renderAll(); }
  }
  return h("div", { class: "composer" }, ta,
    h("div", { class: "row wrap" }, count, h("span", { class: "grow" }),
      h("label", { class: "switch small" }, h("input", { type: "checkbox", checked: S.scheduleOn, onchange: e => { S.scheduleOn = e.target.checked; renderView(); } }), "Send later"),
      S.scheduleOn ? when : null,
      h("button", { class: "btn primary", onclick: send }, S.scheduleOn ? "Schedule" : "Send")));
}
async function renameContact(number) {
  const name = prompt(`Name for ${number} (empty to remove)`, S.contacts[number] || "");
  if (name === null) return;
  await act(() => POST("/api/contacts", { number, name }));
  await loadContacts(); await loadThreads(); renderAll();
}

// ---- calls -------------------------------------------------------------------

const KEYS = [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"], ["6", "MNO"], ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]];
function keypad(onKey) {
  return h("div", { class: "keypad" }, KEYS.map(([k, sub]) => h("button", { class: "key", onclick: () => onKey(k), "aria-label": k }, k, h("small", { text: sub || " " }))));
}
function calls() {
  const display = h("input", { class: "input dial-display", value: S.dial, placeholder: "Number", inputmode: "tel", "aria-label": "Number to call",
    oninput: e => { S.dial = e.target.value; }, onkeydown: e => { if (e.key === "Enter") dial(S.dial); } });
  return h("div", {},
    h("h1", { text: "Calls" }),
    h("p", { class: "lede", text: "Call from the computer: your Mac's microphone and speakers carry the call, and you can record it." }),
    h("div", { class: "grid", style: { gridTemplateColumns: "minmax(260px, 340px) 1fr", alignItems: "start" } },
      h("section", { class: "card dialer" },
        h("div", { class: "dial-field" }, display,
          h("button", { class: "dial-del", "aria-label": "Delete the last digit", title: "Delete",
            onclick: () => { S.dial = S.dial.slice(0, -1); display.value = S.dial; display.focus(); } }, icon("del"))),
        keypad(k => { S.dial += k; display.value = S.dial; }),
        h("button", { class: "btn good dial-call", onclick: () => dial(S.dial) }, icon("phone"), "Call")),
      h("section", { class: "card", style: { padding: "0", overflow: "hidden" } },
        h("div", { style: { padding: "14px 16px 8px" } }, h("h2", { text: "Call log" })),
        S.calls.length ? h("table", { class: "t" },
          h("thead", {}, h("tr", {}, ["", "Who", "When", "Length", ""].map(t => h("th", { text: t })))),
          h("tbody", {}, S.calls.map(c => {
            const missed = c.direction === "in" && !c.answered;
            const len = c.answered && c.ended ? fmtDur((new Date(c.ended) - new Date(c.answered)) / 1000) : missed ? "missed" : "–";
            return h("tr", {},
              h("td", { class: "dir" + (missed ? " missed" : ""), title: c.direction === "in" ? "incoming" : "outgoing", text: c.direction === "in" ? "↙" : "↗" }),
              h("td", {}, h("div", { text: c.name || c.number || "Unknown" }), c.name ? h("div", { class: "faint small mono", text: c.number }) : null),
              h("td", { class: "muted", text: fmtTime(c.started, true) }),
              h("td", { class: missed ? "muted" : "", text: len }),
              h("td", {}, h("div", { class: "row" },
                c.recording ? h("span", { class: "tag", title: c.recording, text: "● recorded" }) : null,
                c.number ? h("button", { class: "btn sm ghost", onclick: () => dial(c.number), title: "Call back" }, icon("phone")) : null,
                c.number ? h("button", { class: "btn sm ghost", onclick: () => go("messages", c.number), title: "Text" }, icon("msg")) : null)));
          }))) : h("div", { class: "empty", text: "No calls yet." }))));
}
async function dial(number) {
  number = (number || "").replace(/[\s.()-]/g, "");
  if (!number) { toast("Type a number first", true); return; }
  if (await act(() => POST("/api/call/dial", { number }), "Calling…")) { S.callMin = false; setTimeout(refreshState, 600); }
}

// ---- the call layer ----------------------------------------------------------

function currentCall() {
  const st = S.state;
  if (!st) return null;
  if (st.calls && st.calls.length) return st.calls[0];
  if (st.ringing) return { direction: "in", state: "incoming", number: st.ringing_number };
  return null;
}
function callLabel(c) {
  if (c.state === "active") return S.callStart ? fmtDur((Date.now() - S.callStart) / 1000) : "in call";
  return { incoming: "Incoming call", waiting: "Call waiting", dialing: "Calling…", alerting: "Ringing…", held: "On hold" }[c.state] || c.state;
}
let ringer = null;
function ring(on) {
  if (on && !ringer) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const beep = () => {
        for (const [f, t] of [[880, 0], [660, .25]]) {
          const o = ctx.createOscillator(), g = ctx.createGain();
          o.frequency.value = f; g.gain.setValueAtTime(.0001, ctx.currentTime + t);
          g.gain.exponentialRampToValueAtTime(.15, ctx.currentTime + t + .02);
          g.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + t + .22);
          o.connect(g).connect(ctx.destination); o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + .25);
        }
      };
      beep();
      ringer = { ctx, timer: setInterval(beep, 2000) };
    } catch (e) { ringer = null; }
  } else if (!on && ringer) {
    clearInterval(ringer.timer); ringer.ctx.close(); ringer = null;
  }
}
function renderCall() {
  const layer = document.getElementById("call");
  const c = currentCall();
  const st = S.state || {};
  ring(!EMBEDDED && !!c && c.state === "incoming" && c.direction === "in");
  if (!c || S.callMin || EMBEDDED) { layer.hidden = true; layer.replaceChildren(); return; }
  layer.hidden = false;
  const incoming = c.direction === "in" && (c.state === "incoming" || c.state === "waiting");
  const who = c.number ? nameOf(c.number) : "Unknown number";
  const card = h("div", { class: "call-card" + (incoming ? " ring" : ""), role: "dialog", "aria-label": "Call" },
    avatar(c.number, "lg"),
    h("div", { class: "who", text: who }),
    S.contacts[c.number] ? h("div", { class: "sub mono", text: c.number }) : null,
    h("div", { class: "state", id: "call-state", text: callLabel(c) }));
  if (incoming) {
    add(card, [h("div", { class: "call-actions" },
      h("div", {}, h("button", { class: "round bad", "aria-label": "Decline", onclick: () => act(() => POST("/api/call/hangup")).then(refreshState) }, icon("hangup")), h("div", { class: "round-label", text: "Decline" })),
      h("div", {}, h("button", { class: "round good", "aria-label": "Answer", onclick: () => act(() => POST("/api/call/answer", { audio: true })).then(refreshState) }, icon("phone")), h("div", { class: "round-label", text: "Answer" }))),
      h("button", { class: "btn sm ghost", style: { marginTop: "10px" }, onclick: () => act(() => POST("/api/call/answer", { audio: false })).then(refreshState) }, "Answer without the Mac's audio")]);
  } else {
    const tools = h("div", { class: "call-tools" },
      h("div", {}, h("button", { class: "round soft" + (st.bridge ? " on" : ""), "aria-pressed": !!st.bridge, "aria-label": "Mac audio",
        onclick: () => act(() => POST("/api/audio/bridge", { on: !st.bridge })).then(refreshState) }, icon("mic")), h("div", { class: "round-label", text: "Mac audio" })),
      h("div", {}, h("button", { class: "round soft" + (st.recording ? " on" : ""), "aria-pressed": !!st.recording, "aria-label": "Record",
        onclick: () => act(() => POST("/api/audio/bridge", { on: !st.recording ? true : false, record: !st.recording })).then(refreshState) }, icon("rec")), h("div", { class: "round-label", text: st.recording ? "Recording" : "Record" })),
      h("div", {}, h("button", { class: "round soft" + (S.keypad ? " on" : ""), "aria-pressed": S.keypad, "aria-label": "Keypad", onclick: () => { S.keypad = !S.keypad; renderCall(); } }, icon("grid")), h("div", { class: "round-label", text: "Keypad" })),
      h("div", {}, h("button", { class: "round soft", "aria-label": "Minimise", onclick: () => { S.callMin = true; renderCall(); renderTop(); } }, icon("min")), h("div", { class: "round-label", text: "Hide" })));
    add(card, [tools,
      S.keypad ? keypad(k => act(() => POST("/api/call/dtmf", { digits: k }))) : null,
      h("div", { class: "call-actions" }, h("div", {}, h("button", { class: "round bad", "aria-label": "Hang up", onclick: () => act(() => POST("/api/call/hangup")).then(refreshState) }, icon("hangup")), h("div", { class: "round-label", text: "Hang up" })))]);
  }
  layer.replaceChildren(card);
}
setInterval(() => { const el = document.getElementById("call-state"); const c = currentCall(); if (el && c) el.textContent = callLabel(c); }, 1000);

// ---- events ------------------------------------------------------------------

function events() {
  const kinds = [...new Set(S.events.map(e => e.event))].sort();
  const q = S.evQuery.toLowerCase();
  const rows = S.events.filter(e => !q || describe(e).toLowerCase().includes(q) || JSON.stringify(e.data).toLowerCase().includes(q));
  const sel = S.events.find(e => e.id === S.evSel);
  return h("div", {},
    h("div", { class: "spread" }, h("div", {}, h("h1", { text: "Events" }), h("p", { class: "lede", text: "Everything the daemon reported, with all of its data. Live." })),
      h("a", { class: "btn sm", href: `/api/export/events?token=${encodeURIComponent(TOKEN)}`, download: "koppar-events.json" }, "Export JSON")),
    h("div", { class: "row wrap", style: { marginBottom: "12px" } },
      h("select", { class: "input", style: { width: "auto" }, "aria-label": "Event type", onchange: async e => { S.evKind = e.target.value; await load("events"); renderView(); } },
        h("option", { value: "", text: "All events" }), [...new Set([...kinds, S.evKind].filter(Boolean))].map(k => h("option", { value: k, text: k, selected: k === S.evKind }))),
      h("input", { class: "input", type: "search", placeholder: "Search events", style: { maxWidth: "320px" }, value: S.evQuery, "aria-label": "Search events",
        oninput: e => { S.evQuery = e.target.value; const p = e.target.selectionStart; renderView(); const i = document.querySelector("[aria-label='Search events']"); i.focus(); i.setSelectionRange(p, p); } })),
    h("div", { class: "grid", style: { gridTemplateColumns: sel ? "1fr minmax(280px, 420px)" : "1fr", alignItems: "start" } },
      h("section", { class: "card", style: { padding: 0, overflow: "auto", maxHeight: "calc(100vh - 230px)" }, "data-keep-scroll": "" },
        rows.length ? h("table", { class: "t" },
          h("thead", {}, h("tr", {}, ["Time", "Event", "What happened"].map(t => h("th", { text: t })))),
          h("tbody", {}, rows.map(e => h("tr", { class: "click" + (e.id === S.evSel ? " sel" : ""), tabindex: "0",
            onclick: () => { S.evSel = e.id === S.evSel ? null : e.id; renderView(); }, onkeydown: k => { if (k.key === "Enter") { S.evSel = e.id; renderView(); } } },
            h("td", { class: "muted", style: { whiteSpace: "nowrap" }, text: fmtTime(e.time, true) }),
            h("td", {}, h("span", { class: "tag", text: e.event })),
            h("td", { text: describe(e) })))),
          h("tfoot", {}, h("tr", {}, h("td", { colspan: "3" }, h("button", { class: "btn sm ghost", onclick: olderEvents }, "Load older")))))
          : h("div", { class: "empty", text: "No events." })),
      sel ? h("section", { class: "card" },
        h("div", { class: "spread" }, h("h2", { text: sel.event }), h("button", { class: "btn sm ghost", onclick: () => { S.evSel = null; renderView(); }, "aria-label": "Close" }, "✕")),
        h("p", { class: "muted small", text: `#${sel.id} · ${new Date(sel.time).toLocaleString()}` }),
        h("pre", { class: "json", text: JSON.stringify({ event: sel.event, time: sel.time, ...sel.data }, null, 2) })) : null));
}
async function olderEvents() {
  const last = S.events[S.events.length - 1];
  if (!last) return;
  const more = await act(() => GET(`/api/events?before=${last.id}&limit=300` + (S.evKind ? "&kind=" + S.evKind : "")));
  if (more) { S.events.push(...more); renderView(); }
}

// ---- console -----------------------------------------------------------------

const QUICK = ["ATI", "AT+CSQ", "AT+QCSQ", 'AT+QENG="servingcell"', "AT+COPS?", "AT+QNWINFO", "AT+CPIN?", "AT+QTEMP", "AT+CBC", "AT+CLCC", "AT+CNUM", "AT+CGPADDR"];
function consoleView() {
  const input = h("input", { class: "input mono", placeholder: "AT command, e.g. AT+CSQ", "aria-label": "AT command", autocomplete: "off", spellcheck: "false",
    onkeydown: e => {
      if (e.key === "Enter") runAt(e.target.value);
      else if (e.key === "ArrowUp" && S.history.length) { S.histPos = Math.min(S.histPos + 1, S.history.length - 1); e.target.value = S.history[S.histPos]; e.preventDefault(); }
      else if (e.key === "ArrowDown") { S.histPos = Math.max(S.histPos - 1, -1); e.target.value = S.histPos < 0 ? "" : S.history[S.histPos]; e.preventDefault(); }
    } });
  setTimeout(() => input.focus(), 0);
  const log = [...S.atlog].reverse();
  return h("div", {},
    h("h1", { text: "Console" }),
    h("p", { class: "lede", text: "Talk to the module directly, and see every AT command anyone sent it: this page, the command line, the daemon." }),
    h("section", { class: "card", style: { marginBottom: "14px" } },
      h("div", { class: "row" }, input, h("button", { class: "btn primary", onclick: () => runAt(input.value) }, "Send")),
      h("div", { class: "quick", style: { marginTop: "10px" } }, QUICK.map(c => h("button", { class: "btn sm mono", onclick: () => runAt(c) }, c)))),
    h("div", { class: "spread", style: { marginBottom: "8px" } },
      h("h2", { text: "AT traffic" }),
      h("label", { class: "switch small" }, h("input", { type: "checkbox", checked: S.showPoll, onchange: async e => { S.showPoll = e.target.checked; await load("console"); renderView(); } }), "Show the daemon's polling")),
    h("div", { class: "console-log" }, log.length ? log.map(atEntry) : h("div", { class: "empty", text: "No commands yet." })));
}
function atEntry(r) {
  return h("div", { class: "at-entry" + (r.ok ? "" : " err") },
    h("div", { class: "row" }, h("span", { class: "tag " + (r.ok ? r.source : "err"), text: r.source }), h("span", { class: "cmd grow", text: r.cmd }),
      h("span", { class: "faint small", text: `${r.ms} ms · ${fmtTime(r.time)}` })),
    (r.lines && r.lines.length) || r.error ? h("pre", { text: [...(r.lines || []), r.ok ? "OK" : r.error].join("\n") }) : h("pre", { text: "OK" }));
}
async function runAt(cmd) {
  cmd = (cmd || "").trim();
  if (!cmd) return;
  if (!/^at/i.test(cmd)) cmd = "AT" + cmd;
  S.history = [cmd, ...S.history.filter(c => c !== cmd)].slice(0, 50); S.histPos = -1;
  const r = await act(() => POST("/api/at", { cmd, timeout: /COPS=\?/i.test(cmd) ? 200 : 15 }));
  if (r && !r.ok) toast(`${cmd}: ${r.error}`, true);
}

// ---- network -----------------------------------------------------------------

const METRICS = { rsrp: ["RSRP", " dBm"], rsrq: ["RSRQ", " dB"], sinr: ["SINR", " dB"], rssi: ["RSSI", " dBm"] };
function network() {
  const pts = S.signal.map((r, i) => ({ x: new Date(r.time).getTime() || i, y: r[S.metric], label: fmtTime(r.time) }));
  const cell = S.cell;
  const ussdIn = h("input", { class: "input mono", placeholder: "*#100#", "aria-label": "USSD code", onkeydown: e => { if (e.key === "Enter") ussd(e.target.value); } });
  return h("div", {},
    h("h1", { text: "Network" }),
    h("p", { class: "lede", text: "What a phone hides: the cell you're on, its neighbours, signal history, every operator in range, and USSD codes." }),
    h("section", { class: "card", style: { marginBottom: "14px" } },
      h("div", { class: "spread", style: { marginBottom: "8px" } },
        h("div", { class: "row" }, Object.entries(METRICS).map(([k, [label]]) => h("button", { class: "btn sm" + (S.metric === k ? " primary" : ""), onclick: () => { S.metric = k; renderView(); } }, label))),
        h("div", { class: "row" }, [1, 6, 24, 168].map(n => h("button", { class: "btn sm" + (S.hours === n ? " primary" : ""), onclick: async () => { S.hours = n; await load("network"); renderView(); } }, n < 24 ? `${n} h` : n === 24 ? "1 day" : "7 days")))),
      pts.filter(p => p.y != null).length > 1 ? chart(pts, { unit: "" }) : h("div", { class: "empty", text: "Not enough samples yet: the daemon records one every 15 s." }),
      h("div", { class: "faint small", text: `${METRICS[S.metric][0]} in${METRICS[S.metric][1]}, ${S.signal.length} samples` })),
    h("div", { class: "grid" },
      h("section", { class: "card" },
        h("div", { class: "spread" }, h("h2", { text: "Serving cell" }), h("button", { class: "btn sm", onclick: loadCell }, cell ? "Refresh" : "Read")),
        cell && cell.serving ? h("dl", { class: "kv mono" }, Object.entries(cell.serving).map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]))
          : h("div", { class: "faint small", text: "Cell ID, band, channel, signal: AT+QENG." }),
        cell && cell.neighbours.length ? h("div", { style: { marginTop: "12px" } }, h("h2", { text: `Neighbours (${cell.neighbours.length})` }),
          h("div", { style: { overflow: "auto" } }, h("table", { class: "t mono" },
            h("thead", {}, h("tr", {}, Object.keys(cell.neighbours[0]).map(k => h("th", { text: k })))),
            h("tbody", {}, cell.neighbours.map(n => h("tr", {}, Object.values(n).map(v => h("td", { text: Array.isArray(v) ? v.join(", ") : v })))))))) : null),
      h("section", { class: "card" },
        h("h2", { text: "USSD" }),
        h("p", { class: "muted small", text: "Balance, your number, operator services: codes like *#100#. The network's answer appears below." }),
        h("div", { class: "row" }, ussdIn, h("button", { class: "btn primary", onclick: () => ussd(ussdIn.value) }, "Send")),
        h("ul", { class: "feed", style: { marginTop: "10px" } }, S.ussd.map(e => h("li", {}, h("span", { class: "when", text: fmtTime(e.time) }),
          h("span", { class: "grow", style: { whiteSpace: "pre-wrap" }, text: (e.data || e).text ?? "" }))))),
      h("section", { class: "card" },
        h("h2", { text: "Operators in range" }),
        h("p", { class: "muted small", text: "A full scan takes up to 3 minutes, and the modem can't do anything else meanwhile (calls and texts wait)." }),
        h("button", { class: "btn", onclick: scan }, S.scan === "busy" ? "Scanning…" : "Scan now"),
        Array.isArray(S.scan) ? h("table", { class: "t", style: { marginTop: "10px" } },
          h("thead", {}, h("tr", {}, ["Operator", "PLMN", "Radio", "Status"].map(t => h("th", { text: t })))),
          h("tbody", {}, S.scan.map(o => h("tr", {}, h("td", { text: o.name }), h("td", { class: "mono", text: o.plmn }), h("td", { text: o.tech }),
            h("td", {}, h("span", { class: "tag" + (o.status === "current" ? " ui" : o.status === "forbidden" ? " err" : ""), text: o.status })))))) : null)));
}
async function loadCell() { const r = await act(() => GET("/api/cell")); if (r) { S.cell = r; renderView(); } }
async function scan() {
  if (S.scan === "busy" || !confirm("Scan for operators? It takes up to 3 minutes and blocks the modem meanwhile.")) return;
  S.scan = "busy"; renderView();
  const r = await act(() => POST("/api/scan"));
  S.scan = r || null; renderView();
}
async function ussd(code) { code = (code || "").trim(); if (code) await act(() => POST("/api/ussd", { code }), "Sent; waiting for the network's answer"); }

// ---- automations -------------------------------------------------------------

function automations() {
  const s = S.settings;
  if (!s) return h("div", { class: "empty", text: "Loading…" });
  const save = async (k, v) => { const r = await act(() => POST("/api/settings", { [k]: v }), "Saved"); if (r) { S.settings = r; renderView(); } };
  const ar = s.auto_reply || {};
  const arText = h("textarea", { class: "input", value: ar.text || "", rows: "2", "aria-label": "Auto-reply text" });
  const arHours = h("input", { class: "input", type: "number", min: "0", step: "1", value: ar.hours ?? 6, style: { width: "90px" }, "aria-label": "Hours between replies" });
  const fwd = h("input", { class: "input", value: s.forward_sms_to || "", placeholder: "+44 7700 900123", inputmode: "tel", "aria-label": "Forward to" });
  const hook = h("input", { class: "input mono", value: s.webhook_url || "", placeholder: "https://example.com/hook", "aria-label": "Webhook URL" });
  return h("div", {},
    h("h1", { text: "Automations" }),
    h("p", { class: "lede", text: "Things a phone won't do for you. They run in the daemon, so they work while this page is closed." }),
    h("div", { class: "grid" },
      h("section", { class: "card stack" }, h("h2", { text: "Calls" }),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: s.mac_audio, onchange: e => save("mac_audio", e.target.checked) }), "Put calls on the Mac's microphone and speakers as soon as they connect"),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: s.popup_on_call, onchange: e => save("popup_on_call", e.target.checked) }), "Notify and pop up a window when a call comes in (browser use; the menu-bar app has its own call panel)")),
      h("section", { class: "card stack" }, h("h2", { text: "Auto-reply to texts" }),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: ar.enabled, onchange: e => save("auto_reply", { ...ar, enabled: e.target.checked }) }), "On"),
        arText,
        h("div", { class: "row wrap" }, h("span", { class: "muted small", text: "At most once per number every" }), h("span", { class: "row" }, arHours, h("span", { class: "muted small", text: "hours" })), h("span", { class: "grow" }),
          h("button", { class: "btn sm", onclick: () => save("auto_reply", { ...ar, text: arText.value, hours: Number(arHours.value) || 0 }) }, "Save")),
        h("p", { class: "faint small", text: "Only to real numbers, never to named senders like banks or the operator." })),
      h("section", { class: "card stack" }, h("h2", { text: "Forward texts" }),
        h("p", { class: "muted small", text: "Every text this line receives is sent on to this number, with who it came from." }),
        h("div", { class: "row" }, fwd, h("button", { class: "btn sm", onclick: () => save("forward_sms_to", fwd.value.replace(/[\s.()-]/g, "")) }, "Save"))),
      h("section", { class: "card stack" }, h("h2", { text: "Webhook" }),
        h("p", { class: "muted small", text: "Every event, POSTed as JSON: hand texts and calls to your own service or an AI agent." }),
        h("div", { class: "row" }, hook, h("button", { class: "btn sm", onclick: () => save("webhook_url", hook.value.trim()) }, "Save")),
        h("p", { class: "faint small" }, "For scripts on this computer: ", h("span", { class: "mono", text: "koppar-lte-modem daemon --on-event ./hook.sh" }))),
      h("section", { class: "card stack" }, h("h2", { text: "Archive" }),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: s.archive_delete_from_module, onchange: e => save("archive_delete_from_module", e.target.checked) }), "Delete texts from the module once they're archived here"),
        h("p", { class: "faint small", text: "Every text is kept on this computer either way. The module holds a few hundred at most." })),
      h("section", { class: "card stack" }, h("h2", { text: "Scheduled texts" }),
        S.scheduled.length ? h("table", { class: "t" }, h("tbody", {}, S.scheduled.map(x => h("tr", {},
          h("td", { class: "muted", style: { whiteSpace: "nowrap" }, text: fmtTime(new Date(x.due * 1000).toISOString(), true) }),
          h("td", {}, h("div", { text: nameOf(x.number) }), h("div", { class: "faint small", text: x.text })),
          h("td", {}, x.state === "pending" ? h("button", { class: "btn sm", onclick: async () => { await act(() => DEL(`/api/scheduled/${x.id}`)); await load("automations"); renderView(); } }, "Cancel") : h("span", { class: "tag" + (x.state === "failed" ? " err" : ""), text: x.state, title: x.result || "" }))))))
          : h("div", { class: "faint small", text: "None. In Messages, switch on \"Send later\"." }))));
}

// ---- device ------------------------------------------------------------------

function device() {
  const d = S.device;
  if (!d) return h("div", { class: "empty", text: "Loading…" });
  const b = d.board || {}, info = d.info || {};
  const temp = d.temperature ? d.temperature.split(",").map(x => x.trim()).filter(Boolean) : [];
  const board = cmd => act(() => POST("/api/board", { cmd }), r => (r.lines || []).join(" ") || "OK").then(() => load("device")).then(renderView);
  const at = (cmd, warn) => { if (!warn || confirm(warn)) act(() => POST("/api/at", { cmd, timeout: 20 }), r => r.ok ? "OK" : r.error); };
  return h("div", {},
    h("div", { class: "spread" }, h("div", {}, h("h1", { text: "Device" }), h("p", { class: "lede", text: "The board and the module inside it." })),
      h("button", { class: "btn sm", onclick: async () => { await load("device"); renderView(); } }, "Refresh")),
    h("div", { class: "grid" },
      h("section", { class: "card" }, h("h2", { text: "Module" }),
        h("dl", { class: "kv" },
          h("dt", { text: "Model" }), h("dd", { text: info.model || "–" }),
          h("dt", { text: "Firmware" }), h("dd", { class: "mono", text: info.revision || "–" }),
          h("dt", { text: "IMEI" }), h("dd", { class: "mono", text: info.imei || "–" }),
          h("dt", { text: "ICCID" }), h("dd", { class: "mono", text: info.iccid || "–" }),
          h("dt", { text: "SIM" }), h("dd", { text: d.sim || "–" }),
          h("dt", { text: "Temperature" }), h("dd", { text: temp.length ? temp.map(t => t + " °C").join(" · ") : "–" }),
          h("dt", { text: "Supply" }), h("dd", { text: d.supply_v ? `${d.supply_v.toFixed(3)} V` : "–" }),
          h("dt", { text: "Network" }), h("dd", { text: d.network_info ? d.network_info.replaceAll('"', "").split(",").join(" · ") : "–" }))),
      h("section", { class: "card" }, h("h2", { text: "Board" }),
        Object.keys(b).length ? h("dl", { class: "kv mono" }, Object.entries(b).map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]))
          : h("div", { class: "faint", text: d.board_error || "Board not connected." })),
      h("section", { class: "card stack" }, h("h2", { text: "Power" }),
        h("div", { class: "row wrap" },
          h("button", { class: "btn", onclick: () => board("power on") }, "Switch on"),
          h("button", { class: "btn", onclick: () => at("AT+CFUN=1,1", "Restart the module? It leaves USB for about 20 s; calls end.") }, "Restart"),
          h("button", { class: "btn bad", onclick: () => at("AT+QPOWD=1", "Switch the module off? Calls end and texts won't arrive until it's back on.") }, "Switch off")),
        h("label", { class: "switch" }, h("input", { type: "checkbox", checked: b.autopower === "1", onchange: e => board("autopower " + (e.target.checked ? "on" : "off")) }), "Switch on by itself at plug-in")),
      h("section", { class: "card stack" }, h("h2", { text: "LEDs and audio" }),
        [["net", "NET (green)"], ["ring", "RING (amber)"], ["call", "CALL (blue)"]].map(([led, label]) => h("label", { class: "row" }, h("span", { class: "grow", text: label }),
          h("select", { class: "input", style: { width: "auto" }, onchange: e => board(`led ${led} ${e.target.value}`) },
            ["auto", "on", "off", "slow", "fast"].map(m => h("option", { value: m, text: m, selected: b["led_" + led] === m }))))),
        h("label", { class: "row" }, h("span", { class: "grow", text: "Audio route" }),
          h("select", { class: "input", style: { width: "auto" }, onchange: e => board("audio " + e.target.value) },
            [["pcm", "calls (normal)"], ["loopback", "loopback test"], ["tone", "1 kHz test tone"]].map(([m, t]) => h("option", { value: m, text: t, selected: b.audio === m })))))));
}

// ---- live events -------------------------------------------------------------

let es = null;
function connect() {
  if (es) es.close();
  es = new EventSource("/api/stream?token=" + encodeURIComponent(TOKEN));
  es.onopen = () => { S.live = true; renderNav(); refreshState(); };
  es.onerror = () => { S.live = false; renderNav(); };
  es.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
}
function notifyDesktop(title, body) {
  // Only if the browser already allows it: this page never asks. The daemon
  // (or the Koppar menu-bar app) does the notifying.
  if (EMBEDDED || !("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
  try { new Notification(title, { body, tag: title }); } catch (e) { /* ignore */ }
}
async function onEvent(ev) {
  if (ev.ui) {
    if (ev.event === "signal") { if (S.state) S.state.signal = ev; S.signal.push({ ...ev, time: new Date().toISOString() }); renderTop(); if (S.view === "overview" || S.view === "network") renderView(); }
    else if (ev.event === "at") { S.atlog.push({ ...ev, time: new Date().toISOString() }); if (S.view === "console") renderView(); }
    else if (ev.event === "message") {
      toast(`Text from ${ev.name || ev.number}: ${ev.text}`);
      notifyDesktop(`Text from ${ev.name || ev.number}`, ev.text);
      await loadThreads();
      if (S.view === "messages" && S.sel === ev.number) S.convo = await GET("/api/messages?number=" + encodeURIComponent(ev.number));
      renderNav(); if (S.view === "messages") renderView();
    } else if (ev.event === "read") { await loadThreads(); renderNav(); if (S.view === "messages") renderView();
    } else if (ev.event === "bridge") { if (ev.error) toast("Mac audio: " + ev.error, true); refreshState(); }
    return;
  }
  S.events.unshift({ id: ev.id, time: ev.time, event: ev.event, data: Object.fromEntries(Object.entries(ev).filter(([k]) => !["id", "time", "event"].includes(k))) });
  if (S.events.length > 1000) S.events.length = 1000;
  if (["ring", "incoming_call", "call_state", "call_ended", "call_audio", "ready", "module_on", "module_off", "network", "modem_disconnected"].includes(ev.event)) refreshState();
  if (ev.event === "incoming_call") { S.callMin = false; notifyDesktop("Incoming call", nameOf(ev.number)); }
  if (ev.event === "ussd") { S.ussd.unshift({ time: ev.time, data: ev }); toast("USSD: " + ev.text); }
  if (ev.event === "sms_sent" && S.view === "messages") { await loadThreads(); renderView(); }
  if (S.view === "events" && (!S.evKind || S.evKind === ev.event)) renderView();
  if (S.view === "overview") renderView();
}

// ---- start -------------------------------------------------------------------

function gate() {
  if (es) es.close();
  document.getElementById("app").replaceChildren(h("div", { class: "gate" },
    h("div", { class: "mark", style: { margin: "0 auto 16px", width: "48px", height: "48px" } }, (() => { const s = icon("home"); return s; })()),
    h("h1", { text: "Open this page from the command line" }),
    h("p", { class: "muted" }, "For your safety the UI needs a key that only this computer has. Run ", h("code", { text: "koppar-lte-modem ui" }), " to open it."),
    h("p", { class: "faint small" }, "Start the daemon first if it isn't running: ", h("code", { text: "koppar-lte-modem daemon" }))));
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && S.evSel) { S.evSel = null; renderView(); } });
function fromHash() {
  const [v, arg] = decodeURIComponent(location.hash.slice(1)).split(":");
  return VIEWS.some(([id]) => id === v) ? [v, arg || null] : [null, null];
}
window.addEventListener("hashchange", () => { const [v, arg] = fromHash(); if (v && (v !== S.view || arg)) go(v, arg); });

(async function start() {
  if (!TOKEN) return gate();
  const [v, arg] = fromHash();
  if (v) S.view = v;
  if (v === "messages" && arg) S.sel = arg;
  try { await loadContacts(); await loadThreads(); } catch (e) { if (e.message === "not authorised") return; }
  await refreshState();
  await load(S.view);
  renderAll();
  connect();
})();
})();
