"""Turn uploaded files into something a model can read."""

import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile

from . import store

INLINE_CHARS = 24000      # text placed straight into the prompt; the rest via read_attachment
MAX_UPLOAD = 200 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".conf", ".csv", ".tsv", ".log", ".sh", ".bash", ".zsh", ".c", ".h", ".cpp", ".hpp", ".cs", ".java",
            ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".html", ".htm", ".css", ".scss", ".xml", ".sql", ".env",
            ".gradle", ".swift", ".lua", ".r", ".pl", ".service", ".timer", ".desktop", ".dockerfile", ".tex"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _clean_name(name):
    name = os.path.basename(name or "file").strip() or "file"
    return re.sub(r"[^\w.\- ()]", "_", name)[:120]


def _docx_text(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pdf_text(path):
    if not shutil.which("pdftotext"):
        return None
    r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else None


def extract_text(path, name, mime):
    ext = os.path.splitext(name.lower())[1]
    if ext == ".pdf" or mime == "application/pdf":
        return _pdf_text(path)
    if ext == ".docx":
        try:
            return _docx_text(path)
        except Exception:
            return None
    with open(path, "rb") as f:
        raw = f.read(4 * 1024 * 1024)
    if ext in TEXT_EXT or (mime or "").startswith("text/") or name.lower() in ("dockerfile", "makefile"):
        return raw.decode("utf8", "replace")
    if b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf8")
    except UnicodeDecodeError:
        return None


def ingest(name, data):
    """Store an upload and return its metadata (what the UI and the prompt builder use)."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("File is larger than 200 MB")
    name = _clean_name(name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    ext = os.path.splitext(name.lower())[1]
    meta = {"id": uuid.uuid4().hex[:16], "name": name, "mime": mime, "size": len(data), "created": time.time()}
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
    try:
        if ext in IMAGE_EXT or mime.startswith("image/"):
            meta["kind"] = "image"
        else:
            text = extract_text(tmp.name, name, mime)
            if text is not None:
                meta.update(kind="text", chars=len(text), text=text)
            else:
                meta["kind"] = "binary"
    finally:
        os.unlink(tmp.name)
    store.save_upload(meta, data)
    return public(meta)


def public(meta):
    return {k: v for k, v in meta.items() if k != "text"}


def text_of(att_id):
    meta, _ = store.upload_path(att_id)
    return meta.get("text")


def prompt_block(att_id):
    """How an attachment appears inside the user's message."""
    meta, _ = store.upload_path(att_id)
    if meta["kind"] == "image":
        return None
    if meta["kind"] == "binary":
        return f"[Attached file {meta['name']} ({meta['mime']}, {meta['size']:,} bytes) — binary, contents not readable]"
    text = meta.get("text") or ""
    shown = text[:INLINE_CHARS]
    more = ""
    if len(text) > INLINE_CHARS:
        more = (f"\n[… {len(text) - INLINE_CHARS:,} more characters. Use the read_attachment tool with "
                f"name=\"{meta['name']}\" and offset={INLINE_CHARS} to read further.]")
    return f"<file name=\"{meta['name']}\">\n{shown}{more}\n</file>"
