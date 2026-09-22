"""Parse YouTube auto-generated / manual VTT into clean, sentence-ish cues.

Auto-subs are *rolling*: each cue repeats the previous line and carries inline
per-word timings like <00:00:01.234><c> word</c>. Naive parsing produces the
famous "duplicated karaoke mess". This rebuilds true word->time pairs, then
groups them into readable caption lines.
"""
from __future__ import annotations
import re, sys, json, argparse
from dataclasses import dataclass

TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})")
INLINE = re.compile(r"<(\d{1,2}:\d{2}:\d{2}[.,]\d{3})>")
# strip markup tags (<c>, </c>, <v Name>, ...) but NEVER the inline word
# timestamps, which are what give us per-word timing.
TAGS = re.compile(r"</?(?!\d{1,2}:\d{2}:\d{2}[.,]\d{3}>)[^>]+>")


def t2s(ts: str) -> float:
    m = TS.search(ts)
    if not m:
        return 0.0
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


@dataclass
class Word:
    t: float      # start
    text: str


def _cues(raw: str) -> list[tuple[float, list[str]]]:
    """[(cue_start, [body lines])] for every readable cue.

    Handles the mangled variants too: blocks with no timing header (a stray
    blank line splits a cue in some exporters) are folded into the previous cue,
    and the cue's end time is kept as the fallback clock for them.
    """
    out: list[tuple[float, list[str]]] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines
                 if not ln.strip().upper().startswith(("WEBVTT", "KIND:", "LANGUAGE:", "NOTE"))]
        if not lines:
            continue
        head = next((ln for ln in lines if "-->" in ln), None)
        body = [ln for ln in lines if "-->" not in ln]
        if not body:
            continue
        if head:
            out.append((t2s(head.split("-->")[0]), body))
        elif out:  # orphaned continuation -> same cue
            out[-1][1].extend(body)
    return out


def parse_vtt(path: str) -> list[Word]:
    words: dict[tuple[int, str], Word] = {}
    raw = open(path, encoding="utf-8", errors="replace").read()
    cues = _cues(raw)
    # YouTube auto-subs are "rolling": every cue restates the previous line.
    # If inline word timings exist anywhere, only the tagged line of each cue is
    # genuinely new content -- everything else is that restated duplicate.
    rolling = any(INLINE.search(ln) for _, body in cues for ln in body)

    for cue_start, body in cues:
        for ln in body:
            if rolling and not INLINE.search(ln):
                continue  # restated copy of the previous line -> skip
            ln = TAGS.sub(" ", INLINE.sub(r"<\1>", ln))  # keep timings, drop <c> etc.
            # split into (optional timestamp, chunk) pieces
            pieces = re.split(r"(<\d{1,2}:\d{2}:\d{2}[.,]\d{3}>)", ln)
            cur = cue_start
            for piece in pieces:
                if not piece:
                    continue
                if piece.startswith("<") and TS.search(piece):
                    cur = t2s(piece)
                    continue
                for w in piece.split():
                    w = w.strip()
                    if not w:
                        continue
                    key = (int(round(cur * 10)), w.lower())
                    words.setdefault(key, Word(cur, w))
    out = _dedupe(sorted(words.values(), key=lambda w: w.t))
    if not out:  # plain manual subs: one timestamp per cue, estimate word times
        for st, body in cues:
            text = " ".join(TAGS.sub(" ", ln).strip() for ln in body)
            for i, w in enumerate(text.split()):
                out.append(Word(st + i * 0.28, w))
    return out


def _dedupe(ws: list[Word]) -> list[Word]:
    """Drop exact repeats within 1.5 s and force non-decreasing timestamps."""
    out: list[Word] = []
    for w in ws:
        if out and w.text.lower() == out[-1].text.lower() and w.t - out[-1].t < 1.5:
            continue
        if out:  # collapse rolling restatements: "the the the"
            recent = [x.text.lower() for x in out[-12:]]
            if recent.count(w.text.lower()) >= 3 and w.t - out[-1].t < 0.35:
                continue
        if out and w.t < out[-1].t:
            w = Word(out[-1].t, w.text)
        out.append(w)
    return out


