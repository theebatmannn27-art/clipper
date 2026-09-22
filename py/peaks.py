"""Find the most clippable moments in a video.

Signals combined into a per-second "peak score":
  1. *relative* loudness spikes (excitement, shouting, laughter, music hits)
     -> measured against a long rolling baseline so it's content-agnostic
  2. speech density (words/sec) from subtitles: fast talking = high energy
  3. visual activity: scene-cut density (only if --scenes is given)

Usage:
  python3 peaks.py VIDEO [--subs cues.json] [--scene-times times.txt]
                         [--len 45] [--count 6] [--json-out peaks.json]
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys
import numpy as np

def _ffmpeg() -> str:
    """$FFMPEG, else wherever ffmpeg lives on PATH, else ./ffmpeg."""
    return (os.environ.get("FFMPEG")
            or shutil.which("ffmpeg")
            or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ffmpeg")))


FFMPEG = _ffmpeg()
SR = 16000


def load_audio(path: str) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.exit(f"ffmpeg audio decode failed:\n{p.stderr.decode()[:800]}")
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def envelope(x: np.ndarray, hop_s: float = 0.25) -> tuple[np.ndarray, float]:
    """Per-hop RMS in dB (roughly -60..0)."""
    hop = max(1, int(hop_s * SR))
    n = len(x) // hop
    if n == 0:
        return np.array([-60.0]), hop_s
    frames = x[: n * hop].reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9), hop_s


def smooth(v: np.ndarray, win: int) -> np.ndarray:
    win = max(1, win | 1)
    k = np.ones(win) / win
    return np.convolve(v, k, mode="same")


def rolling_median(v: np.ndarray, win: int) -> np.ndarray:
    win = max(3, win | 1)
    half = win // 2
    pad = np.pad(v, half, mode="edge")
    from numpy.lib.stride_tricks import sliding_window_view
    return np.median(sliding_window_view(pad, win), axis=-1)


def speech_density(cues: list[dict], hop_s: float, n: int) -> np.ndarray:
    d = np.zeros(n, dtype=np.float32)
    if not cues:
        return d
    for c in cues:
        a, b = int(c["start"] / hop_s), int(c["end"] / hop_s)
        dur = max(0.2, c["end"] - c["start"])
        wps = min(len(c["text"].split()) / dur, 5.0) / 5.0     # normalise
        for i in range(max(0, a), min(n, b + 1)):
            d[i] = max(d[i], wps)
    return d


def scene_density(times: list[float], hop_s: float, n: int) -> np.ndarray:
    d = np.zeros(n, dtype=np.float32)
    for t in times:
        i = int(t / hop_s)
        for j in range(max(0, i - 8), min(n, i + 9)):
            d[j] += 1.0
    return d / (d.max() + 1e-9) if d.max() > 0 else d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--subs", default=None, help="cues.json from vttparse.py")
    ap.add_argument("--scenes", default=None, help="file of scene-cut times, one per line")
    ap.add_argument("--len", type=float, default=45.0, help="clip length in seconds")
    ap.add_argument("--min-len", type=float, default=25.0)
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--gap", type=float, default=20.0, help="min gap between clip starts")
    ap.add_argument("--json-out", default="peaks.json")
    a = ap.parse_args()

    x = load_audio(a.video)
    dur = len(x) / SR
    rms_db, hop = envelope(x)
    n = len(rms_db)
    t = np.arange(n) * hop

    # 1) relative loudness spike: smooth 0.75 s, subtract 30 s rolling median
    lvl = smooth(rms_db, int(0.75 / hop))
    base = rolling_median(lvl, int(30 / hop))
    spike = lvl - base
    spike = np.clip(spike, 0, None)
    spike = smooth(spike, int(1.5 / hop))
    spike_n = spike / (np.percentile(spike, 99) + 1e-9)

    cues = json.load(open(a.subs)) if a.subs and os.path.exists(a.subs) else []
    sd = speech_density(cues, hop, n)

    sc = np.zeros(n, dtype=np.float32)
    if a.scenes and os.path.exists(a.scenes):
        times = [float(l.split()[0]) for l in open(a.scenes) if l.strip()]
        sc = scene_density(times, hop, n)

    score = 1.0 * spike_n + 0.45 * sd + 0.35 * sc

    L = int(a.len / hop)
    minL = int(a.min_len / hop)
    if n <= minL:
        cands = [{"start": 0.0, "end": round(dur, 2), "score": 1.0, "peak": 0.0}]
    else:
        # Anchor clips on *peak moments* rather than sliding windows: rank the
        # local maxima of the excitement curve, then enforce separation so we
        # don't spend six clips on the same three seconds.
        s = smooth(score, int(3.0 / hop))
        idx = np.where((s[1:-1] > s[:-2]) & (s[1:-1] >= s[2:]))[0] + 1
        edge = int(2.0 / hop)
        idx = idx[(idx > edge) & (idx < n - edge)]
        if len(idx) == 0:
            idx = np.array([int(n / 2)])

        min_sep = max(a.gap, 0.75 * a.len)
        order = idx[np.argsort(s[idx])[::-1]]
        chosen: list[float] = []
        for i in order:
            t_peak = float(i) * hop
            if all(abs(t_peak - c) >= min_sep for c in chosen):
                chosen.append(t_peak)
            if len(chosen) >= a.count:
                break
        if not chosen:
            chosen = [float(np.argmax(s)) * hop]

        cands = []
        for t_peak in sorted(chosen):
            # land the spike ~1/3 in: payoff early, but a little run-up first
            start = min(max(0.0, t_peak - 0.35 * a.len), max(0.0, dur - a.len))
            start = min(start, max(0.0, dur - min(a.len, dur)))
            end = min(dur, start + a.len)
            wi = slice(int(start / hop), min(n, int(end / hop)))
            cands.append({"start": round(start, 2), "end": round(end, 2),
                          "peak": round(t_peak, 2),
                          "score": round(float(np.percentile(score[wi], 95)), 4)})

    # snap boundaries to caption cue starts/ends so clips don't start mid-word
    if cues:
        starts = np.array([c["start"] for c in cues])
        ends = np.array([c["end"] for c in cues])
        for c in cands:
            i = int(np.argmin(np.abs(starts - c["start"])))
            if abs(starts[i] - c["start"]) <= 2.0:
                c["start"] = round(max(0.0, float(starts[i]) - 0.25), 2)
            j = int(np.argmin(np.abs(ends - c["end"])))
            if abs(ends[j] - c["end"]) <= 2.5 and ends[j] > c["start"] + a.min_len:
                c["end"] = round(float(ends[j]) + 0.35, 2)

    # label each clip from the transcript. Hand-edit these in the JSON before
    # rendering -- the hook line is what actually decides whether people stop.
    for i, c in enumerate(cands, 1):
        c.setdefault("name", f"clip{i:02d}")
        if cues:
            inside = [q for q in cues if c["start"] <= q["start"] < c["end"]]
            if inside:
                c.setdefault("title", _short(inside[0]["text"]))
                pk = min(inside, key=lambda q: abs(q["start"] - c.get("peak", c["start"])))
                c["hook"] = _short(pk["text"])

    json.dump(cands, open(a.json_out, "w"), indent=1)
    print(f"[peaks] duration {dur/60:.1f} min | {len(cands)} clips -> {a.json_out}")
    for c in cands:
        m1, s1 = divmod(c["start"], 60); m2, s2 = divmod(c["end"], 60)
        print(f"  {int(m1):02d}:{s1:04.1f} -> {int(m2):02d}:{s2:04.1f} "
              f"({c['end']-c['start']:4.0f}s, peak {c.get('peak',0):6.1f}s)  "
              f"score {c['score']:.3f}  | {c.get('hook','')[:52]}")


def _short(text: str, limit: int = 58) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text[:1].upper() + text[1:]
    return text[:limit].rsplit(" ", 1)[0] + "…"


if __name__ == "__main__":
    main()
