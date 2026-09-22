"""Resolve an ffmpeg binary shared by the CLI pipeline and the web app.

Order of preference:
  1. $FFMPEG env var
  2. `ffmpeg` on PATH
  3. the static ffmpeg shipped by the `imageio-ffmpeg` wheel (libx264 + libass +
     fontconfig, which is exactly what the caption rendering needs)
  4. a `ffmpeg` binary dropped at the repo root (see setup.sh / run.sh)
"""
from __future__ import annotations
import os
import shutil


def get_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env and os.path.isfile(env):
        return env

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass

    local = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ffmpeg"))
    if os.path.isfile(local):
        return local

    return "ffmpeg"
