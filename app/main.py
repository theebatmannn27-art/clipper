"""Clipper Studio — a single-user web app on top of the vertical-shorts
pipeline. Paste a YouTube/direct URL or upload a file, get a review board of
the detected peak moments (with thumbnails), edit each clip's hook / title /
trim, then render 9:16 captioned clips and download them.

No accounts, no billing: this is the MVP built directly on `py/`.
"""
from __future__ import annotations
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store
from ._path import REPO, DATA_DIR
from . import worker

app = FastAPI(title="Clipper Studio", version="0.1.0")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipjob")
_job_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# job processing
# --------------------------------------------------------------------------- #

def _update(job_id: str, **fields) -> None:
    store.update_job(job_id, **fields)


def _progress(job_id: str):
    def cb(stage: str, pct: float | None = None) -> None:
        fields = {"stage": stage}
        if pct is not None:
            fields["progress"] = pct
        store.update_job(job_id, **fields)
    return cb


def _run_job(job_id: str) -> None:
    job = store.get_job(job_id)
    if not job:
        return
    jobdir = store.job_dir(job_id)
    try:
        step = _progress(job_id)

        # 1. acquire source
        src = os.path.join(jobdir, "source.mp4")
        if job.get("source_url"):
            step("Downloading source video…", 0.05)
            try:
                got = worker.fetch_source(
                    job["source_url"], src, os.path.join(jobdir, "download"))
                vtt = got.get("vtt")
                if vtt:
                    os.replace(vtt, os.path.join(jobdir, "source.vtt"))
                vtt = os.path.join(jobdir, "source.vtt") if os.path.exists(
                    os.path.join(jobdir, "source.vtt")) else None
            except Exception as e:
                raise RuntimeError(str(e)) from e
        else:
            # upload already stored as source.mp4 / source.vtt
            vtt = os.path.join(jobdir, "source.vtt")
            vtt = vtt if os.path.exists(vtt) else None

        if not os.path.exists(src):
            raise RuntimeError("Source video missing after upload/download.")

        upd = worker.run_through_review(job, jobdir, src, vtt, step)
        upd["state"] = "review"
        _update(job_id, **upd)
    except Exception as e:
        _update(job_id, state="failed", stage="Failed", error=str(e))
        print(f"[job:{job_id}] FAILED: {e}")


def _run_render(job_id: str) -> None:
    job = store.get_job(job_id)
    if not job:
        return
    jobdir = store.job_dir(job_id)
    try:
        upd = worker.render_clips(job, jobdir, _progress(job_id))
        _update(job_id, state="done", stage="Done", **upd)
    except Exception as e:
        _update(job_id, state="review", stage="Render failed", error=str(e),
                render_error=str(e))
        print(f"[job:{job_id}] RENDER FAILED: {e}")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health():
    from . import dl
    return {
        "ok": True,
        "ffmpeg": worker.FFMPEG,
        "ffmpeg_present": os.path.isfile(worker.FFMPEG)
        if worker.FFMPEG != "ffmpeg" else bool(shutil.which("ffmpeg")),
        "ytdlp": bool(dl.ytdlp_path()),
        "storage": DATA_DIR,
    }


@app.get("/api/meta")
def meta():
    """Capability flags the frontend uses to shape the UX."""
    from . import dl
    have_yt = dl.ytdlp_path() is not None
    return {
        "ytdlp": have_yt,
        "ytdlp_configured": dl.ytdlp_config_present(),
        "youtube_ready": have_yt and (dl.ytdlp_config_present() or
                                      os.environ.get("CLIP_ALLOW_YT", "") == "1"),
        "labels": {
            "len": "Clip length (s)", "count": "Number of clips",
            "style": "Caption style", "mode": "Framing",
            "hook": "Burn hook title", "brand": "Brand watermark", "cx": "Crop centre",
        },
    }


def _normalise_target_url(url: str | None) -> tuple[bool, str | None]:
    """Returns (is_youtube, clean_url)."""
    if not url:
        return False, None
    u = url.strip()
    return ("youtube.com" in u or "youtu.be" in u), u


@app.post("/api/jobs")
def create_job(
    url: str = Form(""),
    file: Optional[UploadFile] = File(None),
    vtt_file: Optional[UploadFile] = File(None),
    len_sec: float = Form(40),
    count: int = Form(5),
    style: str = Form("block"),
    mode: str = Form("crop"),
    hook: bool = Form(True),
    brand: str = Form(""),
    cx: float = Form(0.5),
    min_len: float = Form(25),
    gap: float = Form(20),
):
    job = store.new_job(url or None,
                        file.filename if file else None)
    jobdir = store.job_dir(job["id"])
    opts = dict(len=len_sec, count=count, style=style, mode=mode,
                hook=hook, brand=brand, cx=cx, min_len=min_len, gap=gap)
    store.update_job(job["id"], options=opts)

    if file is not None:
        src = os.path.join(jobdir, "source.mp4")
        with open(src, "wb") as f:
            shutil.copyfileobj(file.file, f)
        if vtt_file is not None and vtt_file.filename:
            with open(os.path.join(jobdir, "source.vtt"), "wb") as f:
                shutil.copyfileobj(vtt_file.file, f)

    _executor.submit(_run_job, job["id"])
    return job


