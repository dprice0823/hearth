"""Sandboxed file I/O for the model.

By default the workspace is ~/hearth-workspace. The user can add extra allowed
directories in Settings → Workspace paths. Every path is resolved and checked
against the allowed roots before any operation — nothing outside them can be
touched.
"""

import datetime
import os
import shutil

DEFAULT_ROOT = os.path.expanduser("~/hearth-workspace")
MAX_READ = 60_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".gradle", "build", ".venv"}


def _roots(settings=None):
    """Return a list of resolved, allowed root directories."""
    roots = [os.path.realpath(DEFAULT_ROOT)]
    for p in (settings or {}).get("workspace_paths", []):
        p = p.strip()
        if p:
            roots.append(os.path.realpath(os.path.expanduser(p)))
    return roots


def _ensure(settings=None):
    os.makedirs(DEFAULT_ROOT, exist_ok=True)
    for p in (settings or {}).get("workspace_paths", []):
        p = p.strip()
        if p:
            try:
                os.makedirs(os.path.realpath(os.path.expanduser(p)), exist_ok=True)
            except OSError:
                pass


def _resolve(rel, settings=None):
    """Return an absolute path for *rel*, which may be:
      - a bare relative path → resolved under DEFAULT_ROOT
      - an absolute path      → checked against any allowed root
    Raises ValueError if the resolved path escapes every allowed root.
    """
    _ensure(settings)
    roots = _roots(settings)
    rel = str(rel or "").strip()

    if os.path.isabs(rel):
        full = os.path.realpath(rel)
    else:
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(DEFAULT_ROOT, rel))

    for root in roots:
        if full == root or full.startswith(root + os.sep):
            return full

    # Give a helpful message listing what's allowed
    allowed = ", ".join(roots)
    raise ValueError(f"Path is outside allowed directories ({allowed})")


# ---------------------------------------------------------------------------
# tool implementations (each accepts **_ and a _settings kwarg)
# ---------------------------------------------------------------------------

def list_files(_settings=None, **_):
    _ensure(_settings)
    roots = _roots(_settings)
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                p = os.path.join(base, n)
                try:
                    out.append({
                        "path": os.path.relpath(p, DEFAULT_ROOT) if p.startswith(DEFAULT_ROOT)
                                else p,
                        "bytes": os.path.getsize(p),
                    })
                except OSError:
                    pass
            if len(out) > 500:
                break

    return {
        "workspace": DEFAULT_ROOT,
        "extra_paths": roots[1:],
        "files": sorted(out, key=lambda f: f["path"]),
    }


def read_file(path, _settings=None, **_):
    full = _resolve(path, _settings)
    if not os.path.isfile(full):
        return {"error": f"No file at {path}"}
    with open(full, "r", errors="replace") as f:
        text = f.read(MAX_READ + 1)
    return {"path": path, "content": text[:MAX_READ], "truncated": len(text) > MAX_READ}


def _root_for(full, settings=None):
    for root in _roots(settings):
        if full == root or full.startswith(root + os.sep):
            return root
    return DEFAULT_ROOT


def _backup_and_log(full, settings, action, note=""):
    """Copy the file into <project root>/BackUp/ and append to BackUp/CHANGELOG.md BEFORE it is changed."""
    root = _root_for(full, settings)
    rel = os.path.relpath(full, root)
    if rel.split(os.sep, 1)[0] == "BackUp":          # never back up the backups themselves
        return
    backup_dir = os.path.join(root, "BackUp")
    now = datetime.datetime.now()
    try:
        if os.path.isfile(full):
            dest_dir = os.path.join(backup_dir, os.path.dirname(rel))
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(full, os.path.join(dest_dir, os.path.basename(rel) + f".bak-{now:%Y%m%d-%H%M%S-%f}"))
        elif os.path.isdir(full):
            shutil.copytree(full, os.path.join(backup_dir, rel + f"-{now:%Y%m%d-%H%M%S-%f}"),
                            ignore=shutil.ignore_patterns(*SKIP_DIRS, "BackUp"))
    except OSError:
        pass
    try:
        os.makedirs(backup_dir, exist_ok=True)
        with open(os.path.join(backup_dir, "CHANGELOG.md"), "a") as ch:
            ch.write(f"## {now:%Y-%m-%d %H:%M:%S}\n- `{rel}` \u2014 {(note or '').strip() or action}\n\n")
    except OSError:
        pass


def write_file(path, content="", note="", _settings=None, **_):
    full = _resolve(path, _settings)
    existed = os.path.isfile(full)
    _backup_and_log(full, _settings, "updated" if existed else "created", note)
    os.makedirs(os.path.dirname(full) or DEFAULT_ROOT, exist_ok=True)
    data = content if isinstance(content, str) else str(content)
    with open(full, "w") as f:
        f.write(data)
    return {"path": path, "bytes": len(data.encode()), "saved": True, "backed_up": existed}


def delete_file(path, note="", _settings=None, **_):
    full = _resolve(path, _settings)
    roots = _roots(_settings)
    if full in roots:
        return {"error": "won't delete a root directory"}
    if not os.path.exists(full):
        return {"error": f"Nothing at {path}"}
    _backup_and_log(full, _settings, "deleted", note)
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return {"path": path, "deleted": True}


def search_files(query, _settings=None, **_):
    """Search for a text string across every file in the allowed workspace roots.
    Case-insensitive; skips binaries and files over 2 MB. Returns file + line number + line."""
    _ensure(_settings)
    roots = _roots(_settings)
    q = str(query or "").strip()
    if not q:
        return {"error": "Give some text to search for."}
    ql = q.lower()
    matches = []
    scanned = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                p = os.path.join(base, n)
                try:
                    if os.path.getsize(p) > 2_000_000:
                        continue
                    with open(p, "r", errors="strict") as fh:
                        for i, line in enumerate(fh, 1):
                            if ql in line.lower():
                                rel = os.path.relpath(p, DEFAULT_ROOT) if p.startswith(DEFAULT_ROOT) else p
                                matches.append({"file": rel, "line": i, "text": line.strip()[:200]})
                                if len(matches) >= 200:
                                    return {"query": q, "matches": matches, "truncated": True}
                    scanned += 1
                except (OSError, UnicodeDecodeError):
                    continue
    return {"query": q, "matches": matches, "count": len(matches), "files_searched": scanned}
