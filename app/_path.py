"""Shared path setup: make the repo root and the `py/` pipeline importable
from the web app, and expose the repo/data dirs in one place."""
from __future__ import annotations
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYDIR = os.path.join(REPO, "py")

for p in (REPO, PYDIR):
    if p not in sys.path:
        sys.path.insert(0, p)

DATA_DIR = os.environ.get("CLIP_DATA_DIR") or os.path.join(REPO, "data")
