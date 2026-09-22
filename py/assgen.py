"""Generate a styled .ass subtitle file: burned-in captions + hook + branding.

ASS (via libass) is used rather than drawtext because this ffmpeg build has no
drawtext filter, and because ASS gives real styling control: precise margins,
outlines, and per-clip hook positioning.

Caption styles:
  block  - phrase-at-a-time, up to 2 lines (classic, easy to read)
  punch  - 1-3 words at a time, big and centred (the Shorts/TikTok look)
"""
from __future__ import annotations
import os

FONT = "DejaVu Sans"
W, H = 1080, 1920

# ASS colours are &HAABBGGRR (alpha first; 00 = opaque)
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
AMBER = "&H0000E6FF"          # RGB FFE600
SHADOW = "&H80000000"
FAINT = "&H64FFFFFF"

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{cap_size},{WHITE},{WHITE},{BLACK},{SHADOW},-1,0,0,0,100,100,0.4,0,1,{cap_outline},{cap_shadow},2,{side},{side},{cap_mv},1
Style: Hook,{font},{hook_size},{AMBER},{AMBER},{BLACK},{SHADOW},-1,0,0,0,100,100,0,0,1,7,4,8,70,70,190,1
Style: Brand,{font},40,{FAINT},{FAINT},{BLACK},{SHADOW},0,0,0,0,100,100,0,0,1,3,2,2,70,70,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    """ASS-safe text: no override braces, no stray backslashes."""
    t = (text.replace("{", "(").replace("}", ")").replace("\\", "/")
             .replace("\r", " ").replace("\n", "\\N"))
    return " ".join(t.split())


def punch_cues(words: list, max_words: int = 3, max_chars: int = 18,
               min_dur: float = 0.6) -> list[dict]:
    """Group word-level timings into 1-3 word 'punch' captions."""
    cues, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = sum(len(x.text) for x in cur) + len(cur) - 1
        gap_after = nxt is None or (nxt.t - w.t) > 0.65
        if len(cur) >= max_words or chars >= max_chars or gap_after \
           or w.text.endswith((".", "?", "!", "।", ",")):
            cues.append(cur); cur = []
    if cur:
        cues.append(cur)

    out = []
    for i, ws in enumerate(cues):
        start = max(0.0, ws[0].t - 0.05)
        end = ws[-1].t + max(0.30, 0.05 * len(ws[-1].text))
        if i + 1 < len(cues):
            end = min(end, cues[i + 1][0].t - 0.02)
        if end - start < min_dur:
            end = start + min_dur
        out.append({"start": start, "end": end,
                    "text": " ".join(w.text for w in ws)})
    return out


def build_ass(path: str, cues: list[dict], duration: float, *,
              hook: str = "", brand: str = "", style: str = "block",
              hook_secs: float = 4.5, punch: list[dict] | None = None) -> str:
    """cues are already retimed so clip start == 0. Returns the ASS text."""
    cap_size, cap_outline, cap_shadow = (88, 6, 4) if style == "punch" else (64, 5, 3)
    cap_mv = 470 if style == "punch" else 400
    head = HEADER.format(w=W, h=H, font=FONT, cap_size=cap_size,
                         cap_outline=cap_outline, cap_shadow=cap_shadow,
                         cap_mv=cap_mv, hook_size=74, side=70,
                         WHITE=WHITE, BLACK=BLACK, AMBER=AMBER,
                         SHADOW=SHADOW, FAINT=FAINT)
    ev: list[str] = []
    if hook:
        ev.append(f"Dialogue: 0,{_t(0)},{_t(min(hook_secs, duration))},Hook,,0,0,0,,{_esc(hook)}")
    if brand:
        ev.append(f"Dialogue: 0,{_t(0)},{_t(duration)},Brand,,0,0,0,,{_esc(brand)}")
    for c in cues:
        if c["end"] <= 0 or c["start"] >= duration:
            continue
        st = max(0.0, c["start"]); en = min(duration, c["end"])
        if en - st < 0.20:
            continue
        ev.append(f"Dialogue: 0,{_t(st)},{_t(en)},Caption,,0,0,0,,{_esc(c['text'])}")
    text = head + "\n".join(ev) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    open(path, "w", encoding="utf-8").write(text)
    return text
