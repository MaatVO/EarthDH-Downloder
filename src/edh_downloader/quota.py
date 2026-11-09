"""Local request accounting per API key and calendar month (UTC).

Earth Data Hub does not expose the remaining quota to API-key clients, so the app counts every request it
makes. The user can *calibrate* the count with the "used" number shown on the EDH quota page, which also
accounts for requests made by other tools. Counts are shared by all jobs through a locked JSON file.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from .paths import data_dir


def month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def next_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc)


@contextmanager
def _locked():
    path = data_dir() / "usage.json"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read()
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        yield data
        f.seek(0)
        f.truncate()
        json.dump(data, f, indent=1)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def _entry(data: dict, fp: str, month: str) -> dict:
    months = data.get(fp)
    if not isinstance(months, dict):
        months = data[fp] = {}
    e = months.get(month)
    if not isinstance(e, dict):
        e = months[month] = {}
    e.setdefault("app", 0)
    e.setdefault("offset", 0)
    e.setdefault("calibrated_at", None)
    return e


def add(fp: str, n: int) -> int:
    """Record ``n`` requests for key fingerprint ``fp``; returns this month's total estimate."""
    month = month_key()
    with _locked() as data:
        e = _entry(data, fp, month)
        e["app"] += int(n)
        return e["app"] + e["offset"]


def used(fp: str) -> int:
    month = month_key()
    with _locked() as data:
        e = _entry(data, fp, month)
        return e["app"] + e["offset"]


def info(fp: str) -> dict:
    month = month_key()
    with _locked() as data:
        e = dict(_entry(data, fp, month))
    e["used"] = e["app"] + e["offset"]
    e["month"] = month
    e["resets"] = next_reset().isoformat()
    return e


def calibrate(fp: str, used_on_portal: int) -> None:
    """Align the local estimate with the number shown on the EDH quota page right now."""
    month = month_key()
    with _locked() as data:
        e = _entry(data, fp, month)
        e["offset"] = int(used_on_portal) - e["app"]
        e["calibrated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
