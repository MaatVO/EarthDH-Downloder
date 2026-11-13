"""Job folders and process control.

Each job lives in ``<data>/jobs/<id>/`` with ``job.json`` (spec), ``state.json`` (progress), ``log.txt`` and
``control.json`` (requests from the UI: connections, pause/cancel). Detached jobs run in their own session
(``setsid``) with stdin/stdout detached, so they survive the SSH session or the web UI being closed.
"""

import fcntl
import os
import re
from contextlib import contextmanager
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .job import JobSpec
from .paths import jobs_dir
from .util import read_json, write_json_atomic

ACTIVE = {"starting", "running", "waiting_quota", "exporting"}
FINISHED = {"completed", "cancelled"}
START_GRACE = 30  # seconds a just-launched worker counts as alive before its process can be recognised


@contextmanager
def file_lock(path: Path):
    """Exclusive lock between threads and processes (released when the holder dies)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def hold_run_lock(job_dir: Path) -> int | None:
    """Taken by the worker for its whole life: a second worker of the same job cannot start. None if held."""
    fd = os.open(Path(job_dir) / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def run_lock_held(job_dir: Path) -> bool:
    p = Path(job_dir) / "run.lock"
    if not p.exists():
        return False
    fd = os.open(p, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-")[:40] or "job"


def create_job(spec: JobSpec) -> Path:
    from . import __version__

    jid = f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(spec.output.name)}"
    d = jobs_dir() / jid
    n = 1
    while d.exists():
        n += 1
        d = jobs_dir() / f"{jid}-{n}"
    d.mkdir(parents=True)
    write_json_atomic(d / "job.json", {"id": d.name, "created": datetime.now().isoformat(timespec="seconds"),
                                       "app_version": __version__, "spec": spec.to_dict()})
    write_json_atomic(d / "state.json", {"status": "created", "units_done": []})
    write_json_atomic(d / "control.json", {"connections": spec.download.connections})
    return d


def resolve(job: str | Path) -> Path:
    p = Path(job)
    if (p / "job.json").exists():
        return p
    d = jobs_dir() / str(job)
    if (d / "job.json").exists():
        return d
    matches = [x for x in jobs_dir().iterdir() if x.name.startswith(str(job)) or x.name.endswith(str(job))]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"job {job} not found" + (f" (ambiguous: {len(matches)} matches)" if matches else ""))


def pid_alive(pid: int | None, job_dir: Path) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        return str(job_dir.name) in cmd
    except OSError:
        return True


def _pid_exists(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def status(job_dir: Path) -> dict:
    job_dir = Path(job_dir)
    meta = read_json(job_dir / "job.json", {}, dict)
    st = read_json(job_dir / "state.json", {}, dict)
    s = dict(st)
    s["id"] = meta.get("id", job_dir.name)
    s["dir"] = str(job_dir)
    s["created"] = meta.get("created")
    s["spec"] = meta.get("spec") if isinstance(meta.get("spec"), dict) else {}
    if not s["spec"]:
        s["status"] = "unreadable"
        s["error"] = "job.json is missing or damaged: this job cannot run (delete it)"
    just_launched = st.get("status") == "starting" and time.time() - (st.get("launched_at") or 0) < START_GRACE
    alive = run_lock_held(job_dir) or pid_alive(st.get("pid"), job_dir) or (
        just_launched and _pid_exists(st.get("pid")))
    if st.get("status") in ACTIVE and not alive:
        s["status"] = "interrupted"  # process died without recording a final state
    s["alive"] = alive
    return s


def list_jobs() -> list[dict]:
    out = []
    for d in sorted(jobs_dir().iterdir(), reverse=True):
        if (d / "job.json").exists():
            try:
                out.append(status(d))
            except Exception as e:  # noqa: BLE001 - one broken job folder must not hide the others
                out.append({"id": d.name, "dir": str(d), "status": "unreadable", "spec": {}, "alive": False,
                            "error": f"{type(e).__name__}: {e}"})
    return out


def _self_cmd() -> list[str]:
    if os.environ.get("APPIMAGE"):  # AppImage: relaunch the image file, not its temporary mount
        return [os.environ["APPIMAGE"]]
    if getattr(sys, "frozen", False):  # PyInstaller single-file executable
        return [sys.executable]
    return [sys.executable, "-m", "edh_downloader"]


def launch(job_dir: Path) -> int:
    """Start the job as a detached background process; returns its PID."""
    job_dir = Path(job_dir)
    with file_lock(job_dir / "launch.lock"):  # two clicks on Resume start one worker
        return _launch(job_dir)


def _launch(job_dir: Path) -> int:
    st = status(job_dir)
    if st.get("status") == "unreadable":
        raise RuntimeError(st["error"])
    if st["alive"]:
        raise RuntimeError(f"job {job_dir.name} is already running (pid {st['pid']})")
    if st.get("status") == "completed":
        raise RuntimeError(f"job {job_dir.name} is already completed")
    ctl = read_json(job_dir / "control.json", {}, dict)
    ctl.pop("action", None)
    write_json_atomic(job_dir / "control.json", ctl)
    out = open(job_dir / "worker.out", "ab")
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the worker unpacks its own copy; it outlives the parent
    p = subprocess.Popen(_self_cmd() + ["_worker", str(job_dir)], stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, cwd=str(job_dir), env=env)
    write_json_atomic(job_dir / "state.json", {**read_json(job_dir / "state.json", {}, dict),
                                               "status": "starting", "pid": p.pid, "launched_at": time.time()})
    out.close()
    return p.pid


def run_foreground(job_dir: Path, verbose: bool = False) -> str:
    from .engine import Engine, setup_logging

    job_dir = Path(job_dir)
    st = status(job_dir)
    if st.get("status") == "completed":
        return "completed"
    # launch() records the PID it spawned; in a PyInstaller binary that is the bootloader, our parent
    if st["alive"] and st.get("pid") not in (os.getpid(), os.getppid()):
        raise RuntimeError(f"job {job_dir.name} is already running (pid {st['pid']})")
    lock = hold_run_lock(job_dir)
    if lock is None:  # another worker of this job is running: leave its state alone
        raise RuntimeError(f"job {job_dir.name} is already running")
    try:
        log = setup_logging(job_dir, to_stdout=verbose)
        if (st.get("spec") or {}).get("source") == "cds":
            from .sources.cds.engine import CdsEngine

            return CdsEngine(job_dir, log).run()
        return Engine(job_dir, log).run()
    finally:
        os.close(lock)


def set_control(job_dir: Path, **kw) -> None:
    ctl = read_json(Path(job_dir) / "control.json", {}, dict)
    ctl.update(kw)
    write_json_atomic(Path(job_dir) / "control.json", ctl)


def stop(job_dir: Path, action: str = "pause", wait: float = 0) -> bool:
    """Ask a running job to stop after the unit it is processing (``action``: pause or cancel)."""
    job_dir = Path(job_dir)
    st = status(job_dir)
    set_control(job_dir, action=action)
    if not st["alive"]:
        if action == "cancel" and st.get("status") != "completed":
            write_json_atomic(job_dir / "state.json", {**read_json(job_dir / "state.json", {}, dict),
                                                       "status": "cancelled", "pid": None})
        return False
    os.kill(st["pid"], signal.SIGTERM)
    t = time.time()
    while wait and time.time() - t < wait and pid_alive(st["pid"], job_dir):
        time.sleep(0.5)
    return True


def delete(job_dir: Path, outputs: bool = False) -> None:
    job_dir = Path(job_dir)
    st = status(job_dir)
    if st["alive"]:
        raise RuntimeError("stop the job first")
    spec = JobSpec.from_dict(st["spec"]) if st.get("spec") else None
    if spec:  # temporary chunks live outside the job folder
        spec.remove_spool(job_dir.name, job_dir)
    if outputs and spec:
        for p in (spec.icechunk_path, spec.zarr_path):
            if p.exists():
                shutil.rmtree(p)
    shutil.rmtree(job_dir)
