"""Runtime paths, HTTP security defaults, and reusable constants."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FLUX_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DOWNLOAD_DIR = Path(os.getenv("FLUX_DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).expanduser().resolve()
STATIC_DIR = BASE_DIR / "static"
ALLOW_PRIVATE_HOSTS = os.getenv("FLUX_ALLOW_PRIVATE_HOSTS", "0").lower() in ("1", "true", "yes")
MAX_WORKERS = 6
MAX_PLAYLIST_ITEMS = 100
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

for folder in (DATA_DIR, DOWNLOAD_DIR, DOWNLOAD_DIR / ".parts"):
    folder.mkdir(parents=True, exist_ok=True)
