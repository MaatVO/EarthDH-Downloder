"""Web UI (NiceGUI / Quasar Material). Served on localhost; reach it with ``ssh -L 8765:localhost:8765``.

The UI only builds job specs and controls jobs; downloads run in detached worker processes, so closing the
browser, stopping ``serve`` or dropping the SSH connection does not affect them.
"""

from datetime import date, datetime, timezone
import asyncio
import html
import json
import math
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from nicegui import app, run, ui
from starlette.middleware.base import BaseHTTPMiddleware

from .. import __version__, chunking
from ..backend import BackendError, Local
from ..runner import ACTIVE
from ..quota import next_reset
from ..job import COMPRESSORS, FREQS, STATS, AreaSpec, CdsSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec
from ..util import human_bytes, human_duration
from . import structure, tour
from .auth import TokenMiddleware
from .theme import PINK, apply_theme

STRUCT_KEYS = ("id", "status", "alive", "error", "outputs", "outputs_exist", "spec")
STATUS_COLOR = {"running": "positive", "starting": "positive", "exporting": "positive", "completed": "positive",
                "failed": "negative", "interrupted": "negative", "unreadable": "negative", "paused": "grey-6",
                "paused_quota": "warning", "waiting_quota": "warning", "cancelled": "grey-6", "created": "grey-6"}
# Jobs list order and summary line: (singular, plural, statuses), most urgent first
JOB_GROUPS = (("needs attention", "need attention", {"failed", "interrupted", "unreadable"}),
              ("running", "running", {"starting", "running", "exporting"}),
              ("waiting for quota", "waiting for quota", {"waiting_quota", "paused_quota"}),
              ("stopped", "stopped", {"paused", "created", "cancelled"}),
              ("completed", "completed", {"completed"}))
JOB_GROUP_STOPPED = 3  # where a status the groups don't know is counted
STATUS_ICON = {"starting": "hourglass_top", "running": "downloading", "exporting": "output", "completed": "check_circle",
               "failed": "error", "interrupted": "report", "unreadable": "help", "paused": "pause_circle",
               "paused_quota": "pause_circle", "waiting_quota": "schedule", "cancelled": "cancel",
               "created": "radio_button_unchecked"}


def job_group(status: str | None) -> int:
    return next((i for i, g in enumerate(JOB_GROUPS) if status in g[2]), JOB_GROUP_STOPPED)


def job_rollup_html(jobs: list[dict]) -> str:
    """The line above the jobs list. Built only from counts and the fixed labels above."""
    counts = [0] * len(JOB_GROUPS)
    for j in jobs:
        counts[job_group(j.get("status"))] += 1
    parts = []
    for i, (one, many, _) in enumerate(JOB_GROUPS):
        if counts[i]:
            text = f"{counts[i]} {one if counts[i] == 1 else many}"
            parts.append(f'<span class="text-negative text-weight-medium">{text}</span>' if i == 0 else text)
    return '<span class="edh-muted"> · </span>'.join(parts)


def job_percent(done: int, total: int) -> str:
    """Rounded down, so an unfinished job never reads 100%."""
    return "100%" if done >= total else f"{math.floor(1000 * done / total) / 10:.1f}%"


STATUS_LABEL = {"waiting_quota": "Waiting for quota", "paused_quota": "Paused at quota limit"}


def job_status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status.replace("_", " ").capitalize())


def time_ago(iso: str | None) -> str | None:
    """How long since a UTC timestamp ("4m", "2d 4h"); None if unknown. Coarse, so the card redraws rarely."""
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None
    if seconds < 60:
        return "under a minute"
    return f"{int(seconds // 60)}m" if seconds < 3600 else human_duration(seconds)


def job_error_text(error: str) -> str:
    """The problem and the way out for failures we can name; anything else is shown as it is, never guessed at.
    The raw text is always in the job's log."""
    low = error.lower()
    if "no space left" in low or "errno 28" in low or "gb free on disk" in low:
        m = re.search(r"'(/[^']+)'|cannot write (/\S+):", error)
        where = f" at {Path(m[1] or m[2]).parent}" if m else ""
        return f"Not enough free disk space{where}. Free some space, then Resume."
    if "api key rejected" in low:
        return "Earth Data Hub rejected the API key. Check the key in the Keys tab, then Resume."
    if "http 403" in low:
        return ("Earth Data Hub refused access, possibly because the quota for this key is used up. "
                "Check the quota page, then Resume.")
    if "http 404" in low:
        return "Earth Data Hub could not find part of this dataset (HTTP 404). The Log shows the address."
    if "giving up on" in low:
        return "Could not reach Earth Data Hub after repeated attempts. Check the network connection, then Resume."
    if "permission denied" in low or "errno 13" in low:
        return "No permission to write to the output or temporary folder. Fix its permissions, then Resume."
    return re.sub(r"^Fatal:\s*", "", error)


def is_cds(s: dict) -> bool:
    return (s.get("spec") or {}).get("source") == "cds"


def job_note(s: dict, done: int, total: int) -> str:
    """What happened to a stopped job and what to do next. Running jobs report through their own progress message."""
    st = s.get("status")
    if is_cds(s):
        what = "units are committed" if s.get("units_total") else "files are downloaded"
        committed = f"{done:,} of {total:,} {what} and safe." if done and total else ""
        if st == "failed":
            return f"{re.sub(r'^Fatal:\s*', '', s.get('error') or 'The job stopped because of an error. The Log has the details.')} {committed}"
        if st == "interrupted":
            when = f" Last activity {s['_ago']} ago." if s.get("_ago") else ""
            return f"Stopped unexpectedly.{when} {committed} Requests already on the CDS keep their place: Resume picks them up."
        if st == "paused":
            return f"{committed} Requests already on the CDS keep their place in the queue: Resume picks them up."
        if st == "cancelled":
            return f"{committed} Requests waiting on the CDS were withdrawn. Resume submits them again."
    committed = f"{done:,} of {total:,} units are committed and safe." if done and total else "No units were committed."
    resume = "Resume continues from there." if done else "Resume starts it from the beginning."
    if st == "interrupted":
        when = f" Last activity {s['_ago']} ago." if s.get("_ago") else ""
        return f"Stopped unexpectedly.{when} {committed} {resume}"
    if st == "failed":
        text = job_error_text(s.get("error") or "The job stopped because of an error. The Log has the details.")
        return f"{text} {committed}" + ("" if "Resume" in text else f" {resume}")
    if st in ("paused", "cancelled"):
        return f"{committed} {resume}"
    if st == "paused_quota":
        return f"Reached the monthly quota limit. Resume after the monthly reset. {committed}"
    if st == "waiting_quota":
        return "Reached the monthly quota limit. The job continues by itself after the reset."
    if st == "unreadable":
        return s.get("error") or ""
    return ""


def job_forecast(s: dict) -> tuple[str, str]:
    """A running job's (headline after the status, sentence under the bar), honest about the monthly quota."""
    if is_cds(s):  # queue waits cannot be forecast: the card shows them as they happen
        return "", s.get("message") or ""
    eta, rate = s.get("eta_s"), s.get("req_rate")
    if not eta or not rate:
        return "", ""
    left, used, limit = s.get("requests_left"), s.get("month_used"), s.get("month_limit")
    plain = f"about {human_duration(eta)} left"
    if left is None or used is None or not limit or left <= limit - used:
        return plain, ""
    room = max(0, limit - used)
    when = f"in about {human_duration(room / rate)}" if room / rate >= 60 else "soon"
    reset = next_reset()
    waits = ((s.get("spec") or {}).get("download") or {}).get("wait_for_quota", True)
    months = math.ceil((left - room) / limit)
    then = (f"then waits for the reset on {reset.day} {reset:%b} and continues" if waits else
            f"then pauses until you resume it after the reset on {reset.day} {reset:%b}")
    plural = "s" if months != 1 else ""
    return (f"reaches quota limit {when}",
            f"At the current speed this job reaches the monthly quota limit {when}, {then}. "
            f"It needs about {months} monthly reset{plural} to finish.")


def job_resume_cost(s: dict) -> tuple[str, bool]:
    """What resuming a stopped job will spend, and whether that is more than this month has left."""
    if is_cds(s):
        c = s.get("cds_requests") or {}
        left = sum(v for k, v in c.items() if k in ("planned", "submitting", "failed"))
        return (f"Resuming submits {left:,} more request{'s' if left != 1 else ''} to the CDS queue." if left else
                "", False)
    total = s.get("requests_total")
    if not total:
        return "", False
    left = max(0, total - (s.get("requests_job") or 0))
    room = (s.get("_quota") or {}).get("left")
    if room is None:
        return f"Resuming needs about {left:,} more requests.", False
    if left <= room:
        return f"Resuming needs about {left:,} more requests. {room:,} are left this month by this app's count.", False
    waits = ((s.get("spec") or {}).get("download") or {}).get("wait_for_quota", True)
    then = "The job will wait for the monthly reset." if waits else "The job will pause at the limit."
    return f"Resuming needs about {left:,} more requests, but only {room:,} are left this month by this app's count. {then}", True


def _seconds(a: str | None, b: str | None = None) -> float | None:
    """Seconds from ISO time ``a`` to ``b`` (now when None); None when unknown. Times without a zone are UTC."""
    if not a:
        return None
    try:
        t0 = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(b).replace("Z", "+00:00")) if b else datetime.now(timezone.utc)
    except ValueError:
        return None
    t0 = t0 if t0.tzinfo else t0.replace(tzinfo=timezone.utc)
    t1 = t1 if t1.tzinfo else t1.replace(tzinfo=timezone.utc)
    return max(0.0, (t1 - t0).total_seconds())


def activity_rows(a: dict) -> list[dict]:
    """What a running job is doing right now (``state["activity"]``), as rows for the job card: an icon, a
    title, a detail line, a fraction done (None: not known) and a right-hand label."""
    rows = []
    u = a.get("unit")
    if u:
        items, item = max(1, u.get("items") or 1), max(1, u.get("item") or 1)
        steps = max(1, u.get("steps") or 1)
        frac = min(1.0, ((item - 1) + (u.get("steps_done") or 0) / steps) / items)
        lev = u.get("level")
        what = (u.get("var") or "") + (f" at level {lev:g}" if isinstance(lev, (int, float)) else
                                       f" at level {lev}" if lev is not None else "")
        phase = {"reading": "reading and cutting", "writing": "aggregating and writing",
                 "committing": "committing to Icechunk"}.get(u.get("phase"), u.get("phase") or "")
        rows.append({"icon": "memory", "title": f"Processing unit {u['index']:,} of {u['of']:,}",
                     "detail": f"{str(u.get('first'))[:16]} → {str(u.get('last'))[:16]} · "
                               f"{what or 'starting'} ({item}/{items}) · {u.get('steps_done', 0):,}/{steps:,} "
                               f"source steps · {phase}",
                     "frac": 1.0 if u.get("phase") == "committing" else frac,
                     "right": "commit" if u.get("phase") == "committing" else f"{100 * frac:.0f}%"})
    for d in a.get("downloads") or []:
        size, got, rate = d.get("size"), d.get("bytes") or 0, d.get("rate_bps") or 0
        frac = min(1.0, got / size) if size else None
        left = f" · {human_duration((size - got) / rate)} left" if size and rate > 0 and got < size else ""
        rows.append({"icon": "download", "title": f"Downloading request {d['request']:,}",
                     "detail": f"{str(d.get('start'))[:10]} → {str(d.get('end'))[:10]} · {human_bytes(got)}"
                               + (f" of {human_bytes(size)}" if size else "") + f" · {human_bytes(rate)}/s{left}",
                     "frac": frac, "right": f"{100 * frac:.0f}%" if frac is not None else human_bytes(got)})
    sc = a.get("scan")
    if sc:
        total = sc.get("total") or 0
        frac = sc.get("done", 0) / total if total else None
        rows.append({"icon": "description", "title": "Converting NetCDF",
                     "detail": f"{sc.get('what', '')} · {sc.get('done', 0):,}/{total:,} files", "frac": frac,
                     "right": f"{100 * frac:.0f}%" if frac is not None else ""})
    ex = a.get("export")
    if ex and a.get("stage") == "exporting":
        total, done = ex.get("total"), ex.get("done") or 0
        frac = done / total if total else None
        took = _seconds(ex.get("since"))
        rows.append({"icon": "output", "title": "Exporting to Zarr",
                     "detail": (f"{done:,}/{total:,} objects" if total else "preparing") +
                               (f" · {human_duration(took)} so far" if took and took >= 60 else "") +
                               f" · {ex.get('path', '')}",
                     "frac": frac, "right": f"{100 * frac:.0f}%" if frac is not None else ""})
    return rows


REQ_STATE = {"planned": "not submitted", "submitting": "submitting", "ready": "ready to download",
             "downloaded": "downloaded, waiting to be processed", "failed": "failed"}


def request_rows(requests: list[dict], activity: dict, files_only: bool = False) -> list[dict]:
    """The ledger of a CDS job as table rows: where each request is, how long it waited and ran on the CDS,
    and how much of its file is downloaded."""
    downloading = {d["request"]: d for d in (activity or {}).get("downloads") or []}
    processing = set(((activity or {}).get("unit") or {}).get("requests") or [])
    rows = []
    for r in requests:
        n = r["index"] + 1
        st = r.get("state")
        if n in downloading:
            d = downloading[n]
            pct = f" {100 * d['bytes'] / d['size']:.0f}%" if d.get("size") else ""
            label, tone = f"downloading{pct}", "active"
        elif n in processing and st in ("downloaded", "done"):
            label, tone = "processing", "active"
        elif st == "queued":
            label, tone = ("running on the CDS", "active") if r.get("status") == "running" else ("in the CDS queue", "wait")
        elif st == "done":
            label, tone = ("saved" if files_only else "processed", "ok")
        elif st == "downloaded" and files_only:
            label, tone = "saved", "ok"
        else:
            label = REQ_STATE.get(st, st or "?")
            tone = "err" if st == "failed" else "ok" if st == "downloaded" else "idle"
        if st == "failed" and r.get("error"):
            label += f": {r['error'][:80]}"
        waited = _seconds(r.get("submitted_at"), r.get("started") or (None if st == "queued" else r.get("finished")))
        ran = _seconds(r.get("started"), r.get("finished")) if r.get("started") else None
        if st in ("planned", "submitting"):
            waited = ran = None
        rows.append({"n": n, "period": f"{str(r.get('start'))[:10]} → {str(r.get('end'))[:10]}",
                     "fields": f"{r.get('fields') or 0:,}", "state": label, "tone": tone,
                     "waited": human_duration(waited) if waited is not None else "—",
                     "ran": human_duration(ran) if ran is not None else "—",
                     "size": human_bytes(r.get("size") or r.get("bytes_est") or 0) if (r.get("size") or r.get("bytes_est")) else "—",
                     "tries": r.get("attempts") or 0})
    return rows


def jobs_stale_html(ago: str | None) -> str:
    """Shown in place of the summary line when the list can't be refreshed. ``ago`` comes from time_ago."""
    when = f" Last update {ago} ago." if ago else ""
    return f'<span class="text-warning">Cannot refresh the job list.{when} The numbers below may be out of date. Retrying.</span>'


def open_snippet(path: str) -> str:
    """Python that opens a finished output."""
    p = json.dumps(path, ensure_ascii=False)
    if path.endswith(".zarr"):
        return f"import xarray as xr\n\nds = xr.open_zarr({p})"
    return ("import icechunk\nimport xarray as xr\n\n"
            f"storage = icechunk.local_filesystem_storage({p})\n"
            "repo = icechunk.Repository.open(storage)\n"
            'ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False)')

SAMPLE_REQUESTS = 12  # requests of one "measure on a sample" (service.sample.DEFAULT_REQUESTS)
PRESETS = {
    "Europe": (-25.0, 34.0, 45.0, 72.0),
    "Italy": (6.5, 35.5, 18.6, 47.1),
    "Alps": (5.0, 43.5, 17.0, 48.5),
    "Trentino-Alto Adige": (10.3, 45.6, 12.6, 47.1),
}
OSM_TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"  # darkened with a CSS filter (theme.py)
# ------------------------------------------------------------------ helpers

DEFAULT_QUOTA = 500_000  # free DestinE access: requests per calendar month
QUOTA_URL = "https://earthdatahub.destine.eu/quota-api-keys"
CDS_KEY_URL = "https://cds.climate.copernicus.eu/profile"
CDS_DATASET_URL = "https://cds.climate.copernicus.eu/datasets/{}?tab=download"
TIER_TEXT = {  # what the app does with a CDS dataset, by tier (sources/cds/catalog.py)
    "grib": ("Full pipeline", "GRIB fields: area, statistics, Icechunk and Zarr"),
    "netcdf": ("Full pipeline", "NetCDF files: area, statistics, Icechunk and Zarr (kept as files if not a grid)"),
    "timeseries": ("Files · time series", "one file per point, as the CDS produces it"),
    "table": ("Not available", "in-situ observation tables cannot be made into a gridded store"),
}
FORMAT_LABELS = {"grib": "GRIB", "netcdf": "NetCDF", "netcdf4": "NetCDF4", "netcdf_legacy": "NetCDF",
                 "netcdf_zip": "NetCDF (zip)", "csv": "CSV", "zip": "ZIP", "tgz": "TGZ"}
VAR_COLUMNS = [
    {"name": "name", "label": "Name", "field": "name", "sortable": True, "align": "left"},
    {"name": "long_name", "label": "Description", "field": "long_name", "align": "left"},
    {"name": "units", "label": "Units", "field": "units", "align": "left"},
    {"name": "kind", "label": "Dimensions", "field": "kind", "align": "left"},
]
GRIDS = {"native": "Native", "0.25": "0.25°", "0.5": "0.5°", "1": "1°", "2.5": "2.5°", "custom": "Custom"}
EDH_SOURCE = {"id": "edh", "title": "Earth Data Hub", "short": "EDH", "key_url": QUOTA_URL, "has_quota": True,
              "check_cost": "1 request", "jobs": True,
              "key_hint": "`~/.netrc` (machine data.earthdatahub.destine.eu) is used automatically."}


def parse_points(text: str) -> list[Point]:
    pts = []
    for n, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace(";", ",").split(",")]
        if len(parts) == 2:
            parts = [f"p{n}"] + parts
        if len(parts) != 3:
            raise ValueError(f"line {n}: expected 'name, lat, lon'")
        pts.append(Point(parts[0], float(parts[1]), float(parts[2])))
    return pts


