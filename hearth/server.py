"""Local web server for Hearth: static UI + JSON API + streamed replies (Server-Sent Events)."""

import json
import time
import mimetypes
import os
import re
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, agent, files, store

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
HOST = "127.0.0.1"
PORT = int(os.environ.get("HEARTH_PORT", "8410"))
# Named project folders offered as one-click chips in Settings → Projects.
# These are personal to each install (local paths, private notes), so they are NOT
# hard-coded here. They load from <data dir>/projects.json, which is never committed.
# Format: a JSON array of [name, path, description] entries, e.g.
#   [["My app", "~/code/my-app", "What this project is and how to run it."]]
# Ships empty; add your own in that file (or leave it out for no project chips).
PROJECTS_FILE = os.path.join(store.DATA, "projects.json")


def _load_projects():
    try:
        with open(PROJECTS_FILE) as f:
            data = json.load(f)
        return [(p[0], p[1], p[2] if len(p) > 2 else "")
                for p in data if isinstance(p, (list, tuple)) and len(p) >= 2]
    except (OSError, ValueError, TypeError, IndexError):
        return []


PROJECTS = _load_projects()

# (name, description, category, approx q4 size in GB) — everything here is chosen to
# fit a ~6 GB GPU. The API tags each one fits / tight / spills against the detected VRAM.
CURATED_MODELS = [
    # general
    ("qwen2.5:0.5b",        "Tiny, instant answers",             "General", 0.4),
    ("qwen2.5:1.5b",        "Very fast, surprisingly capable",   "General", 1.0),
    ("qwen2.5:3b",          "Fast, capable everyday model",      "General", 1.9),
    ("qwen2.5:7b",          "Strong general model",              "General", 4.7),
    ("llama3.2:1b",         "Meta's tiny model",                 "General", 1.3),
    ("llama3.2:3b",         "Meta's small, quick model",         "General", 2.0),
    ("llama3.1:8b",         "Meta's solid 8B",                   "General", 4.9),
    ("gemma2:2b",           "Google Gemma 2, small",             "General", 1.6),
    ("gemma2:9b",           "Google Gemma 2, 9B",                "General", 5.4),
    ("gemma3:1b",           "Gemma 3, tiny",                     "General", 0.8),
    ("gemma3:4b",           "Gemma 3, small + multimodal",       "General", 3.3),
    ("phi3.5:3.8b",         "Microsoft Phi-3.5 — sharp & small", "General", 2.2),
    ("phi4-mini:3.8b",      "Microsoft Phi-4 mini",              "General", 2.5),
    ("mistral:7b",          "Mistral 7B",                        "General", 4.1),
    ("granite3.1-dense:2b", "IBM Granite, small",                "General", 1.5),
    ("smollm2:1.7b",        "Small on-device model",             "General", 1.1),
    # reasoning
    ("deepseek-r1:1.5b",    "Reasoning, tiny",                   "Reasoning", 1.1),
    ("deepseek-r1:7b",      "Reasoning, 7B",                     "Reasoning", 4.7),
    ("deepseek-r1:8b",      "Reasoning, 8B (Llama-based)",       "Reasoning", 4.9),
    ("qwen3:1.7b",          "Qwen3 reasoning, tiny",             "Reasoning", 1.4),
    ("qwen3:4b",            "Qwen3 reasoning, small",            "Reasoning", 2.6),
    ("qwen3:8b",            "Qwen3 reasoning, 8B",               "Reasoning", 5.2),
    # code
    ("qwen2.5-coder:1.5b",  "Code, tiny & fast",                 "Code", 1.0),
    ("qwen2.5-coder:3b",    "Code, small",                       "Code", 1.9),
    ("qwen2.5-coder:7b",    "Best small model for code",         "Code", 4.7),
    ("deepseek-coder:6.7b", "Code specialist",                   "Code", 3.8),
    ("codellama:7b",        "Meta CodeLlama",                    "Code", 3.8),
    ("codegemma:2b",        "Code, small",                       "Code", 1.6),
    # vision
    ("moondream",           "Vision, tiny — describe images",    "Vision", 1.7),
    ("llava:7b",            "Vision — describe images",          "Vision", 4.7),
    ("llava-llama3:8b",     "Vision on Llama 3",                 "Vision", 5.5),
    ("qwen2.5vl:3b",        "Vision + reasoning, small",         "Vision", 3.2),
    ("qwen2.5vl:7b",        "Vision + reasoning, 7B",            "Vision", 6.0),
    ("minicpm-v:8b",        "Vision, strong for size",           "Vision", 5.5),
    # embeddings (tiny, for search/RAG)
    ("nomic-embed-text",    "Text embeddings",                   "Embeddings", 0.27),
    ("mxbai-embed-large",   "Larger text embeddings",            "Embeddings", 0.67),
    ("all-minilm",          "Tiny embeddings",                   "Embeddings", 0.05),
]


