"""An HTTP downloader that sticks to the repo's original spirit — keep
unauthenticated YouTube grabs out of the box, but let people who use the CLI
with cookies bring their own cookie-safe setup via a standard `yt-dlp`
config file. Anything that isn't YouTube just downloads directly.

Precedence for a video URL:
  1. yt-dlp with the user's own config (~/.config/yt-dlp/config), if it exists
  2. yt-dlp stock (works for many sites; YouTube will typically be bot-blocked
     from an anonymous datacenter IP)
  3. plain HTTP GET (for direct .mp4/.webm/.mkv links)
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import urllib.request


def ytdlp_path() -> str | None:
    p = os.environ.get("YTDLP")
    if p and os.path.isfile(p):
        return p
    p = shutil.which("yt-dlp")
    if p:
        return p
    # the app runs under a venv whose bin/ may not be on PATH
    exe_dir = os.path.dirname(sys.executable)
    for cand in (os.path.join(exe_dir, "yt-dlp"),
                 os.path.join(sys.prefix, "bin", "yt-dlp")):
        if os.path.isfile(cand):
            return cand
    return None


def ytdlp_config_present() -> bool:
    """True when the host has a custom yt-dlp config (usually holds cookies
    for authenticated YouTube access)."""
    home = os.path.expanduser("~")
    candidates = []
    if os.environ.get("XDG_CONFIG_HOME"):
        candidates.append(os.path.join(os.environ["XDG_CONFIG_HOME"], "yt-dlp", "config"))
    if os.environ.get("YTDLP_CONFIG"):
        candidates.append(os.environ["YTDLP_CONFIG"])
    candidates.append(os.path.join(home, ".config", "yt-dlp", "config"))
    return any(os.path.isfile(c) for c in candidates)


def fetch(video_url: str, out_dir: str) -> dict:
    """Download a video (+ subtitles if available) into out_dir.

    Returns {"video": <abs path>, "vtt": <abs path or None>}.
    """
    yt = ytdlp_path()
    if yt:
        try:
            return _ytdlp_fetch(yt, video_url, out_dir)
        except subprocess.CalledProcessError as e:
            if _looks_like_media(video_url):
                return _http_fetch(video_url, out_dir)
            raise RuntimeError(_blurb(e.stderr or e.stdout or ""))
    return _http_fetch(video_url, out_dir)


def _looks_like_media(url: str) -> bool:
    return url.split("?")[0].lower().endswith((".mp4", ".webm", ".mkv", ".mov"))


def _blurb(err: str) -> str:
    lines = [ln for ln in (err or "").strip().splitlines() if ln.strip()]
    tail = (lines[-1] if lines else "").lower()
    if "certificate" in tail or "ssl" in tail or "connection has been closed" in tail:
        return ("Couldn't reach that video from this server's network. "
                "YouTube and many sites block datacenter IPs. "
                "The reliable paths here are: upload the file directly, or "
                "add your own yt-dlp cookies config. Direct .mp4/.webm links "
                "from a permitted host also work.")
    if "sign in to confirm" in tail or "not a bot" in tail:
        return ("That site is asking for sign-in / a bot check, which this "
                "server can't pass from a datacenter. Upload the file or use "
                "a direct .mp4 link instead.")
    return f"Download failed — {tail[:220]}"



def _ytdlp_fetch(yt: str, url: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cmd = [yt, "--no-warnings", "--no-playlist",
           "-f", ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                  "b[height<=1080]/b"),
           "--merge-output-format", "mp4",
           "--write-auto-subs", "--write-subs",
           "--sub-langs", "en.*,hi.*,en", "--sub-format", "vtt/srt/best",
           "--write-thumbnail",
           "-o", os.path.join(out_dir, "%(id)s.%(ext)s"),
           "-P", out_dir,
           url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)

    video = _first(out_dir, (".mp4", ".mkv", ".webm"))
    vtt = _first(out_dir, (".vtt", ".srt"))
    if video is None:
        raise RuntimeError("Download finished but no video file appeared.")
    return {"video": video, "vtt": vtt}


def _http_fetch(url: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    fname = url.split("?")[0].split("/")[-1] or "video"
    path = os.path.join(out_dir, fname)
    req = urllib.request.Request(url, headers={"User-Agent": "clipper/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return {"video": path, "vtt": None}


def _first(d: str, exts: tuple) -> str | None:
    try:
        files = sorted(
            os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith(exts))
    except FileNotFoundError:
        return None
    return files[0] if files else None