@app.get("/api/jobs")
def jobs():
    return store.list_jobs()


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    j = store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    shutil.rmtree(store.job_dir(job_id), ignore_errors=True)
    db = store._load()
    db["jobs"].pop(job_id, None)
    store._save(db)
    return {"ok": True}


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: str, body: dict):
    """Edit options and/or the clip list while a job is in review.

    body: {"options": {...}} or {"peaks": [...]} or {"rethumb": {...}}
    """
    j = store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    if "options" in body and isinstance(body["options"], dict):
        opts = dict(j.get("options", {}))
        opts.update(body["options"])
        store.update_job(job_id, options=opts)
        return store.get_job(job_id)

    if "peaks" in body:
        peaks = body["peaks"]
        for p in peaks:
            p.setdefault("selected", True)
        store.update_job(job_id, peaks=peaks)
        return store.get_job(job_id)

    if "rethumb" in body:
        rt = body["rethumb"]
        name = rt.get("name", "clip01")
        at = float(rt.get("t", 0.0))
        jobdir = store.job_dir(job_id)
        src = os.path.join(jobdir, "source.mp4")
        cx = float((j.get("options") or {}).get("cx", 0.5))
        ok = worker.gen_thumbnail(src, at,
                                  os.path.join(jobdir, "thumbs", f"{name}.jpg"),
                                  width=360, cx=cx)
        return {"ok": ok}

    raise HTTPException(400, "Nothing recognised in PATCH body")


@app.post("/api/jobs/{job_id}/render")
def start_render(job_id: str, body: dict | None = None):
    j = store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    state = j.get("state")
    if state not in ("review", "done", "failed"):
        raise HTTPException(409, f"Can't render while job is {state}")
    body = body or {}
    if "peaks" in body:
        store.update_job(job_id, peaks=body["peaks"])
    if "options" in body:
        opts = dict(j.get("options", {}))
        opts.update(body["options"])
        store.update_job(job_id, options=opts)
    _update(job_id, state="rendering", progress=0.0, stage="Rendering…",
            render_error=None)
    _executor.submit(_run_render, job_id)
    return store.get_job(job_id)


@app.get("/api/jobs/{job_id}/thumb/{name}")
def thumb(job_id: str, name: str):
    path = os.path.join(store.job_dir(job_id), "thumbs", name)
    if not os.path.exists(path):
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


def _clip_path(job_id: str, name: str) -> str:
    jobdir = store.job_dir(job_id)
    base = os.path.basename(name)  # no traversal
    return os.path.join(jobdir, "out", base)


@app.get("/api/jobs/{job_id}/clips/{name}")
def clip(job_id: str, name: str):
    path = _clip_path(job_id, name)
    if not os.path.exists(path):
        raise HTTPException(404, "Clip not found")
    media = "video/mp4" if path.endswith(".mp4") else "application/octet-stream"
    return FileResponse(path, media_type=media,
                        headers={"Content-Disposition":
                                 f'attachment; filename="{os.path.basename(path)}"'})


@app.get("/api/jobs/{job_id}/clips/captions/{name}")
def caption(job_id: str, name: str):
    path = os.path.join(store.job_dir(job_id), "out", "captions",
                        os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Caption file not found")
    return FileResponse(path, media_type="text/plain")


@app.get("/api/jobs/{job_id}/zip")
def zip_all(job_id: str):
    """ZIP every rendered clip for one-click download."""
    import io
    import zipfile
    from starlette.responses import Response
    jobdir = store.job_dir(job_id)
    outdir = os.path.join(jobdir, "out")
    if not os.path.isdir(outdir):
        raise HTTPException(404, "No rendered clips yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in sorted(os.listdir(outdir)):
            fp = os.path.join(outdir, fn)
            if os.path.isfile(fp) and fn.lower().endswith(".mp4"):
                z.write(fp, arcname=fn)
    buf.seek(0)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="clips-{job_id}.zip"'})


# --------------------------------------------------------------------------- #
# static frontend (served last so /api wins)
# --------------------------------------------------------------------------- #

_static = os.path.join(REPO, "web")
_assets = os.path.join(_static, "assets")
if os.path.isdir(_assets):
    app.mount("/assets", StaticFiles(directory=_assets), name="assets")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="web")
