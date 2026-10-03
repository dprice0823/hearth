/* Hearth — front end (no build step, no framework) */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};
const icon = (name, cls = "") => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) s.setAttribute("class", cls);
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  return s;
};
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(1)} GB`;
const fmtK = n => n > 0 && n % 1024 === 0 ? `${n / 1024}k` : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`;
const esc = s => s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const api = async (path, opts = {}) => {
  const r = await fetch(path, {
    ...opts,
    headers: { "X-Hearth": "1", ...(opts.body && typeof opts.body === "string" ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
  });
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || data || r.statusText);
  return data;
};
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });

const state = {
  models: [], settings: {}, chats: [], chat: null, memories: [],
  streaming: false, abort: null, pending: [], stick: true,
};

/* =========================================================== toasts & clipboard */
function toast(text, bad = false) {
  const t = h("div", { class: `toast${bad ? " bad" : ""}` }, text);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 2600);
}
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    const use = btn.querySelector("use");
    const prev = use?.getAttribute("href");
    use?.setAttribute("href", "#i-check");
    setTimeout(() => use?.setAttribute("href", prev), 1400);
  } else toast("Copied");
}

/* =========================================================== theme */
function applyTheme() {
  const pref = state.settings.theme || "system";
  const dark = pref === "dark" || (pref === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#hl-dark").disabled = !dark;
  $("#hl-light").disabled = dark;
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

/* =========================================================== markdown */
function renderMarkdown(el, text, streaming = false) {
  if (!window.marked || !window.DOMPurify) { el.textContent = text; return; }
  const html = DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: false }), { ADD_ATTR: ["target"] });
  el.innerHTML = html;
  $$("a", el).forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  $$("pre > code", el).forEach(code => {
    const pre = code.parentElement;
    const lang = ([...code.classList].find(c => c.startsWith("language-")) || "").replace("language-", "");
    if (window.hljs) {
      try { lang && hljs.getLanguage(lang) ? hljs.highlightElement(code) : (!streaming && hljs.highlightElement(code)); } catch {}
    }
    const btn = h("button", { title: "Copy code" }, icon("copy"), "Copy");
    btn.onclick = () => copyText(code.innerText, btn);
    const wrap = h("div", { class: "code-block" }, h("div", { class: "code-head" }, h("span", {}, lang || "text"), btn));
    pre.replaceWith(wrap);
    wrap.append(pre);
  });
  if (streaming) el.classList.add("cursor"); else el.classList.remove("cursor");
}

/* =========================================================== models */
const CAP_ICONS = { tools: ["tools", "Tools"], vision: ["eye", "Vision"], thinking: ["spark", "Thinks"] };
const AUTO = "auto";
const currentModel = () => state.chat?.model || state.settings.model || AUTO;
const modelInfo = name => name === AUTO
  ? { caps: ["tools"], ctx_max: Math.max(8192, ...state.models.map(m => m.ctx_max || 0)) }
  : state.models.find(m => m.name === name) || { caps: [], ctx_max: 8192 };
