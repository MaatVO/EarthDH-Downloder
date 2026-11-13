"""Read the GRIB files the Climate Data Store delivers, with bounded memory.

A result file holds one message per field (variable × level × time step). ``index`` reads only the message
headers (fast, nothing decoded) and remembers where each message starts; ``read`` decodes one message from its
offset. So a several-GB file is indexed once and then read message by message, one variable and level at a time.

``grid`` describes the horizontal grid of a message: regular latitude/longitude (1-D coordinates) or a projected
grid such as CERRA's Lambert conformal (2-D coordinates).

ecCodes is loaded only here: Earth Data Hub works without it.
"""

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def _eccodes():
    try:
        import eccodes
    except Exception as e:  # noqa: BLE001  (missing wheel or library)
        raise RuntimeError(f"reading GRIB needs the ecCodes library, which could not be loaded: {e}") from e
    return eccodes


@dataclass(frozen=True)
class Message:
    offset: int
    param: int  # paramId
    short_name: str
    cf_name: str  # cfVarName: the output name (t2m, tp, u10 ...)
    name: str  # long name
    units: str
    step_type: str
    level_type: str
    level: float
    valid: pd.Timestamp  # validity time (UTC)
    ni: int
    nj: int
    grid_type: str


@dataclass
class Grid:
    kind: str  # "regular" or "curvilinear"
    lat: np.ndarray  # 1-D (regular) or 2-D (curvilinear)
    lon: np.ndarray
    attrs: dict = field(default_factory=dict)  # projection parameters (curvilinear)

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.lat), len(self.lon)) if self.kind == "regular" else self.lat.shape


def _get(ec, h, key, default=None):
    try:
        return ec.codes_get(h, key) if ec.codes_is_defined(h, key) else default
    except Exception:  # noqa: BLE001
        return default


