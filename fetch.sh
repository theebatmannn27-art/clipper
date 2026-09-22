#!/usr/bin/env bash
# Fetch a YouTube video + its subtitles + thumbnail.
#   usage: ./fetch.sh "<youtube-url>" [/path/to/cookies.txt]
set -uo pipefail
cd "$(dirname "$0")"
URL="${1:?usage: fetch.sh <url> [cookies.txt]}"
COOKIES="${2:-}"

YTDLP=(yt-dlp --no-warnings --no-playlist
       -f "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
       --merge-output-format mp4
       --write-auto-subs --write-subs --sub-langs "en.*,hi.*,en" --sub-format vtt
       --write-thumbnail --write-info-json
       -o "in/%(id)s.%(ext)s"
       -P ".")

if [ -n "$COOKIES" ]; then
  YTDLP+=(--cookies "$COOKIES")
  echo "[fetch] using cookies: $COOKIES"
fi

echo "[fetch] downloading…"
if ! "${YTDLP[@]}" "$URL"; then
  echo "[fetch] FAILED — datacenter IP block or bad cookies." >&2
  exit 1
fi
echo "[fetch] done:"; ls -la in/ 2>/dev/null
