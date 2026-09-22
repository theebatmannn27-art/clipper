"""Background worker: turn a source video into *editable* candidate clips.

Pipeline (mirrors run.sh, but with live progress + per-clip thumbnails):

  1. video already local (upload) or downloaded via dl.fetch()
  2. subtitles -> cues.json + words.json  (py/vttparse.py)
  3. peak detection -> peaks.json         (py/peaks.py)
  4. thumbnails for each candidate peak   (so the review screen shows a frame)
  5. state -> "review": the user can edit hooks/titles/starts, then hit Render

The heavy render step (py/cut.py + ffmpeg per clip) is invoked by the API layer
so it can be spread over multiple requests; `render_clips()` lives here.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import threading

from ._path import PYDIR

sys.path.insert(0, PYDIR)
from ffmpeg_path import get_ffmpeg  # noqa: E402
from vttparse import parse_vtt, group, to_srt, retime_cues, retime_words, Word  # noqa: E402
from peaks import find_peaks  # noqa: E402
from cut import render as render_cuts  # noqa: E402
from assgen import build_ass, punch_cues  # noqa: E402

FFMPEG = get_ffmpeg()
CGI = os.environ.get("CGI", "clipper-web")  # user-agent tag for ffmpeg metadata


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def _abs(*parts: str) -> str:
    return os.path.abspath(os.path.join(*parts))


def media_duration(video: str) -> float:
    cmd = [FFMPEG, "-v", "error", "-i", video, "-f", "null", "-"]
    p = _run(cmd, timeout=300)
    # fall back to ffprobe-free parse of the error/output "Duration: hh:mm:ss.xx"
    txt = (p.stderr or b"").decode(errors="replace")
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def gen_thumbnail(video: str, at_sec: float, out_path: str,
                  width: int = 480, scale: float = 0.0, cx: float = 0.0) -> bool:
    """Snap a vertical (9:16) frame at `at_sec` for the review cards."""
    W, H = 1080, 1920
    tw = width
    th = int(tw * H / W)
    cx = cx if cx else 0.5
    vf = (f"crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{cx}':0,"
          f"scale={tw}:{th}:flags=lanczos")
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, at_sec):.3f}",
           "-i", video, "-vf", vf, "-frames:v", "1", out_path]
    p = _run(cmd, timeout=120)
    return p.returncode == 0 and os.path.exists(out_path)


def fetch_source(url: str, dest_path: str, tmp_dir: str) -> dict:
    """Download source into tmp_dir, then move the video to dest_path.
    Returns {"vtt": <path or None>} (the vtt, if any, is left in tmp_dir)."""
    from . import dl
    os.makedirs(tmp_dir, exist_ok=True)
    got = dl.fetch(url, tmp_dir)
    video = got["video"] or ""
    if not video or not os.path.exists(video):
        raise RuntimeError("Download did not produce a video file.")
    os.replace(video, dest_path)
    vtt = got.get("vtt")
    if vtt and os.path.exists(vtt):
        return {"vtt": vtt}
    # yt-dlp dumps subs as <id>.<lang>.vtt next to the video; look for one
    for f in sorted(os.listdir(tmp_dir)):
        if f.lower().endswith((".vtt", ".srt")):
            return {"vtt": os.path.join(tmp_dir, f)}
    return {"vtt": None}


# --------------------------------------------------------------------------- #
# job stages
# --------------------------------------------------------------------------- #

def run_through_review(job: dict, jobdir: str, src: str, vtt: str | None,
                       progress) -> dict:
    """Fetch/down has already happened; run analysis -> review."""
    job_id = job["id"]
    opts = job.get("options", {})

    # 2. subtitles
    cues_path, words_path = None, None
    progress("Reading subtitles…", 0.20)
    if vtt and os.path.exists(vtt):
        try:
            if vtt.lower().endswith(".srt"):
                words = _srt_to_words(vtt)
            else:
                words = parse_vtt(vtt)
            cues = group(words)
            cues_path = os.path.join(jobdir, "cues.json")
            words_path = os.path.join(jobdir, "words.json")
            json.dump(cues, open(cues_path, "w"), indent=1)
            json.dump([{"t": round(w.t, 3), "text": w.text} for w in words],
                      open(words_path, "w"), indent=1)
            print(f"[worker:{job_id}] {len(words)} words -> {len(cues)} cues")
        except Exception as e:  # subtitles are best-effort
            print(f"[worker:{job_id}] subtitle parse failed: {e}")
            cues_path = words_path = None

    try:
        duration = float(opts.get("_duration") or media_duration(src))
    except Exception:
        duration = 0.0

    # 3. peak detection
    progress("Finding the peak moments…", 0.35)
    cands, dur = find_peaks(
        src, cues_path, None,
        length=float(opts.get("len", 40)),
        min_len=float(opts.get("min_len", 25)),
        count=int(opts.get("count", 5)),
        gap=float(opts.get("gap", 20)),
    )
    duration = duration or dur or 0.0

    # clamp clip starts so thumbnails never seek past the end
    for c in cands:
        c["start"] = min(c["start"], max(0.0, duration - 1.0))

    # 4. thumbnails
    progress("Snapping previews…", 0.80)
    total = max(1, len(cands))
    thumb_dir = os.path.join(jobdir, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    cx = float(opts.get("cx", 0.5))
    for i, c in enumerate(cands):
        name = c.get("name") or f"clip{i+1:02d}"
        at = min(c.get("peak", (c["start"] + c["end"]) / 2),
                 max(0.0, duration - 0.5))
        ok = gen_thumbnail(src, at, os.path.join(thumb_dir, f"{name}.jpg"),
                           width=360, cx=cx)
        if not ok:  # retry at clip start
            gen_thumbnail(src, c["start"], os.path.join(thumb_dir, f"{name}.jpg"),
                          width=360, cx=cx)
        progress("Snapping previews…", 0.80 + 0.15 * (i + 1) / total)

    json.dump(cands, open(os.path.join(jobdir, "peaks.json"), "w"), indent=1)

    for i, c in enumerate(cands, 1):
        c.setdefault("name", f"clip{i:02d}")
        c.setdefault("title", c.get("hook") or f"Clip {i}")
        c.setdefault("hook", c.get("title") or c.get("hook", ""))
        c["selected"] = True
        c["thumb"] = f"/api/jobs/{job_id}/thumb/{c['name']}.jpg"
    update = {
        "state": "review",
        "progress": 1.0,
        "stage": "Ready to review",
        "peaks": cands,
        "duration": duration,
        "captions": bool(cues_path),
        "src_basename": os.path.basename(src),
    }
    opts.pop("_duration", None)
    update["options"] = opts
    return update


def render_clips(job: dict, jobdir: str, progress) -> dict:
    """Render the final vertical clips from the (possibly user-edited) peaks."""
    opts = job.get("options", {})
    peaks = job.get("peaks", [])
    if not peaks:
        raise RuntimeError("No clips selected to render.")

    src = _abs(jobdir, "source.mp4")
    if not os.path.exists(src):
        raise RuntimeError("Source video is missing from storage.")

    cues_path = _abs(jobdir, "cues.json")
    words_path = _abs(jobdir, "words.json")
    cues = json.load(open(cues_path)) if os.path.exists(cues_path) else None
    words = ([Word(w["t"], w["text"]) for w in json.load(open(words_path))]
             if os.path.exists(words_path) else None)

    outdir = os.path.join(jobdir, "out")
    os.makedirs(outdir, exist_ok=True)

    mode = opts.get("mode", "crop")
    style = opts.get("style", "block")
    hook = bool(opts.get("hook", True))
    brand = opts.get("brand", "")
    cx = float(opts.get("cx", 0.5))

    selected = [p for p in peaks if p.get("selected", True)]
    total = max(1, len(selected))

    made: list = []
    for i, p in enumerate(selected, 1):
        name = p.get("name") or f"clip{i:02d}"
        progress(f"Rendering {name} ({i}/{total})…",
                 0.05 + 0.9 * (i - 1) / total)
        # cut.py writes into <outdir>/captions/ and <outdir>/<name>.mp4
        outs = render_cuts(src, [p], cues, words, outdir,
                           mode=mode, cx=cx, hook=hook, brand=brand,
                           cap_style=style)
        if not outs:
            raise RuntimeError(f"Render failed for {name} "
                               f"(check ffmpeg/disk — see server log).")
        clip_file = outs[0]
        fn = os.path.basename(clip_file)
        entry = {
            "name": name,
            "start": p.get("start"),
            "end": p.get("end"),
            "hook": p.get("hook", ""),
            "title": p.get("title", ""),
            "url": f"/api/jobs/{job['id']}/clips/{fn}",
            "caption_url": f"/api/jobs/{job['id']}/clips/captions/{name}.srt",
            "size": os.path.getsize(clip_file),
        }
        made.append(entry)
    progress("Finishing up…", 1.0)
    return {"clips": made}


def _srt_to_words(srt_path: str) -> list:
    """Cheap SRT -> Word list so SRT subs behave like VTT ones."""
    import re
    raw = open(srt_path, encoding="utf-8", errors="replace").read()
    blocks = re.split(r"\n\s*\n", raw)
    out = []
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        for l in lines:
            m = re.match(r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)", l)
            if m:
                h1, m1, s1, ms1 = (int(x) for x in m.groups()[:4])
                st = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
                body = " ".join(lines[2:])
                text = re.sub(r"<[^>]+>", " ", body)
                for w in text.split():
                    out.append(Word(round(st, 3), w))
                break
    return out
