"""Persistent state: settings, chats, long-term memory and uploaded files."""

import json
import os
import threading
import time
import uuid

DATA = os.environ.get("HEARTH_HOME") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "hearth")
CHATS = os.path.join(DATA, "chats")
UPLOADS = os.path.join(DATA, "uploads")
MEMORY_FILE = os.path.join(DATA, "memory.json")
ACTIVITY_LOG = os.path.join(DATA, "hearth.log")
SETTINGS_FILE = os.path.join(DATA, "settings.json")

DEFAULT_SETTINGS = {
    "model": "auto",              # "auto" = lightest capable model per message
    "num_ctx": 8192,              # context window used unless a model has its own value
    "model_ctx": {},              # {model: num_ctx}
    "temperature": 0.7,
    "top_p": 0.9,                 # nucleus sampling
    "top_k": 40,                  # sample-pool size
    "stop": [],                   # stop sequences
    "reasoning": "on",            # off / low / medium / high / on  (graded only where the model supports it)
    "model_settings": {},         # {model: {temperature, top_p, top_k, num_ctx, stop, reasoning}} overrides
    "tools": True,                # let the model call tools
    "internet": True,             # web_search / fetch_url tools
    "allow_local_urls": False,    # let fetch_url reach 192.168.x.x / localhost
    "thinking": True,             # show reasoning for models that support it
    "compact_at": 0.75,           # summarize when the prompt reaches this share of the context
    "keep_recent": 6,             # messages kept word-for-word after a summary
    "system_prompt": "",
    "theme": "system",
    "hover_tips": True,            # show the full-description tooltip when hovering a model
    "user_name": "",
    "workspace_paths": [],        # extra dirs the file tools may read/write ([] = ~/hearth-workspace only)
    "browseable_nodes": [],       # hostnames/IPs the fetch_url tool may contact beyond normal internet
}

_lock = threading.RLock()


def _ensure():
    for d in (DATA, CHATS, UPLOADS):
        os.makedirs(d, exist_ok=True)


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    _ensure()
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# -- settings ------------------------------------------------------------------------

def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(_read(SETTINGS_FILE, {}))
    return s


def update_settings(changes):
    with _lock:
        s = settings()
        for k, v in changes.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
        _write(SETTINGS_FILE, s)
        return s


GEN_KEYS = ("temperature", "top_p", "top_k", "num_ctx", "stop", "reasoning")

PRESETS = {
    "Precise":  {"temperature": 0.2, "top_p": 0.7, "top_k": 20},
    "Balanced": {"temperature": 0.7, "top_p": 0.9, "top_k": 40},
    "Creative": {"temperature": 1.05, "top_p": 0.95, "top_k": 80},
}


def _layer(dst, src):
    for k in GEN_KEYS:
        if isinstance(src, dict) and k in src and src[k] not in (None, ""):
            dst[k] = src[k]


def effective(chat, model, s=None):
    """Resolve generation params: global defaults <- per-model overrides <- per-chat overrides."""
    s = s or settings()
    reasoning = s.get("reasoning")
    if reasoning in (None, ""):
        reasoning = "on" if s.get("thinking", True) else "off"
    out = {
        "temperature": s.get("temperature", 0.7),
        "top_p": s.get("top_p", 0.9),
        "top_k": s.get("top_k", 40),
        "num_ctx": ctx_for(model, s),
        "stop": s.get("stop", []),
        "reasoning": reasoning,
    }
    _layer(out, (s.get("model_settings") or {}).get(model))
    _layer(out, (chat or {}).get("settings"))
    return out


def ctx_for(model, s=None):
    s = s or settings()
    return int(s.get("model_ctx", {}).get(model) or s["num_ctx"])


# -- chats ---------------------------------------------------------------------------

def _chat_path(chat_id):
    if not chat_id or not all(c.isalnum() or c == "-" for c in chat_id):
        raise ValueError("bad chat id")
    return os.path.join(CHATS, chat_id + ".json")


_ephemeral = {}   # incognito chats: kept in memory only, never written to disk
_EPHEMERAL_MAX_BYTES = 500 * 1024 * 1024   # 500 MB ceiling for ALL incognito chats combined


