# YouTube → Vertical Shorts clipper

Finds the **peak moments** in a long video and cuts them into 9:16 captioned
clips ready for Shorts / Reels / TikTok.

## Use

```bash
./setup.sh                                    # once per session (~5s)
./run.sh "https://youtube.com/watch?v=XXXX" [cookies.txt]   # or:
./run.sh /path/to/video.mp4
```

Output: `out/clip01.mp4`, `out/clip02.mp4`, … plus re-usable caption files in
`out/captions/` (both `.ass` and `.srt`).

Tune it with env vars:

| var | default | meaning |
|---|---|---|
| `LEN` | 40 | clip length in seconds |
| `COUNT` | 5 | number of clips |
| `STYLE` | block | `block` = 2-line phrases, `punch` = 1–3 big words |
| `MODE` | crop | `crop` fills the frame; `blur` letterboxes on a blurred backdrop |
| `HOOK` | 1 | burn the clip's hook line across the top |
| `BRAND` | – | watermark text, e.g. `BRAND="@yourhandle"` |
| `CX` | 0.5 | horizontal crop centre — 0.35 if the speaker sits left of centre |

```bash
LEN=30 COUNT=8 STYLE=punch BRAND="@mychannel" ./run.sh video.mp4
```

## How the peaks are chosen

`py/peaks.py` scores every moment of the video on three signals:

1. **Loudness spikes relative to a 30-second rolling baseline** — laughter,
   shouting, a music hit, a crowd reaction. Using *relative* loudness means it
   works on a quiet vlog and a loud gaming stream alike; the calm parts of a
   video don't win just because they're the loudest audio overall.
2. **Speech density** (words/sec) from the transcript — fast talking = energy.
3. **Scene-cut density** — optional, for heavily edited footage.

Clips are anchored on the local maxima of that curve (not a sliding window, so
you don't get six near-identical clips around one spike), forced at least
`0.75 × LEN` apart, and their boundaries are snapped to caption cue starts so
nothing begins mid-word. The spike lands about a third of the way in: enough
run-up to make sense, payoff early enough to survive the 3-second scroll test.

Each clip also gets a `hook` and `title` line pulled from the transcript at the
peak. **Edit those in `work/peaks.json` before cutting** — the opening line is
what actually decides whether people stop scrolling:

```json
{ "start": 128.4, "end": 168.4, "name": "clip01",
  "hook": "the mistake that cost me six months",
  "title": "..." }
```

Re-run step 3 alone after editing:

```bash
python3 py/cut.py video.mp4 --peaks work/peaks.json --cues work/cues.json \
        --words work/words.json --outdir out --cap-style punch --hook
```

## Files

| file | role |
|---|---|
| `run.sh` | one-command pipeline (fetch → parse subs → detect peaks → cut) |
| `setup.sh` | installs ffmpeg + yt-dlp if missing |
| `fetch.sh` | yt-dlp wrapper (video, subtitles, thumbnail, metadata; optional cookies) |
| `py/vttparse.py` | YouTube VTT → clean cues + per-word timings (handles rolling auto-subs) |
| `py/peaks.py` | peak-moment detection |
| `py/cut.py` | 9:16 rendering, captions, hook/branding, loudness normalisation |
| `py/assgen.py` | generates the styled ASS captions |
| `demo/` | sample output clips from the test fixture |

## Output specs

1080×1920, H.264 high profile, CRF 20, 30 fps, AAC 160 kbps @ 48 kHz,
loudness normalised to −14 LUFS (the platform target), `+faststart`, with
per-clip `.srt`/`.ass` kept in `out/captions/`.

## Notes

- If the source is 16:9 you're cropping a ~600 px wide slice out of a 1080p
  frame and upscaling it, so quality depends on the source. A 1440p/4K source
  gives noticeably sharper vertical clips.
- Download the highest resolution you can (`fetch.sh` caps at 1080p; raise the
  `height<=1080` filter in `fetch.sh` for more).
- Only clip material you have the rights to — your own uploads, channels that
  grant reuse permission, or Creative Commons. Attributing the original isn't a
  substitute for permission, and reuploading someone else's content can get a
  channel struck.