const shortName = n => n === AUTO ? "Auto" : n.replace(/^hf\.co\//, "");
const lastUsedModel = () => [...(state.chat?.messages || [])].reverse().find(m => m.role === "assistant" && m.model)?.model;

function renderModelButton() {
  const m = currentModel();
  const used = m === AUTO ? lastUsedModel() : null;
  $("#model-name").textContent = !state.models.length ? "No models installed" :
    m === AUTO ? (used ? `Auto · ${shortName(used)}` : "Auto") : shortName(m);
  const caps = $("#model-caps");
  const mcaps = modelInfo(m).caps;
  caps.replaceChildren(...Object.keys(CAP_ICONS).map(c =>
    h("button", { class: `cap-badge${mcaps.includes(c) ? " on" : ""}`,
      title: `Show only ${CAP_ICONS[c][1]} models` + (mcaps.includes(c) ? "" : ` (this model has no ${CAP_ICONS[c][1].toLowerCase()})`),
      onclick: e => { e.stopPropagation(); openModelMenu(c); } }, icon(CAP_ICONS[c][0]))));
  const hasTools = modelInfo(m).caps.includes("tools");
  for (const id of ["#toggle-web", "#toggle-tools"]) {
    $(id).hidden = !hasTools;      // a model that can't tool-call gets no Tools/Web buttons at all
    $(id).disabled = !hasTools;
  }
  $("#toggle-tools").classList.toggle("on", hasTools && state.settings.tools !== false);
  $("#toggle-web").classList.toggle("on", hasTools && state.settings.tools !== false && state.settings.internet !== false);
  updateMeter();
}

function openModelMenu(cap = null) {
  state.capFilter = cap;
  closeMenus();
  const menu = $("#model-menu");
  menu.hidden = false;
  $("#model-btn").classList.add("open");
  $("#model-filter").value = "";
  renderModelMenu();
  $("#model-filter").focus();
}


let _modelsTab = "rec";
let _libModels = null;   // cached full-library list
async function openModelsModal() {
  $("#models-modal").hidden = false;
  $("#pull-name").value = "";
  await refreshModelsView();   // in-progress pulls redraw their inline bars from _pullState
}
async function refreshModelsView() {
  if (_modelsTab === "all") await loadLibrary();
  else await loadAvailable();
}
function setModelsTab(tab) {
  _modelsTab = tab;
  $$("#models-tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  $("#lib-search").hidden = tab !== "all";
  refreshModelsView();
}
const _FIT = {
  fits:    { label: "fits your GPU", cls: "fit-ok" },
  tight:   { label: "tight fit",     cls: "fit-tight" },
  spills:  { label: "uses CPU (slow)", cls: "fit-bad" },
  unknown: { label: "",              cls: "" },
};
const _pulling = new Set();
const _pullCtl = {};     // model name -> AbortController
const _pullState = {};   // model name -> { pct, text, err } (latest progress, survives re-render)
function cancelPull(name) {
  const c = _pullCtl[name];
  if (c) { try { c.abort(); } catch (e) {} }
}
async function removeModel(name) {
  if (!confirm(`Remove ${name}?\n\nThis deletes the downloaded model from this computer. You can re-install it anytime.`)) return;
  try {
    await post("/api/remove", { name });
    toast(`${name} removed`);
    await refreshStatus(); renderModelMenu(); await refreshModelsView();
  } catch (e) { toast(`Couldn't remove ${name}: ${e.message}`, true); }
}
function _inlineNode(name) {
  const st = _pullState[name] || { text: "starting\u2026", pct: null };
  const bar = h("div", { class: "pbar" + (st.pct == null ? " indet" : "") },
                h("i", st.pct != null ? { style: `width:${st.pct}%` } : {}));
  const stat = h("div", { class: "pi-stat" }, st.text || "working\u2026");
  return h("div", { class: "pull-inline" + (st.err ? " err" : ""), "data-prog": name }, bar, stat);
}
function _paintPull(name) {   // update the inline bar in place, without rebuilding the list
  const st = _pullState[name]; if (!st) return;
  const wrap = document.querySelector(`[data-prog="${CSS.escape(name)}"]`); if (!wrap) return;
  wrap.classList.toggle("err", !!st.err);
  const bar = wrap.querySelector(".pbar"), fill = wrap.querySelector(".pbar > i"), stat = wrap.querySelector(".pi-stat");
  if (st.pct != null) { bar.classList.remove("indet"); fill.style.width = st.pct + "%"; }
  else { bar.classList.add("indet"); fill.style.width = ""; }
  stat.textContent = st.text || "working\u2026";
}
async function loadAvailable() {
  let data; try { data = await api("/api/available"); } catch (e) { return; }
  const box = $("#available-list"); box.innerHTML = "";
  const hdr = $("#models-vram");
  if (hdr) hdr.textContent = data.vram_gb
    ? `Your GPU has ${data.vram_gb} GB. Everything below is picked to run on it \u2014 badges show how each one fits.`
    : "Models below are small enough for a typical laptop GPU.";
  const cats = [];
  data.models.forEach(m => { if (!cats.includes(m.category)) cats.push(m.category); });
  cats.forEach(cat => {
    box.append(h("div", { class: "avail-cat" }, cat));
    data.models.filter(m => m.category === cat).forEach(m => {
      const f = _FIT[m.fit] || _FIT.unknown;
      const pulling = _pulling.has(m.name);
      let action;
      if (m.installed) action = h("div", { class: "ai-actions" },
        h("span", { class: "ai-installed" }, "\u2713 Installed"),
        h("button", { class: "btn ghost sm", title: "Remove this model", onclick: () => removeModel(m.name) }, "Remove"));
      else if (pulling) action = h("button", { class: "btn danger", onclick: () => cancelPull(m.name) }, "Cancel");
      else action = h("button", { class: "btn", onclick: () => pullModel(m.name) }, "Pull");
      const item = h("div", { class: "avail-item lib-item" },
        h("div", { class: "ai-row" },
          h("div", { class: "ai-main" },
            h("div", { class: "ai-name" }, m.name,
              f.label ? h("span", { class: "fit-badge " + f.cls }, f.label) : ""),
            h("div", { class: "ai-sub" }, `${m.size} \u00b7 ${m.desc}`)),
          action));
      _wireTip(item, m.name.split(":")[0], m.desc);   // full description on hover
      if (pulling) item.append(_inlineNode(m.name));   // bar under the description, above the divider
      box.append(item);
    });
  });
}
let _tipEl = null;
function _tip() {
  if (!_tipEl) { _tipEl = h("div", { class: "lib-tip" }); _tipEl.hidden = true; document.body.append(_tipEl); }
  return _tipEl;
}
const _about = {};               // model name -> full description text
const _aboutLoading = new Set();
async function _ensureAbout(name, fallback) {
  if (name in _about || _aboutLoading.has(name)) return;
  _aboutLoading.add(name);
  try { const d = await api("/api/modelinfo?name=" + encodeURIComponent(name)); _about[name] = d.about || fallback; }
  catch (e) { _about[name] = fallback; }
  _aboutLoading.delete(name);
  if (_tipEl && !_tipEl.hidden && _tipEl.dataset.for === name) _tipEl.textContent = _about[name];
}
function _wireTip(el, name, fallback) {
  el.addEventListener("mouseenter", () => {
    if (state.settings && state.settings.hover_tips === false) return;
    const t = _tip(); t.dataset.for = name;
    t.textContent = _about[name] || fallback || name;
    t.hidden = false;
    _ensureAbout(name, fallback);
  });
  el.addEventListener("mousemove", e => {
    const t = _tip(); const pad = 14;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + t.offsetWidth + 8 > innerWidth) x = innerWidth - t.offsetWidth - 8;
    if (y + t.offsetHeight + 8 > innerHeight) y = e.clientY - t.offsetHeight - pad;
    t.style.left = x + "px"; t.style.top = y + "px";
  });
  el.addEventListener("mouseleave", () => { if (_tipEl) _tipEl.hidden = true; });
}
async function loadLibrary() {
  const box = $("#available-list");
  if (!_libModels) { box.innerHTML = ""; box.append(h("div", { class: "lib-note" }, "Loading the Ollama library…")); }
  let data;
  try { data = await api("/api/library"); }
  catch (e) { box.innerHTML = ""; box.append(h("div", { class: "lib-note" }, "Couldn't reach ollama.com. Check the connection and try again.")); return; }
  _libModels = data.models; _libVram = data.vram_gb;
  const hdr = $("#models-vram");
  if (hdr) hdr.textContent = data.vram_gb
    ? `The whole Ollama library (${data.models.length} models). Your GPU has ${data.vram_gb} GB — size chips are colored by fit; click one to install it.`
    : `The whole Ollama library (${data.models.length} models). Click a size to install it.`;
  renderLibrary();
}
let _libVram = null;
function renderLibrary() {
  if (!_libModels) return;
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  const box = $("#available-list"); box.innerHTML = "";
  const list = q ? _libModels.filter(m => m.name.toLowerCase().includes(q) || (m.desc || "").toLowerCase().includes(q)) : _libModels;
  if (!list.length) { box.append(h("div", { class: "lib-note" }, `No models match “${q}”.`)); return; }
  if (_tipEl) _tipEl.hidden = true;
  list.forEach(m => {
    const caps = (m.caps || []).map(c => h("span", { class: "cap-chip" }, c));
    const item = h("div", { class: "avail-item lib-item", "data-model": m.name },
      h("div", { class: "ai-row" },
        h("div", { class: "ai-main" },
          h("div", { class: "ai-name" }, m.name, ...caps),
          h("div", { class: "ai-sub" }, m.desc || ""))));
    _wireTip(item, m.name, m.desc || "");   // full description on hover
    if (m.sizes && m.sizes.length) {
      const chips = h("div", { class: "size-chips" });
      m.sizes.forEach(s => {
        const full = `${m.name}:${s.tag}`;
        const fitCls = { fits: "fit-ok", tight: "fit-tight", spills: "fit-bad" }[s.fit] || "";
        if (s.installed) chips.append(h("span", { class: "size-chip done removable", title: "Installed — click to remove", onclick: () => removeModel(full) }, s.tag, " ✓"));
        else if (_pulling.has(full)) chips.append(h("span", { class: "size-chip pulling", title: "Click to cancel", onclick: () => cancelPull(full) }, s.tag, " ✕"));
        else chips.append(h("span", { class: "size-chip " + fitCls, title: `${s.size || "?"} · ${_FIT[s.fit] ? _FIT[s.fit].label : ""}`, onclick: () => pullModel(full) },
          s.tag, s.size ? h("span", { class: "cg" }, s.size) : ""));
      });
      item.append(chips);
    } else if (!m.installed) {
      item.append(h("div", { class: "size-chips" }, h("span", { class: "size-chip", onclick: () => pullModel(m.name) }, "install")));
    }
    // inline progress bar(s) for any tag of this model currently pulling
    _pulling.forEach(full => { if (full === m.name || full.startsWith(m.name + ":")) item.append(_inlineNode(full)); });
    box.append(item);
  });
}
async function pullModel(name) {
  name = (name || "").trim(); if (!name) return;
  if (_pulling.has(name)) { toast(`${name} is already downloading`); return; }
  _pulling.add(name);
  _pullState[name] = { pct: null, text: "starting\u2026", err: false };
  const ctl = new AbortController(); _pullCtl[name] = ctl;
  await refreshModelsView();   // flip button/chip to Cancel and draw the inline bar
  let errored = false, errMsg = "", canceled = false;
  try {
    const resp = await fetch("/api/pull", { method: "POST", headers: { "X-Hearth": "1", "Content-Type": "application/json" }, body: JSON.stringify({ name }), signal: ctl.signal });
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const ev = (raw.match(/^event: (.*)$/m) || [])[1];
        const dl = (raw.match(/^data: (.*)$/m) || [])[1];
        if (!ev) continue; const d = dl ? JSON.parse(dl) : {};
        if (ev === "progress") {
          const pct = (d.total && d.completed) ? Math.floor(d.completed / d.total * 100) : null;
          if (pct != null) {
            const gb = d.total ? ` \u00b7 ${(d.completed / 1e9).toFixed(1)}/${(d.total / 1e9).toFixed(1)} GB` : "";
            _pullState[name] = { pct, text: `${d.status || "downloading"} \u2014 ${pct}%${gb}`, err: false };
          } else {
            _pullState[name] = { pct: null, text: d.status || "working\u2026", err: false };
          }
        } else if (ev === "error") {
          errored = true; errMsg = d.message || "download failed";
          _pullState[name] = { pct: null, text: `\u26a0 ${errMsg}`, err: true };
        } else if (ev === "done") {
          _pullState[name] = { pct: 100, text: "installed \u2713", err: false };
        }
        _paintPull(name);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") { canceled = true; }
    else { errored = true; errMsg = e.message; }
  } finally {
    _pulling.delete(name); delete _pullCtl[name]; delete _pullState[name];
    if (_modelsTab === "all") { _libModels = null; await loadLibrary(); }   // re-fetch to refresh installed flags
    else await loadAvailable();   // row becomes "Installed" (or reverts to Pull); inline bar removed
  }
  if (canceled) { toast(`${name} canceled`); }
  else if (errored) { toast(`${name}: ${errMsg}`); }
  else { await refreshStatus(); renderModelMenu(); toast(`${name} ready`); }
}

function renderModelMenu() {
  const f = $("#model-filter").value.toLowerCase();
  const cap = state.capFilter;
  const cur = currentModel();
  const auto = h("button", { class: `model-item${cur === AUTO ? " sel" : ""}`, onclick: () => chooseModel(AUTO) },
    h("div", { class: "mi-main" }, h("div", { class: "mi-name" }, icon("spark"), " Auto"),
      h("div", { class: "mi-sub" }, "Picks the lightest model that can handle each message — small for quick questions, coder for code, vision for images")),
    icon("check", "mi-check"));
  const note = cap ? h("button", { class: "cap-filter-note", onclick: () => openModelMenu(null) },
    icon(CAP_ICONS[cap][0]), h("span", {}, `${CAP_ICONS[cap][1]} models`), h("span", { class: "clear" }, "show all ✕")) : null;
  const models = state.models
    .filter(m => m.name.toLowerCase().includes(f))
    .filter(m => !cap || (m.caps || []).includes(cap));
  $("#model-list").replaceChildren(...(note ? [note] : []), ...(!cap && (!f || "auto".includes(f)) ? [auto] : []), ...models.map(m => {
    const btn = h("button", { class: `model-item${m.name === cur ? " sel" : ""}`, onclick: () => chooseModel(m.name) },
      h("div", { class: "mi-main" },
        h("div", { class: "mi-name" }, shortName(m.name)),
        h("div", { class: "mi-sub" },
          ...m.caps.filter(c => CAP_ICONS[c]).map(c => h("span", { class: "cap" }, icon(CAP_ICONS[c][0]), CAP_ICONS[c][1])),
          `${m.params || ""}${m.params ? " · " : ""}${fmtSize(m.size)} · ${fmtK(m.ctx_max)} context`)),
      icon("check", "mi-check"));
    _wireTip(btn, m.name.split(":")[0], shortName(m.name));   // full description on hover
    return btn;
  }));
  if (!state.models.length) $("#model-list").append(h("div", { class: "empty-list" }, "No models. Run: ollama pull qwen2.5:3b"));
}

async function chooseModel(name) {
  closeMenus();
  state.settings = await post("/api/settings", { model: name });
  if (state.chat) {
    state.chat.model = name;
    await api(`/api/chats/${state.chat.id}`, { method: "PATCH", body: JSON.stringify({ model: name }) });
  }
  renderModelButton();
  toast(name === AUTO ? "Auto: the lightest capable model for each message" : `Using ${shortName(name)}`);
}

/* =========================================================== context meter */
function updateMeter() {
  const m = currentModel();
  const ctx = state.chat?.stats?.ctx || state.settings.model_ctx?.[m] || state.settings.num_ctx || 8192;
  const used = state.chat?.stats ? (state.chat.stats.prompt_tokens || 0) + (state.chat.stats.output_tokens || 0) : 0;
  const frac = Math.min(1, used / ctx);
  $("#ring-fill").style.strokeDashoffset = 88 - 88 * frac;
  $("#ring-fill").style.stroke = frac > (state.settings.compact_at || 0.75) - 0.05 ? "var(--bad)" : "var(--ember)";
  $("#ctx-text").textContent = `${fmtK(used)} / ${fmtK(ctx)}`;
  const sum = state.chat?.summary ? ` Older messages have been summarized ${state.chat.summary.count}×.` : "";
  $("#ctx-meter").title = `Context: ${used.toLocaleString()} of ${ctx.toLocaleString()} tokens used.` +
    ` Hearth summarizes older messages automatically at ${Math.round((state.settings.compact_at || 0.75) * 100)}%.${sum}\nClick to summarize now.`;
}

/* =========================================================== chat list */
function groupOf(ts, pinned) {
  if (pinned) return "Pinned";
  const d = new Date(ts * 1000), now = new Date();
  const day = 864e5, start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return "Today";
  if (d >= start - day) return "Yesterday";
  if (d >= start - 7 * day) return "Previous 7 days";
  if (d >= start - 30 * day) return "Previous 30 days";
  return d.toLocaleString(undefined, { month: "long", year: "numeric" });
}

function renderChatList() {
  const q = $("#chat-search").value.trim().toLowerCase();
  const list = $("#chat-list");
  const chats = state.chats.filter(c => !q || c.title.toLowerCase().includes(q) || (c.preview || "").toLowerCase().includes(q));
  list.replaceChildren();
  if (!chats.length) { list.append(h("div", { class: "empty-list" }, q ? "No chats match." : "Your conversations will appear here.")); return; }
  let last = null;
  for (const c of chats) {
    const g = groupOf(c.updated, c.pinned);
    if (g !== last) { list.append(h("div", { class: "group-label" }, g)); last = g; }
    const active = state.chat?.id === c.id;
    const more = h("span", { class: "more", title: "More", onclick: e => { e.stopPropagation(); chatMenu(c, e.currentTarget); } }, icon("dots"));
    list.append(h("button", { class: `chat-item${active ? " active" : ""}`, "data-id": c.id, onclick: () => openChat(c.id), title: c.preview || c.title },
      c.pinned ? icon("pin", "pin") : null,
      h("span", { class: "t" }, c.title),
      active && state.streaming ? h("span", { class: "busy" }) : null,
      more));
  }
}

function chatMenu(c, anchor) {
  const m = $("#chat-menu");
  const r = anchor.getBoundingClientRect();
  m.replaceChildren(
    h("button", { class: "menu-item", onclick: () => { closeMenus(); renameInList(c); } }, icon("pencil"), "Rename"),
    h("button", { class: "menu-item", onclick: async () => { closeMenus(); await patchChat(c.id, { pinned: !c.pinned }); } }, icon("pin"), c.pinned ? "Unpin" : "Pin"),
    h("button", { class: "menu-item danger", onclick: () => { closeMenus(); deleteChat(c); } }, icon("trash"), "Delete"));
  m.hidden = false;
  m.style.left = `${Math.min(r.left, innerWidth - 190)}px`;
  m.style.top = `${r.bottom + 4}px`;
}

async function patchChat(id, body) {
  await api(`/api/chats/${id}`, { method: "PATCH", body: JSON.stringify(body) });
  if (state.chat?.id === id) Object.assign(state.chat, body);
  await loadChats();
  if (state.chat?.id === id) $("#chat-title").textContent = state.chat.title;
}

function renameInList(c) {
  const item = $(`.chat-item[data-id="${c.id}"]`);
  if (!item) return;
  const input = h("input", { value: c.title });
  item.querySelector(".t").replaceWith(input);
  input.focus(); input.select();
  const done = async save => {
    input.onblur = null;
    if (save && input.value.trim() && input.value.trim() !== c.title) await patchChat(c.id, { title: input.value.trim() });
    else renderChatList();
  };
  input.onkeydown = e => { e.stopPropagation(); if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  input.onclick = e => e.stopPropagation();
  input.onblur = () => done(true);
}

async function deleteChat(c) {
  if (!confirm(`Delete “${c.title}”? This can't be undone.`)) return;
  await api(`/api/chats/${c.id}`, { method: "DELETE" });
  if (state.chat?.id === c.id) newChat();
  await loadChats();
}

async function loadChats() {
  state.chats = await api("/api/chats");
  renderChatList();
}

/* =========================================================== open / new chat */
function updateIncognitoUI() {
  const on = !!(state.incognito || (state.chat && state.chat.incognito));
  const b = document.getElementById("incognito-banner"); if (b) b.hidden = !on;
  const btn = document.getElementById("new-private"); if (btn) btn.classList.toggle("on", on);
}
function newChat(incognito = false) {
  if (state.streaming) stopStreaming();
  if (state.chat && state.chat.incognito) api(`/api/chats/${state.chat.id}`, { method: "DELETE" }).catch(() => {});
  state.incognito = !!incognito;
  state.chat = null;
  localStorage.removeItem("hearth.chat");
  $("#chat-title").textContent = "";
  updateIncognitoUI();
  renderThread();
  renderModelButton();
  renderChatList();
  $("#input").focus();
  closeSidebarNarrow();
}

async function openChat(id) {
  if (state.streaming && state.chat?.id !== id) stopStreaming();
  if (state.chat && state.chat.incognito && state.chat.id !== id) api(`/api/chats/${state.chat.id}`, { method: "DELETE" }).catch(() => {});
  try {
    state.chat = await api(`/api/chats/${id}`);
  } catch { localStorage.removeItem("hearth.chat"); return newChat(); }
  state.incognito = !!state.chat.incognito;
  localStorage.setItem("hearth.chat", id);
  updateIncognitoUI();
  $("#chat-title").textContent = state.chat.title;
  state.stick = true;
  renderThread();
  renderModelButton();
  renderChatList();
  closeSidebarNarrow();
}

/* =========================================================== thread rendering */
function greeting() {
  const hr = new Date().getHours();
  const part = hr < 5 ? "Up late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
  return state.settings.user_name ? `${part}, ${state.settings.user_name}` : part;
}

function renderEmpty() {
  const ideas = [
    ["globe", "What's happening today", "Search the web for the latest news on a topic", "Search the web for today's top technology news and summarize the three biggest stories with links."],
    ["file", "Summarize a document", "Attach a PDF, doc or log and get the key points", null],
    ["tools", "Write a script", "Bash, Python or anything else — with explanations", "Write a bash script that backs up a folder to a USB drive with a timestamped name, and explain each part."],
    ["spark", "Think it through", "Plan a project, compare options, make a decision", "Help me plan a weekend homelab project. Ask me a few questions first."],
    ["image", "Create an image", "Describe a picture and it's made on this computer", "Create a picture of ", true],
  ];
  return h("div", { class: "hello" },
    h("img", { src: "/hearth.svg", alt: "" }),
    h("h1", {}, greeting()),
    h("p", {}, state.models.length ? "What would you like to talk about?" : "Install a model with Ollama to get started."),
    projectsCard(),
    h("div", { class: "ideas" }, ideas.map(([ic, title, sub, prompt, fillOnly]) =>
      h("button", { class: "idea", onclick: () => {
          if (fillOnly) { const i = $("#input"); i.value = prompt; autosize(); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
          else if (prompt) { $("#input").value = prompt; autosize(); sendMessage(); } else $("#file-input").click(); } },
        h("b", {}, icon(ic), title), h("span", {}, sub)))));
}

function attachmentView(ids, meta, align = "end") {
  if (!ids?.length) return null;
  return h("div", { class: "att-row", style: `justify-content:flex-${align}` }, ids.map(id => {
    const a = meta?.[id];
    if (!a) return null;
    if (a.kind === "image") {
      const img = h("img", { class: "att-thumb", src: `/api/uploads/${id}`, alt: a.name, title: a.name });
      img.onclick = () => lightbox(img.src);
      return img;
    }
    return h("span", { class: "file-chip" }, icon("file"), h("span", { class: "fc-name" }, a.name), h("span", { class: "fc-sub" }, fmtSize(a.size)));
  }));
}

function lightbox(src) {
  const lb = h("div", { class: "lightbox", onclick: () => lb.remove() }, h("img", { src }));
  document.body.append(lb);
}

function toolCard(call, result, running = false) {
  const status = running ? h("span", { class: "spinner" }) :
    result?.ok === false ? icon("alert", "tool-status tool-bad") : icon("check", "tool-status tool-ok");
  const body = h("div", { class: "fold-body" });
  fillToolBody(body, call, result);
  const d = h("details", { class: "fold tool", "data-call": call.id },
    h("summary", {}, icon(call.name === "web_search" ? "search" : call.name === "fetch_url" ? "globe" : ["generate_image", "edit_image", "replace_background"].includes(call.name) ? "image" :
      ["remember", "forget"].includes(call.name) ? "brain" : "tools"), h("span", {}, call.label || call.name), status, icon("down", "chev")),
    body);
  let data = result?.data ?? result;
  if (typeof data === "string") { try { data = JSON.parse(data); } catch {} }
  if (["generate_image", "edit_image", "replace_background"].includes(call.name) && result?.ok !== false && data?.image_id) {
    const img = h("img", { class: "gen-image", src: `/api/uploads/${data.image_id}`, alt: data.prompt || "Generated image", title: data.prompt || "" });
    img.onclick = () => lightbox(img.src);
    return h("div", { class: "gen-wrap", "data-call": call.id }, d, img);
  }
  return d;
}

function fillToolBody(body, call, result) {
  body.replaceChildren();
  let data = result?.data ?? result;
  if (typeof data === "string") { try { data = JSON.parse(data); } catch {} }
  if (!data) { body.append(h("div", { class: "help" }, "Working…")); return; }
  if (data.error) { body.append(h("div", {}, `Problem: ${data.error}`)); return; }
  if (call.name === "web_search" && Array.isArray(data.results)) {
    body.append(h("ul", { class: "result-list" }, data.results.map(r =>
      h("li", {}, h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, r.title || r.url),
        h("div", { class: "r-url" }, r.url), r.snippet ? h("div", { class: "r-snip" }, r.snippet) : null))));
    return;
  }
  if (call.name === "fetch_url" && data.url) {
    body.append(h("div", {}, h("a", { href: data.url, target: "_blank", rel: "noopener noreferrer" }, data.title || data.url)),
      h("div", { class: "help" }, `${(data.total_chars || 0).toLocaleString()} characters read${data.truncated ? " (trimmed)" : ""}`));
    return;
  }
  if (call.name === "remember" && data.fact) { body.append(h("div", {}, `“${data.fact}”`)); return; }
  if (call.name === "calculate" && "result" in data) { body.append(h("div", {}, `${data.expression} = `, h("b", {}, String(data.result)))); return; }
  if (call.name === "get_datetime") { body.append(h("div", {}, data.datetime)); return; }
  if (["generate_image", "edit_image", "replace_background"].includes(call.name) && data.image_id) {
    body.append(h("div", {}, `“${data.prompt}”`), h("div", { class: "help" }, `${data.size} · ${data.seconds}s on this computer`));
    return;
  }
  body.append(h("pre", { class: "tool-json" }, JSON.stringify(data, null, 2)));
}

function thinkingFold(text, seconds, live = false) {
  const label = live ? "Thinking…" : `Thought for ${seconds ? `${seconds}s` : "a moment"}`;
  return h("details", { class: "fold thinking" },
    h("summary", {}, icon("spark"), h("span", { class: "think-label" }, label), live ? h("span", { class: "spinner" }) : null, icon("down", "chev")),
    h("div", { class: "fold-body" }, text));
}

/** Group stored messages into user turns and assistant turns (assistant + tool messages). */
function turns(messages) {
  const out = [];
  messages.forEach((m, i) => {
    if (m.role === "user") out.push({ kind: "user", msg: m, index: i });
    else {
      let t = out[out.length - 1];
      if (!t || t.kind !== "assistant") { t = { kind: "assistant", parts: [], index: i }; out.push(t); }
      t.parts.push(m);
    }
  });
  return out;
}

function renderThread() {
  const box = $("#messages");
  box.replaceChildren();
  const chat = state.chat;
  if (!chat || !chat.messages.length) { box.append(renderEmpty()); return; }
  const list = turns(chat.messages);
  const upto = chat.summary?.upto ?? -1;
  let dividerDone = false;
  list.forEach((t, n) => {
    if (!dividerDone && upto > 0 && t.index >= upto) {
      box.append(summaryDivider());
      dividerDone = true;
    }
    box.append(t.kind === "user" ? userMsg(t) : assistantMsg(t, n === list.length - 1));
  });
  if (!dividerDone && upto > 0) box.append(summaryDivider());
  scrollBottom(true);
}

function summaryDivider() {
  return h("div", { class: "divider" }, h("button", { onclick: showSummary }, icon("layers"),
    `Earlier messages summarized${state.chat.summary.count > 1 ? ` (${state.chat.summary.count}×)` : ""} · view summary`));
}

function showSummary() {
  renderMarkdown($("#summary-body"), state.chat?.summary?.text || "No summary yet.");
  $("#summary-modal").hidden = false;
}

function userMsg(t) {
  const m = t.msg;
  const col = h("div", { class: "user-col" },
    attachmentView(m.attachments, state.chat.attachments),
    m.content ? h("div", { class: "bubble" }, m.content) : null,
    h("div", { class: "actions" },
      h("button", { class: "act", title: "Copy", onclick: e => copyText(m.content || "", e.currentTarget) }, icon("copy")),
      h("button", { class: "act", title: "Edit and resend", onclick: () => editMessage(t, col) }, icon("pencil"))));
  return h("div", { class: "msg user" }, col);
}

function editMessage(t, col) {
  if (state.streaming) return;
  const ta = h("textarea", { class: "bubble", style: "width:100%;min-width:320px;resize:vertical;outline:0" });
  ta.value = t.msg.content || "";
  const save = h("button", { class: "btn primary" }, "Save & resend");
  const cancel = h("button", { class: "btn" }, "Cancel");
  const editor = h("div", { class: "user-col", style: "width:78%" }, ta, h("div", { style: "display:flex;gap:8px" }, cancel, save));
  col.replaceWith(editor);
  ta.focus();
  cancel.onclick = () => editor.replaceWith(col);
  save.onclick = () => {
    const text = ta.value.trim();
    if (!text) return;
    streamReply({ text, attachments: t.msg.attachments || [], edit_from: t.index });
  };
}

function assistantMsg(t, isLast) {
  const col = h("div", { class: "assistant-col" });
  const toolResults = {};
  for (const p of t.parts) if (p.role === "tool") toolResults[p.call_id] = { ok: p.ok, data: p.content };
  let fullText = "";
  let stats = null;
  for (const p of t.parts) {
    if (p.role !== "assistant") continue;
    if (p.thinking) col.append(thinkingFold(p.thinking, p.think_seconds));
    if (p.content) {
      const md = h("div", { class: "md" });
      renderMarkdown(md, p.content);
      col.append(md);
      fullText += (fullText ? "\n\n" : "") + p.content;
    }
    for (const c of p.tool_calls || []) col.append(toolCard(c, toolResults[c.id]));
    if (p.error) col.append(h("div", { class: "error-card" }, icon("alert"), h("div", {}, p.error)));
    if (p.stats && p.stats.output_tokens) stats = p.stats;
  }
  const last = t.parts.filter(p => p.role === "assistant").pop();
  col.append(h("div", { class: "actions" },
    h("button", { class: "act", title: "Copy", onclick: e => copyText(fullText, e.currentTarget) }, icon("copy")),
    isLast ? h("button", { class: "act", title: "Regenerate", onclick: regenerate }, icon("refresh")) : null,
    h("span", { class: "stats", title: last?.auto ? `Chosen automatically: ${last.auto}` : "" }, [last?.model ? (last.auto ? `Auto → ${shortName(last.model)}` : shortName(last.model)) : "", stats?.tok_per_s ? `${stats.tok_per_s} tok/s` : "",
      last?.stopped ? "stopped" : ""].filter(Boolean).join(" · "))));
  return h("div", { class: `msg assistant${isLast ? " last" : ""}` }, h("img", { class: "avatar", src: "/hearth.svg", alt: "" }), col);
}

/* =========================================================== scrolling */
const thread = () => $("#thread");
function scrollBottom(force = false) {
  if (force || state.stick) requestAnimationFrame(() => { thread().scrollTop = thread().scrollHeight; });
}

/* =========================================================== sending & streaming */
function composerText() { return $("#input").value.trim(); }
function refreshSend() {
  const uploading = state.pending.some(p => p.loading);
  const btn = $("#send-btn");
  if (state.streaming) {
    btn.disabled = false; btn.classList.add("stop"); btn.title = "Stop";
    btn.querySelector("use").setAttribute("href", "#i-stop");
  } else {
    btn.classList.remove("stop"); btn.title = "Send (Enter)";
    btn.querySelector("use").setAttribute("href", "#i-send");
    btn.disabled = uploading || (!composerText() && !state.pending.some(p => p.id));
  }
}

async function sendMessage() {
  if (state.streaming) return stopStreaming();
  const text = composerText();
  const atts = state.pending.filter(p => p.id).map(p => p.id);
  if ((!text && !atts.length) || state.pending.some(p => p.loading)) return;
  if (!state.models.length) return toast("No models installed yet", true);
  const attMeta = Object.fromEntries(state.pending.filter(p => p.id).map(p => [p.id, p.meta]));
  $("#input").value = "";
  state.pending = [];
  renderPending();
  autosize();
  await streamReply({ text, attachments: atts }, attMeta);
}

function regenerate() { if (!state.streaming) streamReply({ regenerate: true }); }

async function streamReply(body, attMeta = {}) {
  if (!state.chat) {
    state.chat = await post("/api/chats", { model: currentModel(), incognito: state.incognito });
    state.chat.attachments = {};
    if (!state.chat.incognito) localStorage.setItem("hearth.chat", state.chat.id);
    updateIncognitoUI();
  }
  const chat = state.chat;
  Object.assign(chat.attachments = chat.attachments || {}, attMeta);
  // show the new user message immediately
  if (!body.regenerate) {
    if (body.edit_from != null) chat.messages = chat.messages.slice(0, body.edit_from);
    chat.messages.push({ role: "user", content: body.text, attachments: body.attachments });
  } else {
    while (chat.messages.length && chat.messages[chat.messages.length - 1].role !== "user") chat.messages.pop();
  }
  renderThread();
  state.stick = true;

  // live assistant bubble
  const col = h("div", { class: "assistant-col" });
  const liveMsg = h("div", { class: "msg assistant last" }, h("img", { class: "avatar", src: "/hearth.svg", alt: "" }), col);
  const typing = h("div", { class: "typing" }, h("i"), h("i"), h("i"));
  col.append(typing);
  $("#messages").append(liveMsg);
  scrollBottom(true);

  let seg = null, segText = "", think = null, thinkText = "", pendingRender = false;
  const liveCalls = {};
  const ensureSeg = () => {
    if (!seg) { seg = h("div", { class: "md cursor" }); col.append(seg); segText = ""; }
    typing.remove();
    return seg;
  };
  const flush = () => {
    pendingRender = false;
    if (seg) renderMarkdown(seg, segText, true);
    scrollBottom();
  };
  const endThinking = () => {
    if (think && think.querySelector(".spinner")) {
      think.querySelector(".spinner").remove();
      think.querySelector(".think-label").textContent = "Thought for a moment";
    }
  };

  state.streaming = true;
  state.abort = new AbortController();
  refreshSend();
  renderChatList();
  body.model = currentModel();
  let resp;
  try {
    resp = await fetch(`/api/chats/${chat.id}/send`, {
      method: "POST", signal: state.abort.signal,
      headers: { "X-Hearth": "1", "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || resp.statusText);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = (raw.match(/^event: (.*)$/m) || [])[1];
        const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
        if (!ev) continue;
        const data = dataLine ? JSON.parse(dataLine) : {};
        if (!["status", "routed"].includes(ev)) $$(".load-note", col).forEach(e => e.remove());
        switch (ev) {
          case "routed":
            if (typing.isConnected) typing.after(h("div", { class: "help load-note" }, `Auto chose ${shortName(data.model)} · ${data.reason}`));
            $("#model-name").textContent = `Auto · ${shortName(data.model)}`;
            break;
          case "status":
            if (typing.isConnected) typing.after(h("div", { class: "help load-note" }, data.text));
            break;
          case "thinking":
            if (!think) {
              think = thinkingFold("", 0, true);
              if (typing.isConnected) col.insertBefore(think, typing); else col.append(think);
              typing.remove();
            }
            thinkText += data.text;
            think.querySelector(".fold-body").textContent = thinkText;
            break;
          case "token":
            endThinking();
            ensureSeg();
            segText += data.text;
            if (!pendingRender) { pendingRender = true; requestAnimationFrame(flush); }
            break;
          case "retract":
            if (seg) { seg.remove(); seg = null; segText = ""; }
            break;
          case "tool_start":
            endThinking();
            if (seg) { renderMarkdown(seg, segText); seg = null; }
            typing.remove();
            liveCalls[data.id] = { id: data.id, name: data.name, label: data.label };
            col.append(toolCard(liveCalls[data.id], null, true));
            scrollBottom();
            break;
          case "tool_result": {
            const card = col.querySelector(`[data-call="${data.id}"]`);
            if (card) card.replaceWith(toolCard(liveCalls[data.id] || { id: data.id, name: "tool" }, { ok: data.ok, data: data.result }));
            think = null; thinkText = "";
            break;
          }
          case "compacting":
            col.append(h("div", { class: "divider compacting" }, h("span", {}, icon("layers"), " Summarizing earlier messages to make room…")));
            scrollBottom();
            break;
          case "compacted":
            $$(".compacting", col).forEach(e => e.remove());
            chat.summary = { text: data.summary, upto: data.upto, count: data.count };
            toast("Older messages summarized — the chat can keep going");
            break;
          case "memory":
            state.memories = data.memories;
            renderMemories();
            break;
          case "title":
            chat.title = data.title;
            $("#chat-title").textContent = data.title;
            loadChats();
            break;
          case "done":
            chat.stats = data.stats;
            if (seg) renderMarkdown(seg, segText);
            updateMeter();
            break;
          case "error":
            typing.remove();
            col.append(h("div", { class: "error-card" }, icon("alert"), h("div", {}, data.message)));
            break;
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      typing.remove();
      col.append(h("div", { class: "error-card" }, icon("alert"), h("div", {}, e.message)));
    }
  } finally {
    state.streaming = false;
    state.abort = null;
    refreshSend();
    // reload the canonical version (keeps tool results, stats and summaries in sync)
    try {
      const fresh = await api(`/api/chats/${chat.id}`);
      if (state.chat?.id === chat.id) {
        const hadError = col.querySelector(".error-card");
        state.chat = fresh;
        $("#chat-title").textContent = fresh.title;
        renderThread();
        if (hadError && !fresh.messages.at(-1)?.error && fresh.messages.at(-1)?.role === "user") {
          $("#messages").append(h("div", { class: "msg assistant last" }, h("img", { class: "avatar", src: "/hearth.svg", alt: "" }),
            h("div", { class: "assistant-col" }, hadError, h("div", { class: "actions" },
              h("button", { class: "act", title: "Try again", onclick: regenerate }, icon("refresh"))))));
        }
        updateMeter();
      }
    } catch {}
    loadChats();
  }
}

async function stopStreaming() {
  if (!state.chat) return;
  try { await post(`/api/chats/${state.chat.id}/stop`); } catch {}
  setTimeout(() => state.abort?.abort(), 400);
}

/* =========================================================== attachments */
function renderPending() {
  $("#attachments").replaceChildren(...state.pending.map(p => {
    const rm = h("button", { class: "rm", title: "Remove", onclick: () => { state.pending = state.pending.filter(x => x !== p); renderPending(); } }, icon("x"));
    const inner = p.preview ? h("img", { src: p.preview, alt: p.name, title: p.name }) :
      h("span", { class: "file-chip" }, icon("file"), h("span", {}, h("div", { class: "fc-name" }, p.name),
        h("div", { class: "fc-sub" }, p.error ? "Upload failed" : p.meta?.kind === "binary" ? "Can't be read" : fmtSize(p.size))));
    return h("div", { class: `pending${p.loading ? " loading" : ""}` }, inner, rm);
  }));
  refreshSend();
}

function uploadFiles(fileList) {
  for (const file of fileList) {
    if (!file || !file.size && !file.type) continue;
    const name = file.name && file.name !== "image.png" ? file.name : `pasted-${new Date().toISOString().slice(11, 19).replace(/:/g, "")}.${(file.type.split("/")[1] || "png").replace("jpeg", "jpg")}`;
    const p = { name, size: file.size, loading: true, preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : null };
    state.pending.push(p);
    renderPending();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("X-Hearth", "1");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(name));
    xhr.onload = () => {
      p.loading = false;
      if (xhr.status === 200) { p.meta = JSON.parse(xhr.responseText); p.id = p.meta.id; }
      else { p.error = true; toast(`Couldn't attach ${name}: ${(JSON.parse(xhr.responseText || "{}").error) || xhr.status}`, true); state.pending = state.pending.filter(x => x !== p); }
      renderPending();
    };
    xhr.onerror = () => { p.loading = false; state.pending = state.pending.filter(x => x !== p); renderPending(); toast(`Couldn't attach ${name}`, true); };
    xhr.send(file);
  }
  $("#input").focus();
}

/* drag & drop anywhere */
let dragDepth = 0;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false; });
addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
  uploadFiles(e.dataTransfer.files);
});

