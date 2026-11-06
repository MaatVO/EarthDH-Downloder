"""Web UI (NiceGUI / Quasar Material). Served on localhost; reach it with ``ssh -L 8765:localhost:8765``.

The UI only builds job specs and controls jobs; downloads run in detached worker processes, so closing the
browser, stopping ``serve`` or dropping the SSH connection does not affect them.
"""

from datetime import date
import asyncio
import json
import math
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
from ..job import COMPRESSORS, FREQS, STATS, AreaSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec
from ..util import human_bytes, human_duration
from . import structure
from .auth import TokenMiddleware
from .theme import PINK, apply_theme

STATUS_COLOR = {"running": "primary", "starting": "primary", "exporting": "info", "completed": "positive",
                "failed": "negative", "interrupted": "warning", "paused": "grey-6", "paused_quota": "warning",
                "waiting_quota": "warning", "cancelled": "grey-6", "created": "grey-6"}
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
def section(n, title: str, subtitle: str = "", wide: bool = False):
    with ui.card().classes("edh-card w-full min-w-0" + (" col-span-2 max-xl:col-span-1" if wide else "")):
        with ui.row().classes("items-center gap-3 no-wrap w-full"):
            ui.html(f'<div class="edh-step">{n}</div>', sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label(title).classes("edh-title")
                if subtitle:
                    ui.label(subtitle).classes("edh-muted text-body2")
        with ui.column().classes("w-full gap-3 pt-1"):
            yield


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
    return
    for txt in ():
        pass


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
            ui.button(icon="close", on_click=dlg.close).props("flat round")
        with ui.scroll_area().classes("w-full h-[72vh]"):
            structure_panel(d, {}, max_vars=None, sticky=True)
    dlg.on("hide", lambda: dlg.delete())
    dlg.open()


# ------------------------------------------------------------------ page

def build_page(be, local: bool = False):
    """The whole UI for one backend ``be`` (this computer or a server over SSH): it never touches keys, datasets,
    jobs or files itself, only asks ``be`` (see service.py)."""
    apply_theme()
    f = {  # form model
        "collection": "era5", "dataset": "era5-single-levels-atmosphere", "variables": [], "levels": [],
        "start": "2020-01-01", "end": "2020-01-31", "start_hour": 0, "end_hour": 23, "freq": "none", "hours": 6, "tz_offset": 0, "stats": {},
        "mode": "bbox", "west": 10.3, "south": 45.6, "east": 12.6, "north": 47.1, "points": "",
        "lon_convention": "-180..180",
        "directory": be.info["default_dir"], "name": "era5_subset", "out_format": "icechunk",
        "lossless": True, "keepbits": 13,
        "chunk_layout": None,  # None = follow the recommendation
        "chunk_time": None, "chunk_lat": None, "chunk_lon": None, "chunk_points": None,
        "dtype": "float32", "compressor": "blosc-zstd", "clevel": 5, "shuffle": "shuffle", "sharding": False,
        "shard_auto": True, "shard_time": 1, "shard_space": 4, "extra_attrs": "",
        "key": None, "connections": 8, "spool_dir": "", "max_spool_gb": 20.0, "min_free_gb": 10.0,
        "quota_safety": 0.9, "wait_for_quota": True, "memory_gb": 16.0,
    }
    ctx = {"keys": {"default": None, "keys": []}, "plan_seq": 0, "ds": None, "info": None, "plan": None, "plan_task": None, "rec": ("auto", "")}

    # ================================================================ header
    with ui.header().classes("edh-header items-center justify-between px-6 py-2"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.html('<div class="edh-logo"><span class="material-icons">public</span></div>', sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label("EDH Downloader").classes("edh-brand")
                ui.label(f"Earth Data Hub datasets → Icechunk / Zarr · v{__version__}").classes("edh-muted text-caption")
        with ui.tabs().props("no-caps dense indicator-color=transparent active-class=edh-tab-active"
                             ).classes("edh-tabs") as tabs:
            t_new = ui.tab("New job", icon="add_circle_outline")
            t_jobs = ui.tab("Jobs", icon="dns")
            t_keys = ui.tab("Keys", icon="vpn_key")
        with ui.row().classes("items-center gap-3 no-wrap"):
            with ui.column().classes("gap-1 items-end"):
                with ui.row().classes("items-center gap-1 no-wrap"):
                    quota_label = ui.label().classes("text-caption")
                    with ui.link(target=QUOTA_URL, new_tab=True).classes("edh-quota-link"):
                        ui.icon("open_in_new", size="14px")
                        ui.tooltip("Your real usage is on the Earth Data Hub quota page: the app only counts its "
                                   "own requests. Enter the number there in Keys → Calibrate.").classes("edh-tooltip")
                quota_bar = ui.linear_progress(value=0, show_value=False).props("size=6px").classes("edh-grad-bar w-48")
            key_select = ui.select({}, label="API key").props("dense outlined options-dense").classes("w-40")

    def later(fn, delay=0.01):
        ui.timer(delay, fn, once=True)

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
        key_select.options = {k["name"]: k["name"] for k in d["keys"]}
        if f["key"] not in key_select.options:
            f["key"] = d["default"]
        key_select.value = f["key"]
        key_select.update()
        await refresh_quota()

    async def refresh_quota():
        try:
            k = await be.call("key_status", name=f["key"])
        except BackendError:
            k = None
        if not k:
            quota_label.text = "no API key: add one in Keys"
            quota_bar.value = 0
            return
        q = k["usage"]
        pct = q["used"] / k["quota"] if k["quota"] else 0
        quota_label.text = f"quota {q['used']:,} / {k['quota']:,} · resets {q['resets'][:10]}"
        quota_bar.value = min(1.0, pct)

    key_select.on_value_change(lambda e: (f.update(key=e.value), later(refresh_quota), schedule_plan()))

    dialog_host = ui.element("div")  # pop-ups live here, outside the cards that are rebuilt on refresh

    with ui.tab_panels(tabs, value=t_new).classes("edh-panels w-full"):
        # ================================================================ NEW JOB
        with ui.tab_panel(t_new).classes("px-0"):
            with ui.row().classes("w-full no-wrap items-start gap-5 max-lg:flex-wrap"):
                with ui.element("div").classes("grid grid-cols-2 max-xl:grid-cols-1 gap-5 grow min-w-0 content-start"):
                    with ui.column().classes("gap-5 min-w-0 w-full"):
                        # ---------- dataset
                        with section(1, "Dataset"):
                            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                                coll_select = ui.select({"era5": "ERA5"}, value="era5", label="Collection").classes("w-56")
                                ds_select = ui.select({}, label="Dataset", with_input=True).classes("grow")
                                ui.button(icon="refresh", on_click=lambda: load_dataset(refresh=True)).props(
                                    "flat round").tooltip("reload the structure from Earth Data Hub (a few requests)")
                            ds_info = ui.markdown().classes("text-body2 edh-muted")
                            ds_chips = ui.row().classes("gap-2")

                        # ---------- variables
                        with section(2, "Variables and levels"):
                            with ui.row().classes("w-full items-center justify-between"):
                                var_search = ui.input(placeholder="Search variables…").props("dense outlined clearable").classes("w-80")
                                with var_search.add_slot("prepend"):
                                    ui.icon("search")
                                sel_label = ui.label().classes("edh-muted text-caption")
                            sel_chips = ui.row().classes("gap-2 items-center w-full edh-sel-box").style("position:sticky;top:64px;z-index:5;padding:8px 12px")
                            var_table = ui.table(rows=[], columns=[
                                {"name": "name", "label": "Name", "field": "name", "sortable": True, "align": "left"},
                                {"name": "long_name", "label": "Description", "field": "long_name", "align": "left"},
                                {"name": "units", "label": "Units", "field": "units", "align": "left"},
                                {"name": "kind", "label": "Dimensions", "field": "kind", "align": "left"},
                            ], row_key="name", selection="multiple", pagination=8).props("flat dense").classes("edh-table w-full")
                            var_search.bind_value(var_table, "filter")
                            with ui.row().classes("items-center gap-2") as level_row:
                                level_select = ui.select([], multiple=True, label="Pressure levels (hPa)").props(
                                    "use-chips dense outlined").classes("min-w-[28rem]")
                                ui.button("All", on_click=lambda: level_select.set_value(list(level_select.options))).props("flat dense")
                                ui.button("None", on_click=lambda: level_select.set_value([])).props("flat dense")
                            level_row.set_visibility(False)

                    with ui.column().classes("gap-5 min-w-0 w-full"):
                        # ---------- time
                        with section(3, "Time"):
                            with ui.row().classes("items-center gap-4"):
                                for key_, label, hkey in (("start", "First day", "start_hour"), ("end", "Last day", "end_hour")):
                                    with ui.input(label).bind_value(f, key_).classes("w-44") as inp:
                                        with ui.menu().props("no-parent-event") as menu:
                                            ui.date().bind_value(inp)
                                        with inp.add_slot("append"):
                                            ui.icon("event").on("click", menu.open).classes("cursor-pointer")
                                    with_tip(ui.number("from hour" if hkey == "start_hour" else "to hour (incl.)",
                                                       min=0, max=23, step=1, precision=0).bind_value(f, hkey).classes("w-32"),
                                             "Hour of day (0-23, local time with the UTC offset below). First day starts "
                                             "at this hour; last day ends at the end of this hour.")
                                    if key_ == "end":
                                        ui.button("Today", on_click=lambda: f.update(end=date.today().isoformat())).props(
                                            "flat dense no-caps").tooltip("set the last day to today")
                                time_range_label = ui.label().classes("edh-muted text-caption")
                            with ui.row().classes("items-center gap-4"):
                                with_tip(ui.select(FREQS, label="Time step").bind_value(f, "freq").classes("w-48"),
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
                        with section(4, "Area"):
                            with ui.row().classes("items-center gap-5"):
                                ui.toggle({"global": "Global", "bbox": "Box", "points": "Points"}).bind_value(
                                    f, "mode").props("unelevated toggle-color=primary no-caps").classes("edh-toggle")
                                with_tip(ui.select({"-180..180": "−180 … 180", "0..360": "0 … 360"},
                                                   label="Output longitudes").bind_value(f, "lon_convention").classes("w-48"),
                                         "How longitudes are numbered in the output. −180…180: west is negative, map "
                                         "centred on Greenwich (usual for Europe/Africa). 0…360: ERA5's native order, "
                                         "centred on the Pacific; use it for boxes crossing the 180° meridian so "
                                         "longitudes stay increasing.")
                            with ui.row().classes("w-full items-stretch gap-5 no-wrap max-md:flex-wrap"):
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
                                with ui.element("div").classes("relative grow h-72 min-w-[14rem]"):
                                    area_map = ui.leaflet(center=(46.0, 11.0), zoom=4, draw_control=draw,
                                                          hide_drawn_items=True).classes("edh-map w-full h-full")
                                    ui.button("Center", icon="center_focus_strong", on_click=lambda: center_map()).props(
                                        "dense unelevated no-caps size=sm").classes(
                                        "edh-map-btn absolute bottom-3 right-3 z-[1000]").tooltip(
                                        "zoom to the selected area")
                                area_map.clear_layers()
                                area_map.tile_layer(url_template=OSM_TILES, options={
                                    "attribution": "&copy; OpenStreetMap contributors", "maxZoom": 19})

                    # ---------- output
                    with section(5, "Output", wide=True):
                        with ui.row().classes("w-full items-center gap-4 no-wrap max-md:flex-wrap"):
                            ui.input("Directory (on the server)").bind_value(f, "directory").classes("grow")
                            ui.input("Name").bind_value(f, "name").classes("w-64")
                            with_tip(ui.select({"zarr": "Zarr", "both": "Zarr + Icechunk", "icechunk": "Icechunk only"},
                                               label="Format").bind_value(f, "out_format").classes("w-48"),
                                     "Downloads are written to an Icechunk repository first: it makes jobs "
                                     "resumable. Zarr: export a plain Zarr v3 at the end and delete the repository. "
                                     "Zarr + Icechunk: keep both (the repository has the commit history). Icechunk "
                                     "only: skip the export.")
                        out_paths = ui.label().classes("edh-mono text-caption edh-muted")

                        # -- dataset structure: drawing + tree, updated live
                        with ui.column().classes("w-full gap-3 edh-struct"):
                            with ui.row().classes("w-full items-center justify-between gap-3"):
                                ui.label("Dataset structure").classes("edh-subtitle")
                                struct_kpis = ui.row().classes("items-center gap-2")
                            struct_empty = ui.label("Select a dataset and variables.").classes("edh-muted")
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
                        enc_box.bind_visibility_from(f, "out_format", lambda v: v != "icechunk")

                # ---------- summary (sticky)
                with ui.card().classes("edh-card edh-summary w-[26rem] max-lg:w-full sticky shrink-0 edh-scroll").style(
                        "top:80px;max-height:calc(100vh - 100px);overflow-y:auto"):
                    ui.label("Summary").classes("edh-title")
                    summary = ui.column().classes("gap-3 w-full")
                    with ui.row().classes("w-full items-center gap-3 no-wrap"):
                        with_tip(ui.number("Connections", min=1, max=64, step=1).bind_value(
                            f, "connections").classes("w-32"),
                            "Chunks downloaded at the same time. Faster up to what your line and the server "
                            "allow; the sample suggests a value. Can be changed while the job runs.")
                        conn_hint = ui.column().classes("gap-0 grow")
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        sample_btn = ui.button(f"Measure on a sample · {SAMPLE_REQUESTS} requests", icon="science").props(
                            "outline no-caps color=secondary").classes("grow")
                        tip(f"Uses quota: downloads {SAMPLE_REQUESTS} chunks of this selection ({SAMPLE_REQUESTS} requests) to "
                            "measure the real download size, your line speed and the Zarr size with your encoding. "
                            "Then suggests the number of connections.")
                    sample_status = ui.label().classes("edh-muted text-caption")
                    with ui.expansion("Download settings", icon="tune").classes("edh-expansion w-full"):
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
                            with ui.row().classes("items-center gap-3 w-full no-wrap"):
                                ui.label("Quota limit").classes("text-body2")
                                ui.slider(min=0.1, max=1.0, step=0.05).bind_value(f, "quota_safety").props(
                                    "label color=secondary").classes("grow")
                                ui.label().bind_text_from(f, "quota_safety", lambda v: f"{100 * (v or 0):.0f}%")
                                tip("The job stops at this share of your monthly request quota.")
                            with ui.row().classes("items-center gap-1 no-wrap"):
                                ui.switch("Wait for quota reset").bind_value(f, "wait_for_quota")
                                tip("At the limit: wait for the reset on the 1st and continue. Off: pause the job.")
                    launch_btn = ui.button("Launch job", icon="rocket_launch").props("unelevated no-caps size=lg").classes(
                        "edh-grad-btn w-full")
                    ui.button("Download job.json", icon="file_download", on_click=lambda: save_job_file()).props(
                        "outline no-caps color=white").classes("w-full")
                    ui.label("Runs in the background: you can close this page.").classes("edh-muted text-caption")

        # ================================================================ JOBS
        with ui.tab_panel(t_jobs).classes("px-0"):
            jobs_box = ui.column().classes("w-full gap-4")

        # ================================================================ KEYS
        with ui.tab_panel(t_keys).classes("px-0"):
            keys_box = ui.column().classes("w-full gap-4 max-w-5xl mx-auto")

    # ------------------------------------------------------------------ dataset loading
    async def load_collections():
        try:
            cols = await be.call("collections")
            coll_select.set_options({c["id"]: c["title"] for c in cols},
                                    value=f["collection"] if f["collection"] in {c["id"] for c in cols} else "era5")
        except Exception as e:
            ui.notify(f"could not read the catalogue: {e}", type="negative")
        await load_items()

    async def load_items():
        try:
            items = await be.call("datasets", collection=f["collection"])
        except Exception as e:
            ui.notify(f"could not list datasets: {e}", type="negative")
            return
        ctx["items"] = {i["id"]: i for i in items}
        ds_select.options = {i["id"]: f"{i['title']}  ·  {i['id']}" for i in items if not i.get("deprecated")}
        if f["dataset"] not in ds_select.options and ds_select.options:
            f["dataset"] = next(iter(ds_select.options))
        ds_select.value = f["dataset"]
        ds_select.update()
        await load_dataset()

    async def load_dataset(refresh=False):
        info = ctx.get("items", {}).get(f["dataset"])
        if not info:
            return
        if not f["key"]:
            ds_info.content = "**Add an API key** in the Keys tab to read the dataset structure."
            return
        ds_info.content = "_loading dataset structure…_"
        try:
            ds = await be.call("dataset_load", info=info, key=f["key"], refresh=refresh)
        except BackendError as e:
            ds_info.content = f"**Could not load dataset:** {e}"
            return
        ds["vars"] = {v["name"]: v for v in ds["variables"]}
        ctx["ds"], ctx["info"] = ds, info
        s = ds["summary"]
        t = s["time"] or ["—", "—"]
        ds_info.content = info.get("abstract", "")[:420]
        ds_chips.clear()
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

    async def on_collection(e):
        if e.value and e.value != f["collection"]:
            f["collection"] = e.value
            await load_items()

    async def on_dataset(e):
        if e.value and (e.value != f["dataset"] or not ctx["info"] or ctx["info"]["id"] != e.value):
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
        schedule_plan()

    def drop_var(n):
        var_table.selected = [r for r in var_table.selected if r["name"] != n]
        var_table.update()
        on_vars_changed()

    var_table.on("selection", lambda e: on_vars_changed())
    level_select.on_value_change(lambda e: (f.update(levels=e.value or []), schedule_plan()))

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
                cur = f["stats"].get(name) or [v["default_stat"]]
                with ui.row().classes("items-center gap-3"):
                    ui.label(name).classes("w-20 edh-mono")
                    out = ui.label().classes("edh-muted text-caption edh-mono")

                    def show(vals, n=name, out=out):
                        vals = vals or []
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
    def layout_choice() -> str:
        if f["out_format"] == "icechunk":
            return "auto"
        return f["chunk_layout"] or ctx["rec"][0]

    def build_spec() -> JobSpec:
        info = ctx.get("info") or {}
        pts = parse_points(f["points"] or "") if f["mode"] == "points" else []
        custom = layout_choice() == "custom"
        return JobSpec(
            dataset=f["dataset"], collection=f["collection"], dataset_url=info.get("url"),
            variables=list(f["variables"]), levels=list(f["levels"]) or None,
            time=TimeSpec(start=f["start"], end=f["end"], start_hour=int(f["start_hour"] or 0),
                          end_hour=int(23 if f["end_hour"] is None else f["end_hour"]), freq=f["freq"], hours=int(f["hours"] or 1),
                          tz_offset=int(f["tz_offset"] or 0),
                          stats={k: v for k, v in f["stats"].items() if k in f["variables"] and v}),
            area=AreaSpec(mode=f["mode"], west=float(f["west"] or 0), south=float(f["south"] or 0),
                          east=float(f["east"] or 0), north=float(f["north"] or 0), points=pts,
                          lon_convention=f["lon_convention"]),
            output=OutputSpec(
                directory=f["directory"], name=f["name"], export_zarr=f["out_format"] != "icechunk",
                keep_icechunk=f["out_format"] != "zarr",
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
        ctx["plan_task"] = ui.timer(0.4, update_plan, once=True)

    async def update_plan():
        ds = ctx["ds"]
        ctx["plan_seq"] += 1
        seq = ctx["plan_seq"]
        summary.clear()
        with summary:
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
        ctx["rec"] = tuple(res["rec"])
        ctx["plan"] = res
        render_summary(res)
        render_chunking(res)
        render_encoding(res)
        out_paths.text = "→ " + "   ·   ".join(p for p in (res["paths"]["zarr"], res["paths"]["icechunk"]) if p)
        launch_btn.enable()

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
            kv("Chunks", chunking.TITLES[plan["layout"]] + (" (recommended)" if plan["layout"] == ctx["rec"][0] else ""))
            for w in s["warnings"]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("warning_amber", color="warning", size="xs")
                    ui.label(w).classes("text-caption text-warning")

    def render_chunking(plan):
        rows = plan["rows"]
        rec_layout, reason = ctx["rec"]
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
        f["chunk_layout"] = None if lay == ctx["rec"][0] else lay
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
    struct_states: dict = {}  # job id -> {"var": ..., "src": ...}

    async def refresh_jobs():
        jobs = await call("jobs_list")
        if jobs is None:
            return
        for s_ in jobs:  # read the stores off the event loop (cached)
            if s_["id"] in open_struct and (src := _struct_src(s_)):
                try:
                    s_["_struct"] = await be.call("store_structure", path=src)
                except BackendError as e:  # shown in the card
                    s_["_struct_err"] = str(e)
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
                    cards[s_["id"]] = (None, ui.card().classes("edh-card w-full"))
        for s_ in jobs:
            sig = json.dumps({k: v for k, v in s_.items() if k != "_struct"}, sort_keys=True, default=str) + \
                str(s_["id"] in open_struct) + json.dumps(struct_states.get(s_["id"], {}).get("src"))
            old, card = cards[s_["id"]]
            if sig != old or "_struct" in s_ and old is None:
                card.clear()
                with card:
                    _job_card(s_)
                cards[s_["id"]] = (sig, card)

    def _struct_src(s_):
        outs = s_.get("outputs_exist") or []
        if s_.get("status") != "completed" or not outs:
            return None
        want = struct_states.get(s_["id"], {}).get("src")
        return want if want in outs else next((o for o in outs if o.endswith(".zarr")), outs[0])

    def toggle_struct(jid):
        open_struct.symmetric_difference_update({jid})
        ui.timer(0.01, refresh_jobs, once=True)

    def set_struct_src(jid, src):
        struct_states.setdefault(jid, {})["src"] = src
        ui.timer(0.01, refresh_jobs, once=True)

    def _job_card(s):
        st = s.get("status", "?")
        spec = s.get("spec", {})
        total = s.get("units_total") or 0
        done = len(s.get("units_done", []))
        if True:
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(s["id"]).classes("text-subtitle1 edh-mono")
                    tt = spec.get("time", {})
                    ui.label(f"{spec.get('dataset')} · {', '.join(spec.get('variables', []))} · {tt.get('start')} → "
                             f"{tt.get('end')} · {FREQS.get(tt.get('freq'), tt.get('freq'))}").classes("edh-muted text-caption")
                ui.label(st.replace("_", " ").capitalize()).classes(f"text-weight-medium text-{STATUS_COLOR.get(st, 'grey-6')}")
            ui.linear_progress(value=done / total if total else 0, show_value=False).props(
                "size=8px").classes("edh-grad-bar")
            with ui.row().classes("w-full gap-3 items-stretch"):
                kpi("Units", f"{done}/{total}")
                req_total = s.get("requests_total")
                req_done = ((s.get("requests_job", 0) or 0) + (s.get("requests_run", 0) or 0)
                            if st in ACTIVE else s.get("requests_job", 0) or 0)
                if req_total:
                    kpi("Requests", f"{req_done:,}", f"of {req_total:,}")
                if st in ACTIVE:
                    kpi("Speed", f"{human_bytes(s.get('rate_bps') or 0)}/s", f"{s.get('req_rate') or 0:.1f} req/s")
                    kpi("ETA", human_duration(s.get("eta_s")))
                    kpi("Temporary", human_bytes(s.get("spool_bytes") or 0))
                ui.label(s.get("message") or "").classes("edh-muted text-caption grow self-center")
            if s.get("error"):
                ui.label(s["error"]).classes("text-negative text-caption")
            if s.get("outputs"):
                ui.label("  ·  ".join(s["outputs"])).classes("edh-mono text-caption edh-muted")
            with ui.row().classes("gap-2 items-center"):
                d = s["id"]
                if s.get("alive"):
                    conn = ui.number("Connections", value=s.get("connections") or spec.get("download", {}).get("connections"),
                                     min=1, max=64).props("dense outlined").classes("w-32")
                    ui.button("Apply", on_click=lambda c=conn, d=d: act(
                        "job_connections", "connections updated", jid=d, n=int(c.value))).props("flat no-caps")
                    ui.button("Pause", icon="pause", on_click=lambda d=d: act(
                        "job_stop", "pausing after the current unit", jid=d, action="pause")).props(
                        "outline no-caps color=white")
                    ui.button("Cancel", icon="stop", on_click=lambda d=d: confirm(
                        "Cancel this job? Downloaded units stay in the Icechunk repository.",
                        lambda d=d: act("job_stop", "cancelling", jid=d, action="cancel"))).props("flat no-caps color=negative")
                elif st != "completed":
                    ui.button("Resume", icon="play_arrow", on_click=lambda d=d: _resume(d)).props(
                        "unelevated no-caps").classes("edh-grad-btn")
                src = _struct_src(s)
                if src:
                    shown = s["id"] in open_struct
                    ui.button("Structure", icon="account_tree", on_click=lambda j=s["id"]: toggle_struct(j)).props(
                        f"{'unelevated color=primary' if shown else 'flat'} no-caps")
                ui.button("Log", icon="article", on_click=lambda d=d: show_log(d)).props("flat no-caps")
                ui.button("Clone", icon="content_copy", on_click=lambda sp=spec: clone(sp)).props("flat no-caps")
                if not s.get("alive"):
                    ui.button(icon="delete_outline", on_click=lambda d=d: confirm(
                        "Delete the job record and its temporary files? Outputs are kept.",
                        lambda d=d: act("job_delete", None, jid=d))).props("flat round color=grey-6")
            if src and s["id"] in open_struct:
                _job_structure(s, src)

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
            ui.label(src).classes("edh-mono text-caption edh-muted")
            if "_struct_err" in s:
                ui.label(f"Cannot read the store: {s['_struct_err']}").classes("text-negative text-caption")
            elif "_struct" in s:
                d = s["_struct"]
                structure_panel(d, struct_states.setdefault(jid, {}),
                                see_all=lambda d=d, src=src: structure_dialog(dialog_host, d, src.rstrip("/").rsplit("/", 1)[-1]))
            else:
                ui.spinner(size="md")

    async def act(method, msg=None, then=None, **kw):
        """One backend request from a button; notify the outcome and refresh the jobs list."""
        if await call(method, **kw) is not None and msg:
            ui.notify(msg, type="positive")
        await refresh_jobs()

    def _resume(d):
        return act("job_resume", "job resumed", jid=d)

    def confirm(text, action):
        with dialog_host, ui.dialog() as dlg, ui.card().classes("edh-card"):
            ui.label(text)

            async def yes():
                dlg.close()
                r = action()
                if asyncio.iscoroutine(r):
                    await r

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("No", on_click=dlg.close).props("flat no-caps")
                ui.button("Yes", on_click=lambda: yes()).props("unelevated no-caps").classes("edh-grad-btn")
        dlg.open()

    def show_log(jid):
        with dialog_host, ui.dialog().props("maximized") as dlg, ui.card().classes("edh-card w-full h-full"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Log · {jid}").classes("edh-title")
                ui.button(icon="close", on_click=dlg.close).props("flat round")
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
        coll_select.value = spec_.collection
        ds_select.value = spec_.dataset
        tabs.set_value(t_new)
        ui.timer(0.1, load_items, once=True)

    ui.timer(3.0, lambda: refresh_jobs() if tabs.value in (t_jobs, "Jobs") else None)
    tabs.on_value_change(lambda e: refresh_jobs() if e.value in (t_jobs, "Jobs") else
                         refresh_key_list() if e.value in (t_keys, "Keys") else None)

    # ------------------------------------------------------------------ keys tab
    async def refresh_key_list():
        d = await call("keys_list")
        if d is None:
            return
        ctx["keys"] = d
        default = d["default"]
        keys_box.clear()
        with keys_box:
            with section("+", "Add an API key"):
                ui.markdown("Get a key on the [Earth Data Hub API keys page](https://earthdatahub.destine.eu/"
                            "quota-api-keys). It is stored on the machine that runs the downloads, in "
                            "`~/.config/edh-dl/keys.json` (only you can read it); `~/.netrc` is used automatically."
                            ).classes("text-body2 edh-muted")
                with ui.row().classes("items-center gap-3 w-full no-wrap max-md:flex-wrap"):
                    name_in = ui.input("Name", placeholder="personal").classes("w-44")
                    key_in = ui.input("API key", password=True, password_toggle_button=True).classes("grow")
                    q_in = ui.number("Monthly quota", value=DEFAULT_QUOTA, min=1).classes("w-44")
                    chk = ui.switch("Check it (1 request)", value=True)

                    async def add():
                        r = await call("key_add", name=name_in.value or "", secret=key_in.value or "",
                                       quota_=int(q_in.value), check=bool(chk.value))
                        if r is None:
                            return
                        if not r["ok"]:
                            ui.notify(r["msg"], type="negative")
                            return
                        ui.notify(f"saved key {r['name']}", type="positive")
                        await refresh_key_list()
                        await refresh_keys_select()

                    ui.button("Add", icon="add", on_click=add).props("unelevated no-caps").classes("edh-grad-btn")
            for k in d["keys"]:
                q = k["usage"]
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

                            ui.button("Test", icon="check_circle_outline", on_click=test).props("flat no-caps")
                            if k["source"] == "stored":
                                async def remove(n=k["name"]):
                                    await call("key_remove", name=n)
                                    await refresh_key_list()
                                    await refresh_keys_select()

                                ui.button(icon="delete_outline", on_click=lambda n=k["name"]: confirm(
                                    f"Remove key {n}?", lambda n=n: remove(n))).props("flat round color=grey-6")
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
                            await call("key_calibrate", name=n, used=int(u.value or 0))
                            await refresh_key_list()
                            await refresh_quota()
                            ui.notify("calibrated")

                        ui.button("Calibrate", on_click=calibrate).props("outline no-caps color=secondary")
                        quota_in = ui.number("Monthly quota", value=k["quota"], min=1).classes("w-44")

                        async def set_quota(qi=quota_in, n=k["name"]):
                            await call("key_set_quota", name=n, quota_=int(qi.value))
                            await refresh_key_list()
                            await refresh_quota()

                        ui.button("Set quota", on_click=set_quota).props("outline no-caps color=secondary")
            ui.markdown("Earth Data Hub doesn't report your usage to API clients: the app counts its own requests. "
                        f"Open the [quota page]({QUOTA_URL}), then enter the number shown there in **Calibrate** "
                        "(includes requests from other tools), or the limit of your plan in **Set quota**."
                        ).classes("edh-muted text-caption")

    # ------------------------------------------------------------------ start
    async def start():
        await refresh_keys_select()
        await load_collections()

    ui.timer(0.1, start, once=True)
    ui.timer(30, refresh_quota)


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
            "lon_convention", "directory", "name", "out_format", "keepbits", "lossless", "chunk_layout",
            "chunk_time", "chunk_lat", "chunk_lon", "chunk_points", "dtype", "compressor", "clevel", "shuffle",
            "sharding", "shard_auto", "shard_time", "shard_space", "extra_attrs", "connections",
            "quota_safety", "wait_for_quota", "memory_gb", "key")


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
