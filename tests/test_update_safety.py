"""An app update must never break a download in progress on a server.

The job is started by the agent of build A over "SSH"; the connection drops (SIGHUP to the agent's session, as sshd
does); the app is updated and build B connects. The job keeps running with A's code and finishes; B sees and
controls it but cannot start a second worker for it."""

import io
import json
import os
import signal
import subprocess
import sys
import time
import zipfile

from edh_downloader import bundle


def _agent(zip_path, cwd):
    p = subprocess.Popen([sys.executable, "-m", "edh_downloader", "agent"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
                         cwd=str(cwd), env=dict(os.environ, PYTHONPATH=str(zip_path), PYTHONDONTWRITEBYTECODE="1"))
    assert "hello" in json.loads(p.stdout.readline())
    p.n = 0
    return p


def _call(p, m, **a):
    p.n += 1
    p.stdin.write(json.dumps({"id": p.n, "m": m, "a": a}) + "\n")
    p.stdin.flush()
    while True:
        r = json.loads(p.stdout.readline())
        if r.get("id") == p.n and "ev" not in r:
            return r


def test_job_survives_disconnect_and_app_update(home, fake_edh, tmp_path):
    from test_faults import Faulty

    slow = Faulty(fake_edh.root)
    slow.hook = lambda p, n: (time.sleep(0.1), None)[1]
    a, _ = bundle.build()
    (tmp_path / "A.zip").write_bytes(a)
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(a)) as zin, zipfile.ZipFile(buf, "w") as z:
        for i in zin.infolist():
            z.writestr(i, zin.read(i))
        z.writestr("edh_downloader/_new_build.py", "")
    (tmp_path / "B.zip").write_bytes(buf.getvalue())

    spec = {"dataset": "fake", "dataset_url": slow.url, "variables": ["t2m", "tp"],
            "time": {"start": "2019-11-10", "end": "2019-12-25"}, "area": {"mode": "global"},
            "output": {"directory": str(home / "out"), "name": "upd"},
            "download": {"key": "test", "connections": 2, "min_free_gb": 0.1}}
    A = _agent(tmp_path / "A.zip", tmp_path)
    r = _call(A, "job_start", spec=spec)
    assert r["ok"], r
    jid, pid = r["r"]["id"], r["r"]["pid"]
    time.sleep(2)
    os.killpg(A.pid, signal.SIGHUP)  # the SSH connection drops
    A.kill()
    A.wait()
    time.sleep(0.5)
    assert os.path.exists(f"/proc/{pid}"), "the download died with the connection"
    env = open(f"/proc/{pid}/environ", "rb").read()
    assert str(tmp_path / "A.zip").encode() in env  # keeps running build A's code

    B = _agent(tmp_path / "B.zip", tmp_path)  # the updated app connects
    try:
        j = next(x for x in _call(B, "jobs_list")["r"] if x["id"] == jid)
        if j["status"] in ("starting", "running"):
            assert not _call(B, "job_resume", jid=jid)["ok"], "a second worker was started for a running job"
        for _ in range(240):
            j = next(x for x in _call(B, "jobs_list")["r"] if x["id"] == jid)
            if j["status"] not in ("starting", "running", "exporting"):
                break
            time.sleep(0.5)
        assert j["status"] == "completed", j
        assert len(j["units_done"]) == j["units_total"]
    finally:
        B.kill()


def test_worker_imports_nothing_after_it_started(home, fake_edh, monkeypatch):
    """The packaged executable reads its modules from its own file, by path, on import: a self-update replaces
    that file. A job survives it only if its worker imports nothing once the download runs (also export)."""
    from edh_downloader import engine, runner
    from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec

    snap = {}
    orig = engine.Engine._process_unit

    def first(self, u, pool):
        snap.setdefault("m", set(sys.modules))
        return orig(self, u, pool)

    monkeypatch.setattr(engine.Engine, "_process_unit", first)
    for name, s in (
        ("points", JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m", "t", "lsm"], levels=[1000],
                           time=TimeSpec("2019-11-03", "2019-12-20", "day"),
                           area=AreaSpec("points", points=[Point("a", 1, 1)]),
                           output=OutputSpec(directory=str(home / "o"), name="li1", chunk_layout="timeseries",
                                             sharding=True, compressor="gzip", dtype="float64", keep_icechunk=False),
                           download=DownloadSpec(key="test", min_free_gb=0.1))),
        ("bbox", JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["tp"], time=TimeSpec("2019-11-01", "2019-12-31", "month"),
                         area=AreaSpec("bbox", 150, -30, -150, 30), output=OutputSpec(directory=str(home / "o"), name="li2",
                                                                                      compressor="blosc-lz4", keepbits=10),
                         download=DownloadSpec(key="test", min_free_gb=0.1)))):
        snap.clear()
        assert runner.run_foreground(runner.create_job(s)) == "completed"
        late = sorted(set(sys.modules) - snap["m"])
        assert not late, f"{name}: modules imported mid-job: {late}"
