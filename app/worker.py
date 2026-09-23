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

    # 5. per-clip captions: every clip gets its own editable caption list so the
    # subtitle session can freely add/remove/edit cues without re-transcribing.
    words = ([Word(w["t"], w["text"]) for w in json.load(open(words_path))]
             if words_path and os.path.exists(words_path) else None)
    cap_dir = os.path.join(jobdir, "caps")
    os.makedirs(cap_dir, exist_ok=True)
    # master cue list for the whole video (used to seed new clips on demand)
    all_cues = json.load(open(cues_path)) if cues_path and os.path.exists(cues_path) else []
    json.dump(all_cues, open(os.path.join(jobdir, "cues.json"), "w"), indent=1)

    for i, c in enumerate(cands, 1):
        c.setdefault("name", f"clip{i:02d}")
        c.setdefault("title", c.get("hook") or f"Clip {i}")
        c.setdefault("hook", c.get("title") or c.get("hook", ""))
        c["selected"] = True
        c["thumb"] = f"/api/jobs/{job_id}/thumb/{c['name']}.jpg"
        # shorten the clip start so captions map cleanly to the retimed frame
        clip_cues = _retime_for_clip(all_cues, words, c["start"],
                                     c["end"] - c["start"], {}, opts)
        name = c["name"]
        open(os.path.join(cap_dir, f"{name}.json"), "w").write(
            json.dumps(clip_cues, indent=1))
        c["caption_url"] = f"/api/jobs/{job_id}/caps/{name}.json"
        c["caption_count"] = len(clip_cues)
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
    """Render the final vertical clips from the (possibly user-edited) peaks
    AND the user-edited per-clip captions (the subtitle session output)."""
    opts = job.get("options", {})
    peaks = job.get("peaks", [])
    if not peaks:
        raise RuntimeError("No clips selected to render.")

    src = _abs(jobdir, "source.mp4")
    if not os.path.exists(src):
        raise RuntimeError("Source video is missing from storage.")

    outdir = os.path.join(jobdir, "out")
    capdir = os.path.join(outdir, "captions")
    os.makedirs(capdir, exist_ok=True)
    user_caps_dir = os.path.join(jobdir, "caps")

    selected = [p for p in peaks if p.get("selected", True)]
    total = max(1, len(selected))

    made: list = []
    for i, p in enumerate(selected, 1):
        name = p.get("name") or f"clip{i:02d}"
        progress(f"Rendering {name} ({i}/{total})…",
                 0.05 + 0.9 * (i - 1) / total)
        dur = float(p["end"] - p["start"])
        # prefer the user-edited caption list from the subtitle session
        cap_file = os.path.join(user_caps_dir, f"{name}.json")
        if os.path.exists(cap_file):
            clip_cues = json.load(open(cap_file))
        else:
            # fall back to re-deriving from the source transcript (legacy path)
            cues_path = _abs(jobdir, "cues.json")
            words_path = _abs(jobdir, "words.json")
            cues = json.load(open(cues_path)) if os.path.exists(cues_path) else []
            words = ([Word(w["t"], w["text"]) for w in json.load(open(words_path))]
                     if os.path.exists(words_path) else None)
            clip_cues = _retime_for_clip(
                cues, words, float(p["start"]), dur, {}, opts)

        mk = render_one_clip(src, p, clip_cues, opts, outdir)
        if not mk:
            raise RuntimeError(f"Render failed for {name} "
                               f"(check ffmpeg/disk — see server log).")
        entry = {
            "name": name,
            "start": p.get("start"),
            "end": p.get("end"),
            "hook": p.get("hook", ""),
            "title": p.get("title", ""),
            "caption_count": len(clip_cues),
            "url": f"/api/jobs/{job['id']}/clips/{name}.mp4",
            "caption_url": f"/api/jobs/{job['id']}/clips/captions/{name}.srt",
            "size": os.path.getsize(os.path.join(outdir, f"{name}.mp4")),
        }
        made.append(entry)
    progress("Finishing up…", 1.0)
    return {"clips": made}


def build_filter_complex(opts: dict, ass_name: str | None,
                         width: int = 1080) -> str:
    """Return the ffmpeg filter_complex for vertical output (default 1080 wide)."""
    H = int(width * 16 / 9)
    tail = f",ass={ass_name}" if ass_name else ""
    if opts.get("mode", "crop") == "blur":
        return (f"[0:v]split=2[bg][fg];"
                f"[bg]scale={width}:{H}:force_original_aspect_ratio=increase,"
                f"crop={width}:{H},boxblur=28:3,eq=brightness=-0.06[bgb];"
                f"[fg]scale={width}:-2:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[fr];"
                f"[fr]format=yuv420p{tail}[v]")
    cx = float(opts.get("cx", 0.5))
    return (f"[0:v]crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{cx}':0,"
            f"scale={width}:{H}:flags=lanczos,format=yuv420p{tail}[v]")


