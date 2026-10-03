"""Local image generation with stable-diffusion.cpp (SDXL-Turbo on the GPU via Vulkan).

The GPU can't hold a chat model and the image model at once, so loaded Ollama models are
unloaded first; Ollama reloads the chat model by itself on the next turn.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import files

HOME = os.path.expanduser("~/.local/share/hearth-imagegen")
BIN = os.path.join(HOME, "bin")
SD_CLI = os.path.join(BIN, "sd-cli")
MODEL = os.path.join(HOME, "models", "sdxl-turbo-q8_0.gguf")
VENV_PY = os.path.join(HOME, "venv", "bin", "python")
CUTOUT = os.path.join(os.path.dirname(__file__), "cutout.py")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA

SIZES = {"square": (768, 768), "wide": (1024, 576), "tall": (576, 1024)}
_lock = threading.Lock()


def available():
    return os.access(SD_CLI, os.X_OK) and os.path.exists(MODEL)


def _free_gpu():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as r:
            loaded = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return
    for name in loaded:
        body = json.dumps({"model": name, "keep_alive": 0}).encode()
        req = urllib.request.Request(OLLAMA + "/api/generate", data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass
    # give the driver a moment to release the memory
    for _ in range(20):
        try:
            with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as r:
                if not json.load(r).get("models"):
                    break
        except Exception:
            break
        time.sleep(0.5)


def _prepare_init(src, out):
    """Fit the source picture to SDXL-friendly dimensions (multiples of 64, ~0.6-1 megapixel)."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = im.size
    scale = (786_432 / (w * h)) ** 0.5
    w, h = max(512, round(w * scale / 64) * 64), max(512, round(h * scale / 64) * 64)
    w, h = min(w, 1216), min(h, 1216)
    im.resize((w, h), Image.LANCZOS).save(out)
    return w, h


def generate(prompt, size="square", seed=None, init_image=None, strength=0.7):
    if not available():
        raise RuntimeError("Image generation isn't installed on this computer.")
    prompt = " ".join(str(prompt).split())[:1000]
    if not prompt:
        raise ValueError("Describe the image to create.")
    w, h = SIZES.get(str(size).lower(), SIZES["square"])
    seed = int(seed) if seed not in (None, "") else int(time.time()) % 2_147_483_647
    with _lock:
        _free_gpu()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "image.png")
            steps = 4
            extra = []
            if init_image:
                init = os.path.join(tmp, "init.png")
                w, h = _prepare_init(init_image, init)
                strength = min(max(float(strength or 0.7), 0.3), 0.9)
                steps = min(12, max(6, round(6 / strength)))  # ~6 real denoising steps whatever the strength
                extra = ["-i", init, "--strength", f"{strength:.2f}"]
            cmd = [SD_CLI, "-m", MODEL, "-p", prompt, "--steps", str(steps), "--cfg-scale", "1.0",
                   "--sampling-method", "euler_a", "-W", str(w), "-H", str(h), "--seed", str(seed),
                   "--vae-tiling", "-o", out] + extra
            env = dict(os.environ, LD_LIBRARY_PATH=BIN)
            t0 = time.time()
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0 or not os.path.exists(out):
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                raise RuntimeError("Image generation failed: " + " / ".join(tail))
            with open(out, "rb") as f:
                data = f.read()
    slug = "".join(c if c.isalnum() else "-" for c in prompt.lower())[:40].strip("-") or "image"
    meta = files.ingest(f"{slug}.png", data)
    return {"image_id": meta["id"], "prompt": prompt, "size": f"{w}x{h}", "seed": seed,
            **({"edited": True, "strength": round(float(strength), 2)} if init_image else {}),
            "seconds": round(time.time() - t0, 1),
            "note": "The picture is already displayed to the user above your reply. Do NOT add an image link, "
                    "markdown image or URL for it; just reply with one or two short sentences. If the user later "
                    f"wants this picture changed, call edit_image with image=\"{meta['id']}\"."}


def replace_background(image_path, background):
    """Keep the photo's subject untouched and put it in front of a newly generated background."""
    from PIL import Image, ImageFilter, ImageOps
    if not os.access(VENV_PY, os.X_OK):
        raise RuntimeError("Background removal isn't installed on this computer.")
    background = " ".join(str(background).split())[:600]
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.png")
        ImageOps.exif_transpose(Image.open(image_path)).convert("RGB").save(src)
        cut = os.path.join(tmp, "cut.png")
        proc = subprocess.run([VENV_PY, CUTOUT, src, cut], capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(cut):
            raise RuntimeError("Couldn't separate the subject: " + (proc.stderr.strip().splitlines() or ["?"])[-1])
        subject = Image.open(cut).convert("RGBA")
        w0, h0 = subject.size
        # background at an SDXL-friendly size with the photo's shape
        scale = (786_432 / (w0 * h0)) ** 0.5
        w, h = min(1216, max(512, round(w0 * scale / 64) * 64)), min(1216, max(512, round(h0 * scale / 64) * 64))
        bg_prompt = f"{background}, empty scene, no people, no animals, photographic, natural light"
        with _lock:
            _free_gpu()
            out = os.path.join(tmp, "bg.png")
            cmd = [SD_CLI, "-m", MODEL, "-p", bg_prompt, "--steps", "4", "--cfg-scale", "1.0",
                   "--sampling-method", "euler_a", "-W", str(w), "-H", str(h),
                   "--seed", str(int(time.time()) % 2_147_483_647), "--vae-tiling", "-o", out]
            proc = subprocess.run(cmd, env=dict(os.environ, LD_LIBRARY_PATH=BIN), capture_output=True, text=True,
                                  timeout=600)
            if proc.returncode != 0 or not os.path.exists(out):
                raise RuntimeError("Background generation failed")
        # composite at the photo's full resolution; soften the cut edge a little
        bg = Image.open(out).convert("RGB").resize((w0, h0), Image.LANCZOS)
        alpha = subject.getchannel("A").filter(ImageFilter.GaussianBlur(1.2))
        bg.paste(subject.convert("RGB"), (0, 0), alpha)
        buf = os.path.join(tmp, "final.jpg")
        bg.save(buf, quality=92)
        with open(buf, "rb") as f:
            data = f.read()
    slug = "".join(c if c.isalnum() else "-" for c in background.lower())[:40].strip("-") or "new-background"
    meta = files.ingest(f"{slug}.jpg", data)
    return {"image_id": meta["id"], "prompt": background, "size": f"{w0}x{h0}",
            "seconds": round(time.time() - t0, 1),
            "note": "The new picture is already displayed to the user above your reply. Do NOT add an image link "
                    "or URL; just reply with one or two short sentences. To change it again call edit_image or "
                    f"replace_background with image=\"{meta['id']}\"."}