def parse_attrs(text: str) -> dict:
    out = {}
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"attributes line {n}: expected 'key = value'")
        k, v = (x.strip() for x in line.split("=", 1))
        if not k:
            raise ValueError(f"attributes line {n}: empty key")
        out[k] = v
    return out


@contextmanager
def section(n, title: str, subtitle: str = "", wide: bool = False, icon: str | None = None, mark: str = ""):
    """A numbered card; ``mark`` names it for the guided tour (class ``edh-tour-<mark>``)."""
    with ui.card().classes("edh-card w-full min-w-0" + (" col-span-2 max-xl:col-span-1" if wide else "")
                           + (f" edh-tour-{mark}" if mark else "")):
        with ui.row().classes("items-center gap-2 no-wrap w-full"):
            ui.html(f'<div class="edh-step">{n}</div>', sanitize=False)
            if icon:
                ui.icon(icon, size="20px").classes("edh-muted")
            with ui.column().classes("gap-0"):
                ui.label(title).classes("edh-title")
                if subtitle:
                    ui.label(subtitle).classes("edh-muted text-body2")
        with ui.column().classes("w-full gap-3 pt-1"):
            yield


class IconToggle(ui.toggle):
    """A segmented toggle whose segments carry an icon: ``options`` maps value -> (label, icon)."""

    def __init__(self, options: dict[str, tuple[str, str]], **kwargs):
        self._icons = [icon for _, icon in options.values()]
        super().__init__({value: label for value, (label, _) in options.items()}, **kwargs)

    def _update_options(self) -> None:
        super()._update_options()
        self._props["options"] = [{**o, "icon": i} for o, i in zip(self._props["options"], self._icons)]


class SearchSelect(ui.select, component="search_select.js"):
    """A select for long catalogues: typing filters its popup by every word, in any order, in an option's label or
    its search text (``set_search``), ignoring case, accents and punctuation; label matches are listed first."""

    def __init__(self, options, **kwargs):
        super().__init__(options, with_input=True, **kwargs)
        self._props["search"] = []
        with self.add_slot("prepend"):
            ui.icon("search", size="20px").classes("edh-muted")
        with self.add_slot("no-option"):
            ui.item("No dataset matches these words.").classes("edh-muted")

    def set_search(self, texts: list[str]) -> None:
        """Extra words to match per option, in the option order (like ``option_template``'s images)."""
        self._props["search"] = texts


def search_text(item: dict) -> str:
    """What a dataset is found by beyond its title and id: its CDS keywords, without their 'Facet: ' names."""
    return " ".join(k.split(": ", 1)[-1] for k in item.get("keywords") or [])


def dataset_image(url: str | None, alt: str, size: str):
    """A dataset's catalogue image in a tonal well, with a globe when it has none or it cannot be loaded."""
    with ui.element("div").classes(f"edh-thumb {size}"):
        if not url:
            ui.icon("public", size="20px")
            return
        img = ui.image(url).props("no-spinner no-transition fit=contain")
        img.props(f'alt="{alt.replace(chr(34), "")}"')
        with img.add_slot("error"):
            with ui.element("div").classes("absolute-full flex flex-center"):
                ui.icon("public", size="20px")


OPTION_SEPARATOR = "  ·  "  # dataset labels are "title  ·  id"; the option row shows them on two lines


def option_template(urls: list[str | None]) -> str:
    """Quasar option row with a catalogue image. NiceGUI numbers options, so ``urls`` follows the option order."""
    sources = html.escape(json.dumps(urls))
    sep = html.escape(json.dumps(OPTION_SEPARATOR))
    return f"""
      <q-item v-bind="props.itemProps">
        <q-item-section side>
          <div class="edh-thumb edh-thumb-sm">
            <template v-for="src in [{sources}[props.opt.value]]" :key="src">
              <img v-if="src" :src="src" alt="" loading="lazy" @error="$event.target.style.visibility = 'hidden'">
              <q-icon v-else name="public" size="20px" />
            </template>
          </div>
        </q-item-section>
        <q-item-section class="edh-option-text">
          <q-item-label>{{{{ props.opt.label.split({sep})[0] }}}}</q-item-label>
          <q-item-label v-if="props.opt.label.split({sep})[1]" caption class="edh-mono">
            {{{{ props.opt.label.split({sep})[1] }}}}
          </q-item-label>
        </q-item-section>
      </q-item>"""


def tip(text: str):
    """A small '?' icon that explains something on hover."""
    with ui.icon("help_outline", size="18px").classes("edh-tip cursor-help") as icon:
        ui.tooltip(text).classes("edh-tooltip")
    return icon


def with_tip(field, text: str):
    """Attach a '?' explanation right after a form field."""
    with field.add_slot("after"):
        tip(text)
    return field


def kpi(label: str, value: str, sub: str = "", cls: str = ""):
    with ui.column().classes("edh-kpi gap-0"):
        ui.label(label).classes("edh-kpi-label")
        ui.label(value).classes(f"edh-kpi-value {cls}")
        if sub:
            ui.label(sub).classes("edh-muted text-caption")


def kv(k, v, cls=""):
    with ui.row().classes("w-full justify-between no-wrap gap-3"):
        ui.label(k).classes("edh-muted")
        ui.label(v).classes(f"text-right {cls}")


# ------------------------------------------------------------------ dataset structure (shared)

MAX_VARS = 6  # data variables listed in a panel; the rest are in the "See all" pop-up
LEGEND = ('<div class="st-legend"><span class="st-sw st-sw-chunk"></span>one chunk'
          '<span class="st-sw st-sw-grid"></span>chunks<span class="st-sw st-sw-shard"></span>shards</div>')


def structure_badges(d: dict):
    ui.label(" · ".join((_n(len(d["data"]), "variable"), f"{human_bytes(d['raw'])} raw",
                         f"{'' if d.get('measured') else '≈ '}{human_bytes(d['disk'])} on disk",
                         _n(d["files"], "file")))).classes("edh-muted text-caption")


def structure_panel(d: dict, state: dict, max_vars: int | None = MAX_VARS, see_all=None, sticky=False):
    """Chunk-grid drawing of one variable + the store tree. ``state['var']`` keeps the drawn variable."""
    names = [v["name"] for v in d["data"]]
    by = {v["name"]: v for v in d["data"]}
    if not names:
        ui.label("No data variables.").classes("edh-muted")
        return
    cur = state.get("var") if state.get("var") in by else names[0]
    state["var"] = cur
    cut = max_vars is not None and len(names) > max_vars
    with ui.row().classes("w-full gap-6 no-wrap max-lg:flex-wrap items-start"):
        with ui.column().classes("gap-2 w-[400px] max-lg:w-full shrink-0" + (" sticky top-0" if sticky else "")):
            holder = {}

            def pick(e):
                if e.value in by:
                    state["var"] = e.value
                    holder["cube"].content = structure.cube_svg(by[e.value])

            if 1 < len(names) <= MAX_VARS:
                ui.toggle(names, value=cur, on_change=pick).props(
                    "dense no-caps unelevated toggle-color=primary").classes("edh-toggle")
            elif len(names) > MAX_VARS:
                ui.select(names, value=cur, with_input=True, label="Drawn variable", on_change=pick).props(
                    "dense outlined options-dense").classes("w-64")
            holder["cube"] = ui.html(structure.cube_svg(by[cur]), sanitize=False).classes("w-full")
            ui.html(LEGEND, sanitize=False)
        with ui.column().classes("grow min-w-0 gap-1"):
            ui.html(structure.tree_html(d, max_vars if cut else None), sanitize=False).classes("w-full")
            if cut and see_all:
                ui.button(f"See all {len(names):,} variables", icon="unfold_more", on_click=see_all).props(
                    "flat no-caps dense color=secondary")


def structure_dialog(host, d: dict, title: str):
    """The full structure, every variable, in a scrollable pop-up."""
    with host, ui.dialog() as dlg, ui.card().classes("edh-card edh-struct gap-3").style(
            "width:min(1200px,96vw);max-width:96vw"):
        with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
            with ui.column().classes("gap-1 min-w-0"):
                ui.label(title).classes("edh-title edh-mono")
                with ui.row().classes("items-center gap-2"):
                    structure_badges(d)
            ui.button(icon="close", on_click=dlg.close).props('flat round aria-label="Close"')
        with ui.scroll_area().classes("w-full h-[72vh]"):
            structure_panel(d, {}, max_vars=None, sticky=True)
    dlg.on("hide", lambda: dlg.delete())
    dlg.open()


# ------------------------------------------------------------------ page

