"""Discover Earth Data Hub datasets (public STAC API) and read their Zarr structure.

Dataset structure (variables, dimensions, chunking, codecs) comes from the store's consolidated
``zarr.json``; coordinates (time, latitude, longitude, levels) are fetched once and cached locally.
Fetching ``zarr.json`` and coordinates costs a few requests per dataset per day of cache.
"""

import asyncio
import hashlib
import logging
import os
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import numpy as np
import pandas as pd

from .codecs import ChunkDecoder
from .http import EDHClient
from .paths import cache_dir
from .util import read_json, write_json_atomic

log = logging.getLogger("edh.catalog")
STAC = "https://earthdatahub.destine.eu/api/stac/v1"
STAC_TTL = 24 * 3600
META_TTL = 3 * 24 * 3600  # time axis grows monthly; refresh manually for the newest data
LAT_NAMES = ("latitude", "lat")
LON_NAMES = ("longitude", "lon")


# ---------------------------------------------------------------- STAC (public, no key needed)

async def _stac_get(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def fetch_collections() -> list[dict]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        d = await _stac_get(s, f"{STAC}/collections")
    return [{"id": c["id"], "title": c.get("title", c["id"]), "description": c.get("description", "")}
            for c in d.get("collections", [])]


async def fetch_items(collection: str) -> list[dict]:
    url = f"{STAC}/collections/{collection}/items"
    items = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        while url:
            d = await _stac_get(s, url)
            items += d.get("features", [])
            url = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)
    out = []
    for it in items:
        asset = it.get("assets", {}).get("data")
        if not asset:
            continue
        p = it.get("properties", {})
        out.append({
            "id": it["id"],
            "collection": collection,
            "title": p.get("title", it["id"]),
            "url": asset["href"].rstrip("/"),
            "abstract": p.get("dc:abstract", ""),
            "size": p.get("dc:extent", ""),
            "updated": p.get("updated", ""),
            "start": p.get("start_datetime"),
            "end": p.get("end_datetime") or p.get("datetime"),
            "deprecated": p.get("deprecated", False),
            "bbox": it.get("bbox"),
        })
    return sorted(out, key=lambda x: x["id"])


def _cached(name: str, ttl: float, fetch, refresh: bool = False):
    path = cache_dir() / name
    d = read_json(path)
    if d is not None and not refresh and time.time() - d["t"] < ttl:
        return d["v"]
    try:
        v = fetch()
        if asyncio.iscoroutine(v):
            v = asyncio.run(v)
    except Exception:
        if d is not None:  # offline: fall back to stale cache
            return d["v"]
        raise
    write_json_atomic(path, {"t": time.time(), "v": v})
    return v


def collections(refresh: bool = False) -> list[dict]:
    return _cached("stac_collections.json", STAC_TTL, fetch_collections, refresh)


def datasets(collection: str = "era5", refresh: bool = False) -> list[dict]:
    return _cached(f"stac_items_{collection}.json", STAC_TTL, lambda: fetch_items(collection), refresh)


def find_dataset(dataset_id: str, collection: str = "era5") -> dict:
    for d in datasets(collection):
        if d["id"] == dataset_id:
            return d
    raise KeyError(f"dataset {dataset_id!r} not found in collection {collection!r}")


def dataset_info(spec) -> dict:
    """Catalogue entry for a job; a job with an explicit ``dataset_url`` works without STAC."""
    try:
        info = find_dataset(spec.dataset, spec.collection)
    except Exception:
        if not spec.dataset_url:
            raise
        info = {"id": spec.dataset, "title": spec.dataset, "collection": spec.collection}
    if spec.dataset_url:
        info = dict(info, url=spec.dataset_url.rstrip("/"))
    return info


# ---------------------------------------------------------------- Zarr structure

@dataclass
class Variable:
    name: str
    long_name: str
    units: str
    dims: tuple[str, ...]
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    meta: dict = field(repr=False)
    kind: str = "unsupported"  # "time" (t,y,x), "level" (t,z,y,x), "static" (y,x), "unsupported"
    step_type: str = ""

    @property
    def attrs(self) -> dict:
        return self.meta.get("attributes", {})

    def decoder(self) -> ChunkDecoder:
        return ChunkDecoder(self.meta)

    def default_stat(self) -> str:
        """Accumulated fields (precipitation, radiation, ...) add up over a period; others average."""
        return "sum" if self.step_type == "accum" else "mean"


@dataclass
class DatasetMeta:
    url: str
    attrs: dict
    variables: dict[str, Variable]
    time_dim: str | None
    level_dim: str | None
    lat_dim: str
    lon_dim: str
    arrays: dict = field(repr=False)