/* paste images / files from the clipboard */
addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  const inField = e.target.closest?.(".modal, .drawer");
  if (inField) return;
  e.preventDefault();
  uploadFiles(files);
});

/* =========================================================== composer */
function autosize() {
  const ta = $("#input");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  refreshSend();
}

/* =========================================================== memory */
function renderMemories() {
  $("#memory-count").textContent = state.memories.length;
  const list = $("#memory-list");
  if (!state.memories.length) {
    list.replaceChildren(h("div", { class: "empty-list" }, "Nothing yet. Tell Hearth about yourself — your name, projects, how you like answers — and it will remember."));
    return;
  }
  list.replaceChildren(...[...state.memories].reverse().map(m => h("div", { class: "memory" },
    h("div", { class: "m-text" }, m.text, h("div", { class: "m-sub" }, `${m.source === "user" ? "Added by you" : "Learned in chat"} · ${new Date(m.created * 1000).toLocaleDateString()}`)),
    h("button", { class: "act", title: "Forget", onclick: async () => {
      await api(`/api/memory/${m.id}`, { method: "DELETE" });
      state.memories = state.memories.filter(x => x.id !== m.id);
      renderMemories();
    } }, icon("trash")))));
}

/* =========================================================== settings */
function openSettings() {
  const s = state.settings;
  const m = currentModel();
  const info = modelInfo(m);
  $("#s-user_name").value = s.user_name || "";
  $("#s-system_prompt").value = s.system_prompt || "";
  $$("#s-theme button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.theme || "system")));
  const ctx = $("#s-ctx");
  ctx.max = Math.max(4096, Math.min(info.ctx_max || 8192, 131072));
  ctx.value = (m !== AUTO && s.model_ctx?.[m]) || s.num_ctx || 8192;
  $("#s-ctx-model").textContent = m === AUTO ? "all models (Auto)" : shortName(m) || "this model";
  $("#s-compact_at").value = s.compact_at ?? 0.75;
  $("#s-allow_local_urls").classList.toggle("on", !!s.allow_local_urls);
  $("#s-hover_tips").classList.toggle("on", s.hover_tips !== false);
  $("#s-keep_recent").value = s.keep_recent ?? 6;
  $("#s-internet").classList.toggle("on", s.internet !== false);
  $("#s-tools").classList.toggle("on", s.tools !== false);
  $("#s-browseable_nodes").value = (s.browseable_nodes || []).join("\n");
  $("#s-workspace_paths").value = (s.workspace_paths || []).join("\n");
  $("#s-version").textContent = `Hearth ${state.version || ""} · ${state.models.length} models`;
  renderPresets();
  loadGen("global");
  $("#settings-modal").hidden = false;
}
let genScope = "global";
function genForScope(scope) {
  const s = state.settings || {};
  const base = { temperature: s.temperature ?? 0.7, top_p: s.top_p ?? 0.9, top_k: s.top_k ?? 40,
                 reasoning: s.reasoning || (s.thinking === false ? "off" : "on"), stop: s.stop || [] };
  const m = currentModel();
  const modelOv = (s.model_settings || {})[m] || {};
  if (scope === "global") return { vals: base, custom: false, canEdit: true };
  if (scope === "model") return { vals: { ...base, ...modelOv }, custom: !!Object.keys(modelOv).length, canEdit: !!(m && m !== AUTO) };
  const chatOv = (state.chat && state.chat.settings) || {};
  return { vals: { ...base, ...modelOv, ...chatOv }, custom: !!Object.keys(chatOv).length, canEdit: !!state.chat };
}
function loadGen(scope) {
  genScope = scope;
  $$("#s-scope button").forEach(b => b.classList.toggle("on", b.dataset.scope === scope));
  const { vals, custom, canEdit } = genForScope(scope);
  $("#s-temperature").value = vals.temperature;
  $("#s-top_p").value = vals.top_p;
  $("#s-top_k").value = vals.top_k;
  $$("#s-reasoning button").forEach(b => b.classList.toggle("on", b.dataset.r === (vals.reasoning || "on")));
  $("#s-stop").value = (vals.stop || []).join("\n");
  const note = $("#s-scope-note"), mn = shortName(currentModel());
  if (scope === "global") note.textContent = "The default for every chat.";
  else if (scope === "model") note.textContent = canEdit ? (custom ? `Custom for ${mn}. Saving updates it.` : `Inheriting global. Saving pins these for ${mn}.`) : "Pick a specific model (not Auto) to set per-model values.";
  else note.textContent = canEdit ? (custom ? "Custom for this chat. Saving updates it." : "Inheriting. Saving pins these for this chat only.") : "Open or start a chat to set per-chat values.";
  $("#s-clear-scope").hidden = !(scope !== "global" && custom && canEdit);
  updateSettingLabels();
}
function renderPresets() {
  const box = $("#s-presets"); if (!box) return;
  box.innerHTML = "";
  Object.keys(state.presets || {}).forEach(name => {
    const b = document.createElement("button"); b.type = "button"; b.textContent = name;
    b.onclick = () => { const p = state.presets[name]; $("#s-temperature").value = p.temperature; $("#s-top_p").value = p.top_p; $("#s-top_k").value = p.top_k; updateSettingLabels(); };
    box.appendChild(b);
  });
}
async function clearScope() {
  const m = currentModel();
  if (genScope === "model" && m && m !== AUTO) { const ms = { ...(state.settings.model_settings || {}) }; delete ms[m]; state.settings = await post("/api/settings", { model_settings: ms }); }
  else if (genScope === "chat" && state.chat) { try { await api(`/api/chats/${state.chat.id}`, { method: "PATCH", body: JSON.stringify({ settings: {} }) }); } catch (e) {} delete state.chat.settings; }
  loadGen(genScope); toast("Reverted to global");
}
function updateSettingLabels() {
  $("#s-ctx-val").textContent = `${fmtK(+$("#s-ctx").value)} tokens`;
  $("#s-compact-val").textContent = `${Math.round($("#s-compact_at").value * 100)}%`;
  $("#s-temp-val").textContent = (+$("#s-temperature").value).toFixed(2);
  const kv = $("#s-keep-val"); if (kv) kv.textContent = `${$("#s-keep_recent").value} recent`;
  const pp = $("#s-topp-val"); if (pp) pp.textContent = (+$("#s-top_p").value).toFixed(2);
  const pk = $("#s-topk-val"); if (pk) pk.textContent = $("#s-top_k").value;
}
async function saveSettings() {
  const m = currentModel();
  const model_ctx = { ...(state.settings.model_ctx || {}) };
  const ctxVal = +$("#s-ctx").value;
  if (m && m !== AUTO) model_ctx[m] = ctxVal;
  const gen = {
    temperature: +$("#s-temperature").value,
    top_p: +$("#s-top_p").value,
    top_k: +$("#s-top_k").value,
    reasoning: $("#s-reasoning .on")?.dataset.r || "on",
    stop: $("#s-stop").value.split("\n").map(x => x.trim()).filter(Boolean),
  };
  const body = {
    user_name: $("#s-user_name").value.trim(),
    system_prompt: $("#s-system_prompt").value.trim(),
    theme: $("#s-theme .on")?.dataset.v || "system",
    model_ctx,
    ...(m === AUTO ? { num_ctx: ctxVal } : {}),
    compact_at: +$("#s-compact_at").value,
    allow_local_urls: $("#s-allow_local_urls").classList.contains("on"),
    hover_tips: $("#s-hover_tips").classList.contains("on"),
    keep_recent: +$("#s-keep_recent").value,
    internet: $("#s-internet").classList.contains("on"),
    tools: $("#s-tools").classList.contains("on"),
    browseable_nodes: $("#s-browseable_nodes").value.split("\n").map(s => s.trim()).filter(Boolean),
    workspace_paths: $("#s-workspace_paths").value.split("\n").map(s => s.trim()).filter(Boolean),
  };
  if (genScope === "global") Object.assign(body, gen);
  else if (genScope === "model" && m && m !== AUTO) body.model_settings = { ...(state.settings.model_settings || {}), [m]: gen };
  state.settings = await post("/api/settings", body);
  if (genScope === "chat" && state.chat) {
    try { await api(`/api/chats/${state.chat.id}`, { method: "PATCH", body: JSON.stringify({ settings: gen }) }); } catch (e) {}
    state.chat.settings = gen;
  }
  applyTheme();
  updateMeter();
  renderModelButton();
  $("#settings-modal").hidden = true;
  if (!state.chat) renderThread();
  toast("Settings saved");
}

/* =========================================================== sidebar / menus */
function closeMenus() {
  $("#model-menu").hidden = true;
  $("#model-btn").classList.remove("open");
  $("#chat-menu").hidden = true;
}
function toggleSidebar() {
  if (innerWidth <= 860) $("#app").classList.toggle("show-side");
  else {
    $("#app").classList.toggle("collapsed");
    localStorage.setItem("hearth.collapsed", $("#app").classList.contains("collapsed") ? "1" : "");
  }
}
function closeSidebarNarrow() { $("#app").classList.remove("show-side"); }

/* =========================================================== wiring */
function wire() {
  $("#new-chat").onclick = () => newChat(false);
  $("#new-private").onclick = (e) => { e.stopPropagation(); newChat(true); };
  $("#chat-search").oninput = renderChatList;
  $("#toggle-sidebar").onclick = toggleSidebar;
  $("#close-sidebar").onclick = closeSidebarNarrow;
  $("#scrim").onclick = closeSidebarNarrow;
  $("#model-btn").onclick = e => {
    e.stopPropagation();
    const willOpen = $("#model-menu").hidden;
    closeMenus();
    if (willOpen) openModelMenu(null);
  };
  $("#model-filter").oninput = renderModelMenu;
  $("#model-menu").onclick = e => e.stopPropagation();
  document.addEventListener("click", closeMenus);

  $("#toggle-tools").onclick = async () => {
    state.settings = await post("/api/settings", { tools: !(state.settings.tools !== false) });
    renderModelButton();
  };
  $("#toggle-web").onclick = async () => {
    const on = !(state.settings.tools !== false && state.settings.internet !== false);
    state.settings = await post("/api/settings", on ? { internet: true, tools: true } : { internet: false });
    renderModelButton();
    toast(on ? "Web access on" : "Web access off");
  };
  $("#ctx-meter").onclick = async () => {
    if (!state.chat || state.streaming) return;
    if (!confirm("Summarize the older part of this conversation now? Recent messages are kept word-for-word.")) return;
    toast("Summarizing…");
    try {
      const r = await post(`/api/chats/${state.chat.id}/compact`);
      if (!r.compacted) toast("Nothing to summarize yet");
      await openChat(state.chat.id);
    } catch (e) { toast(e.message, true); }
  };

  const title = $("#chat-title");
  title.ondblclick = () => {
    if (!state.chat) return;
    const input = h("input", { value: state.chat.title });
    title.replaceChildren(input);
    input.focus(); input.select();
    const done = async save => {
      input.onblur = null;
      const v = input.value.trim();
      title.textContent = state.chat.title;
      if (save && v && v !== state.chat.title) await patchChat(state.chat.id, { title: v });
    };
    input.onkeydown = e => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
    input.onblur = () => done(true);
  };

  const ta = $("#input");
  ta.addEventListener("input", autosize);
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!state.streaming) sendMessage(); }
  });
  $("#send-btn").onclick = () => state.streaming ? stopStreaming() : sendMessage();
  $("#attach-btn").onclick = () => $("#file-input").click();
  $("#file-input").onchange = e => { uploadFiles(e.target.files); e.target.value = ""; };

  thread().addEventListener("scroll", () => {
    const t = thread();
    state.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  });

  $("#open-memory").onclick = () => { $("#memory-drawer").hidden = false; $("#memory-input").focus(); };
  $("#memory-form").onsubmit = async e => {
    e.preventDefault();
    const v = $("#memory-input").value.trim();
    if (!v) return;
    await post("/api/memory", { text: v });
    state.memories = await api("/api/memory");
    $("#memory-input").value = "";
    renderMemories();
  };
  $("#open-settings").onclick = openSettings;
  $("#settings-save").onclick = saveSettings;
  $("#get-models").onclick = () => { closeMenus(); openModelsModal(); };
  $("#pull-btn").onclick = () => pullModel($("#pull-name").value);
  $("#pull-name").addEventListener("keydown", e => { if (e.key === "Enter") pullModel($("#pull-name").value); });
  $$("#models-tabs button").forEach(b => b.onclick = () => setModelsTab(b.dataset.tab));
  { let t; $("#lib-search").addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderLibrary, 120); }); }
  $$("#s-scope button").forEach(b => b.onclick = () => loadGen(b.dataset.scope));
  $$("#s-reasoning button").forEach(b => b.onclick = () => $$("#s-reasoning button").forEach(x => x.classList.toggle("on", x === b)));
  $("#s-clear-scope").onclick = clearScope;
  $("#browse-folder").onclick = openFolderPicker;
  ["#s-ctx", "#s-compact_at", "#s-temperature", "#s-keep_recent", "#s-top_p", "#s-top_k"].forEach(id => $(id).oninput = updateSettingLabels);
  $$("#s-theme button").forEach(b => b.onclick = () => $$("#s-theme button").forEach(x => x.classList.toggle("on", x === b)));
  $$(".switch").forEach(s => s.onclick = () => s.classList.toggle("on"));
  $$("[data-close]").forEach(b => b.onclick = () => { $(`#${b.dataset.close}`).hidden = true; });
  $$(".modal").forEach(m => m.addEventListener("mousedown", e => { if (e.target === m) m.hidden = true; }));

  addEventListener("keydown", e => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); newChat(); }
    else if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSidebar(); }
    else if (e.key === "Escape") {
      closeMenus();
      $$(".modal, .drawer").forEach(m => m.hidden = true);
      document.querySelector(".lightbox")?.remove();
      if (state.streaming && document.activeElement === $("#input")) stopStreaming();
    } else if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("#input").focus(); }
  });
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.version = s.version;
    state.models = s.models;
    state.settings = s.settings;
    state.presets = s.presets || {};
    state.memories = s.memories;
    $("#status").className = `status ${s.ollama ? "ok" : "bad"}`;
    $("#status-text").textContent = s.ollama ? `Ollama · ${s.models.length} model${s.models.length === 1 ? "" : "s"}` : "Ollama isn't running";
    if (!s.ollama) toast(s.error, true);
  } catch (e) {
    $("#status").className = "status bad";
    $("#status-text").textContent = "Hearth server stopped";
  }
}