def _gpu_vram_gb():
    """Total VRAM of the first GPU in GB, or None if it can't be read. Cached."""
    if hasattr(_gpu_vram_gb, "_v"):
        return _gpu_vram_gb._v
    v = None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=4)
        if out.returncode == 0 and out.stdout.strip():
            v = round(int(out.stdout.strip().splitlines()[0]) / 1024, 1)
    except (OSError, ValueError):
        v = None
    _gpu_vram_gb._v = v
    return v


def _fit(size_gb, vram_gb):
    """How a model of size_gb sits on a vram_gb card. Leaves headroom for the KV cache."""
    if not vram_gb or not size_gb:
        return "unknown"
    if size_gb <= vram_gb - 1.0:
        return "fits"
    if size_gb <= vram_gb + 0.2:
        return "tight"
    return "spills"


def _size_gb(tag):
    """Rough q4 download size in GB from a parameter-size tag like '8b', '1.5b', '8x7b', '270m'."""
    t = tag.strip().lower()
    m = re.match(r'([\d.]+)x([\d.]+)b$', t)
    if m:
        p = float(m.group(1)) * float(m.group(2))
    elif re.match(r'[\d.]+b$', t):
        p = float(t[:-1])
    elif re.match(r'[\d.]+m$', t):
        p = float(t[:-1]) / 1000.0
    else:
        return None
    return round(p * 0.62, 1)   # ~q4_K_M bytes-per-param, calibrated to Ollama's own sizes


def _size_label(gb):
    if not gb:
        return ""
    return f"{gb:.1f} GB" if gb >= 1 else f"{int(round(gb * 1000))} MB"