def _keep_ephemeral(chat):
    """Store an incognito chat in memory, evicting the oldest if the 500 MB ceiling is exceeded."""
    _ephemeral[chat["id"]] = chat
    total = sum(len(json.dumps(c, ensure_ascii=False)) for c in _ephemeral.values())
    while total > _EPHEMERAL_MAX_BYTES and len(_ephemeral) > 1:
        oldest = min(_ephemeral, key=lambda i: _ephemeral[i].get("updated", 0))
        if oldest == chat["id"]:
            break
        total -= len(json.dumps(_ephemeral[oldest], ensure_ascii=False))
        _ephemeral.pop(oldest, None)


def new_chat(model="", incognito=False):
    now = time.time()
    chat = {"id": uuid.uuid4().hex[:12], "title": "New chat", "model": model, "created": now,
            "updated": now, "messages": [], "summary": None, "stats": {}}
    if incognito:
        chat["incognito"] = True
        _keep_ephemeral(chat)
    else:
        save_chat(chat)
    return chat


def load_chat(chat_id):
    if chat_id in _ephemeral:
        return _ephemeral[chat_id]
    chat = _read(_chat_path(chat_id), None)
    if chat is None:
        raise KeyError(chat_id)
    return chat


def save_chat(chat):
    if chat.get("incognito"):
        chat["updated"] = time.time()
        _keep_ephemeral(chat)
        return
    with _lock:
        chat["updated"] = time.time()
        _write(_chat_path(chat["id"]), chat)


def list_chats():
    _ensure()
    out = []
    for name in os.listdir(CHATS):
        if not name.endswith(".json"):
            continue
        c = _read(os.path.join(CHATS, name), None)
        if not c:
            continue
        last = next((m for m in reversed(c["messages"]) if m["role"] in ("user", "assistant") and m.get("content")),
                    None)
        out.append({"id": c["id"], "title": c["title"], "updated": c["updated"], "model": c.get("model", ""),
                    "count": sum(1 for m in c["messages"] if m["role"] == "user"),
                    "preview": (last or {}).get("content", "")[:120], "pinned": c.get("pinned", False)})
    out.sort(key=lambda c: (not c["pinned"], -c["updated"]))
    return out


def delete_chat(chat_id):
    _ephemeral.pop(chat_id, None)
    try:
        os.remove(_chat_path(chat_id))
    except FileNotFoundError:
        pass


# -- long-term memory ------------------------------------------------------------------

def memories():
    return _read(MEMORY_FILE, [])


def add_memory(text, source="model"):
    text = " ".join(str(text).split())[:500]
    if not text:
        raise ValueError("empty memory")
    with _lock:
        mem = memories()
        if any(m["text"].lower() == text.lower() for m in mem):
            return next(m for m in mem if m["text"].lower() == text.lower())
        m = {"id": uuid.uuid4().hex[:6], "text": text, "created": time.time(), "source": source}
        mem.append(m)
        _write(MEMORY_FILE, mem)
        return m


def delete_memory(mem_id):
    with _lock:
        mem = memories()
        keep = [m for m in mem if m["id"] != mem_id]
        _write(MEMORY_FILE, keep)
        return len(keep) != len(mem)


# -- uploads ---------------------------------------------------------------------------

def upload_path(att_id):
    if not att_id or not att_id.isalnum():
        raise ValueError("bad attachment id")
    meta = _read(os.path.join(UPLOADS, att_id + ".json"), None)
    if not meta:
        raise KeyError(att_id)
    return meta, os.path.join(UPLOADS, att_id)


def save_upload(meta, data):
    _ensure()
    with open(os.path.join(UPLOADS, meta["id"]), "wb") as f:
        f.write(data)
    _write(os.path.join(UPLOADS, meta["id"] + ".json"), meta)


def activity(event, detail=""):
    """Append one line to hearth.log — every login, chat and tool use."""
    import time as _t
    line = f"{_t.strftime('%Y-%m-%d %H:%M:%S')}\t{event}\t{detail}\n"
    try:
        os.makedirs(DATA, exist_ok=True)
        open(ACTIVITY_LOG, "a").write(line)
    except Exception:
        pass