def group(words: list[Word], max_chars: int = 42, max_lines: int = 2,
          gap: float = 0.9, min_dur: float = 0.7) -> list[dict]:
    """Group words into caption cues of <= max_chars (2 lines max)."""
    cues, cur, cur_len = [], [], 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        # NB: bool() is essential -- a bare `cur and ...` would return the list
        # object itself, which then mutates into a truthy value as we append.
        hard_break = bool(
            (nxt is not None and nxt.t - w.t > gap)
            or (bool(cur) and w.text.endswith((".", "?", "!", "।", ",", ":", ";"))
                and cur_len > max_chars * 0.5))
        if cur and cur_len + 1 + len(w.text) > max_chars:
            hard_break = True
        cur.append(w)
        cur_len = sum(len(x.text) for x in cur) + len(cur) - 1
        if hard_break or cur_len >= max_chars * 2:
            cues.append(_mk(cur)); cur, cur_len = [], 0
    if cur:
        cues.append(_mk(cur))
    return _fix_overlaps(cues)


def _mk(ws: list[Word]) -> dict:
    text = " ".join(w.text for w in ws).strip()
    end = ws[-1].t + max(0.40, 0.055 * len(ws[-1].text))
    return {"start": max(0.0, ws[0].t - 0.06), "end": end, "text": text}


def _fix_overlaps(cues: list[dict], max_dur: float = 5.5) -> list[dict]:
    """Ensure cues never overlap -- stacked cues render on top of each other
    in libass -- and that none linger on screen for too long."""
    out: list[dict] = []
    for i, c in enumerate(cues):
        if i + 1 < len(cues):
            c["end"] = min(c["end"], cues[i + 1]["start"] - 0.02)
        c["end"] = min(c["end"], c["start"] + max_dur)
        if c["end"] - c["start"] < 0.30:            # too short to read -> extend a bit
            c["end"] = c["start"] + 0.40
            if i + 1 < len(cues):
                nxt_gap = cues[i + 1]["start"] - c["start"]
                if nxt_gap > 0.2:
                    c["end"] = min(c["end"], cues[i + 1]["start"] + 0.6)
        out.append(c)
    return out


def to_srt(cues: list[dict], offset: float = 0.0, clip_end: float | None = None) -> str:
    def f(t: float) -> str:
        t = max(0.0, t)
        h, rem = divmod(t, 3600); m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"

    out = []
    for i, c in enumerate(cues, 1):
        st, en = c["start"] - offset, c["end"] - offset
        if en <= 0 or st < 0 and en - st < 0.2:
            continue
        st = max(0.0, st)
        if clip_end is not None and st > clip_end:
            continue
        if clip_end is not None:
            en = min(en, clip_end)
        if en - st < 0.15:
            en = st + 0.35
        out.append(f"{i}\n{f(st)} --> {f(en)}\n{c['text']}\n")
    return "\n".join(out)


def retime_cues(cues: list[dict], offset: float, dur: float) -> list[dict]:
    """Shift cues so a clip starting at `offset` begins at 0, and trim to length."""
    out = []
    for c in cues:
        st, en = c["start"] - offset, c["end"] - offset
        if en <= 0 or st >= dur:
            continue
        st, en = max(0.0, st), min(dur, en)
        if en - st < 0.20:
            continue
        out.append({"start": st, "end": en, "text": c["text"]})
    return out


def retime_words(words: list[Word], offset: float, dur: float) -> list[Word]:
    """Shift word timings into clip-relative time, dropping anything outside."""
    return [Word(max(0.0, w.t - offset), w.text)
            for w in words if -0.1 <= w.t - offset < dur]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vtt"); ap.add_argument("--json-out", default=None)
    ap.add_argument("--words-out", default=None)
    ap.add_argument("--srt-out", default=None)
    ap.add_argument("--max-chars", type=int, default=42)
    a = ap.parse_args()
    words = parse_vtt(a.vtt)
    cues = group(words, max_chars=a.max_chars)
    print(f"[vtt] {len(words)} words -> {len(cues)} cues", file=sys.stderr)
    if a.json_out:
        json.dump(cues, open(a.json_out, "w"), indent=1)
    if a.words_out:
        json.dump([{"t": round(w.t, 3), "text": w.text} for w in words],
                  open(a.words_out, "w"), indent=1)
    if a.srt_out:
        open(a.srt_out, "w").write(to_srt(cues))
    if not a.json_out and not a.srt_out:
        for c in cues[:20]:
            print(f"{c['start']:7.2f}  {c['text']}")


if __name__ == "__main__":
    main()