async function init() {
  if (localStorage.getItem("hearth.collapsed") && innerWidth > 860) $("#app").classList.add("collapsed");
  wire();
  await refreshStatus();
  applyTheme();
  renderMemories();
  await loadChats();
  const linked = (location.hash.match(/chat=([\w-]+)/) || [])[1];
  const last = linked || localStorage.getItem("hearth.chat");
  if (last && state.chats.some(c => c.id === last)) await openChat(last);
  else newChat();
  renderModelButton();
  autosize();
  setInterval(refreshStatus, 60000);
}

/* phones: swipe in the chat history from the left edge, swipe left to hide it */
(() => {
  let x0 = null, y0 = 0, fromEdge = false;
  addEventListener("touchstart", e => {
    if (innerWidth > 860 || e.touches.length !== 1) return;
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    fromEdge = x0 < 36;
  }, { passive: true });
  addEventListener("touchend", e => {
    if (x0 == null) return;
    const t = e.changedTouches[0], dx = t.clientX - x0, dy = Math.abs(t.clientY - y0);
    const open = $("#app").classList.contains("show-side");
    if (dy < 60) {
      if (!open && fromEdge && dx > 50) $("#app").classList.add("show-side");
      else if (open && dx < -50) $("#app").classList.remove("show-side");
    }
    x0 = null;
  }, { passive: true });
})();

