"""Render og-design.html to og.png at exactly 1200x630."""
import subprocess
import os
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).parent
HTML = DEPLOY_DIR / "og-design.html"
RAW_PNG = DEPLOY_DIR / "og-raw.png"
FINAL_PNG = DEPLOY_DIR / "og.png"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Render at 1300x800 (Chrome headless quirk: body is sized 1300x800 but Chrome
# captures more reliably with extra room). PIL crops to top-left 1200x630.
file_url = "file:///" + str(HTML).replace("\\", "/")
cmd = [
    CHROME,
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--force-device-scale-factor=1",
    "--window-size=1300,800",
    f"--screenshot={RAW_PNG}",
    file_url,
]
print("Rendering with Chrome headless...")
result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
if result.returncode != 0:
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)
    sys.exit(1)

if not RAW_PNG.exists():
    print("ERROR: raw screenshot not produced")
    sys.exit(1)

from PIL import Image
img = Image.open(RAW_PNG)
print(f"Raw size: {img.size}")

# Crop top-left 1200x630
cropped = img.crop((0, 0, 1200, 630))
cropped.save(FINAL_PNG, "PNG", optimize=True)
print(f"Wrote {FINAL_PNG} at {cropped.size}, {FINAL_PNG.stat().st_size} bytes")

# Cleanup raw
RAW_PNG.unlink()
