from datetime import datetime, timedelta, timezone

from edh_downloader.gui.app import (job_error_text, job_forecast, job_group, job_note, job_percent, job_resume_cost,
                                    job_rollup_html, jobs_stale_html, open_snippet, time_ago)


def test_known_failures_name_the_problem_and_the_way_out():
    disk = job_error_text("OSError: [Errno 28] No space left on device: '/data/edh/spool/job/u-000212'")
    assert disk == "Not enough free disk space at /data/edh/spool/job. Free some space, then Resume."
    assert "disk space" in job_error_text("Fatal: less than 10.0 GB free on disk")
    assert "API key" in job_error_text("Fatal: HTTP 401 for https://x/y: API key rejected")
    assert "Errno" not in job_error_text("cannot write /a/b: [Errno 13] Permission denied")


def test_unknown_failure_is_shown_not_guessed_at():
    assert job_error_text("Fatal: /x belongs to another job (j1); choose another name") == \
        "/x belongs to another job (j1); choose another name"
    assert job_error_text("KeyError: 'latitude'") == "KeyError: 'latitude'"


def test_note_for_interrupted_job_says_when_and_what_is_safe():
    note = job_note({"status": "interrupted", "_ago": "2d 4h", "message": "712/1320 units committed"}, 712, 1320)
    assert note == ("Stopped unexpectedly. Last activity 2d 4h ago. 712 of 1,320 units are committed and safe. "
                    "Resume continues from there.")


def test_note_with_nothing_committed_does_not_promise_continuing():
    note = job_note({"status": "paused"}, 0, 10)
    assert note == "No units were committed. Resume starts it from the beginning."


def test_note_does_not_repeat_resume_after_a_named_failure():
    note = job_note({"status": "failed", "error": "Fatal: less than 1 GB free on disk"}, 2, 4)
    assert note.count("Resume") == 1


def test_running_job_has_no_note():
    assert job_note({"status": "running"}, 3, 10) == ""


def test_jobs_needing_attention_come_first():
    order = sorted(["completed", "running", "interrupted", "waiting_quota", "paused", "failed"], key=job_group)
    assert order[:2] == ["interrupted", "failed"] and order[-1] == "completed"


def test_rollup_counts_and_pluralises():
    html = job_rollup_html([{"status": "failed"}, {"status": "interrupted"}, {"status": "running"}])
    assert "2 need attention" in html and "1 running" in html and "completed" not in html
    assert "1 needs attention" in job_rollup_html([{"status": "failed"}])


def test_unfinished_job_never_reads_100_percent():
    assert job_percent(9999, 10000) == "99.9%"
    assert job_percent(10000, 10000) == "100%"


def test_time_ago_tolerates_missing_or_naive_timestamps():
    assert time_ago(None) is None
    assert time_ago("2026-11-09T09:15:02") is None
    assert time_ago((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()) == "5m"


def test_open_snippet_for_each_format():
    assert open_snippet('/a/b "c".zarr') == 'import xarray as xr\n\nds = xr.open_zarr("/a/b \\"c\\".zarr")'
    assert 'icechunk.local_filesystem_storage("/a/b.icechunk")' in open_snippet("/a/b.icechunk")


def _running(**kw):
    return {"eta_s": 90000, "req_rate": 10.0, "requests_left": 1000, "month_used": 0, "month_limit": 100000, **kw}


def test_forecast_within_quota_is_plain_time_left():
    assert job_forecast(_running()) == ("about 1d 1h left", "")


def test_forecast_without_a_speed_says_nothing():
    assert job_forecast(_running(req_rate=None, eta_s=None)) == ("", "")


def test_forecast_says_when_quota_interrupts():
    tail, sentence = job_forecast(_running(requests_left=250000, month_used=50000))
    assert tail == "reaches quota limit in about 1h 23m"
    assert "then waits for the reset" in sentence and "about 2 monthly resets" in sentence


def test_forecast_when_job_pauses_at_the_limit():
    s = _running(requests_left=60000, month_used=50000, spec={"download": {"wait_for_quota": False}})
    tail, sentence = job_forecast(s)
    assert "then pauses until you resume it" in sentence and "about 1 monthly reset to finish" in sentence


def test_forecast_with_no_room_left():
    assert job_forecast(_running(requests_left=5, month_used=100000))[0] == "reaches quota limit soon"


def test_resume_cost_within_balance():
    text, short = job_resume_cost({"requests_total": 40200, "requests_job": 21700, "_quota": {"left": 450000}})
    assert not short and text == "Resuming needs about 18,500 more requests. 450,000 are left this month by this app's count."


def test_resume_cost_over_balance_warns():
    text, short = job_resume_cost({"requests_total": 100, "requests_job": 0, "_quota": {"left": 10}})
    assert short and "wait for the monthly reset" in text


def test_resume_cost_without_key_or_plan():
    assert job_resume_cost({"requests_total": 100, "requests_job": 40}) == ("Resuming needs about 60 more requests.", False)
    assert job_resume_cost({}) == ("", False)


def test_waiting_job_says_it_continues_by_itself():
    assert "continues by itself" in job_note({"status": "waiting_quota"}, 5, 10)


def test_stale_line_says_how_old():
    assert "Last update 4m ago" in jobs_stale_html("4m")
    assert "Last update" not in jobs_stale_html(None)