def _transcode(fc: str, in_args: list, out_mp4: str,
               fast: bool = False, cwd: str | None = None) -> int:
    """Run one ffmpeg transcode; returns ffmpeg's exit code (cwd lets the
    .ass path in the filtergraph resolve without escaping)."""
    cmd = ([FFMPEG, "-y", "-v", "error"] + in_args +
           ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"])
    if fast:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                "-ar", "44100", "-movflags", "+faststart"]
    else:
        cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart"]
    cmd.append(out_mp4)
    r = subprocess.run(cmd, capture_output=True, cwd=cwd)
    if r.returncode != 0:
        print(f"[ffmpeg] transcode failed:\n{r.stderr.decode()[-1500:]}")
    return r.returncode


def render_one_clip(video: str, p: dict, clip_cues: list, opts: dict,
                    outdir: str) -> bool:
    """Render a single final 9:16 clip honouring user-edited captions."""
    from assgen import build_ass
    from vttparse import to_srt
    name = p["name"]
    st, en = float(p["start"]), float(p["end"])
    dur = max(1.0, en - st)
    capdir = os.path.join(outdir, "captions")
    os.makedirs(capdir, exist_ok=True)
    out_mp4 = os.path.join(outdir, f"{name}.mp4")

    ass_path = os.path.join(capdir, f"{name}.ass")
    hook_text = (p.get("hook") or "") if opts.get("hook", True) else ""
    build_ass(ass_path, clip_cues, dur,
              hook=hook_text, brand=opts.get("brand", ""),
              style=opts.get("style", "block"))
    with open(os.path.join(capdir, f"{name}.srt"), "w") as f:
        f.write(to_srt(clip_cues))

    fc = build_filter_complex(opts, os.path.basename(ass_path))
    in_args = ["-ss", f"{st:.3f}", "-t", f"{dur:.3f}", "-i", video]
    rc = _transcode(fc, in_args, out_mp4, fast=False, cwd=capdir)
    return rc == 0 and os.path.exists(out_mp4)


def render_preview(job: dict, jobdir: str, name: str, cues: list) -> str:
    """Render a fast low-res preview of one clip that the browser can play.

    Writes data/jobs/<id>/previews/<name>.mp4 and returns the absolute path.
    """
    peak = next((p for p in job.get("peaks", []) if p.get("name") == name), None)
    if not peak:
        raise RuntimeError(f"No clip named {name}")
    src = os.path.join(jobdir, "source.mp4")
    if not os.path.exists(src):
        raise RuntimeError("Source video missing")
    opts = job.get("options", {})

    outdir = os.path.join(jobdir, "previews")
    capdir = os.path.join(outdir, "captions")
    os.makedirs(capdir, exist_ok=True)
    ass_path = os.path.join(capdir, f"{name}.ass")
    dur = float(peak["end"]) - float(peak["start"])
    hook_text = (peak.get("hook") or "") if opts.get("hook", True) else ""
    from assgen import build_ass
    build_ass(ass_path, cues, max(1.0, dur),
              hook=hook_text, brand=opts.get("brand", ""),
              style=opts.get("style", "block"))

    fc = build_filter_complex(opts, os.path.basename(ass_path), width=324)
    in_args = ["-ss", f"{float(peak['start']):.3f}",
               "-t", f"{max(1.0, dur):.3f}", "-i", src]
    out_mp4 = os.path.join(outdir, f"{name}.mp4")
    _transcode(fc, in_args, out_mp4, fast=True, cwd=capdir)
    return out_mp4


def _retime_for_clip(all_cues: list, words: list | None, start: float,
                     dur: float, rules: dict, opts: dict) -> list:
    """Build the clip-relative caption list (start/end are seconds from 0).
    Uses the *source* cues for the chosen style so the subtitle session starts
    from exactly what will be rendered."""
    style = opts.get("style", "block")
    if style == "punch" and words:
        kept = []
        for w in words:
            st = w.t - start
            if -0.1 <= st < dur:
                kept.append(Word(st, w.text))
        return punch_cues(kept)
    return retime_cues(all_cues, start, dur)


def regenerate_clip_captions(job: dict, jobdir: str, name: str) -> list:
    """Re-derive a clip's caption list from the master transcript (used when
    the user changes style or trims a clip and wants fresh captions)."""
    opts = job.get("options", {})
    peak = next((p for p in job.get("peaks", []) if p.get("name") == name), None)
    if not peak:
        raise RuntimeError(f"No clip named {name}")
    cues_path = os.path.join(jobdir, "cues.json")
    words_path = os.path.join(jobdir, "words.json")
    cues = json.load(open(cues_path)) if os.path.exists(cues_path) else []
    words = ([Word(w["t"], w["text"]) for w in json.load(open(words_path))]
             if os.path.exists(words_path) else None)
    clip_cues = _retime_for_clip(cues, words, float(peak["start"]),
                                 float(peak["end"] - peak["start"]), {}, opts)
    cap_dir = os.path.join(jobdir, "caps")
    os.makedirs(cap_dir, exist_ok=True)
    open(os.path.join(cap_dir, f"{name}.json"), "w").write(
        json.dumps(clip_cues, indent=1))
    return clip_cues


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
