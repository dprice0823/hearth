"""Tools the model can call: web search, reading pages, time, maths, memory, attachments and images."""

import ast
import datetime
import html
import ipaddress
import json
import math
import operator
import re
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import files, imagegen, store, workspace

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def _fn(name, description, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


SPECS = {
    "web_search": _fn("web_search", "Search the internet. Use for current events, facts you're unsure of, "
                                    "documentation, prices, or anything after your training data.",
                      {"query": {"type": "string", "description": "What to search for"},
                       "max_results": {"type": "integer", "description": "1-10, default 6"}}, ["query"]),
    "fetch_url": _fn("fetch_url", "Read the text of a web page or online document. Use after web_search to "
                                  "read a promising result in full.",
                     {"url": {"type": "string", "description": "Full http(s) URL"},
                      "max_chars": {"type": "integer", "description": "How much text to return, default 8000"}},
                     ["url"]),
    "get_datetime": _fn("get_datetime", "Get the current date, time and day of the week - here, or in another "
                                        "city/time zone.",
                        {"timezone": {"type": "string", "description": "Optional IANA zone like America/Mexico_City "
                                                                        "or Europe/London; omit for local time"}}),
    "calculate": _fn("calculate", "Evaluate a maths expression exactly, e.g. '(17.5*12)/3' or 'sqrt(2)*pi'.",
                     {"expression": {"type": "string"}}, ["expression"]),
    "remember": _fn("remember", "Save a lasting fact about the user or their preferences to long-term memory, "
                                "so you know it in future chats. Only for things worth keeping.",
                    {"fact": {"type": "string", "description": "One short, self-contained sentence"}}, ["fact"]),
    "forget": _fn("forget", "Remove an item from long-term memory by its id.",
                  {"memory_id": {"type": "string"}}, ["memory_id"]),
    "read_attachment": _fn("read_attachment", "Read more of a file the user attached, starting at a character offset.",
                           {"name": {"type": "string", "description": "The file name"},
                            "offset": {"type": "integer"}, "length": {"type": "integer", "description": "default 12000"}},
                           ["name"]),
    "list_files": _fn("list_files", "List the files in your workspace folder. Only available in coding mode "
                                    "(when internet is off).", {}),
    "read_file": _fn("read_file", "Read a file from your workspace folder.",
                     {"path": {"type": "string", "description": "Path relative to the workspace, e.g. 'app.js'"}},
                     ["path"]),
    "write_file": _fn("write_file", "Create or overwrite a file in your workspace folder (~/hearth-workspace) with "
                                    "exact contents. Use this to build web pages, scripts or small apps the user can "
                                    "open. Write the whole file each time.",
                      {"path": {"type": "string", "description": "Path relative to the workspace, e.g. "
                                                                 "'game/index.html'"},
                       "content": {"type": "string", "description": "The full file contents"}},
                      ["path", "content"]),
    "delete_file": _fn("delete_file", "Delete a file or folder from your workspace.",
                       {"path": {"type": "string"}}, ["path"]),
    "search_files": _fn("search_files", "Search for a word or phrase across EVERY file in your workspace and any "
                                        "extra project folders. Case-insensitive. Returns each match with its file "
                                        "and line number. Use this instead of guessing or claiming a file does or "
                                        "doesn't contain something. Only available in coding mode (internet off).",
                        {"query": {"type": "string", "description": "The text to search for"}}, ["query"]),
    "generate_image": _fn("generate_image", "Create a picture from a text description, on this computer. Use whenever "
                                            "the user asks you to draw, create, make or generate an image, picture, "
                                            "photo, logo or illustration from scratch. To CHANGE an existing picture "
                                            "(one the user attached or one you already made) use edit_image instead. "
                                            "The picture is shown to the user automatically; afterwards just say a "
                                            "short sentence about it.",
                          {"prompt": {"type": "string", "description": "A vivid English description of the picture: "
                                                                       "subject, setting, style, lighting"},
                           "size": {"type": "string", "enum": ["square", "wide", "tall"],
                                    "description": "Shape of the picture, default square"}},
                          ["prompt"]),
    "edit_image": _fn("edit_image", "You CAN edit pictures with this tool. Use it whenever the user asks to change, "
                                    "turn, make, restyle or edit a picture they attached or one you created earlier. "
                                    "Change an existing picture: restyle it or change its look, season, lighting, "
                                    "colours or setting (e.g. 'make it winter', 'watercolor painting', 'make the barn "
                                    "blue'). Redraws the whole picture keeping its layout; it cannot precisely remove "
                                    "or add small objects or text. The result is shown to the user automatically.",
                      {"image": {"type": "string", "description": "File name of a picture the user attached, or the "
                                                                   "image_id of a picture you created earlier"},
                       "prompt": {"type": "string", "description": "A full English description of how the finished "
                                                                    "picture should look (describe the whole scene, "
                                                                    "including the change)"},
                       "strength": {"type": "number", "description": "How much to change: 0.4 subtle touch-up, 0.7 "
                                                                      "normal (default; use for style changes like "
                                                                      "watercolor or cartoon), 0.85 drastic"}},
                      ["image", "prompt"]),
    "replace_background": _fn("replace_background", "Change the BACKGROUND of a photo while keeping the main subject "
                                                    "(a person, pet or object) exactly as it is. Use when the user "
                                                    "asks to change, replace or remove the background, or to put "
                                                    "someone/something somewhere else ('put my dog on a beach'). The "
                                                    "result is shown to the user automatically.",
                              {"image": {"type": "string", "description": "File name of the attached photo, or the "
                                                                           "image_id of a picture made earlier"},
                               "background": {"type": "string", "description": "The new background scene in English, "
                                                                                "e.g. 'a sunny sandy beach with "
                                                                                "turquoise water'"}},
                              ["image", "background"]),
}
INTERNET_TOOLS = {"web_search", "fetch_url"}
WORKSPACE_TOOLS = {"list_files", "read_file", "write_file", "delete_file", "search_files"}


def specs(settings, attachments_present=False):
    if not settings.get("tools", True):
        return []
    coding = not settings.get("internet", True)

    # Build a dynamic copy of workspace tool descriptions reflecting configured paths
    extra_paths = [p.strip() for p in settings.get("workspace_paths", []) if p.strip()]
    if extra_paths:
        path_note = "~/hearth-workspace and " + ", ".join(extra_paths)
    else:
        path_note = "~/hearth-workspace"

    dynamic_specs = dict(SPECS)
    dynamic_specs["list_files"] = _fn(
        "list_files",
        f"List the files in your workspace ({path_note}). Only available in coding mode (when internet is off).",
        {})
    dynamic_specs["read_file"] = _fn(
        "read_file",
        f"Read a file from your workspace ({path_note}).",
        {"path": {"type": "string", "description": "Path relative to the workspace, or an absolute path inside an allowed directory"}},
        ["path"])
    dynamic_specs["write_file"] = _fn(
        "write_file",
        f"Create or overwrite a file in your workspace ({path_note}). The previous version is AUTOMATICALLY backed "
        f"up to a BackUp/ folder and logged, so just write the new file \u2014 no need to back it up yourself.",
        {"path": {"type": "string", "description": "Relative path (e.g. 'app.js') or absolute path inside an allowed directory"},
         "content": {"type": "string", "description": "The full file contents"},
         "note": {"type": "string", "description": "A short summary of what you changed, recorded in BackUp/CHANGELOG.md"}},
        ["path", "content"])
    dynamic_specs["delete_file"] = _fn(
        "delete_file",
        f"Delete a file or folder from your workspace ({path_note}). It is backed up to BackUp/ first.",
        {"path": {"type": "string"},
         "note": {"type": "string", "description": "Why it is being deleted, recorded in BackUp/CHANGELOG.md"}},
        ["path"])

    names = [n for n in dynamic_specs if n not in INTERNET_TOOLS or settings.get("internet", True)]
    if not coding:
        names = [n for n in names if n not in WORKSPACE_TOOLS]
    if not attachments_present:
        names = [n for n in names if n != "read_attachment"]
    if not imagegen.available():
        names = [n for n in names if n not in ("generate_image", "edit_image", "replace_background")]
    return [dynamic_specs[n] for n in names]


# ---------------------------------------------------------------------------
# implementations
# ---------------------------------------------------------------------------

def _get(url, data=None, timeout=15, limit=3 * 1024 * 1024):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit), r.headers.get("Content-Type", ""), r.geturl()


