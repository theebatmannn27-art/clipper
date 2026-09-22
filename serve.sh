#!/usr/bin/env bash
# Start the Clipper Studio web app (single-user SaaS MVP).
#   ./serve.sh            # on 0.0.0.0:8080
#   PORT=9000 ./serve.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo "[serve] installed Python deps into .venv"
fi

# ffmpeg: PATH first, else the static build shipped by imageio-ffmpeg.
if ! command -v ffmpeg >/dev/null 2>&1; then
  FFMPEG_BIN=$(.venv/bin/python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())" 2>/dev/null || true)
  if [ -n "$FFMPEG_BIN" ] && [ -f "$FFMPEG_BIN" ]; then
    export FFMPEG="$FFMPEG_BIN"
    echo "[serve] using static ffmpeg: $FFMPEG_BIN"
  else
    echo "[serve] WARN: no ffmpeg found on PATH and no static build available." >&2
  fi
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
echo "[serve] http://localhost:$PORT"
exec .venv/bin/python -m app
