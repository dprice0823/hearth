"""Auto model choice: the lightest installed model that can handle the message.

Deliberately heuristic (no extra model call), so choosing costs nothing. Swapping a
model into memory is the expensive part, so a model that's already loaded wins
whenever it's capable enough.
"""

import json
import re
import shutil
import subprocess

from . import store

AUTO = "auto"
_UNCENSORED = re.compile(r"abliterat|obliterat|uncensor|dolphin", re.I)
_CODE = re.compile(r"```|\b(code|script|bash|python|javascript|typescript|kotlin|java|rust|golang|c\+\+|sql|regex|"
                   r"function|compile|compiler|stack ?trace|traceback|exception|segfault|debug|refactor|dockerfile|"
                   r"yaml|json|api|endpoint|git|npm|pip|gradle|systemd|nginx|cron|shell|terminal|command line)\b", re.I)
_DEEP = re.compile(r"\b(why|explain|compare|comparison|pros and cons|trade-?offs?|plan|design|architecture|analy[sz]e|"
                   r"strategy|step by step|in detail|reason|prove|evaluate|recommend|decide|think through|essay|"
                   r"summari[sz]e)\b", re.I)
_CODE_EXT = (".py", ".js", ".ts", ".sh", ".kt", ".kts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sql", ".yml",
             ".yaml", ".json", ".toml", ".gradle", ".php", ".rb", ".css", ".html", ".log", ".service")

_vram = None


def vram_bytes():
    """Total GPU memory (first NVIDIA GPU), or 0 if unknown."""
    global _vram
    if _vram is None:
        _vram = 0
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                _vram = int(out.split()[0]) * 1024 * 1024
            except Exception:
                pass
    return _vram


def _needs(chat):
    """What the latest user message needs."""
    msg = next((m for m in reversed(chat["messages"]) if m["role"] == "user"), None) or {}
    text = msg.get("content") or ""
    image = code = False
    attached_chars = 0
    for a in msg.get("attachments") or []:
        try:
            meta, _ = store.upload_path(a)
        except (KeyError, ValueError):
            continue
        if meta["kind"] == "image":
            image = True
        else:
            attached_chars += meta.get("chars") or 0
            code = code or meta["name"].lower().endswith(_CODE_EXT)
    code = code or bool(_CODE.search(text))
    deep = bool(_DEEP.search(text)) or len(text) > 700 or attached_chars > 6000
    return {"vision": image, "code": code, "deep": deep, "long": attached_chars > 12000 or len(text) > 4000}


def _loaded():
    from .agent import _request, OllamaError
    try:
        with _request("/api/ps", timeout=5) as r:
            return {m.get("name") for m in json.load(r).get("models", [])}
    except OllamaError:
        return set()


def choose(chat, models):
    """Return (model_name, reason). models = agent.list_models() output."""
    need = _needs(chat)
    budget = vram_bytes() or 8 * 1024 ** 3
    fits = lambda m: m["size"] <= budget * 0.95  # noqa: E731
    pool = [m for m in models if not _UNCENSORED.search(m["name"]) and "tools" in m["caps"] and fits(m)]
    if not pool:
        pool = [m for m in models if fits(m)] or models
    by_size = sorted(pool, key=lambda m: m["size"])
    small = [m for m in by_size if m["size"] < 3 * 1024 ** 3]

    def first(cands):
        return cands[0] if cands else None

    if need["vision"]:
        seeing = [m for m in by_size if "vision" in m["caps"]]
        if need["deep"]:
            thinking = [m for m in seeing if "thinking" in m["caps"]]
            pick, why = first(thinking[1:] or thinking or seeing), "image + reasoning"
        else:
            pick, why = first(seeing), "image"
        candidates = seeing
    elif need["code"]:
        coders = [m for m in by_size if "coder" in m["name"].lower() or "code" in m.get("family", "").lower()]
        candidates = coders or [m for m in by_size if m["size"] >= 3 * 1024 ** 3] or by_size
        pick, why = first(candidates), "code"
    elif need["deep"] or need["long"]:
        thinking = [m for m in by_size if "thinking" in m["caps"]]
        mids = [m for m in thinking if m["size"] >= 3 * 1024 ** 3] or thinking or \
            [m for m in by_size if m["size"] >= 3 * 1024 ** 3] or by_size
        candidates = mids
        pick, why = first(mids), "reasoning" if need["deep"] else "long input"
    else:
        # any capable model will do - prefer the smallest, but reuse whatever is loaded
        candidates = by_size
        pick, why = first(small or by_size), "quick question"

    # Reuse a model that's already in memory if it's in the capable set - no swap needed.
    loaded = _loaded()
    warm = [m for m in candidates if m["name"] in loaded]
    if warm and pick and warm[0]["name"] != pick["name"]:
        return warm[0]["name"], f"{why} · already loaded"
    if not pick:
        pick = by_size[0]
    return pick["name"], why
