"""The Climate Data Store catalogue: datasets, their request forms and their constraints. Public, no token.

Every dataset publishes ``form.json`` (the widgets of its download form) and ``constraints.json`` (the value
combinations that exist). ``describe`` turns a form into what the app needs: which fields are the variables,
levels, time and area (the app's own controls fill them), which are dataset options to show as they are, which
licences must be accepted, and how far the app can take the data (``tier``):

* ``grib``: gridded, delivered as GRIB (ERA5, ERA5-Land, CERRA, CARRA, seasonal...): the full pipeline;
* ``netcdf``: gridded files in NetCDF (satellite, projections, derived products...);
* ``timeseries``: one time series per point (``location`` field);
* ``table``: in-situ observations (tables, not grids).

Everything is cached for a day; offline (``EDH_DL_OFFLINE``) the cache is used even when old.
"""

import asyncio
import os
import time

from ...paths import cache_dir
from ...util import read_json, write_json_atomic
from . import api

TTL = 24 * 3600
LEVEL_FIELDS = ("pressure_level", "model_level", "height_level", "soil_level")
TIME_FIELDS = ("year", "month", "day", "time")
FORMAT_FIELDS = ("data_format", "format")
OWN_FIELDS = {"variable", *LEVEL_FIELDS, *TIME_FIELDS, "date", "area", "location", "licences",
              *FORMAT_FIELDS, "download_format"}
PRODUCT_TYPE = "Product type: "


async def _get_json(url: str):
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=True) as s:
        async with s.get(url, headers={"Accept": "application/json"}) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


def _cached(name: str, fetch, refresh: bool = False):
    path = cache_dir() / name
    d = read_json(path)
    fresh = d is not None and time.time() - d.get("t", 0) < TTL
    if d is not None and (os.environ.get("EDH_DL_OFFLINE") or (fresh and not refresh)):
        return d["v"]
    if os.environ.get("EDH_DL_OFFLINE"):
        raise RuntimeError(f"offline mode: {name} is not cached")
    try:
        v = asyncio.run(fetch())
    except Exception:
        if d is not None:  # offline: the old copy is better than nothing
            return d["v"]
        raise
    write_json_atomic(path, {"t": time.time(), "v": v})
    return v


# ------------------------------------------------------------------ datasets

def _summary(c: dict) -> dict:
    kw = c.get("keywords") or []
    interval = (((c.get("extent") or {}).get("temporal") or {}).get("interval") or [[None, None]])[0]
    bbox = (((c.get("extent") or {}).get("spatial") or {}).get("bbox") or [None])[0]
    thumb = ((c.get("assets") or {}).get("thumbnail") or {}).get("href")
    return {
        "id": c["id"],
        "title": c.get("title", c["id"]),
        "abstract": (c.get("description") or "")[:1200],
        "category": next((k[len(PRODUCT_TYPE):] for k in kw if k.startswith(PRODUCT_TYPE)), "Other"),
        "keywords": kw,
        "start": interval[0] if interval else None,
        "end": interval[1] if interval and len(interval) > 1 else None,
        "bbox": bbox,
        "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith("https://") else None,
        "updated": c.get("updated"),
        "doi": c.get("sci:doi"),
        "licence": c.get("license"),
        "disabled": c.get("cads:disabled_reason"),
    }


def datasets(refresh: bool = False) -> list[dict]:
    """Every CDS dataset, sorted by id."""
    async def fetch():
        d = await _get_json(f"{api()}/catalogue/v1/collections?limit=1000")
        return sorted((_summary(c) for c in d.get("collections", [])), key=lambda x: x["id"])

    return _cached("cds_collections.json", fetch, refresh)


