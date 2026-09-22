"""In-memory job store persisted to a JSON file under data/.

Single-user MVP: no auth, but the job model is shaped so a user_id (auth,
multi-tenant) can be added later without changing the API.
"""
from __future__ import annotations
import json
import os
import threading
import time
import uuid

from ._path import DATA_DIR

_LOCK = threading.RLock()
_STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Job lifecycle:
#   queued -> fetching/downloading -> analyzing -> review -> rendering -> done
#   (any step can jump to failed)
VALID_STATES = (
    "queued", "downloading", "analyzing", "review", "rendering",
    "done", "failed",
)


def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"jobs": {}}


def _save(db: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=1)
    os.replace(tmp, _STATE_FILE)


def new_job(source_url: str | None, source_filename: str | None) -> dict:
    with _LOCK:
        db = _load()
        now = time.time()
        job = {
            "id": uuid.uuid4().hex[:16],
            "state": "queued",
            "progress": 0,
            "stage": "Waiting in queue…",
            "error": None,
            "source_url": source_url,
            "source_filename": source_filename,
            "options": {
                "len": 40, "count": 5, "style": "block", "mode": "crop",
                "hook": True, "brand": "", "cx": 0.5,
            },
            "peaks": [],
            "clips": [],
            "duration": None,
            "captions": False,
            "created_at": now,
            "updated_at": now,
        }
        db["jobs"][job["id"]] = job
        _save(db)
        return job


def get_job(job_id: str) -> dict | None:
    db = _load()
    return db["jobs"].get(job_id)


def list_jobs() -> list[dict]:
    db = _load()
    jobs = list(db["jobs"].values())
    jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
    return jobs


def update_job(job_id: str, **fields) -> dict | None:
    with _LOCK:
        db = _load()
        job = db["jobs"].get(job_id)
        if job is None:
            return None
        for k, v in fields.items():
            if v is not None:
                job[k] = v
        job["updated_at"] = time.time()
        if "state" in fields and fields["state"] not in VALID_STATES:
            raise ValueError(f"bad job state {fields['state']!r}")
        _save(db)
        return job


def set_stage(job_id: str, stage: str, progress: float | None = None) -> None:
    fields: dict = {"stage": stage}
    if progress is not None:
        fields["progress"] = round(float(progress), 3)
    update_job(job_id, **fields)


def job_dir(job_id: str) -> str:
    d = os.path.join(DATA_DIR, "jobs", job_id)
    os.makedirs(d, exist_ok=True)
    return d
