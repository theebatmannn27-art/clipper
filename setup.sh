#!/usr/bin/env bash
# Reinstall the two external tools this pipeline needs (ffmpeg + yt-dlp).
# Nothing here is needed inside the repo itself -- run it once per session.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[setup] installing ffmpeg (static build)…"
  curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    | tar -xJ -C /tmp
  D=$(ls -d /tmp/ffmpeg-*-amd64-static | head -1)
  install -m755 "$D/ffmpeg" "$D/ffprobe" /usr/local/bin/
  rm -rf "$D"
fi
command -v yt-dlp >/dev/null 2>&1 || { echo "[setup] installing yt-dlp…"; pip install -q yt-dlp; }
python3 -c "import numpy" 2>/dev/null || pip install -q numpy

echo "[setup] ffmpeg  $(ffmpeg -version | head -1 | cut -d' ' -f3)"
echo "[setup] yt-dlp  $(yt-dlp --version)"
echo "[setup] ready"
