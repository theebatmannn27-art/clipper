"""Cut clips into 1080x1920 vertical Shorts with burned-in captions.

  python3 cut.py VIDEO --peaks peaks.json --cues cues.json [--words words.json] \
        --outdir out --mode crop --cap-style block --hook --brand "@yourhandle"

Each clip gets:
  * 9:16 vertical framing (centre-crop, or blurred-fill with --mode blur)
  * styled, burned-in captions (ASS via libass) retimed to the clip
  * optional hook headline (top) and brand line (bottom)
  * loudness-normalised, fast-start MP4 -> upload straight to Shorts/Reels/TikTok

Per-clip .srt and .ass files are kept in <outdir>/captions/ so you can re-style
or re-upload captions without re-cutting.
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vttparse import to_srt, retime_cues, retime_words, Word  # noqa: E402
from assgen import build_ass, punch_cues                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = (os.environ.get("FFMPEG") or shutil.which("ffmpeg")
          or os.path.abspath(os.path.join(HERE, "..", "..", "ffmpeg")))


def build_filter(mode: str, cx: float, ass_name: str | None) -> str:
    W, H = 1080, 1920
    tail = f",ass={ass_name}" if ass_name else ""
    if mode == "blur":
        return (f"[0:v]split=2[bg][fg];"
                f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=28:3,eq=brightness=-0.06[bgb];"
                f"[fg]scale={W}:-2:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[fr];"
                f"[fr]format=yuv420p{tail}[v]")
    return (f"[0:v]crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{cx}':0,"
            f"scale={W}:{H}:flags=lanczos,format=yuv420p{tail}[v]")


def render(video: str, peaks: list[dict], cues: list[dict] | None,
           words: list[Word] | None, outdir: str, mode: str = "crop",
           cx: float = 0.5, hook: bool = False, brand: str = "",
           cap_style: str = "block", dry: bool = False) -> list[str]:
    video = os.path.abspath(video)
    capdir = os.path.join(os.path.abspath(outdir), "captions")
    os.makedirs(capdir, exist_ok=True)
    made: list[str] = []

    for i, p in enumerate(peaks, 1):
        st, en = float(p["start"]), float(p["end"])
        dur = max(1.0, en - st)
        name = p.get("name") or f"clip{i:02d}"
        out = os.path.join(os.path.abspath(outdir), f"{name}.mp4")

        # ---- captions -------------------------------------------------------
        ass_name = None
        if words or cues:
            if cap_style == "punch" and words:
                clip_cues = punch_cues(retime_words(words, st, dur))
            else:
                clip_cues = retime_cues(cues or [], st, dur)
            if clip_cues:
                hook_text = (p.get("hook") or p.get("title") or "") if hook else ""
                build_ass(os.path.join(capdir, f"{name}.ass"), clip_cues, dur,
                          hook=hook_text, brand=brand, style=cap_style)
                open(os.path.join(capdir, f"{name}.srt"), "w").write(to_srt(clip_cues))
                ass_name = f"{name}.ass"
            else:
                print(f"  (note: no captions inside {name} - subtitle timing gap)")

        fc = build_filter(mode, cx, ass_name)
        cmd = [FFMPEG, "-y", "-v", "error",
               "-ss", f"{st:.3f}", "-t", f"{dur:.3f}", "-i", video,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart", out]

        if dry:
            print(f"# {name}\n{' '.join(cmd)}\n")
            continue

        # run from the captions dir so the .ass path needs no filtergraph escaping
        r = subprocess.run(cmd, capture_output=True, cwd=capdir)
        if r.returncode != 0:
            print(f"!! {name} FAILED:\n{r.stderr.decode()[-1500:]}", file=sys.stderr)
        else:
            mb = os.path.getsize(out) / 1e6
            cap = "captions" if ass_name else "no captions"
            print(f"[cut] {name}: {st:7.1f}s +{dur:4.0f}s -> {os.path.basename(out)} "
                  f"({mb:.1f} MB, {cap})")
            made.append(out)
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--peaks", required=True, help="peaks.json, or your own clip list")
    ap.add_argument("--cues", default=None, help="cues.json from vttparse.py (block captions)")
    ap.add_argument("--words", default=None, help="words.json from vttparse.py (punch captions)")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--mode", choices=["crop", "blur"], default="crop")
    ap.add_argument("--cx", type=float, default=0.5, help="crop centre 0..1 (0.35 = left-ish)")
    ap.add_argument("--cap-style", choices=["block", "punch"], default="block")
    ap.add_argument("--hook", action="store_true", help="burn each clip's hook text at the top")
    ap.add_argument("--brand", default="", help="small watermark line at the bottom")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    peaks = json.load(open(a.peaks))
    cues = json.load(open(a.cues)) if a.cues and os.path.exists(a.cues) else None
    words = None
    if a.words and os.path.exists(a.words):
        words = [Word(w["t"], w["text"]) for w in json.load(open(a.words))]
    render(a.video, peaks, cues, words, a.outdir, a.mode, a.cx, a.hook,
           a.brand, a.cap_style, a.dry_run)


if __name__ == "__main__":
    main()
