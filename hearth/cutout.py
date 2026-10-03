"""Run by the image-gen venv's Python: cut the main subject out of a photo (transparent PNG)."""
import sys

from PIL import Image
from rembg import new_session, remove

src, out = sys.argv[1], sys.argv[2]
session = new_session("isnet-general-use")
im = Image.open(src).convert("RGB")
remove(im, session=session, post_process_mask=True).save(out)
