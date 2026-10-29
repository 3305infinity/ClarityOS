"""Convert a high-res PNG to a macOS .icns file using iconutil."""

import os
import shutil
import subprocess
from PIL import Image

SRC = "/Users/enaihouwaspaul/Downloads/icon.png"
ICONSET = "/tmp/moonwalk.iconset"
PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ICNS_OUT = os.path.join(PROJECT, "build", "icon.icns")
PNG_OUT = os.path.join(PROJECT, "build", "icon.png")

# Required icon sizes for macOS .icns
SIZES = [16, 32, 64, 128, 256, 512, 1024]

# Clean up old iconset
if os.path.exists(ICONSET):
    shutil.rmtree(ICONSET)
os.makedirs(ICONSET)

img = Image.open(SRC).convert("RGBA")
# Make it square by cropping to the smaller dimension
w, h = img.size
sz = min(w, h)
left = (w - sz) // 2
top = (h - sz) // 2
img = img.crop((left, top, left + sz, top + sz))
print(f"Cropped to {img.size}")

for size in SIZES:
    resized = img.resize((size, size), Image.LANCZOS)
    # Standard resolution
    resized.save(os.path.join(ICONSET, f"icon_{size}x{size}.png"))
    # @2x (Retina) for the half-size category
    if size >= 32:
        half = size // 2
        resized.save(os.path.join(ICONSET, f"icon_{half}x{half}@2x.png"))

# Also save 1024x1024 PNG for electron-builder
img.resize((1024, 1024), Image.LANCZOS).save(PNG_OUT)
print(f"Saved {PNG_OUT}")

# Convert iconset to icns
result = subprocess.run(
    ["iconutil", "-c", "icns", ICONSET, "-o", ICNS_OUT],
    capture_output=True,
    text=True,
)
if result.returncode == 0:
    print(f"Created {ICNS_OUT} ({os.path.getsize(ICNS_OUT)} bytes)")
else:
    print(f"iconutil failed: {result.stderr}")

# Cleanup
shutil.rmtree(ICONSET)