// Not an installable app: unregister any previously-installed service worker and clear its caches.
if ("serviceWorker" in navigator) navigator.serviceWorker.getRegistrations().then(rs => rs.forEach(r => r.unregister())).catch(() => {});
if (window.caches) caches.keys().then(ks => ks.forEach(k => caches.delete(k))).catch(() => {});

document.addEventListener("DOMContentLoaded", () => {
  init();
});


/* folder picker for Extra workspace paths (browses the local machine) */
let fpPath = "";
async function fpLoad(path) {
  let d;
  try { d = await api("/api/browse?path=" + encodeURIComponent(path || "")); }
  catch (e) { return; }
  fpPath = d.path;
  $("#fp-path").textContent = d.path;
  $("#fp-current").textContent = d.path;
  const list = $("#fp-list");
  list.innerHTML = "";
  if (d.parent) {
    const up = document.createElement("div");
    up.className = "fp-item up";
    up.textContent = "\u2191  ..";
    up.onclick = () => fpLoad(d.parent);
    list.appendChild(up);
  }
  if (!d.dirs.length) {
    const empty = document.createElement("div");
    empty.className = "fp-empty";
    empty.textContent = d.error ? ("Can\u2019t open this folder: " + d.error) : "No sub-folders here.";
    list.appendChild(empty);
  }
  d.dirs.forEach(dir => {
    const it = document.createElement("div");
    it.className = "fp-item";
    it.textContent = dir.name;
    it.onclick = () => fpLoad(dir.path);
    list.appendChild(it);
  });
}
function openFolderPicker() {
  let modal = $("#folder-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.className = "modal";
    modal.id = "folder-modal";
    modal.innerHTML =
      '<div class="modal-card">' +
      '<div class="modal-head"><h2>Choose a folder</h2><button class="icon-btn" data-fp-close><svg><use href="#i-x"/></svg></button></div>' +
      '<div class="modal-body"><div class="fp-path" id="fp-path"></div><div class="fp-list" id="fp-list"></div></div>' +
      '<div class="modal-foot"><span class="help" id="fp-current"></span><button class="btn primary" id="fp-add">Add this folder</button></div>' +
      '</div>';
    document.body.appendChild(modal);
    modal.addEventListener("click", e => { if (e.target === modal || e.target.closest("[data-fp-close]")) modal.hidden = true; });
    $("#fp-add").onclick = () => {
      const ta = $("#s-workspace_paths");
      const cur = ta.value.split("\n").map(s => s.trim()).filter(Boolean);
      if (fpPath && !cur.includes(fpPath)) cur.push(fpPath);
      ta.value = cur.join("\n");
      modal.hidden = true;
    };
  }
  modal.hidden = false;
  fpLoad("");
}