def _valid(ec, h) -> pd.Timestamp:
    d, t = _get(ec, h, "validityDate"), _get(ec, h, "validityTime")
    return pd.Timestamp(f"{int(d):08d}") + pd.Timedelta(hours=int(t) // 100, minutes=int(t) % 100)


def index(path) -> list[Message]:
    """Every message of a GRIB file, from its headers only."""
    ec = _eccodes()
    out = []
    with open(path, "rb") as f:
        while True:
            offset = f.tell()
            h = ec.codes_grib_new_from_file(f, headers_only=True)
            if h is None:
                break
            try:
                out.append(Message(
                    offset=offset, param=int(_get(ec, h, "paramId", 0)),
                    short_name=str(_get(ec, h, "shortName", "")), cf_name=str(_get(ec, h, "cfVarName", "")),
                    name=str(_get(ec, h, "name", "")), units=str(_get(ec, h, "units", "")),
                    step_type=str(_get(ec, h, "stepType", "")), level_type=str(_get(ec, h, "typeOfLevel", "")),
                    level=float(_get(ec, h, "level", 0) or 0), valid=_valid(ec, h),
                    ni=int(_get(ec, h, "Ni", 0) or _get(ec, h, "Nx", 0) or 0),
                    nj=int(_get(ec, h, "Nj", 0) or _get(ec, h, "Ny", 0) or 0),
                    grid_type=str(_get(ec, h, "gridType", ""))))
            finally:
                ec.codes_release(h)
    return out


def read(path, msg: Message) -> np.ndarray:
    """The values of one message as float32 ``(nj, ni)``, missing values as NaN."""
    ec = _eccodes()
    with open(path, "rb") as f:
        f.seek(msg.offset)
        h = ec.codes_grib_new_from_file(f)
    try:
        ec.codes_set(h, "missingValue", 1e20)
        v = ec.codes_get_values(h).astype(np.float32)
        if _get(ec, h, "bitmapPresent", 0):
            v[v >= 1e19] = np.nan
        return v.reshape(msg.nj, msg.ni)
    finally:
        ec.codes_release(h)


def grid(path, msg: Message) -> Grid:
    """The horizontal grid of a message."""
    ec = _eccodes()
    with open(path, "rb") as f:
        f.seek(msg.offset)
        h = ec.codes_grib_new_from_file(f)
    try:
        if msg.grid_type == "regular_ll":
            lat0, lat1 = _get(ec, h, "latitudeOfFirstGridPointInDegrees"), _get(ec, h, "latitudeOfLastGridPointInDegrees")
            lon0 = _get(ec, h, "longitudeOfFirstGridPointInDegrees")
            dj, di = _get(ec, h, "jDirectionIncrementInDegrees"), _get(ec, h, "iDirectionIncrementInDegrees")
            lat = lat0 + np.arange(msg.nj) * (dj if lat1 >= lat0 else -dj)
            lon = lon0 + np.arange(msg.ni) * di
            return Grid("regular", np.round(lat, 6), np.round(lon, 6))
        lat = np.asarray(ec.codes_get_array(h, "latitudes"), dtype="f8").reshape(msg.nj, msg.ni)
        lon = np.asarray(ec.codes_get_array(h, "longitudes"), dtype="f8").reshape(msg.nj, msg.ni)
        attrs = {"grid_type": msg.grid_type}
        for k in ("LaDInDegrees", "LoVInDegrees", "Latin1InDegrees", "Latin2InDegrees", "DxInMetres", "DyInMetres"):
            v = _get(ec, h, k)
            if v is not None:
                attrs[k] = float(v)
        return Grid("curvilinear", lat, lon, attrs)
    finally:
        ec.codes_release(h)


# ------------------------------------------------------------------ form names ↔ GRIB parameters

_SYN = [(r"\bmetres?\b", "m"), (r"\b(\d+) m\b", r"\1m"), (r"\bu wind component\b", "u component of wind"),
        (r"\bv wind component\b", "v component of wind"), (r"short-wave \(solar\)", "solar"),
        (r"long-wave \(thermal\)", "thermal"), (r"post-processing", "postprocessing"),
        (r"post_processing", "postprocessing")]
_STOP = {"of", "at", "the", "since", "previous", "a", "in", "on", "and"}


def _tokens(text: str) -> set[str]:
    t = text.lower().replace("_", " ")
    for pat, rep in _SYN:
        t = re.sub(pat, rep, t)
    return {w for w in re.split(r"[^a-z0-9]+", t) if w and w not in _STOP}


def match_variables(form_names: list[str], params: list[Message]) -> dict[str, int]:
    """Which GRIB parameter (paramId) each CDS form variable became, as far as the names tell: best word overlap
    first, then elimination. Only used to apply per-variable statistics; unmatched names are left out."""
    by_param = {}
    for m in params:
        by_param.setdefault(m.param, m)
    if len(form_names) == 1 and len(by_param) == 1:
        return {form_names[0]: next(iter(by_param))}
    scores = []
    for f in form_names:
        ft = _tokens(f)
        for p, m in by_param.items():
            pt = _tokens(m.name) | {m.short_name.lower(), m.cf_name.lower()}
            inter = len(ft & pt)
            if inter:
                scores.append((inter / len(ft | _tokens(m.name)), f, p))
    out: dict[str, int] = {}
    used: set[int] = set()
    for score, f, p in sorted(scores, reverse=True):
        if f not in out and p not in used and score >= 0.34:
            out[f] = p
            used.add(p)
    left_f = [f for f in form_names if f not in out]
    left_p = [p for p in by_param if p not in used]
    if len(left_f) == 1 and len(left_p) == 1:
        out[left_f[0]] = left_p[0]
    return out


class Reader:
    """Decode messages of one file through a single open handle (``read`` opens the file each time)."""

    def __init__(self, path):
        self.ec = _eccodes()
        self.f = open(path, "rb")

    def read(self, msg: Message) -> np.ndarray:
        ec = self.ec
        self.f.seek(msg.offset)
        h = ec.codes_grib_new_from_file(self.f)
        try:
            ec.codes_set(h, "missingValue", 1e20)
            v = ec.codes_get_values(h).astype(np.float32)
            if _get(ec, h, "bitmapPresent", 0):
                v[v >= 1e19] = np.nan
            return v.reshape(msg.nj, msg.ni)
        finally:
            ec.codes_release(h)

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
