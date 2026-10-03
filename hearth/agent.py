"""The conversation engine: builds prompts, streams from Ollama, runs tools, and keeps
long conversations inside the context window by summarizing older turns."""

import base64
import datetime
import html as _html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import files, imagegen, router, store, tools, workspace

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
MAX_TOOL_ROUNDS = 8
TOOL_RESULT_CHARS = 12000
CHARS_PER_TOKEN = 3.6

_model_cache = {}
_model_lock = threading.Lock()


class OllamaError(Exception):
    pass


def _request(path, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error")
        except Exception:
            msg = None
        raise OllamaError(msg or f"Ollama returned {e.code}")
    except urllib.error.URLError:
        raise OllamaError("Can't reach Ollama. Is it running? (systemctl status ollama)")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def pull(name, emit):
    """Stream `ollama pull <name>` progress to emit(event, data)."""
    body = json.dumps({"model": name, "stream": True}).encode()
    req = urllib.request.Request(OLLAMA + "/api/pull", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=None) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    emit("error", {"message": d["error"]})
                    return
                # emit returns False once the browser has disconnected (Cancel /
                # closed tab). Stop reading so this connection to Ollama closes,
                # which makes Ollama abort the download too.
                if emit("progress", d) is False:
                    return
    except urllib.error.HTTPError as e:
        emit("error", {"message": f"Ollama returned {e.code}"})
        return
    except urllib.error.URLError:
        emit("error", {"message": "Can't reach Ollama."})
        return
    emit("done", {"model": name})


def remove_model(name):
    """Delete an installed model via Ollama's DELETE /api/delete."""
    name = (name or "").strip()
    if not name:
        raise ValueError("no model name")
    body = json.dumps({"model": name}).encode()
    req = urllib.request.Request(OLLAMA + "/api/delete", data=body,
                                 headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama returned {e.code} removing {name}")
    except urllib.error.URLError:
        raise OllamaError("Can't reach Ollama.")
    _ABOUT_CACHE.pop(name.split(":")[0], None)
    return True


_LIB_CACHE = {"at": 0.0, "models": None}
_LIB_TTL = 3600           # ollama.com/library changes slowly; cache for an hour
_SIZE_RE = re.compile(r'^\d[\d.]*(x[\d.]+)?[bm]$', re.I)


def fetch_library(force=False):
    """Scrape ollama.com/library into [{name, desc, caps:[...], sizes:[tag,...]}]. Cached."""
    now = time.time()
    if not force and _LIB_CACHE["models"] is not None and now - _LIB_CACHE["at"] < _LIB_TTL:
        return _LIB_CACHE["models"]
    req = urllib.request.Request("https://ollama.com/library",
                                 headers={"User-Agent": "Hearth/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        h = r.read().decode("utf-8", "replace")
    m = re.search(r'<div\s+id="repo">(.*?)</ul>', h, re.S)
    body = m.group(1) if m else h
    out = []
    for c in re.split(r'<li\b', body)[1:]:
        nm = re.search(r'group-hover:underline truncate">([^<]+)</span>', c)
        if not nm:
            continue
        name = _html.unescape(nm.group(1).strip())
        dm = re.search(r'text-md">([^<]*)</p>', c)
        desc = _html.unescape(dm.group(1).strip()) if dm else ""
        tags = re.findall(r'sm:text-\[13px\]">([^<]+)</span>', c)
        sizes = [t.strip() for t in tags if _SIZE_RE.match(t.strip())]
        caps = [t.strip() for t in tags if not _SIZE_RE.match(t.strip())]
        out.append({"name": name, "desc": desc, "caps": caps, "sizes": sizes})
    if out:
        _LIB_CACHE["models"] = out
        _LIB_CACHE["at"] = now
    return out


_ABOUT_CACHE = {}         # model name -> description text
_NAME_OK = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def model_about(name):
    """Fetch a model's long description (the top of its Readme) from its library page. Cached."""
    name = (name or "").strip()
    if not _NAME_OK.match(name):
        return ""
    if name in _ABOUT_CACHE:
        return _ABOUT_CACHE[name]
    try:
        req = urllib.request.Request(f"https://ollama.com/library/{name}",
                                     headers={"User-Agent": "Hearth/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            h = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    about = ""
    i = h.find('id="readme"')
    if i >= 0:
        i = h.find('>', i) + 1          # start after the opening tag closes
        txt = re.sub(r'<[^>]+>', ' ', h[i:i + 8000])
        txt = re.sub(r'\s+', ' ', _html.unescape(txt)).strip()
        if txt[:6].lower() == "readme":
            txt = txt[6:].strip()
        about = txt[:900].strip()
    if not about:
        m = re.search(r'<meta name="description" content="([^"]*)"', h)
        about = _html.unescape(m.group(1)) if m else ""
    _ABOUT_CACHE[name] = about
    return about


def list_models():
    with _request("/api/tags", timeout=10) as r:
        tags = json.load(r).get("models", [])
    out = []
    for t in tags:
        info = model_info(t["name"], t.get("digest"))
        out.append({"name": t["name"], "size": t.get("size", 0),
                    "params": t.get("details", {}).get("parameter_size", ""),
                    "family": t.get("details", {}).get("family", ""),
                    "caps": info["caps"], "ctx_max": info["ctx_max"]})
    out.sort(key=lambda m: m["name"].lower())
    return out


def model_info(name, digest=None):
    with _model_lock:
        hit = _model_cache.get(name)
        if hit and (digest is None or hit["digest"] == digest):
            return hit
    try:
        with _request("/api/show", {"model": name}, timeout=20) as r:
            d = json.load(r)
    except OllamaError:
        d = {}
    ctx = next((v for k, v in (d.get("model_info") or {}).items() if k.endswith(".context_length")), None)
    info = {"caps": d.get("capabilities") or ["completion"], "ctx_max": ctx or 8192, "digest": digest}
    with _model_lock:
        _model_cache[name] = info
    return info


def selection(chat, s):
    """What the user chose for this chat: a model name or 'auto'."""
    return chat.get("model") or s.get("model") or router.AUTO


def pick_model(chat, s):
    """The concrete model to run now, and why (for Auto)."""
    sel = selection(chat, s)
    models = list_models()
    if not models:
        raise OllamaError("No models installed. Run: ollama pull qwen2.5:3b")
    if sel != router.AUTO and any(m["name"] == sel for m in models):
        return sel, ""
    return router.choose(chat, models)


# ---------------------------------------------------------------------------
# prompt building
# ---------------------------------------------------------------------------

def _gen_options(eff):
    o = {"num_ctx": int(eff["num_ctx"]), "temperature": float(eff.get("temperature", 0.7))}
    if eff.get("top_p") not in (None, ""): o["top_p"] = float(eff["top_p"])
    if eff.get("top_k") not in (None, ""): o["top_k"] = int(eff["top_k"])
    stop = eff.get("stop")
    if stop: o["stop"] = stop if isinstance(stop, list) else [stop]
    return o


def system_prompt(model, s, chat):
    now = datetime.datetime.now().astimezone()
    who = s.get("user_name") or "the user"
    parts = [f"You are Hearth, a warm, capable assistant running privately on {who}'s own computer "
             f"through Ollama (model: {model}). Today is {now:%A, %B %-d, %Y}, local time {now:%-I:%M %p %Z}.",
             "Answer directly and clearly. Use Markdown: short paragraphs, lists, tables and fenced code "
             "blocks with a language tag where they help."]
    tool_names = [sp["function"]["name"] for sp in tools.specs(s)] if s.get("tools", True) else []
    can_write = "write_file" in tool_names
    if can_write:
        parts.append("You act only through the tools listed below. You cannot run programs or commands or control "
                     "the computer — but you CAN save files with write_file. Never say you did something (saved a "
                     "file, made an image) unless you actually called the tool that does it, and don't apologise "
                     "repeatedly.")
    else:
        parts.append("Be honest about your abilities. You can only act through the tools listed for you. You cannot "
                     "create or change files, run programs or commands, install software, or control the computer. "
                     "If asked to, say so in one sentence, then offer the most useful help you can instead (for "
                     "example, complete ready-to-paste code and the exact commands to run). Never claim you did "
                     "something you have no tool for, and don't apologise repeatedly.")
    if s.get("tools", True):
        if tool_names:
            parts.append("You have these tools available right now: " + ", ".join(tool_names) + ". They are real and "
                         "they work on this computer. When a request can be done with a tool, CALL the tool — do not "
                         "just describe what you would do, do not print the code or command instead, and never say "
                         "you are unable to do something a tool above can do.")
        if s.get("internet", True):
            parts.append("For anything current, factual that you're unsure of, or outside your training, call "
                         "web_search and then fetch_url on the best results before answering. Cite the pages you "
                         "used as Markdown links.")
            parts.append("You have no file-writing tools right now ONLY because Web access is on. If the user asks "
                         "you to build, create, save, write or put a file, app, script, page or document anywhere, "
                         "do NOT say you can't access files or that you're 'just an AI' — instead tell them, in one "
                         "sentence, to turn OFF the Web button at the top right, which switches on your workspace so "
                         "you can write the file for real. Then stop and wait for them to do it.")
        else:
            parts.append("Internet is off, so you are in CODING MODE with a workspace folder at "
                         f"{workspace.DEFAULT_ROOT}. ANY file, code, web page, script, app or document you produce MUST be "
                         "saved there with write_file — write the complete file, do not paste a whole file into the "
                         "chat, and do not claim you saved it unless you called write_file. Use read_file and "
                         "list_files to review what is there, delete_file to remove something. For a web app or game, "
                         "make a single index.html that works when opened in a browser. When you finish, tell the "
                         "user the exact file path. You cannot run or build anything — only write the files. "
                         "NEVER tell the user you are 'just an AI', that you 'don't have access to their files', or "
                         "that you can only show code — that is false here: write_file works and saves to their disk. "
                         "If you claimed otherwise earlier in this chat, correct it now by actually calling write_file.")
        if imagegen.available():
            parts.append("You CAN make and change pictures on this computer: generate_image creates a new picture, "
                         "edit_image restyles or changes a picture (one the user attached or one you made), and "
                         "replace_background keeps the subject of a photo and swaps its background. Use these tools "
                         "whenever pictures are asked for — never say you can't create or edit images. To edit or "
                         "change the background of the user's own photo it must be ATTACHED with the paperclip — a web "
                         "link or Google Photos URL will not work, so if they paste a link, ask them to attach the "
                         "image file instead.")
        parts.append("When the user shares a lasting fact about themselves or a preference (name, projects, "
                     "setup, how they like answers), save it with the remember tool. Don't save trivia.")
    mem = store.memories()
    if mem:
        parts.append("What you remember about the user from earlier chats (id: fact):\n" +
                     "\n".join(f"- {m['id']}: {m['text']}" for m in mem))
    if s.get("system_prompt"):
        parts.append(s["system_prompt"])
    if chat.get("project_context"):
        parts.append("This chat is dedicated to one of the user's projects. Everything you need to know about it "
                     "(how it works, the ports it uses and for what, and how it is deployed):\n"
                     + chat["project_context"])
    proot = chat.get("project_root")
    if proot:
        try:
            with open(os.path.join(os.path.expanduser(proot), "HEARTH.md"), encoding="utf-8") as _fh:
                _instr = _fh.read(8000).strip()
            if _instr:
                parts.append("Project instructions (from HEARTH.md at the project root) \u2014 follow these:\n" + _instr)
        except OSError:
            pass
    if chat.get("summary"):
        parts.append("The earlier part of this conversation was summarized to save space. Summary:\n" +
                     chat["summary"]["text"])
    return "\n\n".join(parts)


def _user_payload(m, vision):
    text = m.get("content") or ""
    images, notes = [], []
    for att_id in m.get("attachments") or []:
        try:
            meta, path = store.upload_path(att_id)
        except (KeyError, ValueError):
            continue
        if meta["kind"] == "image":
            if vision:
                with open(path, "rb") as f:
                    images.append(base64.b64encode(f.read()).decode())
            else:
                notes.append(f"[Image attached: {meta['name']} — the current model can't see images]")
        else:
            block = files.prompt_block(att_id)
            if block:
                notes.append(block)
    if notes:
        text = (text + "\n\n" if text else "") + "\n\n".join(notes)
    out = {"role": "user", "content": text}
    if images:
        out["images"] = images
    return out


def build_messages(chat, model, s, info):
    vision = "vision" in info["caps"]
    start = (chat.get("summary") or {}).get("upto", 0)
    msgs = [{"role": "system", "content": system_prompt(model, s, chat)}]
    for m in chat["messages"][start:]:
        if m["role"] == "user":
            msgs.append(_user_payload(m, vision))
        elif m["role"] == "assistant":
            a = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                a["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
            msgs.append(a)
        elif m["role"] == "tool":
            msgs.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
    return msgs


def estimate_tokens(messages, tool_specs=()):
    chars = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", ""))) for m in messages)
    images = sum(len(m.get("images", [])) for m in messages)
    return int((chars + len(json.dumps(list(tool_specs)))) / CHARS_PER_TOKEN) + 280 * images + 8 * len(messages)


def all_attachments(chat):
    return [a for m in chat["messages"] if m["role"] == "user" for a in m.get("attachments") or []]


# ---------------------------------------------------------------------------
# summarizing older turns ("compaction")
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are condensing a conversation so it can continue in a fresh, smaller context.
Write a faithful summary that lets the assistant carry on seamlessly. Include:
- who the user is and what they are trying to achieve
- decisions made, facts and results found (with key numbers, names, file names, URLs)
- code, commands or text that later messages depend on (quote the essential parts exactly)
- anything the user asked for that is still open, and what the assistant was about to do next
Write in concise bullet points under short headings. Do not add commentary."""


def _render_for_summary(msgs, limit_each=2500):
    lines = []
    for m in msgs:
        if m["role"] == "user":
            names = []
            for a in m.get("attachments") or []:
                try:
                    names.append(store.upload_path(a)[0]["name"])
                except (KeyError, ValueError):
                    pass
            extra = f" [attached: {', '.join(names)}]" if names else ""
            lines.append(f"USER{extra}: {(m.get('content') or '')[:limit_each]}")
        elif m["role"] == "assistant":
            if m.get("tool_calls"):
                for c in m["tool_calls"]:
                    lines.append(f"ASSISTANT used {c['name']}({json.dumps(c['arguments'])[:300]})")
            if m.get("content"):
                lines.append(f"ASSISTANT: {m['content'][:limit_each]}")
        elif m["role"] == "tool":
            lines.append(f"TOOL RESULT ({m.get('name')}): {m['content'][:800]}")
    return "\n\n".join(lines)


def _complete(model, prompt, num_ctx, max_tokens=900, temperature=0.2):
    payload = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}],
               "options": {"num_ctx": num_ctx, "temperature": temperature, "num_predict": max_tokens},
               "keep_alive": "2m"}
    if "thinking" in model_info(model)["caps"]:
        payload["think"] = False
    with _request("/api/chat", payload) as r:
        return (json.load(r).get("message") or {}).get("content", "").strip()


def compact(chat, model, s, emit=lambda *a: None, force=False):
    """Summarize everything except the most recent turns. Returns True if it did."""
    msgs = chat["messages"]
    start = (chat.get("summary") or {}).get("upto", 0)
    cut = len(msgs) - max(2, int(s.get("keep_recent", 6)))
    while cut > start and msgs[cut]["role"] != "user":   # never split a tool-call sequence
        cut -= 1
    if cut <= start + (0 if force else 1):
        return False
    emit("compacting", {"messages": cut - start})
    num_ctx = store.ctx_for(model, s)
    budget = int(num_ctx * CHARS_PER_TOKEN * 0.55)       # room for the transcript in one summary call
    summary = (chat.get("summary") or {}).get("text", "")
    transcript = _render_for_summary(msgs[start:cut])
    chunks = [transcript[i:i + budget] for i in range(0, len(transcript), budget)] or [""]
    max_tokens = max(200, min(1200, int(num_ctx * 0.15)))     # the summary must leave room to keep chatting
    words = int(max_tokens * 0.7)
    for chunk in chunks:
        prompt = SUMMARY_PROMPT + f"\nKeep the whole summary under {words} words. Summarize; don't copy the messages."
        if summary:
            prompt += f"\n\nSummary of the conversation before this part:\n{summary}"
        prompt += f"\n\nConversation to add to the summary:\n{chunk}\n\nUpdated summary:"
        summary = _complete(model, prompt, num_ctx, max_tokens=max_tokens) or summary
    prev = chat.get("summary") or {}
    chat["summary"] = {"text": summary, "upto": cut, "created": time.time(),
                       "count": prev.get("count", 0) + 1}
    store.save_chat(chat)
    emit("compacted", {"upto": cut, "summary": summary, "count": chat["summary"]["count"]})
    return True


# ---------------------------------------------------------------------------
# responding
# ---------------------------------------------------------------------------

_TEXT_CALL = re.compile(r"^\s*(?:<tool_call>|```(?:json)?)?\s*(\{.*\})\s*(?:</tool_call>|```)?\s*$", re.S)


def _text_tool_call(content, allowed):
    """Some models print a tool call as JSON instead of using the tool-call field."""
    m = _TEXT_CALL.match(content or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except ValueError:
        return None
    name = obj.get("name") or (obj.get("function") or {}).get("name")
    args = obj.get("arguments") or obj.get("parameters") or (obj.get("function") or {}).get("arguments") or {}
    if name in allowed:
        return [{"name": name, "arguments": args if isinstance(args, dict) else {}}]
    return None


def respond(chat_id, emit, stop):
    """Generate the assistant's reply to the last user message, streaming events through emit()."""
    chat = store.load_chat(chat_id)
    s = store.settings()
    model, why = pick_model(chat, s)
    chat["model"] = selection(chat, s)
    store.activity("message", model)
    if why:
        emit("routed", {"model": model, "reason": why})
    info = model_info(model)
    num_ctx = store.ctx_for(model, s)
    atts = all_attachments(chat)
    specs = tools.specs(s, bool(atts)) if "tools" in info["caps"] else []
    allowed = {sp["function"]["name"] for sp in specs}

    if estimate_tokens(build_messages(chat, model, s, info), specs) > s["compact_at"] * num_ctx:
        compact(chat, model, s, emit)

    try:  # tell the page when a model has to be loaded into memory first (can take ~30 s)
        with _request("/api/ps", timeout=5) as r:
            loaded = {m.get("name") for m in json.load(r).get("models", [])}
        if model not in loaded:
            emit("status", {"text": f"Loading {model.removeprefix('hf.co/')} into memory…"})
    except OllamaError:
        pass

    stats = {}
    for _round in range(MAX_TOOL_ROUNDS):
        messages = build_messages(chat, model, s, info)
        payload = {"model": model, "messages": messages, "stream": True, "keep_alive": "2m",
                   "options": _gen_options(eff)}
        if specs:
            payload["tools"] = specs
        if "thinking" in info["caps"]:
            r = eff.get("reasoning", "on")
            if r in ("off", "false", False):
                payload["think"] = False
            elif r in ("low", "medium", "high") and "gpt-oss" in model.lower():
                payload["think"] = r
            else:
                payload["think"] = True
        text, thinking, calls = "", "", []
        started = time.time()
        think_started = think_ended = None
        try:
            with _request("/api/chat", payload, timeout=900) as r:
                for line in r:
                    if stop.is_set():
                        break
                    if not line.strip():
                        continue
                    j = json.loads(line)
                    if j.get("error"):
                        raise OllamaError(j["error"])
                    msg = j.get("message") or {}
                    if msg.get("thinking"):
                        think_started = think_started or time.time()
                        thinking += msg["thinking"]
                        emit("thinking", {"text": msg["thinking"]})
                    if msg.get("content"):
                        if think_started and not think_ended:
                            think_ended = time.time()
                        text += msg["content"]
                        emit("token", {"text": msg["content"]})
                    for c in msg.get("tool_calls") or []:
                        fn = c.get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        calls.append({"name": fn.get("name", ""), "arguments": args})
                    if j.get("done"):
                        stats = {"prompt_tokens": j.get("prompt_eval_count", 0), "output_tokens": j.get("eval_count", 0),
                                 "seconds": round(time.time() - started, 1),
                                 "tok_per_s": round(j.get("eval_count", 0) / (j.get("eval_duration", 1) / 1e9), 1)
                                 if j.get("eval_duration") else None}
        except OllamaError as e:
            if text:
                chat["messages"].append({"role": "assistant", "content": text, "model": model, "ts": time.time(),
                                         "error": str(e)})
                store.save_chat(chat)
            raise
        if not calls and allowed:
            parsed = _text_tool_call(text, allowed)
            if parsed:
                calls, text = parsed, ""
                emit("retract", {})
        entry = {"role": "assistant", "content": text, "model": model, "ts": time.time(), "stats": stats}
        if why:
            entry["auto"] = why
        if thinking:
            entry["thinking"] = thinking
            entry["think_seconds"] = round((think_ended or time.time()) - (think_started or started), 1)
        if calls:
            for i, c in enumerate(calls):
                c["id"] = f"{int(time.time() * 1000)}-{i}"
                c["label"] = tools.describe(c["name"], c["arguments"])
            entry["tool_calls"] = calls
        if stop.is_set():
            entry["stopped"] = True
        chat["messages"].append(entry)
        store.save_chat(chat)
        if not calls or stop.is_set():
            break
        for c in calls:
            emit("tool_start", {"id": c["id"], "name": c["name"], "label": c["label"], "arguments": c["arguments"]})
            store.activity("tool", c["name"])
            result = tools.run(c["name"], c["arguments"], s, atts)
            content = json.dumps(result, ensure_ascii=False)
            if len(content) > TOOL_RESULT_CHARS:
                content = content[:TOOL_RESULT_CHARS] + "… [truncated]"
            ok = not (isinstance(result, dict) and result.get("error"))
            chat["messages"].append({"role": "tool", "name": c["name"], "call_id": c["id"], "content": content,
                                     "ok": ok, "ts": time.time()})
            store.save_chat(chat)
            emit("tool_result", {"id": c["id"], "ok": ok, "result": result})
            if c["name"] in ("remember", "forget"):
                emit("memory", {"memories": store.memories()})
        # a long tool loop can itself fill the window
        if estimate_tokens(build_messages(chat, model, s, info), specs) > 0.9 * num_ctx:
            compact(chat, model, s, emit)

    chat["stats"] = dict(stats, ctx=num_ctx, model=model)
    store.save_chat(chat)
    emit("done", {"stats": chat["stats"]})
    if chat["title"] == "New chat" and not stop.is_set():
        _title(chat["id"], model, num_ctx, emit)   # the stream stays open for this last event


def _title(chat_id, model, num_ctx, emit):
    chat = store.load_chat(chat_id)
    first = next((m for m in chat["messages"] if m["role"] == "user"), None)
    answer = next((m for m in chat["messages"] if m["role"] == "assistant" and m.get("content")), None)
    if not first:
        return
    fallback = " ".join((first.get("content") or "Attachment").split()[:7])[:60] or "New chat"
    try:
        t = _complete(model, "Write a short title (3 to 6 words, no quotes, no trailing period) for this "
                             f"conversation.\n\nUser: {(first.get('content') or '')[:800]}\n\n"
                             f"Assistant: {((answer or {}).get('content') or '')[:400]}\n\nTitle:",
                      num_ctx, max_tokens=24, temperature=0.3)   # same num_ctx, or Ollama reloads the model
        t = t.strip().strip('"\'*#').split("\n")[0].replace("_", " ")
        t = " ".join(t.split())[:70] or fallback
    except Exception:
        t = fallback
    chat = store.load_chat(chat_id)
    if chat["title"] == "New chat":
        chat["title"] = t
        store.save_chat(chat)
        emit("title", {"title": t})