def categories(refresh: bool = False) -> list[dict]:
    """The dataset groups (CDS 'product types'), largest first: what the app shows as collections."""
    counts: dict[str, int] = {}
    for d in datasets(refresh):
        counts[d["category"]] = counts.get(d["category"], 0) + 1
    return [{"id": k, "title": k, "count": n} for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def find(dataset_id: str) -> dict:
    for d in datasets():
        if d["id"] == dataset_id:
            return d
    raise KeyError(f"dataset {dataset_id!r} is not in the Climate Data Store catalogue")


def form(dataset_id: str, refresh: bool = False) -> list[dict]:
    async def fetch():
        import aiohttp

        try:
            f = await _get_json(f"{api()}/catalogue/v1/collections/{dataset_id}/form.json")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise ValueError(f"{dataset_id} has no download form on the Climate Data Store "
                                 "(it cannot be requested through the API)") from None
            raise
        if not isinstance(f, list):
            raise ValueError(f"{dataset_id} has no download form on the Climate Data Store")
        return [w for w in f if isinstance(w, dict)]

    return _cached(f"cds_form_{dataset_id}.json", fetch, refresh)


def constraints(dataset_id: str, refresh: bool = False) -> list[dict]:
    async def fetch():
        import aiohttp

        try:
            c = await _get_json(f"{api()}/catalogue/v1/collections/{dataset_id}/constraints.json")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:  # no constraints published: nothing is excluded
                return []
            raise
        return [e for e in c if isinstance(e, dict)] if isinstance(c, list) else []

    return _cached(f"cds_constraints_{dataset_id}.json", fetch, refresh)


# ------------------------------------------------------------------ forms

def _choices(w: dict) -> tuple[list[str], dict[str, str]]:
    det = w.get("details") or {}
    values = list(det.get("values") or [])
    labels = dict(det.get("labels") or {})
    for g in det.get("groups") or []:  # StringListArrayWidget: values in labelled groups
        values += [v for v in g.get("values", []) if v not in values]
        labels.update(g.get("labels") or {})
    return [str(v) for v in values], {str(k): str(v) for k, v in labels.items()}


def _option(w: dict) -> dict:
    values, labels = _choices(w)
    det = w.get("details") or {}
    default = det.get("default")
    return {
        "name": w["name"], "label": w.get("label") or w["name"], "help": w.get("help"),
        "type": w.get("type"), "required": bool(w.get("required")),
        "multiple": w.get("type") in ("StringListWidget", "StringListArrayWidget"),
        "values": values, "labels": labels,
        "groups": [{"label": g.get("label"), "values": [str(v) for v in g.get("values", [])]}
                   for g in det.get("groups") or []],
        "default": default if isinstance(default, list) else ([default] if default is not None else []),
    }


def _tier(widgets: dict, formats: list[str], summary: dict | None) -> str:
    if summary and summary.get("category") == "In-situ observations":
        return "table"
    if "location" in widgets:
        return "timeseries"
    if "grib" in formats:
        return "grib"
    return "netcdf"


def describe(dataset_id: str, refresh: bool = False) -> dict:
    """What the app needs from a dataset's form (see the module docstring)."""
    ws = {w["name"]: w for w in form(dataset_id, refresh) if w.get("name")}
    try:
        summary = find(dataset_id)
    except Exception:  # the form alone is enough to describe a dataset
        summary = None
    variables = []
    if "variable" in ws:
        values, labels = _choices(ws["variable"])
        variables = [{"name": v, "label": labels.get(v, v)} for v in values]
    level = next((f for f in LEVEL_FIELDS if f in ws), None)
    levels = None
    if level:
        values, labels = _choices(ws[level])
        levels = {"field": level, "values": values, "labels": labels}
    time_fields = [f for f in TIME_FIELDS if f in ws]
    kind = ("date" if "date" in ws else "ymd" if {"year", "month", "day"} <= set(ws)
            else "ym" if {"year", "month"} <= set(ws) else "y" if "year" in ws else "none")
    tm = {"kind": kind, "fields": time_fields + (["date"] if "date" in ws else []),
          "times": _choices(ws["time"])[0] if "time" in ws else None,
          "years": _choices(ws["year"])[0] if "year" in ws else None,
          "date_range": {k: (ws["date"].get("details") or {}).get(k) for k in ("minStart", "maxEnd")}
          if "date" in ws else None}
    area = None
    if "area" in ws:
        det = ws["area"].get("details") or {}
        area = {"step": [det.get("stepY"), det.get("stepX")], "range": det.get("range"),
                "default": det.get("default")}
    location = None
    if "location" in ws:
        det = ws["location"].get("details") or {}
        location = {"step": [det.get("stepY"), det.get("stepX")], "range": det.get("range")}
    fmt_field = next((f for f in FORMAT_FIELDS if f in ws), None)
    formats = _choices(ws[fmt_field])[0] if fmt_field else []
    licences = [{"id": x.get("id"), "revision": int(x.get("revision", 0)), "label": x.get("label") or x.get("id"),
                 "url": x.get("contents_url")}
                for x in ((ws.get("licences") or {}).get("details") or {}).get("licences", [])]
    groups, in_group = [], set()
    for w in ws.values():
        if w.get("type") in ("ExclusiveGroupWidget", "ExclusiveGroupAccordionWidget"):
            children = [c for c in w.get("children") or [] if isinstance(c, str)]
            in_group.update(children)
            groups.append({"name": w["name"], "label": w.get("label") or w["name"], "children": children,
                           "default": (w.get("details") or {}).get("default")})
    notes = [(w.get("details") or {}).get("text", "") for w in ws.values() if w.get("type") == "FreeEditionWidget"]
    skip = OWN_FIELDS | {g["name"] for g in groups}
    options = [_option(w) for n, w in ws.items()
               if n not in skip and w.get("type") in ("StringListWidget", "StringChoiceWidget", "StringListArrayWidget")]
    return {
        "id": dataset_id,
        "title": (summary or {}).get("title", dataset_id),
        "start": (summary or {}).get("start"),  # the catalogue's time coverage
        "end": (summary or {}).get("end"),
        "tier": _tier(ws, formats, summary),
        "variables": variables,
        "levels": levels,
        "time": tm,
        "area": area,  # None: CDS always sends the whole domain (cut locally)
        "location": location,
        "format_field": fmt_field,
        "formats": formats,
        "download_format": "download_format" in ws,
        "licences": licences,
        "groups": groups,
        "options": options,
        "notes": [n for n in notes if n],
        "grouped": sorted(in_group),  # fields that are only sent when their group is chosen
    }


# ------------------------------------------------------------------ constraints (local, free)

def _as_set(v) -> set[str]:
    if v is None:
        return set()
    return {str(x) for x in v} if isinstance(v, (list, tuple, set)) else {str(v)}


def allowed(table: list[dict], selection: dict) -> dict[str, list[str]]:
    """For each constrained field, the values that exist together with what is selected in the *other* fields
    (as the CDS form greys out options). An entry that does not mention a field does not restrict it."""
    fields = set().union(*(e.keys() for e in table)) if table else set()
    sel = {k: _as_set(v) for k, v in selection.items() if k in fields and _as_set(v)}
    out = {}
    for f in fields:
        vals: set[str] = set()
        for e in table:
            if all(_as_set(e[g]) & s for g, s in sel.items() if g != f and g in e):
                vals |= _as_set(e.get(f))
        out[f] = sorted(vals)
    return out


def invalid(table: list[dict], request: dict) -> dict[str, list[str]]:
    """What in a request cannot be delivered, as ``{field: [values]}`` (empty = fine). Values the dataset does
    not have at all come first (they are the cause, so only they are reported); otherwise the values that exist
    with none of the other selected values."""
    if not table:
        return {}
    every: dict[str, set[str]] = {}
    for e in table:
        for f, v in e.items():
            every.setdefault(f, set()).update(_as_set(v))
    out = {}
    for f, vals in every.items():
        bad = sorted(_as_set(request.get(f)) - vals)
        if bad:
            out[f] = bad
    if out:
        return out
    ok = allowed(table, request)
    for f, vals in ok.items():
        bad = sorted(_as_set(request.get(f)) - set(vals))
        if bad:
            out[f] = bad
    return out