/* Projects card on the welcome screen — each project opens its own chat */
function projectsCard() {
  const row = h("div", { class: "proj-pills" });
  api("/api/projects").then(({ projects }) => {
    projects.forEach(p => {
      const savedId = localStorage.getItem("hearth.project:" + p.path);
      const hasChat = savedId && (state.chats || []).some(c => c.id === savedId);
      const pill = h("button", {
        class: `proj-pill${p.exists ? "" : " missing"}${hasChat ? " has-chat" : ""}`,
        title: p.exists ? p.path : p.path + " (not found on this PC)",
        onclick: () => openProject(p)
      }, p.name);
      row.append(pill);
    });
  }).catch(() => {});
  return row;
}

async function openProject(p) {
  // make sure the project's folder is in the workspace so the file tools can use it
  const paths = new Set(state.settings.workspace_paths || []);
  if (!paths.has(p.path)) {
    try { state.settings = await post("/api/settings", { workspace_paths: [...paths, p.path] }); } catch (e) {}
  }
  const key = "hearth.project:" + p.path;
  const savedId = localStorage.getItem(key);
  const hasChat = savedId && (state.chats || []).some(c => c.id === savedId);
  let id = null;
  if (hasChat) {
    const choice = await askContinue(p.name);
    if (!choice) return;                       // cancelled
    if (choice === "continue") id = savedId;
  }
  if (!id) {
    const chat = await post("/api/chats", { model: currentModel() });
    localStorage.setItem(key, chat.id);
    id = chat.id;
  }
  // set the title and (re)load this project's context: how it works, ports, deployment
  try { await api(`/api/chats/${id}`, { method: "PATCH", body: JSON.stringify({ title: p.name, project_context: p.desc || "", project_root: p.path || "" }) }); } catch (e) {}
  state.chats = await api("/api/chats");
  openChat(id);
}

function askContinue(name) {
  return new Promise(resolve => {
    let done = v => { modal.remove(); resolve(v); };
    const modal = h("div", { class: "modal" },
      h("div", { class: "modal-card confirm-card" },
        h("div", { class: "modal-head" }, h("h2", {}, name)),
        h("div", { class: "modal-body" },
          h("p", { class: "confirm-text" }, `You already have a ${name} chat. Continue it, or start a new one?`)),
        h("div", { class: "modal-foot confirm-foot" },
          h("button", { class: "btn", onclick: () => done(null) }, "Cancel"),
          h("button", { class: "btn", onclick: () => done("new") }, "New chat"),
          h("button", { class: "btn primary", onclick: () => done("continue") }, "Continue"))));
    modal.addEventListener("click", e => { if (e.target === modal) done(null); });
    document.body.append(modal);
  });
}
