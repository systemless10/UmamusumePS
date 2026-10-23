
from __future__ import annotations

import itertools
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

LOGS = Path(__file__).resolve().parent / "logs" / "jobs"
KEEP = 40


class Job:
    def __init__(self, jid: int, region: str, recipe: str, title: str, steps: list[dict]):
        self.id, self.region, self.recipe, self.title, self.steps = jid, region, recipe, title, steps
        self.state = "running"
        self.step = 0
        self.started, self.ended = time.time(), None
        self.proc: subprocess.Popen | None = None
        self.log = LOGS / f"job-{jid}-{int(self.started)}.log"
        self.cancelled = False

    def view(self) -> dict:
        return {"id": self.id, "region": self.region, "recipe": self.recipe, "title": self.title,
                "state": self.state, "step": self.step, "steps": [s.get("label") or s["argv"][-1] for s in self.steps],
                "started": self.started, "ended": self.ended,
                "size": self.log.stat().st_size if self.log.exists() else 0}


class Jobs:
    def __init__(self):
        self.jobs: dict[int, Job] = {}
        self.ids = itertools.count(int(time.time()) % 100000)
        self.lock = threading.Lock()

    def start(self, region: str, recipe: str, plan: dict) -> dict:
        LOGS.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if plan.get("exclusive") and any(j.state == "running" and j.recipe == recipe for j in self.jobs.values()):
                raise ValueError(f"A {recipe} job is already running.")
            job = Job(next(self.ids), region, recipe, plan.get("title") or recipe, plan["steps"])
            self.jobs[job.id] = job
            for old in sorted(self.jobs)[:-KEEP]:
                if self.jobs[old].state != "running":
                    del self.jobs[old]
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job.view()

    def _run(self, job: Job) -> None:
        with open(job.log, "ab", buffering=0) as log:
            for i, step in enumerate(job.steps):
                job.step = i
                log.write(f"$ {' '.join(step['argv'])}\n".encode())
                env = dict(os.environ, PYTHONUNBUFFERED="1", **(step.get("env") or {}))
                try:
                    job.proc = subprocess.Popen(step["argv"], cwd=step.get("cwd"), env=env, stdout=log,
                                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                start_new_session=os.name != "nt")
                    code = job.proc.wait()
                except OSError as ex:
                    log.write(f"could not start: {ex}\n".encode())
                    code = -1
                if job.cancelled:
                    job.state = "cancelled"
                    break
                if code != 0:
                    log.write(f"\n[exit {code}]\n".encode())
                    job.state = "failed"
                    break
                log.write(b"\n")
            else:
                job.state = "done"
                job.step = len(job.steps)
        job.ended = time.time()
        job.proc = None

    def list(self) -> list[dict]:
        return [j.view() for j in sorted(self.jobs.values(), key=lambda j: -j.started)]

    def get(self, jid: int, offset: int = 0) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        text = ""
        if job.log.exists():
            with open(job.log, "rb") as fh:
                fh.seek(max(0, offset))
                text = fh.read(512 * 1024).decode("utf-8", "replace")
        return {**job.view(), "from": offset, "text": text}

    def cancel(self, jid: int) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        job.cancelled = True
        if job.proc and job.proc.poll() is None:
            try:
                if os.name == "nt":
                    job.proc.terminate()
                else:
                    os.killpg(job.proc.pid, signal.SIGTERM)
            except OSError:
                pass
        return job.view()

    def close(self) -> None:
        for jid in list(self.jobs):
            if self.jobs[jid].state == "running":
                self.cancel(jid)
