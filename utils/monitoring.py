import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


STATUSES = {
    "CONFIRMED", "SUBMIT_UNCONFIRMED", "INCOMPLETE", "NO_EASY_APPLY",
    "NO_MODAL", "ALREADY_APPLIED", "ERROR", "CLOSED",
}
ROOT = Path(__file__).resolve().parent.parent


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def public_job(job):
    job_id = str(job.get("job_id") or "")
    job_id = job_id if re.fullmatch(r"[0-9]{1,30}", job_id) else ""
    return {
        "job_id": job_id,
        "title": str(job.get("title") or "Unknown")[:250],
        "company": str(job.get("company") or "Unknown")[:200],
        "link": f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else "",
    }


def error_summary(error, stage):
    if "timeout" in type(error).__name__.lower() or "timeout" in str(error).lower():
        message = f"Timed out during {stage}. Check the browser and retry after resolving the cause."
    elif "ERR_NETWORK" in str(error) or "ERR_INTERNET" in str(error):
        message = "Browser network connection failed or was suspended."
    else:
        message = f"{stage} failed. See the local bot terminal for details."
    return {"at": timestamp(), "type": type(error).__name__, "stage": stage, "message": message}


class RunStatus:
    def __init__(self, path=None, heartbeat_interval=5):
        self.path = Path(path) if path else ROOT / "bot_status.json"
        self.heartbeat_interval = heartbeat_interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._active = False
        self._write_warned = False
        self._confirmed = set()
        self.state = {}

    def start(self):
        with self._lock:
            if self._active:
                return
            now = timestamp()
            self._stop.clear()
            self._confirmed.clear()
            self.state = {
                "schema_version": 1, "run_id": uuid.uuid4().hex, "pid": os.getpid(),
                "started_at": now, "updated_at": now, "heartbeat_at": now,
                "phase": "starting", "activity": "Starting browser", "cycle": 0,
                "page_offset": 0, "current_job": None, "next_cycle_at": None,
                "results_found": 0, "last_cycle_result": None, "last_error": None,
                "cycle_target": None, "cycle_confirmed": 0, "cycle_processed": 0,
                "cycle_pages": 0, "cycle_candidate_limit": None, "cycle_page_limit": None,
                "consecutive_failures": 0, "history_write_failed": False,
                "counts": {status: 0 for status in sorted(STATUSES)},
                "confirmed_unique": 0, "recent_results": [], "events": [],
            }
            self._active = True
            self._event_locked("started", "Bot run started")
            self._write_locked()
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()

    def _write_locked(self):
        self.state["heartbeat_at"] = timestamp()
        try:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.01)
            self._write_warned = False
        except OSError:
            if not self._write_warned:
                print("Monitor status could not be saved; live monitoring may be unavailable.")
                self._write_warned = True

    def _heartbeat(self):
        while not self._stop.wait(self.heartbeat_interval):
            with self._lock:
                if self._active:
                    self._write_locked()

    def _event_locked(self, kind, message):
        self.state["events"].append({"at": timestamp(), "kind": kind, "message": message})
        self.state["events"] = self.state["events"][-60:]

    def update(self, **fields):
        with self._lock:
            if not self._active:
                return
            if fields.get("current_job"):
                fields["current_job"] = public_job(fields["current_job"])
            self.state.update(fields)
            self.state["updated_at"] = timestamp()
            self._write_locked()

    def event(self, kind, message):
        with self._lock:
            if self._active:
                self._event_locked(kind, message)
                self._write_locked()

    def error(self, error, stage, cycle_failed=False):
        with self._lock:
            if not self._active:
                return
            detail = error_summary(error, stage)
            detail["cycle"] = self.state["cycle"]
            self.state["last_error"] = detail
            self.state["phase"] = "error"
            self.state["activity"] = detail["message"]
            if cycle_failed:
                self.state["consecutive_failures"] += 1
                self.state["last_cycle_result"] = "failed"
            self._event_locked("error", detail["message"])
            self.state["updated_at"] = timestamp()
            self._write_locked()

    def record_application(self, job, status, persisted=True):
        with self._lock:
            if not self._active:
                return
            status = status if status in STATUSES else "ERROR"
            result = {**public_job(job), "status": status, "timestamp": timestamp()}
            self.state["counts"][status] += 1
            if status == "CONFIRMED":
                self._confirmed.add(result["job_id"] or uuid.uuid4().hex)
                self.state["confirmed_unique"] = len(self._confirmed)
            self.state["recent_results"].append(result)
            self.state["recent_results"] = self.state["recent_results"][-50:]
            self.state["history_write_failed"] |= not persisted
            self._event_locked("application", f"{status}: {result['title']} at {result['company']}")
            self.state["updated_at"] = timestamp()
            self._write_locked()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self._lock:
            if self._active:
                self.state.update(phase="stopped", activity="Bot stopped", next_cycle_at=None, current_job=None)
                self.state["updated_at"] = timestamp()
                self._event_locked("stopped", "Bot run stopped")
                self._write_locked()
                self._active = False


reporter = RunStatus()
