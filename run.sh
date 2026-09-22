#!/usr/bin/env bash
# One-command pipeline: source video -> 9:16 captioned Shorts.
#
#   ./run.sh "<youtube-url>" [cookies.txt]      # download + clip
#   ./run.sh /path/to/local.mp4                 # clip a file you already have
#
# Optional env overrides:
#   LEN=45        clip length in seconds          (default 40)
#   COUNT=6       how many clips                  (default 5)
#   STYLE=punch   caption look: block | punch     (default block)
#   MODE=crop     framing: crop | blur            (default crop)
#   BRAND="@you"  watermark text                  (default none)
#   HOOK=1        burn the hook line at the top   (default on)
set -euo pipefail
cd "$(dirname "$0")"

LEN="${LEN:-40}"; COUNT="${COUNT:-5}"; STYLE="${STYLE:-block}"
MODE="${MODE:-crop}"; BRAND="${BRAND:-}"; HOOK="${HOOK:-1}"
WORK=work; mkdir -p "$WORK" out in

SRC="$1"
if [[ "$SRC" =~ ^https?:// ]]; then
  ./fetch.sh "$SRC" "${2:-}"
  SRC=$(ls -t in/*.mp4 in/*.mkv in/*.webm 2>/dev/null | head -1)
  echo "[run] source: $SRC"
fi
[ -f "$SRC" ] || { echo "no source video found" >&2; exit 1; }
BASE=$(basename "$SRC"); ID="${BASE%%.*}"

# 1. subtitles -> clean cues + word timings
VTT=$(ls "$WORK"/*.vtt in/*.vtt 2>/dev/null | head -1 || true)
if [ -z "${VTT:-}" ]; then
  VTT=$(ls in/"$ID"*.vtt 2>/dev/null | head -1 || true)
fi
if [ -n "${VTT:-}" ]; then
  python3 py/vttparse.py "$VTT" --json-out "$WORK/cues.json" --words-out "$WORK/words.json"
  CUES="--cues $WORK/cues.json --words $WORK/words.json"
else
  echo "[run] no subtitle file found - clips will have no captions"
  CUES=""
fi

# 2. find the peak moments
python3 py/peaks.py "$SRC" ${CUES:+--subs $WORK/cues.json} \
        --len "$LEN" --count "$COUNT" --json-out "$WORK/peaks.json"

# 3. cut vertical, captioned clips
python3 py/cut.py "$SRC" --peaks "$WORK/peaks.json" $CUES \
        --outdir out --mode "$MODE" --cap-style "$STYLE" \
        ${BRAND:+--brand "$BRAND"} $([ "$HOOK" = "1" ] && echo --hook)

# 4. thumbnail contact sheet for a quick visual review
FIRST=$(ls out/*.mp4 2>/dev/null | head -1 || true)
if [ -n "$FIRST" ]; then
  ../ffmpeg -y -v error -i "$FIRST" -vf "select='eq(n\,30)',scale=270:-1" \
      -frames:v 1 work/preview_$(basename "${FIRST%.mp4}").jpg || true
fi
echo
echo "done -> out/"
ls -la out/*.mp4
