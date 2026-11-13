"""What a running job reports about itself (``state["activity"]``) and how the job card shows it."""

from test_cds_engine import fast, spec  # noqa: F401  (fixture)

from edh_downloader import runner
from edh_downloader.gui.app import activity_rows, request_rows
from edh_downloader.runner import create_job, run_foreground
from edh_downloader.sources.cds import engine as cds_engine


def test_activity_rows():
    a = {"stage": "processing",
         "unit": {"index": 2, "of": 5, "first": "2020-01-01 00:00:00", "last": "2020-01-31 23:00:00", "item": 2,
                  "items": 4, "var": "t", "level": 500, "steps_done": 372, "steps": 744, "phase": "reading"},
         "downloads": [{"request": 3, "start": "2020-02-01", "end": "2020-02-29", "bytes": 25_000_000,
                        "size": 100_000_000, "rate_bps": 5_000_000}]}
    unit, dl = activity_rows(a)
    assert unit["title"] == "Processing unit 2 of 5" and "t at level 500 (2/4)" in unit["detail"]
    assert unit["frac"] == (1 + 0.5) / 4 and unit["right"] == "38%"
    assert "reading and cutting" in unit["detail"]
    assert dl["title"] == "Downloading request 3" and dl["frac"] == 0.25 and dl["right"] == "25%"
    assert "25.0 MB of 100.0 MB" in dl["detail"] and "5.0 MB/s" in dl["detail"] and "15s left" in dl["detail"]
    ex, = activity_rows({"stage": "exporting", "export": {"done": 30, "total": 120, "path": "/o/x.zarr"}})
    assert ex["title"] == "Exporting to Zarr" and ex["right"] == "25%"
    sc, = activity_rows({"stage": "converting", "scan": {"what": "reading", "done": 1, "total": 4}})
    assert sc["title"] == "Converting NetCDF" and sc["frac"] == 0.25
    assert activity_rows({"stage": "done"}) == []
    unknown, = activity_rows({"downloads": [{"request": 1, "bytes": 10, "size": None, "rate_bps": 0}]})
    assert unknown["frac"] is None  # size not announced: an indeterminate bar


def test_request_rows():
    reqs = [
        {"index": 0, "state": "done", "start": "2020-01-01", "end": "2020-01-31", "fields": 744,
         "submitted_at": "2026-11-13T10:00:00+00:00", "started": "2026-11-13T10:30:00",
         "finished": "2026-11-13T10:31:00", "size": 2_000_000},
        {"index": 1, "state": "queued", "status": "running", "start": "2020-02-01", "end": "2020-02-29",
         "fields": 696, "submitted_at": "2026-11-13T10:00:00+00:00", "started": "2026-11-13T10:40:00"},
        {"index": 2, "state": "queued", "status": "accepted", "start": "2020-03-01", "end": "2020-03-31",
         "fields": 744, "submitted_at": "2026-11-13T10:00:00+00:00"},
        {"index": 3, "state": "ready", "start": "2020-04-01", "end": "2020-04-30", "fields": 720, "size": 10},
        {"index": 4, "state": "planned", "start": "2020-05-01", "end": "2020-05-31", "fields": 744},
        {"index": 5, "state": "failed", "start": "2020-06-01", "end": "2020-06-30", "fields": 720,
         "error": "MARS returned no data", "attempts": 3},
    ]
    act = {"downloads": [{"request": 4, "bytes": 5, "size": 10}]}
    rows = request_rows(reqs, act)
    assert [r["state"] for r in rows] == ["processed", "running on the CDS", "in the CDS queue", "downloading 50%",
                                          "not submitted", "failed: MARS returned no data"]
    assert rows[0]["waited"] == "30m 0s" and rows[0]["ran"] == "1m 0s"
    assert rows[1]["waited"] == "40m 0s" and rows[4]["waited"] == "—"
    assert [r["tone"] for r in rows] == ["ok", "active", "wait", "active", "idle", "err"]
    assert rows[5]["tries"] == 3
    assert request_rows(reqs[:1], {}, files_only=True)[0]["state"] == "saved"
    busy = request_rows([dict(reqs[0], state="downloaded")], {"unit": {"requests": [1]}})
    assert busy[0]["state"] == "processing" and busy[0]["tone"] == "active"


def test_engine_reports_units_downloads_and_export(home, fast, monkeypatch):
    seen = []
    orig = cds_engine.CdsEngine.activity

    def record(self, force=False, **kw):
        seen.append(kw)
        return orig(self, force, **kw)

    monkeypatch.setattr(cds_engine.CdsEngine, "activity", record)
    fast.run_delay = 1.5  # long enough for the ticker to see a request on the CDS
    s = spec(home)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    units = [kw["unit"] for kw in seen if kw.get("unit")]
    assert units and units[0]["of"] == 1 and {"t2m", "tp"} <= {u["var"] for u in units}
    assert units[-1]["phase"] == "committing" and units[-1]["item"] == units[-1]["items"] == 2
    assert any(kw.get("requests") for kw in seen)  # the ticker ran
    assert any(kw.get("stage") == "exporting" for kw in seen)
    assert runner.status(job)["activity"] == {"stage": "done"}