def web_search(query, max_results=6, **_):
    max_results = max(1, min(int(max_results or 6), 10))
    results = []
    try:
        body, _, _ = _get("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode())
        page = body.decode("utf8", "replace")
        blocks = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                            page, re.S)
        for href, title, rest in blocks:
            m = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
            url = href
            q = urllib.parse.urlparse(href)
            if "uddg" in urllib.parse.parse_qs(q.query):
                url = urllib.parse.parse_qs(q.query)["uddg"][0]
            if "duckduckgo.com/y.js" in url:
                continue  # ads
            results.append({"title": _strip(title), "url": url, "snippet": _strip(m.group(1)) if m else ""})
            if len(results) >= max_results:
                break
    except Exception as e:
        err = str(e)
    else:
        err = ""
    if not results:
        try:  # lite endpoint as a fallback
            body, _, _ = _get("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}))
            page = body.decode("utf8", "replace")
            for href, title in re.findall(r'<a rel="nofollow" href="([^"]+)" class=.result-link.>(.*?)</a>', page, re.S):
                results.append({"title": _strip(title), "url": href, "snippet": ""})
                if len(results) >= max_results:
                    break
        except Exception as e:
            err = err or str(e)
    if not results:
        return {"query": query, "results": [], "error": err or "No results (the search engine may be rate-limiting)."}
    return {"query": query, "results": results}


def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "button", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table",
             "blockquote", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3"):
                self.out.append("## ")
            elif tag == "li":
                self.out.append("• ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)

    def text(self):
        t = "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = "\n".join(line for line in t.split("\n") if not re.fullmatch(r"[\s•#]*", line) or not line)
        t = re.sub(r"\n\s*\n\s*", "\n\n", t)
        return t.strip()


def _check_host(url, allow_local, browseable_nodes=()):
    host = urllib.parse.urlparse(url).hostname or ""
    if allow_local:
        return
    # Explicitly allowed nodes (from Settings → Browseable nodes) bypass the
    # private-IP check — match on bare hostname or host:port.
    netloc = urllib.parse.urlparse(url).netloc or ""
    for node in browseable_nodes:
        node = node.strip()
        if node and (host == node or netloc == node):
            return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Can't resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("That address is on your local network. Add it to 'Browseable nodes' in "
                             "settings, or turn on 'Allow local network pages', to let the assistant read it.")


def fetch_url(url, max_chars=8000, _settings=None, **_):
    if not re.match(r"^https?://", url or ""):
        return {"error": "Only http(s) URLs can be fetched"}
    try:
        _check_host(url, (_settings or {}).get("allow_local_urls"),
                    (_settings or {}).get("browseable_nodes", ()))
        body, ctype, final = _get(url, timeout=20)
    except Exception as e:
        return {"url": url, "error": str(e)}
    max_chars = max(500, min(int(max_chars or 8000), 40000))
    if "pdf" in ctype or final.lower().endswith(".pdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(body)
            tmp.flush()
            r = subprocess.run(["pdftotext", "-layout", tmp.name, "-"], capture_output=True, text=True, timeout=60)
        text, title = r.stdout, final.rsplit("/", 1)[-1]
    elif "html" in ctype or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        p = _Text()
        p.feed(body.decode("utf8", "replace"))
        text, title = p.text(), " ".join(p.title.split())
    else:
        text, title = body.decode("utf8", "replace"), final.rsplit("/", 1)[-1]
    return {"url": final, "title": title, "text": text[:max_chars],
            "truncated": len(text) > max_chars, "total_chars": len(text)}


def get_datetime(timezone=None, **_):
    now = datetime.datetime.now().astimezone()
    zone = "local"
    if timezone:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(str(timezone).strip().replace(" ", "_")))
            zone = str(timezone)
        except Exception:
            return {"error": f"Unknown time zone {timezone!r}. Use a name like America/Mexico_City."}
    return {"zone": zone, "datetime": now.strftime("%A, %B %-d, %Y %-I:%M %p ") + (now.tzname() or ""),
            "utc_offset": now.strftime("%z"), "iso": now.isoformat(timespec="seconds")}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "log", "log10",
                                         "log2", "exp", "floor", "ceil", "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round, min=min, max=max)
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("unsupported expression")


def calculate(expression, **_):
    try:
        expr = str(expression).replace("^", "**").replace("×", "*").replace("÷", "/")
        return {"expression": expression, "result": _eval(ast.parse(expr, mode="eval"))}
    except Exception as e:
        return {"expression": expression, "error": str(e)}


def remember(fact, **_):
    m = store.add_memory(fact, source="model")
    return {"saved": True, "id": m["id"], "fact": m["text"]}


def forget(memory_id, **_):
    return {"removed": store.delete_memory(str(memory_id).strip())}


def read_attachment(name, offset=0, length=12000, _attachments=(), **_):
    for att_id in _attachments:
        try:
            meta, _ = store.upload_path(att_id)
        except (KeyError, ValueError):
            continue
        if meta["name"] == name or meta["name"].lower() == str(name).lower():
            text = meta.get("text") or ""
            offset, length = max(0, int(offset or 0)), max(500, min(int(length or 12000), 40000))
            return {"name": meta["name"], "offset": offset, "text": text[offset:offset + length],
                    "remaining": max(0, len(text) - offset - length)}
    return {"error": f"No attached file named {name}"}


def generate_image(prompt, size="square", **_):
    return imagegen.generate(prompt, size)


def _find_image(image, attachments):
    ref = str(image).strip()
    candidates = list(attachments)
    if ref.isalnum():
        candidates.insert(0, ref)
    images = []
    for att_id in candidates:
        try:
            meta, path = store.upload_path(att_id)
        except (KeyError, ValueError):
            continue
        if meta.get("kind") != "image":
            continue
        if att_id == ref or meta["name"].lower() == ref.lower():
            return path
        images.append(path)
    return images[-1] if images else None     # fall back to the most recent attached picture


def replace_background(image, background, _attachments=(), **_):
    path = _find_image(image, _attachments)
    if not path:
        return {"error": "No picture to work on. Ask the user to attach the photo."}
    return imagegen.replace_background(path, background)


def edit_image(image, prompt, strength=0.7, _attachments=(), **_):
    ref = str(image).strip()
    candidates = list(_attachments)
    if ref.isalnum():
        candidates.insert(0, ref)                     # an image_id from an earlier generate/edit
    for att_id in candidates:
        try:
            meta, path = store.upload_path(att_id)
        except (KeyError, ValueError):
            continue
        if meta.get("kind") == "image" and (att_id == ref or meta["name"].lower() == ref.lower()
                                            or len([a for a in _attachments]) == 1):
            return imagegen.generate(prompt, init_image=path, strength=strength)
    return {"error": f"No picture named {image} in this chat. Ask the user to attach it."}


IMPL = {"web_search": web_search, "fetch_url": fetch_url, "get_datetime": get_datetime, "calculate": calculate,
        "list_files": workspace.list_files, "read_file": workspace.read_file,
        "write_file": workspace.write_file, "delete_file": workspace.delete_file,
        "search_files": workspace.search_files,
        "remember": remember, "forget": forget, "read_attachment": read_attachment, "generate_image": generate_image,
        "edit_image": edit_image, "replace_background": replace_background}


def run(name, args, settings, attachments=()):
    fn = IMPL.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}"}
    if name in INTERNET_TOOLS and not settings.get("internet", True):
        return {"error": "Internet access is turned off"}
    if not isinstance(args, dict):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            args = {}
    try:
        return fn(**args, _settings=settings, _attachments=attachments)
    except TypeError as e:
        return {"error": f"Bad arguments: {e}"}
    except Exception as e:
        return {"error": str(e)}


def describe(name, args):
    """One-line human label for a tool call, shown in the chat."""
    args = args if isinstance(args, dict) else {}
    return {
        "web_search": lambda: f"Searched the web for “{args.get('query', '')}”",
        "fetch_url": lambda: f"Read {urllib.parse.urlparse(args.get('url', '')).netloc or args.get('url', '')}",
        "get_datetime": lambda: f"Checked the time in {args['timezone'].split('/')[-1].replace('_', ' ')}"
        if args.get("timezone") else "Checked the date and time",
        "calculate": lambda: f"Calculated {args.get('expression', '')}",
        "remember": lambda: "Saved to memory",
        "forget": lambda: "Removed from memory",
        "read_attachment": lambda: f"Read more of {args.get('name', 'a file')}",
        "list_files": lambda: "Listed workspace files",
        "read_file": lambda: f"Read {args.get('path', 'a file')}",
        "write_file": lambda: f"Wrote {args.get('path', 'a file')}",
        "delete_file": lambda: f"Deleted {args.get('path', 'a file')}",
        "search_files": lambda: f"Searched files for “{args.get('query', '')}”",
        "generate_image": lambda: "Created an image",
        "edit_image": lambda: "Edited the image",
        "replace_background": lambda: "Changed the background",
    }.get(name, lambda: f"Used {name}")()
