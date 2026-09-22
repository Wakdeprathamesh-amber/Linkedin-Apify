"""Background job registry.

A full 295-keyword sweep takes ~15 minutes, which cannot fit inside the 600s
gunicorn request timeout. Work therefore runs in a daemon thread and the browser
polls for progress; closing the tab does not stop the run.

IMPORTANT: the registry lives in process memory, so gunicorn MUST stay on
`--workers 1` (see Procfile). A second worker would not see jobs created by the
first. Threads are fine — the Procfile uses the gthread worker.
"""
import threading
import traceback
import uuid
from datetime import datetime, timezone

_JOBS = {}
_LOCK = threading.Lock()
MAX_RETAINED_JOBS = 20


class Job:
    """Mutable progress record for one background run."""

    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"           # queued | running | done | error | cancelled
        self.phase = "starting"
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished_at = None
        self.error = None
        self.cancel_requested = False

        self.keywords_done = 0
        self.keywords_total = 0
        self.posts_found = 0
        self.estimated_cost = 0.0
        self.steps = []                  # human-readable log
        self.result = {}                 # payload for the UI once finished

    # ── progress helpers (called from the worker thread) ──────────────────
    def set_phase(self, phase: str):
        self.phase = phase
        self.log(phase)

    def log(self, message: str):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.steps.append(f"{stamp}  {message}")
        del self.steps[:-100]

    def progress(self, done: int, total: int, posts: int):
        self.keywords_done, self.keywords_total, self.posts_found = done, total, posts

    def should_cancel(self) -> bool:
        return self.cancel_requested

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "error": self.error,
            "keywordsDone": self.keywords_done,
            "keywordsTotal": self.keywords_total,
            "postsFound": self.posts_found,
            "estimatedCost": round(self.estimated_cost, 2),
            "steps": self.steps[-25:],
            "result": self.result,
        }


# ─── Registry ───────────────────────────────────────────────────────────────

def create(kind: str) -> Job:
    job = Job(kind)
    with _LOCK:
        _JOBS[job.id] = job
        if len(_JOBS) > MAX_RETAINED_JOBS:
            finished = [j for j in _JOBS.values() if j.finished_at]
            finished.sort(key=lambda j: j.finished_at)
            for old in finished[: len(_JOBS) - MAX_RETAINED_JOBS]:
                _JOBS.pop(old.id, None)
    return job


def get(job_id: str):
    with _LOCK:
        return _JOBS.get(job_id)


def cancel(job_id: str) -> bool:
    job = get(job_id)
    if not job or job.status in ("done", "error", "cancelled"):
        return False
    job.cancel_requested = True
    job.log("cancellation requested")
    return True


def start(job: Job, target, *args, **kwargs):
    """Run `target(job, *args)` in a daemon thread and keep the job updated."""

    def runner():
        job.status = "running"
        try:
            target(job, *args, **kwargs)
            if job.cancel_requested:
                job.status = "cancelled"
                job.set_phase("cancelled")
            else:
                job.status = "done"
                job.set_phase("finished")
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)[:500]
            job.log(f"ERROR: {exc}")
            traceback.print_exc()
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=runner, daemon=True, name=f"job-{job.id}").start()
    return job
