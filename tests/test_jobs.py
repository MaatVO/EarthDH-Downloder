"""Job management: the same output must never be written by two jobs, a job never runs twice at once."""

import threading
import time

import pytest

from edh_downloader import runner, service
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, TimeSpec


def _spec(fake, home, name="dup"):
    return JobSpec(dataset="fake", dataset_url=fake.url, variables=["t2m", "tp"],
                   time=TimeSpec("2019-11-10", "2019-12-20", "none"), area=AreaSpec("global"),
                   output=OutputSpec(directory=str(home / "out"), name=name),
                   download=DownloadSpec(key="test", connections=2, min_free_gb=0.1))


def _wait(job, timeout=90):
    for _ in range(int(timeout / 0.3)):
        st = runner.status(job)
        if st["status"] not in runner.ACTIVE and st["status"] != "created":
            return st
        time.sleep(0.3)
    return runner.status(job)


def test_double_launch_of_the_same_output_is_refused(home, fake_edh):
    d = _spec(fake_edh, home).to_dict()
    first = service.job_start(d)
    with pytest.raises(ValueError):
        service.job_start(d)  # a double click on Launch, before the worker created the repository
    assert _wait(runner.resolve(first["id"]))["status"] == "completed"


def test_concurrent_resume_runs_the_job_once(home, fake_edh):
    job = runner.create_job(_spec(fake_edh, home, "twice"))
    out, errs = [], []

    def go():
        try:
            out.append(runner.launch(job))
        except RuntimeError as e:
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(out) == 1, f"{len(out)} worker processes started for one job"
    assert _wait(job)["status"] == "completed"


def test_log_tail_is_incremental(home):
    job = runner.create_job(JobSpec(variables=["t2m"]))
    log = job / "log.txt"
    log.write_text("".join(f"line {i}\n" for i in range(5000)))
    r = service.job_log(job.name)
    assert r["lines"][-1] == "line 4999" and len(r["lines"]) == service.LOG_TAIL
    with open(log, "a") as f:
        f.write("new 1\nnew 2\npartial")
    r2 = service.job_log(job.name, r["next"])
    assert r2["lines"] == ["new 1", "new 2"]
    with open(log, "a") as f:
        f.write(" done\n")
    assert service.job_log(job.name, r2["next"])["lines"] == ["partial done"]


def test_structure_of_two_stores_is_not_recomputed_on_every_refresh(home, fake_edh, monkeypatch):
    from edh_downloader.gui import structure

    a, b = home / "a.zarr", home / "b.zarr"
    for p in (a, b):
        p.mkdir()
        (p / "zarr.json").write_text("{}")
    calls = []
    monkeypatch.setattr(structure, "from_store", lambda p: calls.append(p) or {"p": str(p)})
    for _ in range(3):  # three refreshes of the Jobs tab with both panels open
        service.store_structure(str(a))
        service.store_structure(str(b))
    assert len(calls) == 2, f"read the stores {len(calls)} times"


def test_default_spool_is_tmp_in_the_output_folder(home):
    s = JobSpec(variables=["t2m"])
    s.output.directory = str(home / "out")
    job = runner.create_job(s)
    assert s.spool_path(job.name) == home / "out" / "tmp" / job.name
    sp = s.spool_path(job.name)
    sp.mkdir(parents=True)
    (sp / "chunk").write_bytes(b"x" * 100)
    other = home / "out" / "tmp" / "another-job"
    other.mkdir()
    s.remove_spool(job.name)  # leaves tmp/ while another job uses it
    assert not sp.exists() and other.exists()
    other.rmdir()
    sp.mkdir()
    service.job_delete(job.name)
    assert not job.exists() and not (home / "out" / "tmp").exists()


def test_a_job_started_with_the_old_spool_keeps_it(home):
    """Resuming a job whose chunks are in <job folder>/spool must find them there, not start over in tmp/."""
    s = JobSpec(variables=["t2m"])
    s.output.directory = str(home / "out")
    job = runner.create_job(s)
    old = job / "spool" / "t2m"
    old.mkdir(parents=True)
    (old / "0.0.0").write_bytes(b"x" * 100)
    assert s.spool_path(job.name, job) == job / "spool"
    assert s.spool_path(job.name) == home / "out" / "tmp" / job.name  # a fresh job gets the new default
    s.remove_spool(job.name, job)
    assert not (job / "spool").exists()


def test_delete_removes_a_custom_spool(home):
    s = JobSpec(variables=["t2m"])
    s.download.spool_dir = str(home / "spool")
    job = runner.create_job(s)
    sp = home / "spool" / job.name
    sp.mkdir(parents=True)
    (sp / "chunk").write_bytes(b"x" * 100)
    service.job_delete(job.name)
    assert not job.exists() and not sp.exists()