_running = {}          # chat_id -> threading.Event (set to stop)
_running_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = f"Hearth/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("HEARTH_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, obj)

    def _body(self, limit=250 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large")
        return self.rfile.read(n) if n else b""

    def _host(self):
        return (self.headers.get("Host") or "").split(":")[0].lower()

    def _allowed(self):
        # Only the page served by us may call the API: blocks DNS-rebinding and cross-site requests.
        if self._host() not in ("127.0.0.1", "localhost"):
            return False
        if self.command != "GET" and self.headers.get("X-Hearth") != "1":
            return False
        return True

    def _route(self):
        path = urllib.parse.urlparse(self.path).path
        return path, [p for p in path.split("/") if p]

    # -- verbs --------------------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, verb):
        try:
            if not self._allowed():
                return self._json(403, {"error": "forbidden"})
            path, parts = self._route()
            if verb == "GET" and not path.startswith("/api/"):
                return self._static(path)
            handler = getattr(self, f"api_{verb.lower()}_{parts[1] if len(parts) > 1 else ''}", None)
            if not handler:
                return self._json(404, {"error": "not found"})
            handler(parts[2:])
        except (KeyError, FileNotFoundError):
            self._json(404, {"error": "not found"})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except agent.OllamaError as e:
            self._json(502, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def _static(self, path):
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.realpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        ctype = {".webmanifest": "application/manifest+json"}.get(os.path.splitext(full)[1]) or \
            mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype, {"Content-Security-Policy":
                                      "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                      "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"})

    # -- API ------------------------------------------------------------------------------
    def api_get_status(self, _):
        try:
            models = agent.list_models()
            ok, err = True, ""
        except agent.OllamaError as e:
            models, ok, err = [], False, str(e)
        self._json(200, {"version": VERSION, "ollama": ok, "error": err, "models": models,
                         "settings": store.settings(), "memories": store.memories(), "presets": store.PRESETS})

    def api_get_browse(self, _):
        # Directory listing for the Settings folder picker (local machine only).
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        home = os.path.expanduser("~")
        raw = (q.get("path") or [""])[0]
        base = os.path.realpath(os.path.expanduser(raw)) if raw else home
        if not os.path.isdir(base):
            base = home
        dirs, err = [], ""
        try:
            for e in sorted(os.scandir(base), key=lambda e: e.name.lower()):
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=True):
                        dirs.append({"name": e.name, "path": os.path.join(base, e.name)})
                except OSError:
                    pass
        except OSError as ex:
            err = ex.strerror or str(ex)
        self._json(200, {"path": base, "parent": (None if base == "/" else os.path.dirname(base)),
                         "home": home, "dirs": dirs, "error": err})

    def api_get_available(self, _):
        try:
            installed = {m["name"] for m in agent.list_models()}
        except Exception:
            installed = set()
        def inst(n):
            return n in installed or (n + ":latest") in installed
        vram = _gpu_vram_gb()
        out = [{"name": n, "desc": d, "category": cat,
                "size": (f"{gb:.1f} GB" if gb >= 1 else f"{int(gb * 1000)} MB"),
                "size_gb": gb, "fit": _fit(gb, vram), "installed": inst(n)}
               for n, d, cat, gb in CURATED_MODELS]
        return self._json(200, {"models": out, "vram_gb": vram})

    def api_get_library(self, parts):
        try:
            models = agent.fetch_library(force=("refresh" in urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)))
        except Exception as e:
            return self._json(502, {"error": f"Couldn't reach ollama.com: {e}"})
        try:
            installed = {m["name"] for m in agent.list_models()}
        except Exception:
            installed = set()
        def inst(n):
            return n in installed or (n + ":latest") in installed
        vram = _gpu_vram_gb()
        out = []
        for m in models:
            sizes = []
            for tag in m["sizes"]:
                gb = _size_gb(tag)
                full = f"{m['name']}:{tag}"
                sizes.append({"tag": tag, "gb": gb, "size": _size_label(gb),
                              "fit": _fit(gb, vram), "installed": inst(full)})
            out.append({"name": m["name"], "desc": m["desc"], "caps": m["caps"],
                        "sizes": sizes, "installed": inst(m["name"])})
        return self._json(200, {"models": out, "vram_gb": vram})

    def api_post_remove(self, _):
        body = json.loads(self._body() or b"{}")
        name = (body.get("name") or "").strip()
        if not name:
            return self._json(400, {"error": "name required"})
        agent.remove_model(name)
        return self._json(200, {"ok": True, "removed": name})

    def api_get_modelinfo(self, _):
        name = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("name", [""])[0]).strip()
        if not name:
            return self._json(400, {"error": "name required"})
        try:
            about = agent.model_about(name)
        except Exception:
            about = ""
        return self._json(200, {"name": name, "about": about})

    def api_post_pull(self, _):
        body = json.loads(self._body() or b"{}")
        name = (body.get("name") or "").strip()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event, data):
            try:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                return False   # browser disconnected — pull() will stop the download
        if not name:
            emit("error", {"message": "No model name given."})
        else:
            try:
                agent.pull(name, emit)
            except Exception as e:
                emit("error", {"message": f"{type(e).__name__}: {e}"})
        emit("end", {})

    def api_get_projects(self, _):
        out = []
        for name, p, desc in PROJECTS:
            full = os.path.realpath(os.path.expanduser(p))
            out.append({"name": name, "path": full, "exists": os.path.isdir(full), "desc": desc})
        self._json(200, {"projects": out})

    def api_get_models(self, _):
        self._json(200, agent.list_models())

    def api_get_settings(self, _):
        self._json(200, store.settings())

    def api_post_settings(self, _):
        self._json(200, store.update_settings(json.loads(self._body() or b"{}")))

    def api_get_chats(self, parts):
        if parts:
            chat = store.load_chat(parts[0])
            chat["attachments"] = self._attachment_meta(chat)
            chat["running"] = parts[0] in _running
            return self._json(200, chat)
        self._json(200, store.list_chats())

    def _attachment_meta(self, chat):
        out = {}
        for m in chat["messages"]:
            for a in m.get("attachments") or []:
                try:
                    out[a] = files.public(store.upload_path(a)[0])
                except (KeyError, ValueError):
                    pass
        return out

    def api_post_chats(self, parts):
        body = json.loads(self._body() or b"{}")
        if not parts:                                           # new chat
            return self._json(200, store.new_chat(body.get("model") or store.settings().get("model", ""), incognito=bool(body.get("incognito"))))
        chat_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
        if action == "send":
            chat = store.load_chat(chat_id)
            if body.get("model"):
                chat["model"] = body["model"]
            if body.get("regenerate"):
                while chat["messages"] and chat["messages"][-1]["role"] != "user":
                    chat["messages"].pop()
            else:
                text = (body.get("text") or "").strip()
                atts = [a for a in body.get("attachments") or [] if isinstance(a, str)]
                if not text and not atts:
                    raise ValueError("Empty message")
                if body.get("edit_from") is not None:           # edit an earlier message
                    idx = int(body["edit_from"])
                    chat["messages"] = chat["messages"][:idx]
                    if chat.get("summary") and chat["summary"]["upto"] > idx:
                        chat["summary"] = None
                chat["messages"].append({"role": "user", "content": text, "attachments": atts,
                                         "ts": time.time()})
            store.save_chat(chat)
            return self._stream(chat_id)
        if action == "stop":
            ev = _running.get(chat_id)
            if ev:
                ev.set()
            return self._json(200, {"stopped": bool(ev)})
        if action == "compact":
            chat = store.load_chat(chat_id)
            done = agent.compact(chat, agent.pick_model(chat, store.settings())[0], store.settings(), force=True)
            return self._json(200, {"compacted": done, "summary": chat.get("summary")})
        raise KeyError(action)

    def api_patch_chats(self, parts):
        chat = store.load_chat(parts[0])
        body = json.loads(self._body() or b"{}")
        for k in ("title", "model", "pinned", "project_context", "project_root", "settings"):
            if k in body:
                chat[k] = body[k]
        store.save_chat(chat)
        self._json(200, {"ok": True})

    def api_delete_chats(self, parts):
        store.delete_chat(parts[0])
        self._json(200, {"ok": True})

    def api_post_upload(self, _):
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "file")
        self._json(200, files.ingest(name, self._body()))

    def api_get_uploads(self, parts):
        meta, path = store.upload_path(parts[0])
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, meta["mime"] if meta["kind"] == "image" else "application/octet-stream",
                   {"Content-Disposition": f"inline; filename=\"{meta['name']}\""})

    def api_get_memory(self, _):
        self._json(200, store.memories())

    def api_post_memory(self, _):
        self._json(200, store.add_memory(json.loads(self._body() or b"{}").get("text", ""), source="user"))

    def api_delete_memory(self, parts):
        self._json(200, {"removed": store.delete_memory(parts[0])})

    # -- streaming -------------------------------------------------------------------------
    def _stream(self, chat_id):
        with _running_lock:
            if chat_id in _running:
                return self._json(409, {"error": "This chat is already answering"})
            stop = _running[chat_id] = threading.Event()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()

        def emit(event, data):
            chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            with lock:
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    stop.set()       # the page went away - stop generating

        try:
            agent.respond(chat_id, emit, stop)
        except agent.OllamaError as e:
            emit("error", {"message": str(e)})
        except Exception as e:
            traceback.print_exc()
            emit("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            with _running_lock:
                _running.pop(chat_id, None)
            emit("end", {})


def main():
    store._ensure()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    store.activity("server_start", f"{HOST}:{PORT}")
    print(f"Hearth {VERSION} on http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