def build_page(be, local: bool = False, embedded: bool = False):
    """The whole UI for one backend ``be`` (this computer or a server over SSH): it never touches keys, datasets,
    jobs or files itself, only asks ``be`` (see service.py). ``embedded``: shown in a tab of the connect page,
    whose top bar has the Tour button."""
    apply_theme()
    # a failure in any handler must not break the page: say so and carry on
    ui.on_exception(lambda e: ui.notify(f"Something went wrong: {type(e).__name__}: {e}", type="negative"))
    f = {  # form model
        "collection": "era5", "dataset": "era5-single-levels-atmosphere", "variables": [], "levels": [],
        "start": "2020-01-01", "end": "2020-01-31", "start_hour": 0, "end_hour": 23, "freq": "none", "hours": 6, "tz_offset": 0, "stats": {},
        "mode": "bbox", "west": 10.3, "south": 45.6, "east": 12.6, "north": 47.1, "points": "",
        "lon_convention": "-180..180",
        "directory": be.info["default_dir"], "name": "era5_subset", "out_format": "icechunk",
        "target_mode": "create", "target_store": "", "extend_direction": "forward",
        "lossless": True, "keepbits": 13,
        "chunk_layout": None,  # None = follow the recommendation
        "chunk_time": None, "chunk_lat": None, "chunk_lon": None, "chunk_points": None,
        "dtype": "float32", "compressor": "blosc-zstd", "clevel": 5, "shuffle": "shuffle", "sharding": False,
        "shard_auto": True, "shard_time": 1, "shard_space": 4, "extra_attrs": "",
        "key": None, "connections": 8, "spool_dir": "", "max_spool_gb": 20.0, "min_free_gb": 10.0,
        "quota_safety": 0.9, "wait_for_quota": True, "memory_gb": 16.0,
        # Climate Data Store
        "source": "edh", "cds_options": {}, "grid": "native", "grid_custom": 0.5, "max_inflight": 4,
        "pack": "auto", "max_request_gb": 10.0,
    }
    ctx = {"keys": {"default": None, "keys": []}, "plan_seq": 0, "ds": None, "info": None, "plan": None, "plan_task": None, "rec": ("auto", ""),
           "thumbs": {}, "thumbs_asked": set(),  # thumbs: collection -> dataset id -> image URL
           # per source: the collection and dataset last chosen, and the key
           "sel": {"edh": ("era5", "era5-single-levels-atmosphere"), "cds": ("Reanalysis", "reanalysis-era5-single-levels")},
           "keysel": {}, "cds_desc": None, "cds_thumbs": {},
           "src_gen": 0, "items_tok": 0, "opts_seq": 0}  # tokens: drop answers that a newer request has overtaken

    # ================================================================ header
    with ui.header().classes("edh-header items-center justify-between px-3 sm:px-6 py-2"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.html('<div class="edh-logo"><span class="material-icons">public</span></div>', sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label("EDH Downloader").classes("edh-brand").props("role=heading aria-level=1")
                ui.label(f"Earth Data Hub and Climate Data Store → Icechunk / Zarr · v{__version__}").classes("edh-muted text-caption max-sm:hidden")
        with ui.tabs().props("no-caps dense indicator-color=transparent active-class=edh-tab-active"
                             ).classes("edh-tabs edh-tour-tabs") as tabs:
            t_new = ui.tab("New job", icon="add_circle_outline").classes("edh-tour-tab-new")
            t_jobs = ui.tab("Jobs", icon="dns").classes("edh-tour-tab-jobs")
            t_keys = ui.tab("Keys", icon="vpn_key").classes("edh-tour-tab-keys")
        with ui.row().classes("items-center gap-3 no-wrap max-sm:w-full"):
            if not embedded:
                ui.button("Tour", icon="school", on_click=lambda: run_tour()).props(
                    "flat dense no-caps size=sm color=grey-5").tooltip("a guided tour of the app")
            with ui.column().classes("gap-1 items-end min-w-0 max-sm:grow edh-tour-quota"):
                with ui.row().classes("items-center gap-1 no-wrap"):
                    quota_label = ui.label().classes("text-caption")
                    quota_label.on("click", lambda: tabs.set_value(t_keys) if not f.get("key") else None)
                    with ui.link(target=QUOTA_URL, new_tab=True).classes("edh-quota-link").props(
                            'aria-label="Open the Earth Data Hub quota page"'):
                        ui.icon("open_in_new", size="14px")
                        ui.tooltip("Your real usage is on the Earth Data Hub quota page: the app only counts its "
                                   "own requests. Enter the number there in Keys → Calibrate.").classes("edh-tooltip")
                quota_bar = ui.linear_progress(value=0, show_value=False).props(
                    'size=6px aria-label="Monthly quota used"').classes("edh-grad-bar w-48 max-w-full")
            key_select = ui.select({}, label="API key").props("dense outlined options-dense").classes("w-40")

    def later(fn, delay=0.01):
        ui.timer(delay, fn, once=True)

    missing_key_banner = None

    async def call(method, **kw):
        """Ask the backend; a failure becomes a notification and ``None``."""
        try:
            return await be.call(method, **kw)
        except BackendError as e:
            ui.notify(str(e), type="negative")
            return None

    async def refresh_keys_select():
        d = await call("keys_list")
        if d is None:
            return
        ctx["keys"] = d
        src = f["source"]
        key_select.options = {k["name"]: k["name"] for k in d["keys"] if k.get("provider", "edh") == src}
        if f["key"] not in key_select.options:
            defaults = d.get("defaults") or {"edh": d["default"]}
            f["key"] = ctx["keysel"].get(src) if ctx["keysel"].get(src) in key_select.options else defaults.get(src)
        key_select.value = f["key"]
        key_select.update()
        await refresh_quota()

    async def refresh_quota():
        cds = f["source"] == "cds"
        try:
            k = await be.call("key_status", name=f["key"], **({"provider": "cds"} if cds else {}))
        except BackendError:
            k = None
        quota_bar.set_visibility(not cds)
        if missing_key_banner:
            banner_title.text = ("Climate Data Store key required" if cds else "Earth Data Hub API Key Required")
            banner_text.text = ("A Climate Data Store personal access token is needed to check licences and costs, "
                                "and to download." if cds else "An API key is required to inspect dataset variables, "
                                "calculate request quotas, and download data.")
        if not k:
            quota_label.text = "⚠️ no API key: add one in Keys"
            quota_label.classes(replace="text-caption text-amber-400 font-medium cursor-pointer")
            quota_bar.value = 0
            if missing_key_banner:
                missing_key_banner.set_visibility(True)
            return
        quota_label.classes(replace="text-caption")
        if missing_key_banner:
            missing_key_banner.set_visibility(False)
        if cds:
            quota_label.text = "Climate Data Store · no monthly quota: requests wait in a queue"
            return
        q = k["usage"]
        pct = q["used"] / k["quota"] if k["quota"] else 0
        quota_label.text = f"quota {q['used']:,} / {k['quota']:,} · resets {q['resets'][:10]}"
        quota_bar.value = min(1.0, pct)

    key_select.on_value_change(lambda e: (f.update(key=e.value), ctx["keysel"].__setitem__(f["source"], e.value),
                                          later(refresh_quota), schedule_plan()))

    dialog_host = ui.element("div")  # pop-ups live here, outside the cards that are rebuilt on refresh

    with ui.tab_panels(tabs, value=t_new).classes("edh-panels w-full"):
        # ================================================================ NEW JOB
        with ui.tab_panel(t_new).classes("px-0"):
            missing_key_banner = ui.card().classes(
                "w-full bg-amber-950/30 border border-amber-500/60 p-4 mb-5 rounded-xl shadow-lg"
            )
            with missing_key_banner:
                with ui.row().classes("w-full items-center justify-between gap-4 no-wrap max-md:flex-wrap"):
                    with ui.row().classes("items-center gap-3 no-wrap"):
                        ui.icon("vpn_key", color="warning", size="36px")
                        with ui.column().classes("gap-0"):
                            banner_title = ui.label("Earth Data Hub API Key Required").classes(
                                "text-body1 text-weight-bold text-amber-300")
                            banner_text = ui.label(
                                "An API key is required to inspect dataset variables, calculate request quotas, and download data."
                            ).classes("text-caption text-amber-100/80")
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.button("Get API Key ↗", on_click=lambda: ui.navigate.to(
                            CDS_KEY_URL if f["source"] == "cds" else QUOTA_URL, new_tab=True)).props(
                            "outline no-caps color=warning size=sm"
                        )
                        ui.button("Configure in Keys", icon="vpn_key", on_click=lambda: tabs.set_value(t_keys)).props(
                            "unelevated no-caps color=warning size=sm"
                        ).classes("text-black font-semibold")
            missing_key_banner.set_visibility(False)

            with ui.row().classes("w-full no-wrap items-start gap-5 max-lg:flex-wrap"):
                with ui.element("div").classes("grid grid-cols-2 max-xl:grid-cols-1 gap-5 grow min-w-0 content-start"):
                    with ui.column().classes("gap-5 min-w-0 w-full"):
                        # ---------- dataset
                        with section(1, "Dataset", icon="dataset", mark="dataset"):
                            src_toggle = IconToggle({"edh": ("Earth Data Hub", "hub"),
                                                     "cds": ("Climate Data Store", "cloud_queue")}, value="edh").props(
                                "unelevated toggle-color=primary no-caps").classes("edh-toggle edh-tour-source")
                            with ui.row().classes("w-full items-center gap-3 no-wrap max-sm:flex-wrap"):
                                coll_select = ui.select({"era5": "ERA5"}, value="era5", label="Collection").classes("w-48")
                                ds_select = SearchSelect({}, label="Dataset").classes(
                                    "grow min-w-0 max-sm:order-last max-sm:basis-full")
                                ui.button(icon="refresh", on_click=lambda: load_dataset(refresh=True)).props(
                                    "flat round").tooltip("reload the dataset description from its source")
                            with ui.row().classes("w-full items-start gap-4 no-wrap max-sm:flex-wrap"):
                                ds_image = ui.element("div")
                                with ui.column().classes("gap-1 grow min-w-0"):
                                    ds_title = ui.label().classes("edh-subtitle")
                                    ds_chips = ui.row().classes("gap-2")
                            ds_info = ui.markdown().classes("text-body2 edh-muted")
                            cds_box = ui.column().classes("w-full gap-3")  # CDS: tier, licences, dataset options
                            cds_box.set_visibility(False)

                        # ---------- variables
                        with section(2, "Variables and levels", icon="layers", mark="variables"):
                            with ui.row().classes("w-full items-center justify-between"):
                                var_search = ui.input(placeholder="Search variables…").props("dense outlined clearable").classes("w-80")
                                with var_search.add_slot("prepend"):
                                    ui.icon("search")
                                sel_label = ui.label().classes("edh-muted text-caption")
                            sel_chips = ui.row().classes("gap-2 items-center w-full edh-sel-box").style("position:sticky;top:64px;z-index:5;padding:8px 12px")
                            var_table = ui.table(rows=[], columns=list(VAR_COLUMNS), row_key="name", selection="multiple", pagination=8).props("flat dense").classes("edh-table w-full")
                            var_search.bind_value(var_table, "filter")
                            with ui.row().classes("items-center gap-2") as level_row:
                                level_select = ui.select([], multiple=True, label="Pressure levels (hPa)").props(
                                    "use-chips dense outlined").classes("min-w-[28rem]")
                                ui.button("All", on_click=lambda: level_select.set_value(list(level_select.options))).props("flat dense")
                                ui.button("None", on_click=lambda: level_select.set_value([])).props("flat dense")
                            level_row.set_visibility(False)

                    with ui.column().classes("gap-5 min-w-0 w-full"):
                        # ---------- time
                        with section(3, "Time", icon="date_range", mark="time"):
                            hour_inputs = []
                            with ui.row().classes("items-center gap-4"):
                                for key_, label, hkey in (("start", "First day", "start_hour"), ("end", "Last day", "end_hour")):
                                    with ui.input(label).bind_value(f, key_).classes("w-44") as inp:
                                        with ui.menu().props("no-parent-event") as menu:
                                            ui.date().bind_value(inp)
                                        with inp.add_slot("append"):
                                            ui.icon("event").on("click", menu.open).classes("cursor-pointer")
                                    hour_inputs.append(with_tip(
                                        ui.number("from hour" if hkey == "start_hour" else "to hour (incl.)",
                                                  min=0, max=23, step=1, precision=0).bind_value(f, hkey).classes("w-32"),
                                        "Hour of day (0-23, local time with the UTC offset below). First day starts "
                                        "at this hour; last day ends at the end of this hour."))
                                    if key_ == "end":
                                        ui.button("Today", on_click=lambda: f.update(end=date.today().isoformat())).props(
                                            "flat dense no-caps").tooltip("set the last day to today")
                                time_range_label = ui.label().classes("edh-muted text-caption")
                            with ui.row().classes("items-center gap-4"):
                                freq_select = with_tip(ui.select(FREQS, label="Time step").bind_value(f, "freq").classes("w-48"),
                                         "As stored keeps every hour. Other steps aggregate hours into periods using the "
                                         "statistics chosen below.")
                                ui.number("Hours per step", min=1, max=24 * 31, step=1).bind_value(f, "hours").classes(
                                    "w-40").bind_visibility_from(f, "freq", lambda v: v == "hours")
                                with ui.row().classes("items-center gap-1 no-wrap") as tz_row:
                                    with_tip(ui.number("UTC offset (h)", min=-12, max=14, step=1).bind_value(
                                        f, "tz_offset").classes("w-36"),
                                        "Shifts where days/months start. 0 = UTC days; 1 = days from 00:00 CET.")
                                tz_row.bind_visibility_from(f, "freq", lambda v: v != "none")
                            with ui.row().classes("items-center gap-1") as stats_head:
                                ui.label("Statistics per variable").classes("text-body2")
                                tip("Each statistic is computed over every period and saved as its own array, named "
                                    "variable_statistic (t2m_mean, t2m_max). With one statistic the name is unchanged. "
                                    "Defaults: sum for accumulated fields such as tp, mean otherwise.")
                            stats_head.bind_visibility_from(f, "freq", lambda v: v != "none")
                            stats_box = ui.column().classes("gap-2")

                        # ---------- area
                        with section(4, "Area", icon="map", mark="area"):
                            with ui.row().classes("items-center gap-5"):
                                area_toggle = IconToggle({"global": ("Global", "public"), "bbox": ("Box", "crop_free"),
                                                          "points": ("Points", "place")}).bind_value(
                                    f, "mode").props("unelevated toggle-color=primary no-caps").classes("edh-toggle")
                                with_tip(ui.select({"-180..180": "−180 … 180", "0..360": "0 … 360"},
                                                   label="Output longitudes").bind_value(f, "lon_convention").classes("w-48"),
                                         "How longitudes are numbered in the output. −180…180: west is negative, map "
                                         "centred on Greenwich (usual for Europe/Africa). 0…360: ERA5's native order, "
                                         "centred on the Pacific; use it for boxes crossing the 180° meridian so "
                                         "longitudes stay increasing.")
                            # wraps: when the card is too narrow for both, the map goes full width under the inputs
                            with ui.row().classes("w-full items-stretch gap-5"):
                                with ui.column().classes("gap-3 w-60 shrink-0") as bbox_box:
                                    with ui.grid(columns=2).classes("gap-3 w-full"):
                                        ui.number("North", min=-90, max=90, format="%.2f").bind_value(f, "north")
                                        ui.number("South", min=-90, max=90, format="%.2f").bind_value(f, "south")
                                        ui.number("West", min=-180, max=360, format="%.2f").bind_value(f, "west")
                                        ui.number("East", min=-180, max=360, format="%.2f").bind_value(f, "east")
                                    with ui.row().classes("gap-2"):
                                        for pname, b in PRESETS.items():
                                            ui.button(pname, on_click=lambda b=b: set_bbox(*b)).props("flat dense no-caps color=primary")
                                    ui.label("Or draw a rectangle on the map.").classes("edh-muted text-caption")
                                bbox_box.bind_visibility_from(f, "mode", lambda v: v == "bbox")
                                with ui.column().classes("gap-2 w-60 shrink-0") as pts_box:
                                    ui.textarea("name, lat, lon (one per line)",
                                                placeholder="trento, 46.07, 11.12\nbolzano, 46.50, 11.35").bind_value(
                                        f, "points").props("rows=9").classes("w-full")
                                    ui.label("Nearest grid cell is used.").classes("edh-muted text-caption")
                                pts_box.bind_visibility_from(f, "mode", lambda v: v == "points")
                                draw = {"draw": {"rectangle": {"shapeOptions": {"color": PINK}}, "polyline": False,
                                                 "polygon": False, "circle": False, "marker": False, "circlemarker": False},
                                        "edit": {"edit": False, "remove": False}}
                                with ui.element("div").classes("relative grow basis-72 h-72 min-w-0"):
                                    area_map = ui.leaflet(center=(46.0, 11.0), zoom=4, draw_control=draw,
                                                          hide_drawn_items=True).classes("edh-map w-full h-full")
                                    ui.button("Center", icon="center_focus_strong", on_click=lambda: center_map()).props(
                                        "dense unelevated no-caps size=sm").classes(
                                        "edh-map-btn absolute bottom-3 right-3 z-[1000]").tooltip(
                                        "zoom to the selected area")
                                area_map.clear_layers()
                                area_map.tile_layer(url_template=OSM_TILES, options={
                                    "attribution": "&copy; OpenStreetMap contributors", "maxZoom": 19})
                            with ui.row().classes("items-center gap-3") as grid_row:
                                with_tip(ui.select(GRIDS, label="Grid").bind_value(f, "grid").classes("w-36"),
                                         "Native keeps the dataset's own grid (ERA5: 0.25°, ERA5-Land: 0.1°). A coarser "
                                         "grid is interpolated by the Climate Data Store before download: smaller "
                                         "files, the same number of fields.")
                                ui.number("Spacing (°)", min=0.05, step=0.05).bind_value(f, "grid_custom").classes(
                                    "w-36").bind_visibility_from(f, "grid", lambda v: v == "custom")
                            grid_row.set_visibility(False)
                            area_note = ui.label().classes("text-caption text-warning")
                            area_note.set_visibility(False)

                    # ---------- output
                    with section(5, "Output", wide=True, icon="folder", mark="output"):
                        # what goes in and what comes out: input format -> staging -> output (render_flow)
                        flow_box = ui.row().classes("w-full items-stretch gap-2 no-wrap max-md:flex-wrap edh-flow")
                        files_note = ui.label().classes("text-body2 text-accent")
                        files_note.set_visibility(False)
                        with ui.row().classes("w-full items-center justify-between pb-2 mb-2 border-b border-white/10") as mode_row:
                            with ui.row().classes("items-center gap-2"):
                                ui.label("Store mode").classes("text-body2 text-weight-medium")
                                tip("Create a brand-new dataset or extend an existing Icechunk dataset forward or backward in time.")
                            mode_toggle = IconToggle({"create": ("New store", "create_new_folder"),
                                                      "extend": ("Extend existing Icechunk", "more_time")}).bind_value(
                                f, "target_mode").props("no-caps unelevated toggle-color=primary").classes("edh-toggle")

                        with ui.row().classes("w-full items-center gap-4 no-wrap max-md:flex-wrap") as new_store_row:
                            ui.input("Directory (on the server)").bind_value(f, "directory").classes("grow")
                            ui.input("Name").bind_value(f, "name").classes("w-64")
                            fmt_select = with_tip(ui.select({"zarr": "Zarr", "both": "Zarr + Icechunk", "icechunk": "Icechunk only"},
                                               label="Format").bind_value(f, "out_format").classes("w-48"),
                                     "Downloads are written to an Icechunk repository first: it makes jobs "
                                     "resumable. Zarr: export a plain Zarr v3 at the end and delete the repository. "
                                     "Zarr + Icechunk: keep both (the repository has the commit history). Icechunk "
                                     "only: skip the export.")

                        with ui.column().classes("w-full gap-3") as extend_store_box:
                            with ui.row().classes("w-full items-center gap-3 no-wrap max-md:flex-wrap"):
                                ui.input("Existing store path (.icechunk or companion .zarr)").bind_value(
                                    f, "target_store").classes("grow")
                                ui.button("Inspect store", icon="search", on_click=lambda: later(inspect_target)).props("outline no-caps")
                            with ui.row().classes("w-full items-center gap-4"):
                                ui.label("Extension direction:").classes("text-caption text-weight-medium")
                                ui.radio({"forward": "Forward (After / Future)", "backward": "Backward (Before / Past)"}).bind_value(
                                    f, "extend_direction").props("inline")
                            extend_msg = ui.markdown().classes("text-caption edh-muted")
                            extend_alert = ui.card().classes("w-full bg-red-950/40 border border-red-500/50 p-3 rounded text-red-200 text-caption")
                            extend_alert.set_visibility(False)

                        fmt_select.on_value_change(lambda _: render_flow())
                        mode_toggle.on_value_change(lambda _: render_flow())
                        new_store_row.bind_visibility_from(f, "target_mode", lambda v: v == "create")
                        extend_store_box.bind_visibility_from(f, "target_mode", lambda v: v == "extend")
                        out_paths = ui.label().classes("edh-mono text-caption edh-muted")

                        # -- dataset structure: drawing + tree, updated live
                        with ui.column().classes("w-full gap-3 edh-struct") as struct_box:
                            with ui.row().classes("w-full items-center justify-between gap-3"):
                                ui.label("Dataset structure").classes("edh-subtitle")
                                struct_kpis = ui.row().classes("items-center gap-2")
                            struct_empty = ui.label("Select a dataset and variables.").classes("edh-muted")
                            struct_notes = ui.column().classes("w-full gap-1")
                            with ui.column().classes("w-full gap-3") as struct_body:
                                struct_view = ui.column().classes("w-full")

                                # -- layout picker
                                with ui.row().classes("w-full items-center gap-3 pt-2") as layout_row:
                                    ui.label("Chunk layout").classes("text-body2 text-weight-medium")
                                    layout_toggle = ui.toggle({}, on_change=lambda e: pick_layout(e.value)).props(
                                        "no-caps unelevated toggle-color=primary").classes("edh-toggle")
                                    rec_label = ui.label().classes("text-caption text-accent")
                                metrics = ui.row().classes("w-full gap-2")
                                with ui.row().classes("items-center gap-4") as custom_row:
                                    ui.number("Time steps", min=1).bind_value(f, "chunk_time").classes("w-36")
                                    ui.number("Latitudes", min=1).bind_value(f, "chunk_lat").classes("w-36")
                                    ui.number("Longitudes", min=1).bind_value(f, "chunk_lon").classes("w-36")
                                    ui.number("Points", min=1).bind_value(f, "chunk_points").classes("w-36")
                                custom_row.bind_visibility_from(f, "chunk_layout", lambda v: v == "custom")
                                with ui.expansion("Compare layouts").classes("w-full edh-expansion") as cmp_exp:
                                    compare = ui.column().classes("w-full")
                                layout_row.bind_visibility_from(f, "out_format", lambda v: v != "icechunk")
                                cmp_exp.bind_visibility_from(f, "out_format", lambda v: v != "icechunk")
                            struct_body.set_visibility(False)

                        # -- zarr encoding
                        with ui.column().classes("w-full gap-3") as enc_box:
                            ui.label("Zarr encoding").classes("edh-subtitle pt-3")
                            with ui.row().classes("w-full items-center gap-4"):
                                with ui.row().classes("items-center gap-1 no-wrap"):
                                    ui.switch("Lossless").bind_value(f, "lossless")
                                    tip("Store values exactly. Off: round to fewer mantissa bits for smaller files; "
                                        "13 bits is the precision ERA5 already has on Earth Data Hub.")
                                ui.number("Mantissa bits", min=1, max=23, step=1).bind_value(f, "keepbits").classes(
                                    "w-36").bind_visibility_from(f, "lossless", lambda v: not v)
                                with_tip(ui.select({"float32": "float32", "float64": "float64"}, label="Data type")
                                         .bind_value(f, "dtype").classes("w-36"),
                                         "float32 is ERA5's native precision. float64 doubles the size and only adds "
                                         "digits to computed statistics; bit rounding does not apply to it.")
                                with_tip(ui.select({k: k.replace("blosc-", "Blosc ").replace("zstd", "Zstd")
                                                    .replace("lz4hc", "LZ4HC").replace("lz4", "LZ4").replace("zlib", "zlib")
                                                    .replace("gzip", "gzip").replace("none", "none")
                                                    for k in COMPRESSORS}, label="Compressor")
                                         .bind_value(f, "compressor").classes("w-44"),
                                         "Blosc Zstd: good ratio and fast (default). Blosc LZ4: fastest, larger files. "
                                         "LZ4HC: slow to write, fast to read. zlib/gzip: compact and widely readable "
                                         "but slow. Zstd without Blosc compresses floats less well. none: no "
                                         "compression.")
                                with_tip(ui.number("Level", min=0, max=22, step=1).bind_value(f, "clevel").classes("w-28")
                                         .bind_visibility_from(f, "compressor", lambda v: v != "none"),
                                         "Higher = smaller files, slower writing. Blosc and gzip go up to 9, Zstd to "
                                         "22. 3–5 is a good balance.")
                                with_tip(ui.select({"shuffle": "byte", "bitshuffle": "bit", "noshuffle": "off"},
                                                   label="Shuffle").bind_value(f, "shuffle").classes("w-32")
                                         .bind_visibility_from(f, "compressor", lambda v: str(v).startswith("blosc")),
                                         "Reorders bytes (or bits) before compressing so similar ones sit together. "
                                         "Byte shuffle is best for most float data; bit shuffle can help for "
                                         "bit-rounded data.")
                            with ui.row().classes("w-full items-center gap-4"):
                                with ui.row().classes("items-center gap-1 no-wrap"):
                                    ui.switch("Sharding").bind_value(f, "sharding")
                                    tip("Stores many chunks inside one file (a shard). Fewer files on disk, same "
                                        "read granularity. Useful above ~20,000 chunk files. Needs Zarr v3 readers "
                                        "(zarr-python ≥ 3, xarray ≥ 2024.10).")
                                with ui.row().classes("items-center gap-1 no-wrap") as shard_auto_row:
                                    ui.switch("Auto size").bind_value(f, "shard_auto")
                                    tip("Picks chunks per shard so each shard is about 512 MB.")
                                shard_auto_row.bind_visibility_from(f, "sharding")
                                with ui.row().classes("items-center gap-3") as shard_custom:
                                    ui.number("Chunks/shard: time", min=1, step=1).bind_value(f, "shard_time").classes("w-40")
                                    ui.number("Chunks/shard: lat, lon", min=1, step=1).bind_value(f, "shard_space").classes("w-44")
                                shard_custom.bind_visibility_from(f, "sharding", lambda v: v and not f["shard_auto"])
                                shard_hint = ui.label().classes("edh-muted text-caption")
                            with_tip(ui.textarea("Extra global attributes", placeholder="project = MY-PROJECT\n"
                                                 "contact = name@example.org").bind_value(f, "extra_attrs")
                                     .props("rows=2").classes("w-full"),
                                     "One 'key = value' per line, added to the dataset's global attributes.")
                        enc_box.bind_visibility_from(f, "out_format", lambda v: v != "icechunk" and not ctx.get("files_only"))

                # ---------- summary (sticky)
                with ui.card().classes("edh-card edh-summary edh-tour-summary w-[26rem] max-lg:w-full sticky shrink-0 "
                                       "edh-scroll").style(
                        "top:80px;max-height:calc(100vh - 100px);overflow-y:auto"):
                    ui.label("Summary").classes("edh-title")
                    summary = ui.column().classes("gap-3 w-full")
                    with ui.row().classes("w-full items-center gap-3 no-wrap") as conn_row:
                        with_tip(ui.number("Connections", min=1, max=64, step=1).bind_value(
                            f, "connections").classes("w-32"),
                            "Chunks downloaded at the same time. Faster up to what your line and the server "
                            "allow; the sample suggests a value. Can be changed while the job runs.")
                        conn_hint = ui.column().classes("gap-0 grow")
                    with ui.row().classes("w-full items-center gap-2 no-wrap edh-tour-sample") as sample_row:
                        sample_btn = ui.button(f"Measure on a sample · {SAMPLE_REQUESTS} requests", icon="science").props(
                            "outline no-caps color=secondary").classes("grow")
                        tip(f"Uses quota: downloads {SAMPLE_REQUESTS} chunks of this selection ({SAMPLE_REQUESTS} requests) to "
                            "measure the real download size, your line speed and the Zarr size with your encoding. "
                            "Then suggests the number of connections.")
                    sample_status = ui.label().classes("edh-muted text-caption")
                    with ui.expansion("Download settings", icon="tune").classes("edh-expansion w-full edh-tour-settings"):
                        with ui.column().classes("w-full gap-3 p-2"):
                            with_tip(ui.input("Temporary directory", placeholder="job folder").bind_value(
                                f, "spool_dir").classes("w-full"),
                                "Downloaded chunks wait here, still compressed, until they are processed and "
                                "deleted. Empty = inside the job folder (~/.local/share/edh-dl/jobs). Pick a disk "
                                "with free space.")
                            with ui.row().classes("w-full gap-3 no-wrap"):
                                with_tip(ui.number("Max temporary (GB)", min=1).bind_value(f, "max_spool_gb").classes("grow"),
                                         "Downloads pause when the temporary directory holds more than this, until "
                                         "processing catches up (an ERA5 chunk is ~3.5 MB).")
                                with_tip(ui.number("Min free disk (GB)", min=0).bind_value(f, "min_free_gb").classes("grow"),
                                         "Downloads pause when free space on the temporary or output disk drops below "
                                         "this; the job stops with an error rather than fill the disk.")
                            with_tip(ui.number("Memory (GB)", min=0.5, step=0.5).bind_value(f, "memory_gb").classes("w-full"),
                                     "RAM the processing and export may use at once. Lower it on a busy shared server.")
                            with ui.column().classes("w-full gap-3") as cds_settings:
                                with_tip(ui.number("Requests waiting on the CDS", min=1, max=32, step=1).bind_value(
                                    f, "max_inflight").classes("w-full"),
                                    "How many requests may wait in the Climate Data Store queue at once. When the "
                                    "CDS limits you, the job lowers this by itself and raises it again later. "
                                    "Can be changed while the job runs.")
                                with ui.row().classes("items-center gap-1 no-wrap"):
                                    ui.toggle({"auto": "Fewest requests", "month": "One month each"}).bind_value(
                                        f, "pack").props("dense no-caps unelevated toggle-color=primary").classes("edh-toggle")
                                    tip("Fewest requests: whole months packed into one request up to the dataset's "
                                        "cost limit; each request waits in the queue once, so this is usually much "
                                        "faster. One month each: ECMWF's general advice for archived data.")
                                with_tip(ui.number("Largest request (GB)", min=0.5, step=0.5).bind_value(
                                    f, "max_request_gb").classes("w-full"),
                                    "No request downloads more than this. Smaller requests mean more of them.")
                            cds_settings.set_visibility(False)
                            with ui.row().classes("items-center gap-3 w-full no-wrap") as quota_row:
                                ui.label("Quota limit").classes("text-body2")
                                ui.slider(min=0.1, max=1.0, step=0.05).bind_value(f, "quota_safety").props(
                                    "label color=secondary").classes("grow")
                                ui.label().bind_text_from(f, "quota_safety", lambda v: f"{100 * (v or 0):.0f}%")
                                tip("The job stops at this share of your monthly request quota.")
                            with ui.row().classes("items-center gap-1 no-wrap") as wait_row:
                                ui.switch("Wait for quota reset").bind_value(f, "wait_for_quota")
                                tip("At the limit: wait for the reset on the 1st and continue. Off: pause the job.")
                    with ui.column().classes("w-full gap-3 edh-tour-launch"):
                        launch_btn = ui.button("Launch job", icon="rocket_launch").props(
                            "unelevated no-caps size=lg").classes("edh-grad-btn w-full")
                        ui.button("Download job.json", icon="file_download", on_click=lambda: save_job_file()).props(
                            "outline no-caps color=white").classes("w-full")
                    ui.label("Runs in the background: you can close this page.").classes("edh-muted text-caption")

        # ================================================================ JOBS
        with ui.tab_panel(t_jobs).classes("px-0 edh-tour-jobs"):
            ui.label("Jobs").classes("sr-only").props("role=heading aria-level=2")
            jobs_rollup = ui.html("", sanitize=False).classes("text-body2 pb-3")
            jobs_announce = ui.label("").classes("sr-only").props("role=status aria-live=polite")
            jobs_box = ui.column().classes("w-full gap-4")

        # ================================================================ KEYS
        with ui.tab_panel(t_keys).classes("px-0 edh-tour-keys"):
            keys_box = ui.column().classes("w-full gap-4 max-w-5xl mx-auto")

    # ------------------------------------------------------------------ dataset loading
    def src_kw() -> dict:
        """Backend arguments that name the source (none for Earth Data Hub: older servers know only that one)."""
        return {"source": "cds"} if f["source"] == "cds" else {}

    async def load_collections():
        gen, cds = ctx["src_gen"], f["source"] == "cds"
        try:
            cols = await be.call("collections", **src_kw())
            if gen != ctx["src_gen"]:  # the source was switched meanwhile
                return
            coll_select.props(f'label="{"Category" if cds else "Collection"}"')
            coll_select.add_slot("option", option_template([c.get("thumbnail") for c in cols]))
            fallback = "Reanalysis" if cds else "era5"
            coll_select.set_options({c["id"]: c["title"] + (f" · {c['count']}" if cds else "") for c in cols},
                                    value=f["collection"] if f["collection"] in {c["id"] for c in cols} else fallback)
        except Exception as e:
            if gen == ctx["src_gen"]:
                ui.notify(f"could not read the catalogue: {e}", type="negative")
        if gen == ctx["src_gen"]:
            await load_items()

    async def load_items():
        ctx["items_tok"] += 1
        tok, gen = ctx["items_tok"], ctx["src_gen"]

        def stale():
            return (tok, gen) != (ctx["items_tok"], ctx["src_gen"])  # a newer request, or another source, meanwhile
        try:
            items = await be.call("datasets", collection=f["collection"], **src_kw())
        except Exception as e:
            if not stale():
                ui.notify(f"could not list datasets: {e}", type="negative")
            return
        if stale():
            return
        ctx["items"] = {i["id"]: i for i in items}
        if f["source"] == "cds":
            ctx["cds_thumbs"].update({i["id"]: i.get("thumbnail") for i in items})
        else:
            ctx["thumbs"][f["collection"]] = {i["id"]: i.get("thumbnail") for i in items}
        offered = [i for i in items if not i.get("deprecated")]
        ds_select.add_slot("option", option_template([i.get("thumbnail") for i in offered]))
        ds_select.set_search([search_text(i) for i in offered])
        ds_select.options = {i["id"]: f"{i['title']}{OPTION_SEPARATOR}{i['id']}" for i in offered}
        if f["dataset"] not in ds_select.options and ds_select.options:
            f["dataset"] = next(iter(ds_select.options))
        ds_select.value = f["dataset"]
        ds_select.update()
        await load_dataset()

    def show_dataset(info):
        """The chosen dataset's image and name, which come from the catalogue and need no API key."""
        ds_image.clear()
        with ds_image:
            dataset_image(info.get("thumbnail"), f"Preview of {info.get('title', info['id'])}", "edh-thumb-lg")
        ds_title.text = info.get("title", info["id"])
        ds_chips.clear()

    async def load_dataset(refresh=False):
        info = ctx.get("items", {}).get(f["dataset"])
        if not info:
            return
        show_dataset(info)
        if f["source"] == "cds":
            await load_cds_dataset(info, refresh)
            return
        if not f["key"]:
            ds_info.content = (
                "**Earth Data Hub API key required**: configure an API key in the **Keys** tab "
                "to inspect dataset variables and calculate download plans."
            )
            return
        ds_info.content = "_loading dataset structure…_"
        try:
            ds = await be.call("dataset_load", info=info, key=f["key"], refresh=refresh)
        except BackendError as e:
            if f["dataset"] == info["id"] and f["source"] == "edh":
                ds_info.content = f"**Could not load dataset:** {e}"
            return
        if f["dataset"] != info["id"] or f["source"] != "edh":  # another dataset or source was chosen meanwhile
            return
        ds["vars"] = {v["name"]: v for v in ds["variables"]}
        ctx["ds"], ctx["info"] = ds, info
        s = ds["summary"]
        t = s["time"] or ["—", "—"]
        ds_info.content = info.get("abstract", "")[:420]
        ds_chips.clear()  # again: two loads can overlap across the await above
        with ds_chips:
            parts = [f"{t[0][:10]} to {t[1][:10]}", f"step {s['time_step']}", f"grid {s['grid'][0]}×{s['grid'][1]}",
                     f"{s['variables']} variables", info.get("size", "?")]
            if s["levels"]:
                parts.append(f"{len(s['levels'])} levels")
            ui.label(" · ".join(parts)).classes("edh-muted text-caption")
        time_range_label.text = f"available {t[0][:10]} → {t[1][:10]}"
        rows = [{**v, "kind": {"time": "time × lat × lon", "level": "time × level × lat × lon",
                               "static": "lat × lon (static)"}.get(v["kind"], "unsupported")}
                for v in ds["variables"]]
        var_table.rows = rows
        var_table.selected = [r for r in rows if r["name"] in f["variables"]]
        var_table.update()
        if ds["levels"] is not None:
            level_select.options = ds["levels"]
            level_select.value = [l for l in f["levels"] if l in level_select.options] or []
            level_select.update()
        level_row.set_visibility(ds["levels"] is not None)
        on_vars_changed()

    async def load_cds_dataset(info, refresh=False):
        """A CDS dataset from its download form (public); with a key, also its licence status (free)."""
        ds_info.content = "_reading the dataset's download form…_"
        try:
            d = await be.call("cds_describe", dataset=info["id"], key=f["key"], refresh=refresh)
        except BackendError as e:
            if f["dataset"] == info["id"] and f["source"] == "cds":
                ds_info.content = f"**Could not read the dataset:** {e}"
            return
        if d["id"] != f["dataset"] or f["source"] != "cds":  # another dataset or source was chosen meanwhile
            return
        ctx["cds_desc"] = d
        lv = d["levels"]
        kind = "time × level × space" if lv else "time × space"
        ds = {"summary": {"time": [info.get("start") or "", info.get("end") or ""], "levels": lv["values"] if lv else None},
              "variables": [{"name": v["name"], "long_name": v["label"], "units": "", "kind": "level" if lv else "time",
                             "default_stat": None} for v in d["variables"]],
              "levels": lv["values"] if lv else None}
        ds["vars"] = {v["name"]: v for v in ds["variables"]}
        ctx["ds"], ctx["info"] = ds, info
        ds_info.content = info.get("abstract", "")[:420]
        t0, t1 = (info.get("start") or "")[:10], (info.get("end") or "")[:10]
        ds_chips.clear()
        with ds_chips:
            parts = [f"{t0} to {t1}" if t0 else "", f"{len(d['variables'])} variables",
                     f"{len(lv['values'])} levels" if lv else "",
                     "area cut by the CDS" if d.get("area") else "whole domain delivered"]
            ui.label(" · ".join(p for p in parts if p)).classes("edh-muted text-caption")
        time_range_label.text = f"available {t0} → {t1}" if t0 else ""
        rows = [{**v, "kind": kind} for v in ds["variables"]]
        var_table.rows = rows
        var_table.selected = [r for r in rows if r["name"] in f["variables"]]
        var_table.update()
        if lv:
            level_select.props(f'label="{"Pressure levels (hPa)" if lv["field"] == "pressure_level" else lv["field"].replace("_", " ").capitalize()}"')
            level_select.options = lv["values"]
            level_select.value = [l for l in f["levels"] if l in lv["values"]]
            level_select.update()
        level_row.set_visibility(bool(lv))
        render_cds_box(d)
        apply_source_ui()
        on_vars_changed()

    def render_cds_box(d):
        cds_box.clear()
        with cds_box:
            title, sub = TIER_TEXT.get(d["tier"], ("", ""))
            with ui.row().classes("items-center gap-2"):
                ui.badge(title).props("color=primary" if d["tier"] in ("grib", "netcdf") else "color=grey-7")
                ui.label(sub).classes("edh-muted text-caption")
            labels = ", ".join(l["label"] for l in d["licences"])
            missing = d.get("licences_missing")
            url = CDS_DATASET_URL.format(d["id"])
            if missing:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("gpp_maybe", color="warning")
                    ui.markdown(f"Accept the licence first: **{', '.join(l['label'] for l in missing)}**. Open the "
                                f"[dataset page]({url}), accept it at the bottom of the Download tab, then reload "
                                "the dataset here.").classes("text-caption text-warning")
            elif missing is None and labels:
                ui.label(f"Licence: {labels} (checked when a CDS key is set)").classes("edh-muted text-caption")
            elif labels:
                ui.label(f"Licence accepted: {labels}").classes("edh-muted text-caption")
            ctx["cds_opts_box"] = ui.column().classes("w-full gap-2")
            ctx["cds_opt_sel"] = {}
            for n in d["notes"][:2]:
                with ui.expansion("Notes from the CDS about this dataset", icon="info").classes("w-full edh-expansion"):
                    ui.markdown(re.sub(r"<[^>]+>", " ", n)[:3000]).classes("text-caption")

    def _option_values(o) -> tuple[dict, object]:
        """A dataset option's choices (with the chosen values even when they no longer fit the rest, marked) and
        the value to show: the choice, else the form's default, else the only possible value."""
        cur = f["cds_options"].get(o["name"])
        if cur in (None, [], ""):
            cur = o["default"] or (o["values"][:1] if len(o["values"]) == 1 else [])
        cur = cur if isinstance(cur, list) else [cur]
        opts = {v: o["labels"].get(v, v) for v in o["values"]}
        for c in cur:
            if c not in opts:
                opts[c] = f"{o['labels'].get(c, c)} (not with this selection)"
        return opts, (cur if o["multiple"] else (cur[0] if cur else None))

    async def refresh_cds_options():
        """Show only the dataset options that apply to the chosen variables, levels and options, each with the
        values that exist together with them, as the CDS download form does (a lead time only for forecasts, a
        soil layer only for soil fields, the models that ran the chosen experiment)."""
        d, box = ctx.get("cds_desc"), ctx.get("cds_opts_box")
        if f["source"] != "cds" or not d or box is None:
            return
        ctx["opts_seq"] += 1
        seq = ctx["opts_seq"]
        sel = {"variable": list(f["variables"]), **f["cds_options"]}
        if d["levels"] and f["levels"]:
            sel[d["levels"]["field"]] = [str(x) for x in f["levels"]]
        try:
            st = await be.call("cds_options", dataset=d["id"], selection=sel)
        except Exception:  # noqa: BLE001  (an older server: every option, every value)
            st = {"options": d["options"], "dropped": []}
        if seq != ctx["opts_seq"] or ctx.get("cds_desc") is not d:  # a newer request, or another dataset, meanwhile
            return
        for n in st["dropped"]:  # chosen for another kind of request: not sent
            f["cds_options"].pop(n, None)
        opts = st["options"]
        filled = False
        for o in opts:  # one possible value and nothing chosen: that value (the CDS still wants it named)
            if f["cds_options"].get(o["name"]) in (None, [], "") and len(o["values"]) == 1 and not o["default"]:
                f["cds_options"][o["name"]] = o["values"] if o["multiple"] else o["values"][0]
                filled = True
        sels = ctx["cds_opt_sel"]
        if [o["name"] for o in opts] == list(sels):  # same options: update their choices in place
            for o in opts:
                choices, value = _option_values(o)
                if sels[o["name"]].options != choices:
                    sels[o["name"]].set_options(choices, value=value)
            if st["dropped"] or filled:
                schedule_plan()
            if filled:  # the filled value can narrow the others
                later(refresh_cds_options)
            return
        box.clear()
        sels.clear()
        with box:
            if not opts:
                return
            with ui.row().classes("items-center gap-1"):
                ui.label("Dataset options").classes("text-body2 text-weight-medium")
                tip("Only the options that apply to the selected variables and to the options chosen so far, "
                    "each with the values that exist for them.")
            with ui.row().classes("w-full gap-3"):
                for o in opts:
                    choices, value = _option_values(o)

                    def changed(e, n=o["name"]):
                        f["cds_options"][n] = e.value
                        later(refresh_cds_options)
                        schedule_plan()

                    sel_ = ui.select(choices, label=o["label"], multiple=o["multiple"], with_input=len(choices) > 12,
                                     value=value, on_change=changed).props(
                        "dense outlined" + (" use-chips" if o["multiple"] else "")).classes("min-w-[14rem]")
                    if o.get("help"):
                        with_tip(sel_, re.sub(r"<[^>]+>", " ", o["help"])[:500])
                    sels[o["name"]] = sel_
        if st["dropped"] or filled:
            schedule_plan()
        if filled:
            later(refresh_cds_options)

    def render_flow():
        """Section 5's first line: the input format, then what the job makes of it (the Icechunk staging
        repository, the Zarr export), or the files kept as delivered."""
        from ..sources.cds.planner import input_format

        cds = f["source"] == "cds"
        d = ctx.get("cds_desc") if cds else None
        fmt_out = f["out_format"]
        steps: list[tuple[str, str, str]] = []
        if cds and d is None:
            steps.append(("Input format", "—", "choose a dataset"))
        elif cds:
            fmt = input_format(d)
            label = FORMAT_LABELS.get(fmt, fmt.upper())
            sub = {"grib": "decoded field by field", "netcdf": "zips unpacked"}.get(
                d["tier"], "as the CDS produces it")
            steps.append(("Input format", label, sub))
        else:
            steps.append(("Input format", "Zarr", "Earth Data Hub chunks"))
        if cds and d is not None and d["tier"] not in ("grib", "netcdf"):
            ok = d["tier"] != "table"
            steps.append(("Output", "Files" if ok else "Not available",
                          "as delivered, with manifest.json" if ok else "tables are not a gridded store"))
        elif not (cds and d is None):
            extend = not cds and f["target_mode"] == "extend"
            if extend:
                steps.append(("Store", "Icechunk", "the existing one, extended"))
            else:
                steps.append(("Staging" if fmt_out == "zarr" else "Store", "Icechunk",
                              "resumable; removed after export" if fmt_out == "zarr" else
                              "kept, with its commit history"))
            if fmt_out != "icechunk":
                steps.append(("Output", "Zarr v3", "written at the end"))
        flow_box.clear()
        with flow_box:
            for i, (lab, val, sub) in enumerate(steps):
                if i:
                    ui.icon("arrow_forward", size="18px").classes("edh-muted self-center max-md:hidden")
                with ui.column().classes("edh-kpi gap-0 min-w-0 edh-flow-step"):
                    ui.label(lab).classes("edh-kpi-label")
                    ui.label(val).classes("edh-flow-value")
                    ui.label(sub).classes("edh-muted text-caption")
            if cds and d is not None and d["tier"] == "netcdf":
                ui.label("Files that are not a grid one store can hold (tables, several products for one time) "
                         "are kept as delivered instead.").classes("edh-muted text-caption self-center grow "
                                                                   "min-w-0 edh-flow-note")

    def apply_source_ui():
        """Show what applies to the chosen source (and, for the CDS, to the chosen dataset)."""
        cds = f["source"] == "cds"
        d = ctx.get("cds_desc") if cds else None
        tier = d["tier"] if d else None
        files = bool(cds and tier and tier not in ("grib", "netcdf"))
        ctx["files_only"] = files
        cds_box.set_visibility(cds and d is not None)
        grid_row.set_visibility(bool(cds and tier == "grib" and d.get("area")))
        note = ""
        if d and tier == "grib" and not d.get("area"):
            note = ("This dataset is delivered for its whole domain: the area is cut after the download, so the "
                    "download size does not depend on it.")
        elif d and tier == "timeseries":
            note = "This dataset gives one time series per point: choose Points."
        area_note.text = note
        area_note.set_visibility(bool(note))
        files_note.text = (f"Saved as the files the Climate Data Store delivers, in {f['directory']}/{f['name']}.files "
                           "with a manifest.json listing each request.") if files else ""
        files_note.set_visibility(files)
        render_flow()
        fmt_select.set_visibility(not files)
        struct_box.set_visibility(not files)
        mode_row.set_visibility(not cds)
        # time: monthly and yearly products have no hours, and are not aggregated to finer steps
        tkind = d["time"]["kind"] if d else "ymd"
        for h in hour_inputs:
            h.set_visibility(tkind == "ymd")
        freqs = (FREQS if tkind == "ymd" else {k: v for k, v in FREQS.items() if k in ("none", "year")}
                 if tkind == "ym" else {"none": FREQS["none"]})
        if f["freq"] not in freqs:
            f["freq"] = "none"
        freq_select.set_options(freqs, value=f["freq"])
        # area: a dataset delivered for its own domain has no "global"
        whole = bool(d and not d.get("area"))
        area_toggle.set_options({"global": "Whole domain" if whole else "Global", "bbox": "Box", "points": "Points"},
                                value=f["mode"])
        # variables: the CDS form gives no units, and every variable has the same dimensions
        var_table.columns = [c for c in VAR_COLUMNS if not (cds and c["name"] == "units")]
        var_table.update()
        if cds:
            f["target_mode"] = "create"
        for el in (conn_row, sample_row, quota_row, wait_row):
            el.set_visibility(not cds)
        cds_settings.set_visibility(cds)

    async def switch_source(e):
        v = e.value
        if not v or v == f["source"]:
            return
        ctx["sel"][f["source"]] = (f["collection"], f["dataset"])
        f["source"] = v
        f["collection"], f["dataset"] = ctx["sel"][v]
        f.update(variables=[], levels=[], stats={}, cds_options={}, chunk_layout=None)
        # what came from the previous source's dataset goes too; answers still on their way are dropped
        ctx["src_gen"] += 1
        ctx["plan_seq"] += 1
        ctx.update(ds=None, info=None, cds_desc=None, plan=None, rec=None, items={}, cds_opts_box=None, cds_opt_sel={})
        launch_btn.disable()
        var_table.rows, var_table.selected = [], []
        var_table.update()
        level_row.set_visibility(False)
        ds_chips.clear()
        apply_source_ui()
        await refresh_keys_select()
        await load_collections()

    src_toggle.on_value_change(switch_source)

    async def on_collection(e):
        if e.value and e.value != f["collection"]:
            f["collection"] = e.value
            await load_items()

    async def on_dataset(e):
        if e.value and (e.value != f["dataset"] or not ctx["info"] or ctx["info"]["id"] != e.value):
            if e.value != f["dataset"]:
                f["cds_options"] = {}
            f["dataset"] = e.value
            await load_dataset()

    coll_select.on_value_change(on_collection)
    ds_select.on_value_change(on_dataset)

    # ------------------------------------------------------------------ variables / stats
    def on_vars_changed():
        f["variables"] = [r["name"] for r in var_table.selected]
        sel_label.text = f"{len(f['variables'])} selected" if f["variables"] else "none selected"
        sel_chips.clear()
        with sel_chips:
            ui.label("Selected").classes("text-weight-medium")
            ui.label(", ".join(f["variables"]) if f["variables"] else "none, tick variables below").classes(
                "edh-mono" if f["variables"] else "edh-muted")
        render_stats()
        if f["source"] == "cds":
            later(refresh_cds_options)
        schedule_plan()

    var_table.on("selection", lambda e: on_vars_changed())
    level_select.on_value_change(lambda e: (f.update(levels=e.value or []), schedule_plan(),
                                            later(refresh_cds_options) if f["source"] == "cds" else None))

    def render_stats():
        stats_box.clear()
        ds = ctx["ds"]
        if not ds or f["freq"] == "none":
            return
        with stats_box:
            for name in f["variables"]:
                v = ds["vars"].get(name)
                if v is None or v["kind"] == "static":
                    continue
                cds = f["source"] == "cds"
                cur = f["stats"].get(name) or ([] if cds else [v["default_stat"]])
                with ui.row().classes("items-center gap-3"):
                    ui.label(name).classes("w-56 edh-mono" if cds else "w-20 edh-mono")
                    out = ui.label().classes("edh-muted text-caption edh-mono")

                    def show(vals, n=name, out=out, cds=cds):
                        vals = vals or []
                        if cds:
                            out.text = ("Default: sum for accumulations, mean otherwise" if not vals else
                                        "Output named after the GRIB field" + ("" if len(vals) == 1 else
                                        ", " + ", ".join(f"…_{x}" for x in vals)))
                            return
                        out.text = "Output: " + (n if len(vals) == 1 else ", ".join(f"{n}_{x}" for x in vals))

                    def changed(e, n=name, show=show):
                        f["stats"][n] = e.value
                        show(e.value)
                        schedule_plan()

                    ui.select(list(STATS), value=cur, multiple=True, on_change=changed).props(
                        "dense outlined use-chips").classes("min-w-[18rem]")
                    show(cur)

    # ------------------------------------------------------------------ area map
    layers: list = []

    def set_bbox(w, s, e, n):
        f.update(west=w, south=s, east=e, north=n, mode="bbox")
        draw_rect()
        schedule_plan()

    def draw_rect():
        for layer in layers:
            area_map.remove_layer(layer)
        layers.clear()
        style = {"color": PINK, "weight": 2, "fillColor": PINK, "fillOpacity": 0.12}
        if f["mode"] == "global":
            layers.append(area_map.generic_layer(name="rectangle", args=[[[-90, -180], [90, 180]], style]))
        elif f["mode"] == "bbox":
            try:
                w, s, e, n = (float(f[k]) for k in ("west", "south", "east", "north"))
            except (TypeError, ValueError):
                return
            if e < w:
                e += 360
            layers.append(area_map.generic_layer(name="rectangle", args=[[[s, w], [n, e]], style]))
        elif f["mode"] == "points":
            try:
                pts = parse_points(f["points"] or "")
            except ValueError:
                return
            for p in pts[:500]:
                layers.append(area_map.generic_layer(name="circleMarker", args=[[p.lat, p.lon], {
                    "radius": 6, "color": PINK, "fillColor": PINK, "fillOpacity": 0.8, "weight": 2}]))
        center_map()

    def center_map():
        """Fit the map to the selection: the rectangle, the points, or the whole globe."""
        if f["mode"] == "global":
            area_map.run_map_method("fitBounds", [[-75, -180], [80, 180]], {"padding": [10, 10]})
        elif f["mode"] == "bbox":
            try:
                w, s, e, n = (float(f[k]) for k in ("west", "south", "east", "north"))
            except (TypeError, ValueError):
                return
            if e < w:
                e += 360
            area_map.run_map_method("fitBounds", [[s, w], [n, e]], {"padding": [30, 30], "maxZoom": 8})
        elif f["mode"] == "points":
            try:
                pts = parse_points(f["points"] or "")
            except ValueError:
                return
            if pts:
                lats, lons = [p.lat for p in pts], [p.lon for p in pts]
                area_map.run_map_method("fitBounds", [[min(lats), min(lons)], [max(lats), max(lons)]],
                                        {"padding": [40, 40], "maxZoom": 7})

    def on_draw(e):
        layer = e.args.get("layer", {})
        ll = layer.get("_latlngs")
        if not ll:
            return
        pts = ll[0] if isinstance(ll[0], list) else ll
        lats = [p["lat"] for p in pts]
        lngs = [p["lng"] for p in pts]
        w, ea = min(lngs), max(lngs)
        w = (w + 180) % 360 - 180
        ea = (ea + 180) % 360 - 180 if ea - min(lngs) < 360 else w + 360
        set_bbox(round(w, 2), round(max(-90, min(lats)), 2), round(ea, 2), round(min(90, max(lats)), 2))

    area_map.on("draw:created", on_draw)

    # ------------------------------------------------------------------ spec + plan
    async def inspect_target():
        p = (f.get("target_store") or "").strip()
        extend_alert.set_visibility(False)
        extend_msg.content = ""
        if not p:
            extend_msg.content = "_Enter a path to an existing .icechunk store (or companion .zarr)._"
            return
        res = await call("store_inspect", path=p)
        if not res or not res.get("ok"):
            err = res.get("error", "could not inspect store") if res else "error inspecting store"
            extend_alert.clear()
            with extend_alert:
                ui.label(err).classes("text-caption")
            extend_alert.set_visibility(True)
            return
        job = res.get("job") or {}
        t_start = res["time_start"]
        t_end = res["time_end"]
        n_steps = res["time_len"]
        step_s = res.get("step_seconds", 3600)
        extend_msg.content = (
            f"**Existing Icechunk dataset found**: `{res['icechunk_path']}`\n\n"
            f"• **Time coverage**: `{t_start[:16]}` to `{t_end[:16]}` ({n_steps:,} steps)\n\n"
            f"• **Variables**: {', '.join(res.get('variables', []))}"
        )
        if job.get("dataset"):
            f["dataset"] = job["dataset"]
            if job["dataset"] in ds_select.options:
                ds_select.value = job["dataset"]
        if job.get("variables"):
            f["variables"] = list(job["variables"])
        if job.get("levels"):
            f["levels"] = list(job["levels"])
        t_spec = job.get("time") or {}
        if t_spec.get("freq"):
            f["freq"] = t_spec["freq"]
        if t_spec.get("hours"):
            f["hours"] = t_spec["hours"]
        if t_spec.get("tz_offset") is not None:
            f["tz_offset"] = t_spec["tz_offset"]
        a_spec = job.get("area") or {}
        if a_spec.get("mode"):
            f["mode"] = a_spec["mode"]
            for k_ in ("west", "south", "east", "north"):
                if k_ in a_spec:
                    f[k_] = a_spec[k_]

        import pandas as pd
        if f.get("extend_direction") == "forward":
            next_start = pd.to_datetime(t_end) + pd.Timedelta(seconds=step_s)
            f["start"] = next_start.strftime("%Y-%m-%d")
            f["start_hour"] = next_start.hour
        elif f.get("extend_direction") == "backward":
            prev_end = pd.to_datetime(t_start) - pd.Timedelta(seconds=step_s)
            f["end"] = prev_end.strftime("%Y-%m-%d")
            f["end_hour"] = prev_end.hour

        later(schedule_plan)

    def rec_of() -> tuple:
        """The recommended chunk layout and why; ("auto", "") until a plan has one."""
        return ctx["rec"] or ("auto", "")

    def layout_choice() -> str:
        if f["out_format"] == "icechunk":
            return "auto"
        return f["chunk_layout"] or rec_of()[0]

    def build_spec() -> JobSpec:
        info = ctx.get("info") or {}
        pts = parse_points(f["points"] or "") if f["mode"] == "points" else []
        custom = layout_choice() == "custom"
        cds = f["source"] == "cds"
        grid = None
        if cds and f["grid"] != "native":
            g = float(f["grid_custom"] or 0.5) if f["grid"] == "custom" else float(f["grid"])
            grid = [g, g]
        # the CDS's monthly and yearly products have no hours (apply_source_ui hides them): whole days then
        hourly = not cds or ((ctx.get("cds_desc") or {}).get("time") or {}).get("kind", "ymd") == "ymd"
        start_hour = int(f["start_hour"] or 0) if hourly else 0
        end_hour = int(23 if f["end_hour"] is None else f["end_hour"]) if hourly else 23
        return JobSpec(
            source=f["source"],
            dataset=f["dataset"], collection=f["collection"], dataset_url=None if cds else info.get("url"),
            variables=list(f["variables"]),
            levels=([float(x) for x in f["levels"]] if cds else list(f["levels"])) or None,
            cds=CdsSpec(grid=grid, options={k: v for k, v in f["cds_options"].items() if v not in (None, [], "")},
                        max_inflight=int(f["max_inflight"] or 4), pack=f["pack"],
                        max_request_gb=float(f["max_request_gb"] or 10)),
            time=TimeSpec(start=f["start"], end=f["end"], start_hour=start_hour,
                          end_hour=end_hour, freq=f["freq"], hours=int(f["hours"] or 1),
                          tz_offset=int(f["tz_offset"] or 0),
                          stats={k: v for k, v in f["stats"].items() if k in f["variables"] and v}),
            area=AreaSpec(mode=f["mode"], west=float(f["west"] or 0), south=float(f["south"] or 0),
                          east=float(f["east"] or 0), north=float(f["north"] or 0), points=pts,
                          lon_convention=f["lon_convention"]),
            output=OutputSpec(
                directory=f["directory"], name=f["name"], export_zarr=f["out_format"] != "icechunk",
                keep_icechunk=f["out_format"] != "zarr",
                target_mode=f.get("target_mode", "create"),
                target_store=f.get("target_store") or None,
                extend_direction=f.get("extend_direction", "forward"),
                keepbits=None if f["lossless"] else int(f["keepbits"] or 13),
                chunk_layout=layout_choice(),
                chunk_time=_int(f["chunk_time"]) if custom else None,
                chunk_lat=_int(f["chunk_lat"]) if custom else None,
                chunk_lon=_int(f["chunk_lon"]) if custom else None,
                chunk_points=_int(f["chunk_points"]) if custom else None,
                dtype=f["dtype"], compressor=f["compressor"], clevel=int(f["clevel"] or 0), shuffle=f["shuffle"],
                sharding=bool(f["sharding"]), shard_auto=bool(f["shard_auto"]),
                shard_time=_int(f["shard_time"]), shard_space=_int(f["shard_space"]),
                extra_attrs=parse_attrs(f["extra_attrs"])),
            download=DownloadSpec(key=f["key"], connections=int(f["connections"] or 1),
                                  spool_dir=f["spool_dir"] or None, max_spool_gb=float(f["max_spool_gb"] or 20),
                                  min_free_gb=float(f["min_free_gb"] or 0), quota_safety=float(f["quota_safety"]),
                                  wait_for_quota=f["wait_for_quota"], memory_gb=float(f["memory_gb"] or 16)),
        )

    def schedule_plan():
        if ctx["plan_task"]:
            ctx["plan_task"].cancel()
        ctx["plan"] = None  # the form changed: the plan shown no longer applies, so it cannot be launched
        launch_btn.disable()
        ctx["plan_task"] = ui.timer(0.4, update_plan, once=True)

    def plan_failed(e):
        """A plan that cannot be shown is not launched either: the button stays disabled and the reason is shown."""
        ctx["plan"] = None
        launch_btn.disable()
        summary.clear()
        with summary:
            with ui.row().classes("items-start gap-2 no-wrap"):
                ui.icon("error_outline", color="negative")
                ui.label(f"Could not show the plan: {e}").classes("text-negative")

    async def update_plan():
        ctx["plan"] = None  # until this plan is ready, the previous one must not be launched
        launch_btn.disable()
        ds = ctx["ds"]
        ctx["plan_seq"] += 1
        seq = ctx["plan_seq"]
        summary.clear()
        with summary:
            if not f.get("key"):
                ui.label("⚠️ Climate Data Store key required. Add your token in the Keys tab to check licences and "
                         "costs and to download." if f["source"] == "cds" else
                         "⚠️ Earth Data Hub API key required. Add your key in the Keys tab to view request cost and plan downloads.").classes("text-amber-400 font-medium")
                launch_btn.disable()
                return
            if f.get("target_mode") == "extend" and not (f.get("target_store") or "").strip():
                ui.label("Enter an existing Icechunk store path in Output to extend.").classes("edh-muted")
                launch_btn.disable()
                return
            if not ds:
                ui.label("Select a dataset to see the plan.").classes("edh-muted")
                launch_btn.disable()
                return
            try:
                spec_ = build_spec()
                res = await be.call("plan", spec=spec_.to_dict(), connections=int(f["connections"] or 1),
                                    follow=f["chunk_layout"] is None and f["out_format"] != "icechunk")
            except (BackendError, ValueError) as e:
                res = {"ok": False, "error": str(e)}
            if seq != ctx["plan_seq"]:
                return
            if res["ok"] and f["source"] == "cds":
                try:
                    ctx["plan"] = res
                    render_cds_summary(res, spec_)
                    render_cds_structure(res)
                except Exception as e:  # noqa: BLE001
                    plan_failed(e)
                return
            if not res["ok"]:
                ctx["plan"] = None
                struct_body.set_visibility(False)
                struct_kpis.clear()
                struct_empty.text = res["error"]
                struct_empty.set_visibility(True)
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("error_outline", color="negative")
                    ui.label(res["error"]).classes("text-negative")
                launch_btn.disable()
                return
        try:
            ctx["rec"] = tuple(res["rec"])
            ctx["plan"] = res
            render_summary(res)
            render_chunking(res)
            render_encoding(res)
            if f.get("target_mode") == "extend":
                tgt = f.get("target_store") or ""
                direction = f.get("extend_direction", "forward")
                out_paths.text = f"→ Extending ({direction}) in {tgt}"
            else:
                out_paths.text = "→ " + "   ·   ".join(p for p in (res["paths"]["zarr"], res["paths"]["icechunk"]) if p)
            launch_btn.enable()
        except Exception as e:  # noqa: BLE001
            plan_failed(e)

    def render_cds_summary(res, spec_):
        s = res["summary"]
        summary.clear()
        blocked = []
        with summary:
            with ui.row().classes("w-full items-center gap-4 no-wrap"):
                lim = s["largest_request"]
                share = (lim["cost"] or lim["fields"]) / lim["limit"] if lim["limit"] else 0
                ui.circular_progress(value=min(1.0, share), show_value=False, size="84px",
                                     color="positive" if share <= 1 else "negative").props(
                    "thickness=0.18 track-color=grey-9")
                with ui.column().classes("gap-0"):
                    ui.label(f"{s['requests']:,}").classes("edh-kpi-value")
                    ui.label("request" + ("s" if s["requests"] != 1 else "") + " to the CDS queue").classes("edh-muted")
                    ui.label(f"largest uses {100 * share:.0f}% of the dataset's limit"
                             + ("" if res["checked"] else " (local count)")).classes("text-caption edh-muted")
            with ui.grid(columns=2).classes("w-full gap-3"):
                kpi("Download", f"≈ {human_bytes(s['download_bytes_est'])}" if s["download_bytes_est"] else "—",
                    "estimate · GRIB, 16 bits a value" if s["download_bytes_est"] else "known after the first file")
                st = res.get("struct")
                fmt = f["out_format"]
                if ctx.get("files_only"):
                    kpi("Output", "files", "as the CDS delivers them")
                elif st:
                    kpi({"zarr": "Zarr size", "both": "Zarr + Icechunk", "icechunk": "Icechunk size"}[fmt],
                        f"≈ {human_bytes(st['disk'] * (2 if fmt == 'both' else 1))}",
                        f"estimate · {human_bytes(st['raw'])} raw")
                else:
                    kpi("Zarr size", "—", "known once the files arrive" if s["tier"] == "netcdf"
                        else "known after the first file")
                kpi("Fields", f"{s['fields']:,}", "variables × levels × time steps")
                kpi("Time steps", f"{s['steps']:,}" if s["steps"] else "—", "requested from the CDS")
            if s["first"]:
                kv("Period", f"{s['first'][:16]} → {s['last'][:16]}")
            kv("Area", "N {} · W {} · S {} · E {}".format(*s["area"]) if s["area"] else
               "whole domain (cut after download)" if f["mode"] != "global" else "global")
            kv("Grid", s["grid"].split("/")[0] + "°" if s["grid"] else "native")
            if s["levels"]:
                kv("Levels", ", ".join(s["levels"]))
            if res.get("layout") and not ctx.get("files_only"):
                kv("Chunks", chunking.TITLES[res["layout"]] + (" (recommended)" if res["layout"] == res["rec"][0] else ""))
            opts = {k: v for k, v in s["options"].items() if k not in ("data_format", "format")}
            if opts:
                kv("Options", "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in opts.items()))
            # costs not checked because the CDS refused the key: the plan is not trusted, nothing is launched
            key_refused = bool(res.get("key_refused"))
            if key_refused:
                blocked.append(res["note"])
            if res["licences_missing"]:
                blocked.append("Accept the licence first: " + ", ".join(l["label"] for l in res["licences_missing"])
                               + " (on the dataset page of the CDS, Download tab).")
            for i, bad in list((res["invalid"] or {}).items())[:3]:
                blocked.append(f"Request {int(i) + 1} asks for data the dataset does not have: "
                               + "; ".join(f"{k} {', '.join(v)}" for k, v in bad.items()))
            if s["tier"] == "table":
                blocked.append("In-situ observation tables cannot be downloaded into a store by this app.")
            for b in blocked:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("error_outline", color="negative", size="xs")
                    ui.label(b).classes("text-caption text-negative")
            if res["note"] and not key_refused:
                ui.label(res["note"]).classes("text-caption text-warning")
            if alt := res.get("alternative"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("bolt", color="accent", size="xs")
                    ui.label(f"Faster for points: {alt['dataset']}, {alt['requests']} request(s). {alt['note']}."
                             ).classes("text-caption text-accent grow")
                    ui.button("Use it", on_click=lambda a=alt["dataset"]: use_dataset(a)).props(
                        "flat dense no-caps color=accent")
            for w in s["warnings"]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("warning_amber", color="warning", size="xs")
                    ui.label(w).classes("text-caption text-warning")
            with ui.expansion(f"Requests ({s['requests']:,})", icon="list").classes("w-full edh-expansion"):
                rows = [{"n": r["index"] + 1, "period": f"{r['start'][:10]} → {r['end'][:10]}",
                         "fields": f"{r['fields']:,}", "size": human_bytes(r["bytes_est"]) if r["bytes_est"] else "—"}
                        for r in res["requests"][:200]]
                ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left" if k == "period" else "right"}
                                  for k, l in (("n", "#"), ("period", "Period"), ("fields", "Fields"), ("size", "Size"))],
                         rows=rows, row_key="n").props("dense flat").classes("w-full edh-table")
        o = spec_.output
        if ctx.get("files_only"):
            out_paths.text = f"→ {o.directory}/{o.name}.files"
        else:
            out_paths.text = "→ " + "   ·   ".join(str(p) for p, keep in ((spec_.zarr_path, o.export_zarr),
                                                                          (spec_.icechunk_path, o.keep_icechunk)) if keep)
        launch_btn.set_enabled(not blocked)

    def render_cds_structure(res):
        """The Zarr store a CDS job will write, as for Earth Data Hub, from the plan alone (``CdsOutput.preview``):
        what is still a guess (GRIB names, a projected grid) is said under it."""
        struct_notes.clear()
        if not res.get("struct"):
            ctx["rec"] = None
            struct_body.set_visibility(False)
            struct_kpis.clear()
            struct_empty.text = res.get("struct_error") or (
                "The store's structure is known once the first file arrives: this dataset's grid is read from "
                "its files." if f["variables"] else "Select variables to see the store.")
            struct_empty.set_visibility(True)
            return
        ctx["rec"] = tuple(res["rec"])
        render_chunking(res)
        render_encoding(res)
        with struct_notes:
            for n in res["struct"].get("notes") or []:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("info", size="xs").classes("edh-muted")
                    ui.label(n[:1].upper() + n[1:] + ".").classes("edh-muted text-caption")

    def use_dataset(ds_id):
        """Switch to another CDS dataset of the same category (the time-series alternative)."""
        if ds_id in ds_select.options:
            ds_select.value = ds_id

    def render_summary(plan):
        smp = plan["sample"]
        s = plan["summary"]
        est = plan["est"]
        ctx["est"] = est
        render_conn_hint(plan, smp)
        n_s = plan["sample_n"]
        sample_btn.text = f"Measure on a sample · {n_s} request{'s' if n_s != 1 else ''}"
        k = plan["key"]
        used = k["usage"]["used"] if k else 0
        limit = int(k["quota"] * f["quota_safety"]) if k else 0
        left = max(0, limit - used)
        share = s["requests"] / left if left else float("inf")
        summary.clear()
        with summary:
            with ui.row().classes("w-full items-center gap-4 no-wrap"):
                ring = min(1.0, share) if k else 0
                color = "positive" if share <= 0.5 else "warning" if share <= 1 else "negative"
                ui.circular_progress(value=ring, show_value=False, size="84px", color=color).props(
                    "thickness=0.18 track-color=grey-9")
                with ui.column().classes("gap-0"):
                    ui.label(f"{s['requests']:,}").classes("edh-kpi-value")
                    ui.label("requests").classes("edh-muted")
                    if k:
                        ui.label(f"{100 * share:.1f}% of {left:,} left this month" if share <= 1 else
                                 f"{share:.1f}× this month's remaining {left:,}").classes(f"text-caption text-{color}")
            if k and share > 1:
                months = -(-(s["requests"] - left) // max(1, int(k["quota"] * f["quota_safety"])))
                ui.label(f"Needs about {months} more monthly reset(s). " +
                         ("The job waits for each reset and continues." if f["wait_for_quota"] else
                          "The job pauses at the limit.")).classes("text-caption text-warning")
            with ui.grid(columns=2).classes("w-full gap-3"):
                kpi("Download", f"≈ {human_bytes(est['download'])}",
                    "measured" if est["download_measured"] else "estimate · whole source chunks")
                fmt = f["out_format"]
                kpi({"zarr": "Zarr size", "both": "Zarr + Icechunk", "icechunk": "Icechunk size"}[fmt],
                    f"≈ {human_bytes(est['zarr'] * (2 if fmt == 'both' else 1))}",
                    ("measured" if est["zarr_measured"] else "estimate") + f" · {human_bytes(est['raw'])} raw")
                kpi("Time steps", f"{s['periods']:,}", f"{s['units']:,} commits")
                kpi("Est. time", human_duration(est["eta_s"]) if est["eta_s"] else "—",
                    f"at {human_bytes(est['throughput'])}/s" if est["eta_s"] else "measure on a sample")
            if s["first"]:
                kv("Period", f"{s['first'][:16]} → {s['last'][:16]}")
            kv("Grid", " × ".join(map(str, s["space"])) + (" points" if f["mode"] == "points" else ""))
            if s["levels"]:
                kv("Levels", ", ".join(map(str, s["levels"])))
            kv("Outputs", ", ".join(s["outputs"]))
            kv("Chunks", chunking.TITLES[plan["layout"]] + (" (recommended)" if plan["layout"] == rec_of()[0] else ""))
            for w in s["warnings"]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("warning_amber", color="warning", size="xs")
                    ui.label(w).classes("text-caption text-warning")

    def render_chunking(plan):
        rows = plan["rows"]
        rec_layout, reason = rec_of()
        chosen = plan["layout"]
        opts = {r["layout"]: ("★ " if r["layout"] == rec_layout else "") + r["title"] for r in rows}
        layout_toggle.set_options(opts, value=chosen)
        rec_label.text = f"★ recommended: {reason}"
        r = next(x for x in rows if x["layout"] == chosen)
        metrics.clear()
        with metrics:
            _chip("Chunk", _mb(r["chunk_mb"]), f"≈ {_mb(r['chunk_mb_compressed'])} on disk")
            _chip("Chunks", f"{r['n_chunks']:,}", "first variable")
            _chip("Time series at a cell", _n(r["point_series_chunks"], "chunk"), _mb(r["point_series_mb"]) + " read")
            _chip("Map at one step", _n(r["map_chunks"], "chunk"), _mb(r["map_mb"]) + " read")
            _chip("Export", r["export"], "from the download staging")
        compare.clear()
        with compare:
            cols = [{"name": k, "label": l, "field": k, "align": "left" if k == "layout" else "right"} for k, l in (
                ("layout", "Layout"), ("chunk", "Chunk shape"), ("size", "Chunk"), ("n", "Chunks"),
                ("ts", "Time-series read"), ("map", "Map read"), ("export", "Export"))]
            data = [{"layout": ("★ " if x["layout"] == rec_layout else "") + x["title"],
                     "chunk": " × ".join(f"{v:,}" for v in x["chunks"].values()), "size": _mb(x["chunk_mb"]),
                     "n": f"{x['n_chunks']:,}", "ts": f"{x['point_series_chunks']:,} · {_mb(x['point_series_mb'])}",
                     "map": f"{x['map_chunks']:,} · {_mb(x['map_mb'])}", "export": x["export"]} for x in rows]
            ui.table(columns=cols, rows=data, row_key="layout").props("dense flat").classes("w-full edh-table")
        render_structure(plan)

    def render_structure(plan):
        d = plan["struct"]
        ctx["struct"] = d
        struct_view.clear()
        with struct_view:
            structure_panel(d, ctx.setdefault("struct_state", {}),
                            see_all=lambda: structure_dialog(dialog_host, ctx["struct"], ctx["struct"]["name"]))
        struct_kpis.clear()
        with struct_kpis:
            structure_badges(d)
        struct_empty.set_visibility(False)
        struct_body.set_visibility(True)

    def _chip(label, value, sub=""):
        with ui.column().classes("edh-kpi gap-0 grow"):
            ui.label(label).classes("edh-kpi-label")
            ui.label(value).classes("text-subtitle1 text-weight-bold")
            if sub:
                ui.label(sub).classes("edh-muted text-caption")

    def pick_layout(lay):
        if not lay or not ctx["plan"] or lay == ctx["plan"]["layout"]:
            return
        f["chunk_layout"] = None if lay == rec_of()[0] else lay
        schedule_plan()

    def render_encoding(plan):
        sh = plan["shards"]
        if not sh:
            shard_hint.text = ""
        elif sh["files"]:
            shard_hint.text = f"{sh['chunks']:,} chunks in {sh['files']:,} files per variable"
        else:
            shard_hint.text = f"{sh['chunks']:,} chunk files per variable" + (" (sharding suggested)" if sh["chunks"] > 20_000 else "")

    ui.timer(1.0, lambda: _watch_form(f, ctx, schedule_plan, render_stats, draw_rect))

    async def launch():
        if not f.get("key"):
            ui.notify("Please configure an API key in the Keys tab first", type="warning")
            return
        if f.get("target_mode") == "extend" and not (f.get("target_store") or "").strip():
            ui.notify("Please enter an existing Icechunk store path to extend", type="warning")
            return
        if not ctx["plan"]:
            return
        try:
            spec_ = build_spec()
        except ValueError as e:
            ui.notify(str(e), type="negative")
            return
        try:
            job = await be.call("job_start", spec=spec_.to_dict())
        except BackendError as e:
            ui.notify(str(e), type="negative")
            return
        ui.notify(f'job {job["id"]} started (pid {job["pid"]})', type="positive")
        tabs.set_value(t_jobs)
        await refresh_jobs()

    launch_btn.on_click(launch)

    def save_job_file():
        try:
            spec_ = build_spec()
        except ValueError as e:
            ui.notify(str(e), type="negative")
            return
        ui.download.content(json.dumps({"spec": spec_.to_dict()}, indent=2), f"{spec_.output.name}.job.json")

    # ------------------------------------------------------------------ sample estimate
    def render_conn_hint(plan, smp):
        conn_hint.clear()
        ok = bool(smp)
        with conn_hint:
            if not ok:
                ui.label("not measured yet").classes("edh-muted text-caption")
                return
            sug = smp["suggested"]
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(f"suggested {sug}").classes("text-caption text-accent")
                if int(f["connections"] or 0) != sug:
                    ui.button("use", on_click=lambda: f.update(connections=sug)).props(
                        "flat dense no-caps color=accent size=sm")
            line = (f"1 conn {human_bytes(smp['single_bps'])}/s · {smp['parallel_connections']} conn "
                    f"{human_bytes(smp['parallel_bps'])}/s") if smp.get("parallel_bps") else ""
            if line:
                ui.label(line + (" (saturated)" if smp["saturated"] else "")).classes("edh-muted text-caption")
            ui.label(f"latency {smp['latency_s']:.2f} s · {_ago(smp['time'])}").classes("edh-muted text-caption")

    def ask_sample():
        plan = ctx["plan"]
        key = plan and plan["key"]
        if not plan or not key:
            ui.notify("select a dataset, variables and a key first", type="warning")
            return
        n = plan["sample_n"]
        left = max(0, int(key["quota"] * f["quota_safety"]) - key["usage"]["used"])
        async def go():
            dlg.close()
            await run_sample(n)

        with ui.dialog() as dlg, ui.card().classes("edh-card gap-3"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("warning_amber", color="warning")
                ui.label("This uses your quota").classes("text-subtitle1 text-weight-medium")
            ui.label(f"Downloads {n} chunks = {n} requests from key '{key['name']}' "
                     f"({left:,} left this month).").classes("text-body2")
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat no-caps")
                ui.button(f"Use {n} requests", on_click=lambda: go()).props(
                    "unelevated no-caps").classes("edh-grad-btn")
        dlg.open()

    async def run_sample(n):
        if not ctx["plan"] or not f["key"]:
            return
        sample_btn.disable()
        sample_status.text = f"downloading 0/{n} chunks…"

        def prog(ev):
            sample_status.text = f"downloading {ev['i']}/{ev['total']} chunks…"

        try:
            await be.call("sample_run", spec=build_spec().to_dict(), key=f["key"], n=n, on_event=prog)
            sample_status.text = f"measured on {n} chunks ({n} requests)"
        except (BackendError, ValueError) as e:
            sample_status.text = ""
            ui.notify(f"sample failed: {e}", type="negative")
        finally:
            sample_btn.enable()
        await refresh_quota()
        schedule_plan()

    sample_btn.on_click(ask_sample)

    # ------------------------------------------------------------------ jobs tab
    cards: dict = {}  # job id -> (signature, card)
    open_struct: set = set()  # completed jobs whose structure is shown
    open_reqs: set = set()  # CDS jobs whose request table is shown
    last_jobs: dict = {}  # job id -> its latest status (the request table reads its live downloads)
    struct_states: dict = {}  # job id -> {"var": ..., "src": ...}
    job_states: dict = {}  # job id -> last status, to announce changes

    async def load_thumbs(collection):
        """Images for job cards; a card without one just shows the globe, so a failure here is not reported."""
        try:
            items = await be.call("datasets", collection=collection)
        except Exception:
            return
        ctx["thumbs"][collection] = {i["id"]: i.get("thumbnail") for i in items}

    async def refresh_jobs():
        try:
            jobs = await be.call("jobs_list")
        except BackendError:  # polled every 3 s: say so once, in place, not with a toast each time
            jobs_rollup.set_content(jobs_stale_html(time_ago(ctx.get("jobs_seen"))))
            jobs_rollup.set_visibility(True)
            return
        ctx["jobs_seen"] = datetime.now(timezone.utc).isoformat()
        last_jobs.clear()
        last_jobs.update({s_["id"]: s_ for s_ in jobs})
        changed = [f"{s_['id']}: {job_status_label(s_.get('status', '?'))}" for s_ in jobs
                   if s_["id"] in job_states and job_states[s_["id"]] != s_.get("status")]
        job_states.clear()
        job_states.update({s_["id"]: s_.get("status") for s_ in jobs})
        if changed:
            jobs_announce.set_text(". ".join(changed))
        balances: dict = {}  # API key -> status, read once per refresh
        for s_ in jobs:
            if s_.get("alive") or s_.get("status") in ("completed", "unreadable") or not s_.get("spec") or is_cds(s_):
                continue
            dl = s_["spec"].get("download") or {}
            name = dl.get("key")
            if name not in balances:
                try:
                    balances[name] = await be.call("key_status", name=name)
                except BackendError:
                    balances[name] = None
            if k := balances[name]:
                s_["_quota"] = {"left": max(0, int(k["quota"] * dl.get("quota_safety", 0.9)) - k["usage"]["used"])}
        for s_ in jobs:  # read the stores off the event loop (cached)
            if s_["id"] in open_struct and (src := _struct_src(s_)):
                try:
                    s_["_struct"] = await be.call("store_structure", path=src)
                except BackendError as e:  # shown in the card
                    s_["_struct_err"] = str(e)
        for s_ in jobs:
            s_["_ago"] = time_ago(s_.get("finished_at") if s_.get("status") == "completed" else s_.get("updated_at"))
            if (spec_ := s_.get("spec")) and spec_.get("source") == "cds":
                s_["_thumb"] = ctx["cds_thumbs"].get(spec_.get("dataset"))
            elif spec_ := s_.get("spec"):
                coll = spec_.get("collection", "era5")
                s_["_thumb"] = ctx["thumbs"].get(coll, {}).get(spec_.get("dataset"))
                if coll not in ctx["thumbs"] and coll not in ctx["thumbs_asked"]:  # off the refresh: may be slow
                    ctx["thumbs_asked"].add(coll)
                    later(lambda c=coll: load_thumbs(c))
        jobs.sort(key=lambda s_: job_group(s_.get("status")))  # stable: newest first within each group
        jobs_rollup.set_content(job_rollup_html(jobs))
        jobs_rollup.set_visibility(bool(jobs))
        ids = [s_["id"] for s_ in jobs]
        if ids != list(cards) or not jobs:
            jobs_box.clear()
            cards.clear()
            with jobs_box:
                if not jobs:
                    with ui.card().classes("edh-card w-full items-center py-10"):
                        ui.icon("inbox", size="xl", color="grey-6")
                        ui.label("No jobs yet. Create one in New job.").classes("edh-muted")
                for s_ in jobs:
                    cards[s_["id"]] = (None, ui.card().classes("edh-card w-full"), None, None)
        for s_ in jobs:
            # structure (buttons, inputs) is rebuilt only when it changes; live numbers are redrawn in place, so
            # what the user is typing (e.g. connections) survives the refresh
            sig = json.dumps({k: s_.get(k) for k in (*STRUCT_KEYS, "_thumb")}, sort_keys=True, default=str) + \
                str(s_["id"] in open_struct) + str(s_["id"] in open_reqs) + \
                json.dumps(struct_states.get(s_["id"], {}).get("src"))
            live_sig = json.dumps({k: v for k, v in s_.items() if k != "_struct"}, sort_keys=True, default=str)
            old, card = cards[s_["id"]][:2]
            if sig != old or "_struct" in s_ and old is None:
                card.clear()
                with card:
                    live = _job_card(s_)
                cards[s_["id"]] = (sig, card, live, live_sig)
            elif live_sig != cards[s_["id"]][3]:
                live = cards[s_["id"]][2]
                live.clear()
                with live:
                    _job_live(s_)
                cards[s_["id"]] = (sig, card, live, live_sig)

    def _struct_src(s_):
        outs = s_.get("outputs_exist") or []
        if s_.get("status") != "completed" or not outs:
            return None
        want = struct_states.get(s_["id"], {}).get("src")
        return want if want in outs else next((o for o in outs if o.endswith(".zarr")), outs[0])

    def toggle_struct(jid):
        open_struct.symmetric_difference_update({jid})
        ui.timer(0.01, refresh_jobs, once=True)

    def toggle_reqs(jid):
        open_reqs.symmetric_difference_update({jid})
        ui.timer(0.01, refresh_jobs, once=True)

    def _job_activity(s, cds):
        """What a running job is doing now: each download with its share and speed, the unit being processed
        (variable, level, source steps), the NetCDF reading, the export. Redrawn with the card's numbers."""
        a = s.get("activity") or {}
        rows = activity_rows(a)
        if cds and s.get("status") == "running" and not rows:
            c = a.get("requests") or s.get("cds_requests") or {}
            waiting, running = s.get("cds_waiting") or 0, s.get("cds_running") or 0
            left = (c.get("planned") or 0) + (c.get("submitting") or 0)
            ready = c.get("ready") or 0
            parts = [f"{waiting:,} in the queue" if waiting else "", f"{running:,} running" if running else "",
                     f"{ready:,} ready to download" if ready else "", f"{left:,} not submitted yet" if left else ""]
            wait = s.get("cds_oldest_wait_s")
            rows.append({"icon": "hourglass_top", "title": "Waiting on the CDS",
                         "detail": " · ".join(x for x in parts if x) +
                                   (f" · longest wait {human_duration(wait)}" if wait else ""),
                         "frac": None, "right": ""})
        for r in rows:
            with ui.column().classes("w-full gap-1 edh-act"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.icon(r["icon"], size="18px").classes("edh-muted")
                    ui.label(r["title"]).classes("text-body2 text-weight-medium grow min-w-0")
                    if r["right"]:
                        ui.label(r["right"]).classes("text-body2 edh-tabular shrink-0")
                ui.label(r["detail"]).classes("edh-muted text-caption break-words")
                bar = ui.linear_progress(value=r["frac"] or 0, show_value=False).props(
                    "size=4px" + (" indeterminate" if r["frac"] is None else "")).classes("edh-grad-bar")
                bar.props(f'aria-label="{r["title"]}"')

    def _job_requests(s):
        """Every request of a CDS job: where it is, how long it waited and ran, its file. Refreshed while open."""
        jid = s["id"]
        files_only = not s.get("cds_convert", True)
        with ui.column().classes("w-full gap-2 edh-struct"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.label("Requests").classes("edh-subtitle")
                summary_ = ui.label().classes("edh-muted text-caption")
            cols = [("n", "#", "right"), ("period", "Period", "left"), ("fields", "Fields", "right"),
                    ("state", "State", "left"), ("waited", "Queued for", "right"), ("ran", "Ran for", "right"),
                    ("size", "File", "right"), ("tries", "Tries", "right")]
            table = ui.table(columns=[{"name": k, "label": l, "field": k, "align": a} for k, l, a in cols],
                             rows=[], row_key="n", pagination={"rowsPerPage": 10}).props(
                "dense flat").classes("w-full edh-table")
            table.add_slot("body-cell-state", '''
                <q-td :props="props"><span :class="'edh-req-' + props.row.tone">{{ props.value }}</span></q-td>''')

            async def load():
                r = await call("job_requests", jid=jid)
                if not r:
                    return
                live = last_jobs.get(jid) or s
                rows = request_rows(r["requests"], live.get("activity") or {}, files_only)
                tally: dict[str, int] = {}
                for row in rows:
                    key = row["state"].split(":")[0]
                    key = "downloading" if key.startswith("downloading") else key
                    tally[key] = tally.get(key, 0) + 1
                summary_.text = f"{len(rows):,} request{'s' if len(rows) != 1 else ''} · " + " · ".join(
                    f"{v:,} {k}" for k, v in tally.items())
                table.rows = rows
                table.update()
                if not live.get("alive"):
                    t.active = False

            later(load)
            t = ui.timer(3.0, load)

    def set_struct_src(jid, src):
        struct_states.setdefault(jid, {})["src"] = src
        ui.timer(0.01, refresh_jobs, once=True)

    def _job_live(s):
        st = s.get("status", "?")
        total = s.get("units_total") or 0
        done = len(s.get("units_done", []))
        cds = is_cds(s)
        counts = s.get("cds_requests") or {}
        if cds and not total:  # files-only job: progress is files downloaded
            total = s.get("requests_total") or 0
            done = counts.get("downloaded", 0) + counts.get("done", 0)
        color = STATUS_COLOR.get(st, "grey-6")
        tail, forecast = job_forecast(s) if st == "running" else ("", "")
        with ui.row().classes("w-full items-baseline justify-between no-wrap gap-4"):
            with ui.row().classes("items-baseline gap-x-2 gap-y-0 min-w-0"):
                ui.icon(STATUS_ICON.get(st, "help"), size="22px").classes(f"text-{color} self-center")
                ui.label(job_status_label(st)).classes(f"edh-kpi-value text-{color}")
                if tail:
                    ui.label(tail).classes("edh-kpi-value")
            if total:
                ui.label(job_percent(done, total)).classes("edh-subtitle edh-tabular shrink-0")
        ui.linear_progress(value=done / total if total else 0, show_value=False).props(
            f'size=8px aria-label="Job progress" aria-valuetext="{job_percent(done, total) if total else "unknown"}"'
        ).classes(f"edh-grad-bar edh-bar-{color}")
        note = job_note(s, done, total) or forecast
        if note:
            ui.label(note).classes("text-body2 max-w-prose")
        if s.get("alive"):
            _job_activity(s, cds)
        if not s.get("alive") and st not in ("completed", "unreadable"):
            cost, short = job_resume_cost(s)
            if cost:
                ui.label(cost).classes("text-caption max-w-prose " + ("text-warning" if short else "edh-muted"))
        with ui.row().classes("w-full gap-3 items-center pt-1"):
            with ui.element("div").classes("edh-job-tiles"):  # fixed slots: a value sits in the same place on every card
                if cds:
                    _cds_tiles(s, st, counts)
                else:
                    _edh_tiles(s, st, done, total)
            caption = ("Estimate at the current speed." if tail and not forecast else
                       s.get("message") or "" if st == "exporting" else "")
            if caption:
                ui.label(caption).classes("edh-muted text-caption grow min-w-0")

    def _edh_tiles(s, st, done, total):
        kpi("Units", f"{done:,}/{total:,}")
        req_total = s.get("requests_total")
        req_done = ((s.get("requests_job", 0) or 0) + (s.get("requests_run", 0) or 0)
                    if st in ACTIVE else s.get("requests_job", 0) or 0)
        if req_total:
            kpi("Requests", f"{req_done:,}", f"of {req_total:,}")
        if st == "waiting_quota":
            r = next_reset()
            kpi("Resumes", f"{r.day} {r:%b}", f"after {r:%H:%M} UTC")
        elif st in ACTIVE:
            kpi("Speed", f"{human_bytes(s.get('rate_bps') or 0)}/s", f"{s.get('req_rate') or 0:.1f} req/s")
            kpi("Temporary files", human_bytes(s.get("spool_bytes") or 0))
        if st == "completed":
            if s.get("output_bytes"):
                kpi("Output size", human_bytes(s["output_bytes"]))
            if s.get("_ago"):
                kpi("Finished", f"{s['_ago']} ago", s["finished_at"][:16].replace("T", " ") + " UTC")

    def _cds_tiles(s, st, counts):
        total = s.get("requests_total") or 0
        got = counts.get("downloaded", 0) + counts.get("done", 0)
        if s.get("units_total"):
            kpi("Units", f"{len(s.get('units_done', [])):,}/{s['units_total']:,}", "committed")
        kpi("Requests", f"{got:,}/{total:,}", "downloaded")
        if st in ACTIVE:
            kpi("On the CDS", f"{s.get('cds_waiting') or 0} waiting · {s.get('cds_running') or 0} running",
                f"up to {s.get('cds_inflight_cap') or '?'} at once")
            wait = s.get("cds_oldest_wait_s")
            kpi("Longest wait", human_duration(wait) if wait else "—", "in the CDS queue")
            kpi("Downloaded", human_bytes(s.get("bytes_run") or 0))
        if st == "completed":
            if s.get("output_bytes"):
                kpi("Output size", human_bytes(s["output_bytes"]))
            if s.get("_ago"):
                kpi("Finished", f"{s['_ago']} ago", s["finished_at"][:16].replace("T", " ") + " UTC")

    def _job_outputs(s):
        """A finished output, with the code that opens it; only the paths for anything else."""
        exist = s.get("outputs_exist") or []
        main = next((o for o in exist if o.endswith(".zarr")), exist[0] if exist else None)
        if s.get("status") != "completed" or main is None:
            ui.label("  ·  ".join(s["outputs"])).classes("edh-mono text-caption edh-muted")
            return
        snippet = open_snippet(main)
        with ui.element("div").classes("edh-code w-full"):
            ui.label(snippet).classes("edh-code-text grow min-w-0")
            ui.button(icon="content_copy", on_click=lambda t=snippet: (
                ui.clipboard.write(t), ui.notify("Code copied", type="positive"))).props(
                'flat round dense color=grey-5 aria-label="Copy code"').tooltip("Copy code")
        others = [o for o in s["outputs"] if o != main]
        if others:
            ui.label("Also saved: " + "  ·  ".join(others)).classes("edh-mono text-caption edh-muted")

    def _queue_menu(s, d, spec):
        """CDS jobs: how many requests may wait on the CDS at once."""
        with ui.button("Queue", icon="tune").props("flat no-caps"):
            with ui.menu().classes("edh-menu") as menu:
                with ui.row().classes("items-center gap-2 no-wrap p-3"):
                    n = ui.number("Requests waiting on the CDS",
                                  value=(spec.get("cds") or {}).get("max_inflight", 4), min=1, max=32).props(
                        "dense outlined").classes("w-56")

                    async def apply():
                        menu.close()
                        await act("job_inflight", "queue size updated", jid=d, n=max(1, min(32, int(n.value or 1))))

                    ui.button("Apply", on_click=apply).props("flat no-caps")

    def _connections_menu(s, d, spec):
        """Rarely changed, so kept out of the card until asked for."""
        if is_cds(s):
            return _queue_menu(s, d, spec)
        with ui.button("Connections", icon="tune").props("flat no-caps"):
            with ui.menu().classes("edh-menu") as menu:
                with ui.row().classes("items-center gap-2 no-wrap p-3"):
                    conn = ui.number("Parallel connections",
                                     value=s.get("connections") or spec.get("download", {}).get("connections"),
                                     min=1, max=64).props("dense outlined").classes("w-44")

                    async def apply():
                        menu.close()
                        await act("job_connections", "connections updated", jid=d,
                                  n=max(1, min(64, int(conn.value or 1))))

                    ui.button("Apply", on_click=apply).props("flat no-caps")

    def _job_card(s):
        """The card; returns its live part (progress, numbers), which refreshes on its own."""
        st = s.get("status", "?")
        spec = s.get("spec", {})
        if True:
            with ui.row().classes("w-full items-center gap-3 no-wrap min-w-0"):
                if spec:
                    dataset_image(s.get("_thumb"), "", "edh-thumb-md")  # the caption beside it names the dataset
                with ui.column().classes("grow gap-0 min-w-0"):
                    ui.label(s["id"]).classes("text-subtitle1 edh-mono break-all").props("role=heading aria-level=3")
                    tt = spec.get("time") or {}
                    if spec:
                        ui.label(f"{spec.get('dataset')} · {', '.join(spec.get('variables') or [])} · "
                                 f"{tt.get('start')} → {tt.get('end')}"
                                 + ("" if tt.get("freq") in (None, "none") else f" · {FREQS.get(tt['freq'], tt['freq'])}")
                                 ).classes("edh-muted text-caption")
                    else:
                        ui.label("job definition unreadable").classes("edh-muted text-caption")
            live = ui.column().classes("w-full gap-2")
            with live:
                _job_live(s)
            if s.get("outputs"):
                _job_outputs(s)
            d = s["id"]
            src = _struct_src(s)
            with ui.row().classes("edh-job-actions w-full gap-2 items-center justify-between"):
                with ui.row().classes("gap-2 items-center"):  # what this state calls for
                    if s.get("alive"):
                        ui.button("Pause", icon="pause", on_click=lambda d=d: act(
                            "job_stop", "pausing after the current unit", jid=d, action="pause")).props(
                            "flat no-caps color=white")
                        ui.button("Cancel", icon="stop", on_click=lambda d=d: confirm(
                            f"Cancel {d}? Committed units stay in the Icechunk repository, and you can resume this job later.",
                            lambda d=d: act("job_stop", "cancelling", jid=d, action="cancel"),
                            yes="Cancel job", no="Keep running", danger=True)).props("flat no-caps color=negative")
                    elif st not in ("completed", "unreadable"):
                        resume = ui.button("Resume", icon="play_arrow", on_click=lambda d=d: _resume(d))
                        if st == "failed":  # the note says what to fix first; retrying is not the loudest thing here
                            resume.props("outline no-caps color=primary")
                        else:
                            resume.props("unelevated no-caps").classes("edh-grad-btn")
                    if src:
                        shown = s["id"] in open_struct
                        ui.button("Structure", icon="account_tree", on_click=lambda j=s["id"]: toggle_struct(j)).props(
                            f"{'unelevated color=primary' if shown else 'outline color=primary'} no-caps")
                with ui.row().classes("gap-2 items-center"):  # always here, in the same place
                    if s.get("alive") and st != "waiting_quota":
                        _connections_menu(s, d, spec)
                    if not s.get("alive"):
                        ui.button(icon="delete_outline", on_click=lambda d=d: confirm(
                            f"Delete the record of {d} and its temporary files? Outputs are kept.",
                            lambda d=d: act("job_delete", None, jid=d),
                            yes="Delete record", no="Keep record", danger=True)).props(
                            'flat round color=grey-6 aria-label="Delete record"').tooltip("Delete record")
                    if spec:
                        ui.button("Clone", icon="content_copy", on_click=lambda sp=spec: clone(sp)).props("flat no-caps")
                    if is_cds(s) and spec:
                        shown_r = d in open_reqs
                        ui.button("Requests", icon="expand_less" if shown_r else "list",
                                  on_click=lambda d=d: toggle_reqs(d)).props(
                            f"flat no-caps aria-expanded={'true' if shown_r else 'false'}")
                    ui.button("Log", icon="article", on_click=lambda d=d: show_log(d)).props("flat no-caps")
            if is_cds(s) and d in open_reqs:
                _job_requests(s)
            if src and s["id"] in open_struct:
                _job_structure(s, src)
        return live

    def _job_structure(s, src):
        jid = s["id"]
        outs = s.get("outputs_exist") or []
        with ui.column().classes("w-full gap-3 edh-struct"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("Dataset structure").classes("edh-subtitle")
                    if len(outs) > 1:
                        ui.toggle({o: ("Zarr" if o.endswith(".zarr") else "Icechunk") for o in outs}, value=src,
                                  on_change=lambda e, j=jid: set_struct_src(j, e.value)).props(
                            "dense no-caps unelevated toggle-color=primary").classes("edh-toggle")
                if "_struct" in s:
                    with ui.row().classes("items-center gap-2"):
                        structure_badges(s["_struct"])
            if len(outs) > 1:
                ui.label(src).classes("edh-mono text-caption edh-muted")
            if "_struct_err" in s:
                ui.label(f"Cannot read the store: {s['_struct_err']}").classes("text-negative text-caption")
            elif "_struct" in s:
                d = s["_struct"]
                structure_panel(d, struct_states.setdefault(jid, {}),
                                see_all=lambda d=d, src=src: structure_dialog(dialog_host, d, src.rstrip("/").rsplit("/", 1)[-1]))
            else:
                ui.spinner(size="md")

    async def act(method, msg=None, **kw):
        """One backend request from a button; notify the outcome and refresh the jobs list."""
        if await call(method, **kw) is not None and msg:
            ui.notify(msg, type="positive")
        await refresh_jobs()

    def _resume(d):
        return act("job_resume", "job resumed", jid=d)

    def confirm(text, action, yes="Yes", no="No", danger=False):
        with dialog_host, ui.dialog() as dlg, ui.card().classes("edh-card").style("width:min(480px,94vw)"):
            ui.label(text).classes("break-words w-full")

            async def go():
                dlg.close()
                r = action()
                if asyncio.iscoroutine(r):
                    await r

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button(no, on_click=dlg.close).props("flat no-caps autofocus")
                btn = ui.button(yes, on_click=lambda: go())
                if danger:
                    btn.props("unelevated no-caps color=negative text-color=dark")
                else:
                    btn.props("unelevated no-caps").classes("edh-grad-btn")
        dlg.open()

    def show_log(jid):
        with dialog_host, ui.dialog().props("maximized") as dlg, ui.card().classes("edh-card w-full h-full"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Log · {jid}").classes("edh-title")
                ui.button(icon="close", on_click=dlg.close).props('flat round aria-label="Close"')
            log = ui.log(max_lines=2000).classes("edh-log w-full grow")
            pos = {"n": 0}

            async def tail():
                r = await call("job_log", jid=jid, offset=pos["n"])
                if r:
                    for line in r["lines"]:
                        log.push(line)
                    pos["n"] = r["next"]

            later(tail)
            t = ui.timer(2, tail)
            dlg.on("hide", lambda: t.cancel())
        dlg.open()

    def clone(sp: dict):
        spec_ = JobSpec.from_dict(sp)
        t, a, o, dl = spec_.time, spec_.area, spec_.output, spec_.download
        src = spec_.source or "edh"
        switched = src != f["source"]
        if switched:
            ctx["sel"][f["source"]] = (f["collection"], f["dataset"])
        g = spec_.cds.grid
        gs = "native" if not g else (f"{g[0]:g}" if f"{g[0]:g}" in GRIDS else "custom")
        f.update(source=src, cds_options=dict(spec_.cds.options), grid=gs, grid_custom=g[0] if g else 0.5,
                 max_inflight=spec_.cds.max_inflight, pack=spec_.cds.pack, max_request_gb=spec_.cds.max_request_gb)
        if src == "cds":
            f["levels"] = [f"{x:g}" for x in (spec_.levels or [])]
        src_toggle.value = src  # f["source"] already says so: the handler leaves the form alone
        ctx["keysel"][src] = dl.key
        f.update(collection=spec_.collection, dataset=spec_.dataset, variables=spec_.variables,
                 levels=spec_.levels or [], start=t.start, end=t.end, start_hour=t.start_hour, end_hour=t.end_hour, freq=t.freq, hours=t.hours,
                 tz_offset=t.tz_offset, stats=dict(t.stats), mode=a.mode, west=a.west, south=a.south, east=a.east,
                 north=a.north, points="\n".join(f"{p.name}, {p.lat}, {p.lon}" for p in a.points),
                 lon_convention=a.lon_convention, directory=o.directory, name=o.name + "_copy",
                 out_format="icechunk" if not o.export_zarr else "both" if o.keep_icechunk else "zarr",
                 lossless=o.keepbits is None,
                 keepbits=o.keepbits or 13, chunk_layout=o.chunk_layout, chunk_time=o.chunk_time,
                 chunk_lat=o.chunk_lat, chunk_lon=o.chunk_lon, chunk_points=o.chunk_points, dtype=o.dtype,
                 compressor=o.compressor, clevel=o.clevel, shuffle=o.shuffle, sharding=o.sharding,
                 shard_auto=o.shard_auto, shard_time=o.shard_time or 1, shard_space=o.shard_space or 4,
                 extra_attrs="\n".join(f"{k} = {v}" for k, v in o.extra_attrs.items()),
                 key=dl.key or ctx["keys"]["default"], connections=dl.connections, spool_dir=dl.spool_dir or "",
                 max_spool_gb=dl.max_spool_gb, min_free_gb=dl.min_free_gb, quota_safety=dl.quota_safety,
                 wait_for_quota=dl.wait_for_quota, memory_gb=dl.memory_gb)
        if src == "cds":
            f["levels"] = [f"{x:g}" for x in (spec_.levels or [])]
            f["chunk_layout"] = o.chunk_layout if o.chunk_layout in ("auto", "timeseries", "maps") else "auto"
        coll_select.value = spec_.collection
        ds_select.value = spec_.dataset
        tabs.set_value(t_new)
        apply_source_ui()
        if switched:
            later(refresh_keys_select)
            ui.timer(0.1, load_collections, once=True)
        else:
            ui.timer(0.1, load_items, once=True)

    ui.timer(3.0, lambda: refresh_jobs() if tabs.value in (t_jobs, "Jobs") else None)
    tabs.on_value_change(lambda e: refresh_jobs() if e.value in (t_jobs, "Jobs") else
                         refresh_key_list() if e.value in (t_keys, "Keys") else None)

    # ------------------------------------------------------------------ keys tab
    async def sources_meta() -> list[dict]:
        """The data sources the backend knows (only Earth Data Hub from a backend older than sources)."""
        if not ctx.get("sources"):
            try:
                ctx["sources"] = await be.call("sources_list")
            except BackendError:
                return [EDH_SOURCE]
        return ctx["sources"]

    async def refresh_key_list():
        d = await call("keys_list")
        if d is None:
            return
        ctx["keys"] = d
        srcs = await sources_meta()
        defaults = d.get("defaults") or {"edh": d["default"]}
        keys_box.clear()
        with keys_box:
            for src in srcs:
                pid = src["id"]
                with ui.row().classes("items-center gap-2 w-full pt-2"):
                    ui.icon("hub", color="secondary")
                    ui.label(src["title"]).classes("text-h6").props("role=heading aria-level=2")
                    if not src.get("jobs", True):
                        ui.label("downloads coming soon: keys can be added already").classes("edh-muted text-caption")
                key_form(src)
                mine = [k for k in d["keys"] if k.get("provider", "edh") == pid]
                if not mine:
                    ui.label(f"No {src['short']} key yet.").classes("edh-muted text-body2")
                for k in mine:
                    key_card(k, src, defaults.get(pid))
                if src.get("has_quota", pid == "edh") and mine:
                    ui.markdown("Earth Data Hub doesn't report your usage to API clients: the app counts its own "
                                f"requests. Open the [quota page]({QUOTA_URL}), then enter the number shown there in "
                                "**Calibrate** (includes requests from other tools), or the limit of your plan in "
                                "**Set quota**.").classes("edh-muted text-caption")

    def key_form(src: dict):
        pid = src["id"]
        quota_kept = src.get("has_quota", pid == "edh")
        with section("+", "Add an API key", mark="keys-add" if pid == "edh" else ""):
            ui.markdown(f"Get a key on [{src['key_url'].split('//')[-1].split('/')[0]}]({src['key_url']}). It is "
                        "stored on the machine that runs the downloads, in `~/.config/edh-dl/keys.json` (only you "
                        f"can read it); {src['key_hint']}").classes("text-body2 edh-muted")
            with ui.row().classes("items-center gap-3 w-full no-wrap max-md:flex-wrap"):
                name_in = ui.input("Name", placeholder="personal").classes("w-44")
                key_in = ui.input("API key" if quota_kept else "Personal access token", password=True,
                                  password_toggle_button=True).classes("grow")
                q_in = ui.number("Monthly quota", value=DEFAULT_QUOTA, min=1).classes("w-44") if quota_kept else None
                chk = ui.switch(f"Check it ({src.get('check_cost', '1 request')})", value=True)

                async def add():
                    if q_in is not None and (q_in.value is None or q_in.value < 1):
                        ui.notify("Monthly quota must be a positive number", type="warning")
                        return
                    r = await call("key_add", name=name_in.value or "", secret=key_in.value or "",
                                   quota_=int(q_in.value) if q_in is not None else None, check=bool(chk.value),
                                   **({} if pid == "edh" else {"provider": pid}))
                    if r is None:
                        return
                    if not r["ok"]:
                        ui.notify(r["msg"], type="negative")
                        return
                    ui.notify(f"saved key {r['name']}", type="positive")
                    await refresh_key_list()
                    await refresh_keys_select()

                ui.button("Add", icon="add", on_click=add).props("unelevated no-caps").classes("edh-grad-btn")

    def key_card(k: dict, src: dict, default: str | None):
        with ui.card().classes("edh-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("vpn_key", color="secondary")
                    ui.label(k["name"]).classes("text-h6")
                    ui.label(f"{'default · ' if k['name'] == default else ''}{k['source']} · id {k['fingerprint']}"
                             ).classes("edh-muted text-caption")
                with ui.row().classes("gap-1"):
                    if k["name"] != default:
                        async def make_default(n=k["name"]):
                            await call("key_default", name=n)
                            await refresh_key_list()
                            await refresh_keys_select()

                        ui.button("Make default", on_click=make_default).props("flat no-caps")

                    async def test(n=k["name"]):
                        r = await call("key_test", name=n)
                        if r:
                            ui.notify(r["msg"], type="positive" if r["ok"] else "negative")
                        await refresh_key_list()

                    ui.button("Test", icon="check_circle_outline", on_click=test).props("flat no-caps").tooltip(
                        f"check the key ({src.get('check_cost', '1 request')})")
                    if k["source"] == "stored":
                        async def remove(n=k["name"]):
                            await call("key_remove", name=n)
                            await refresh_key_list()
                            await refresh_keys_select()

                        ui.button(icon="delete_outline", on_click=lambda n=k["name"]: confirm(
                            f"Remove key {n}?", lambda n=n: remove(n))).props('flat round color=grey-6 aria-label="Remove key"').tooltip(
                            "Remove key")
            q = k.get("usage")
            if not q:
                ui.label("No monthly request quota: the Climate Data Store limits the size of each request and "
                         "queues them instead.").classes("edh-muted text-caption")
                return
            pct = q["used"] / k["quota"] if k["quota"] else 0
            with ui.row().classes("w-full items-center gap-6 no-wrap"):
                ui.circular_progress(value=min(1, pct), show_value=False, size="64px",
                                     color="negative" if pct > 0.9 else "warning" if pct > 0.7 else "secondary").props(
                    "thickness=0.2 track-color=grey-9")
                with ui.column().classes("gap-0"):
                    ui.label(f"{q['used']:,} / {k['quota']:,}").classes("edh-kpi-value")
                    ui.label(f"requests used in {q['month']} · this app {q['app']:,} · "
                             + (f"calibrated {q['calibrated_at'][:16]}" if q["calibrated_at"] else "not calibrated")
                             + f" · resets {q['resets'][:10]}").classes("edh-muted text-caption")
            with ui.row().classes("items-center gap-3"):
                ui.button("Check on Earth Data Hub", icon="open_in_new",
                          on_click=lambda: ui.navigate.to(QUOTA_URL, new_tab=True)).props(
                    "unelevated no-caps").classes("edh-grad-btn")
                used_in = ui.number("Used, as shown on the EDH quota page", min=0).classes("w-80")

                async def calibrate(u=used_in, n=k["name"]):
                    if u.value is None or u.value < 0:
                        ui.notify("Enter the number shown on the Earth Data Hub quota page", type="warning")
                        return
                    if await call("key_calibrate", name=n, used=int(u.value)) is None:
                        return
                    await refresh_key_list()
                    await refresh_quota()
                    ui.notify("calibrated")

                ui.button("Calibrate", on_click=calibrate).props("outline no-caps color=secondary")
                quota_in = ui.number("Monthly quota", value=k["quota"], min=1).classes("w-44")

                async def set_quota(qi=quota_in, n=k["name"]):
                    if qi.value is None or qi.value < 1:
                        ui.notify("Monthly quota must be a positive number", type="warning")
                        return
                    await call("key_set_quota", name=n, quota_=int(qi.value))
                    await refresh_key_list()
                    await refresh_quota()

                ui.button("Set quota", on_click=set_quota).props("outline no-caps color=secondary")

    # ------------------------------------------------------------------ start
    async def start():
        render_flow()
        await refresh_keys_select()
        await load_collections()

    ui.timer(0.1, start, once=True)
    ui.timer(30, refresh_quota)

    # ------------------------------------------------------------------ guided tour
    def run_tour():
        where = "this computer" if local else getattr(be, "name", "") or "the server"
        hint = ("Replay this tour any time from <b>Tour</b> in the top bar." if embedded else
                "Replay this tour any time from the <b>Tour</b> button in the header.")
        tour.run("app", tour.app_steps(where, SAMPLE_REQUESTS, hint))

    tour.install(lambda name, result: tour.set_stage(tour.DONE) if name == "app" else None)
    ui.on(tour.START_EVENT, lambda: run_tour())  # the connect page's Tour button, from the top bar
    ui.add_body_html(f"<script>window.edhRequestTour = () => emitEvent('{tour.START_EVENT}');</script>")
    stage = tour.stage()
    if stage == tour.APP or (stage == tour.WELCOME and not embedded):
        def first_tour():
            if stage == tour.APP:  # the connect part was followed: carry on straight away
                tour.set_stage(tour.DONE)
                run_tour()
            else:  # edh-dl serve / connect: no connect page, so offer the tour here
                tour.welcome(on_start=run_tour, on_skip=lambda: tour.set_stage(tour.DONE), connect=False,
                             replay="the Tour button in the header")

        later(first_tour, 0.8)


def _mb(mb: float) -> str:
    return human_bytes(mb * 1e6)


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _ago(ts: float) -> str:
    import time as _t

    return human_duration(max(0, _t.time() - ts)) + " ago"


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


_WATCHED = ("start", "end", "start_hour", "end_hour", "freq", "hours", "tz_offset", "mode", "west", "south", "east", "north", "points",
            "lon_convention", "directory", "name", "out_format", "target_mode", "target_store", "extend_direction", "keepbits", "lossless", "chunk_layout",
            "chunk_time", "chunk_lat", "chunk_lon", "chunk_points", "dtype", "compressor", "clevel", "shuffle",
            "sharding", "shard_auto", "shard_time", "shard_space", "extra_attrs", "connections",
            "quota_safety", "wait_for_quota", "memory_gb", "key", "grid", "grid_custom", "max_inflight", "pack",
            "max_request_gb")


def _watch_form(f, ctx, schedule_plan, render_stats, draw_rect):
    snap = {k: f[k] for k in _WATCHED}
    last = ctx.get("_snap")
    if snap != last:
        ctx["_snap"] = snap
        if last is not None:
            if snap["freq"] != last["freq"]:
                render_stats()
            if any(snap[k] != last[k] for k in ("mode", "west", "south", "east", "north", "points")):
                draw_rect()
            schedule_plan()
        else:
            draw_rect()


# ------------------------------------------------------------------ server

def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket() as so:
        so.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            so.bind((host, port))
            return True
        except OSError:
            return False


def _stdio_threads(port: int):
    """--stdio (started by the launcher over SSH): say when the UI accepts connections, and exit when the
    launcher goes away (the heartbeat write fails once the SSH channel is closed)."""
    import os
    import socket
    import threading
    import time

    from ..launcher import ALIVE, READY

    def ready():
        for _ in range(600):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                print(READY, flush=True)
                return
            except OSError:
                time.sleep(0.1)

    def alive():
        while True:
            time.sleep(2)
            try:
                print(ALIVE, flush=True)
            except Exception:  # noqa: BLE001 - broken pipe: the launcher is gone
                os._exit(0)

    threading.Thread(target=ready, daemon=True).start()
    threading.Thread(target=alive, daemon=True).start()


def serve(host: str = "127.0.0.1", port: int = 8765, token: bool = True, stdio: bool = False,
          open_browser: bool | None = None):
    import os
    import sys

    from ..launcher import BUSY, default_host, remote_cmd

    if stdio:
        tok = sys.stdin.readline().strip() or secrets.token_urlsafe(18)
    else:
        tok = secrets.token_urlsafe(18) if token else None
    if not _port_free(host, port):
        if stdio:
            print(BUSY, flush=True)
            sys.exit(3)
        raise SystemExit(f"port {port} is in use: choose another with --port")
    if tok:
        app.add_middleware(TokenMiddleware, token=tok)

    @ui.page("/")
    def index():
        build_page(Local())

    path = "/" + (f"?token={tok}" if tok else "")
    url = f"http://localhost:{port}{path}"
    over_ssh = bool(os.environ.get("SSH_CONNECTION"))
    show = False
    if stdio:
        _stdio_threads(port)
    else:
        print("EDH Downloader web UI")
        print(f"  {url}")
        if over_ssh:
            h = default_host()
            print("\n  You are on SSH. Next time skip the port forwarding: run the launcher on your computer,")
            print("  it opens the tunnel and the browser for you. Get it once with:")
            print(f"    macOS/Linux:  ssh {h} '{remote_cmd()} launcher sh' > edh-connect && chmod +x edh-connect")
            print("                  then:  ./edh-connect")
            print(f"    Windows:      ssh {h} \"{remote_cmd()} launcher ps1\" > edh-connect.ps1")
            print("                  then:  powershell -ExecutionPolicy Bypass -File edh-connect.ps1")
            print(f"  For now: ssh -L {port}:localhost:{port} {h}   on your computer, then open the link above.")
        else:
            show = path if open_browser is not False else False
        print("  Jobs keep running after you stop this server (Ctrl+C) or disconnect.", flush=True)
    ui.run(host=host, port=port, title="EDH Downloader", show=show, reload=False, favicon="🌍", dark=True,
           show_welcome_message=False, storage_secret=secrets.token_hex(16), uvicorn_logging_level="warning")