def parse_metadata(url: str, zj: dict) -> DatasetMeta:
    arrays = zj.get("consolidated_metadata", {}).get("metadata", {})
    if not arrays:
        raise ValueError("dataset has no consolidated metadata in zarr.json; not supported")
    coords1d = {k: v for k, v in arrays.items() if v.get("dimension_names") == [k]}
    lat = next((n for n in LAT_NAMES if n in coords1d), None)
    lon = next((n for n in LON_NAMES if n in coords1d), None)
    if not lat or not lon:
        raise ValueError("dataset has no latitude/longitude coordinates; not supported")
    tdim = next((k for k, v in coords1d.items() if "since" in str(v.get("attributes", {}).get("units", ""))), None)
    others = [k for k in coords1d if k not in (lat, lon, tdim)]
    variables = {}
    level_dim = None
    for name, m in arrays.items():
        if name in coords1d or m.get("node_type") != "array":
            continue
        dims = tuple(m.get("dimension_names") or ())
        a = m.get("attributes", {})
        v = Variable(name=name, long_name=a.get("long_name", name), units=a.get("units", ""), dims=dims,
                     shape=tuple(m["shape"]), chunks=tuple(m["chunk_grid"]["configuration"]["chunk_shape"]),
                     meta=m, step_type=a.get("GRIB_stepType", ""))
        try:
            v.decoder()
            ok = True
        except ValueError:
            ok = False
        if ok and dims == (tdim, lat, lon) and tdim:
            v.kind = "time"
        elif ok and tdim and len(dims) == 4 and dims[0] == tdim and dims[2:] == (lat, lon) and dims[1] in others:
            v.kind = "level"
            level_dim = dims[1]
        elif ok and dims == (lat, lon):
            v.kind = "static"
        variables[name] = v
    return DatasetMeta(url=url, attrs=zj.get("attributes", {}), variables=dict(sorted(variables.items())),
                       time_dim=tdim, level_dim=level_dim, lat_dim=lat, lon_dim=lon, arrays=arrays)


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


async def _fetch_coord(client: EDHClient, url: str, name: str, meta: dict) -> list:
    dec = ChunkDecoder(meta)
    raw = await client.get(f"{url}/{name}/c/0", missing_ok=False)
    return dec.decode(raw)[: meta["shape"][0]].tolist()


async def fetch_structure(url: str, client: EDHClient) -> dict:
    raw = await client.get(f"{url}/zarr.json", missing_ok=False)
    zj = json.loads(raw)
    dm = parse_metadata(url, zj)
    coords = {}
    for name in filter(None, (dm.time_dim, dm.level_dim, dm.lat_dim, dm.lon_dim)):
        coords[name] = await _fetch_coord(client, url, name, dm.arrays[name])
    return {"zarr_json": zj, "coords": coords}


class Dataset:
    """A dataset's metadata plus decoded coordinates."""

    def __init__(self, url: str, zarr_json: dict, coords: dict, info: dict | None = None):
        self.url = url
        self.info = info or {}
        self.meta = parse_metadata(url, zarr_json)
        m = self.meta
        self.lat = np.asarray(coords[m.lat_dim], dtype="f8")
        self.lon = np.asarray(coords[m.lon_dim], dtype="f8")
        self.levels = np.asarray(coords[m.level_dim]) if m.level_dim else None
        self.time = None
        if m.time_dim:
            tattrs = m.arrays[m.time_dim]["attributes"]
            unit, _, origin = tattrs["units"].partition(" since ")
            self.time_units = tattrs["units"]
            self.time = pd.Timestamp(origin) + pd.to_timedelta(np.asarray(coords[m.time_dim]), unit=unit)

    @property
    def variables(self) -> dict[str, Variable]:
        return self.meta.variables

    @property
    def time_range(self):
        return (self.time[0], self.time[-1]) if self.time is not None else (None, None)

    def time_step(self) -> pd.Timedelta | None:
        if self.time is None or len(self.time) < 2:
            return None
        d = np.diff(self.time.values[: min(len(self.time), 1000)])
        return pd.Timedelta(np.median(d))

    def summary(self) -> dict:
        t0, t1 = self.time_range
        return {
            "url": self.url,
            "variables": len([v for v in self.variables.values() if v.kind != "unsupported"]),
            "levels": None if self.levels is None else self.levels.tolist(),
            "time": None if t0 is None else [str(t0), str(t1)],
            "time_step": _fmt_step(self.time_step()) if self.time is not None else None,
            "grid": [len(self.lat), len(self.lon)],
        }


def _fmt_step(td) -> str | None:
    if td is None:
        return None
    h = td / pd.Timedelta(hours=1)
    if h < 24:
        return f"{h:g} h"
    d = h / 24
    return f"{d:g} days" if d < 28 else "monthly" if d < 32 else f"{d:g} days"


def load_dataset(url: str, secret: str, counter=None, refresh: bool = False, info: dict | None = None) -> Dataset:
    url = url.rstrip("/")
    path = cache_dir() / f"meta_{_url_hash(url)}.json"
    d = read_json(path)
    if d is None or refresh or time.time() - d["t"] > META_TTL:
        if os.environ.get("EDH_DL_OFFLINE"):
            if d is not None:
                return Dataset(url, d["v"]["zarr_json"], d["v"]["coords"], info)
            raise RuntimeError(f"offline mode: {url} is not cached")
        log.info(f"fetching structure of {url}")

        async def go():
            async with EDHClient(secret, counter=counter, connections=2, timeout=120, retries=3) as c:
                return await fetch_structure(url, c)
        try:
            v = asyncio.run(go())
            write_json_atomic(path, {"t": time.time(), "url": url, "v": v})
            d = {"v": v}
        except Exception:
            if d is None:
                raise
    return Dataset(url, d["v"]["zarr_json"], d["v"]["coords"], info)
