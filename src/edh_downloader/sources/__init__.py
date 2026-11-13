"""The data sources the app can download from, and what the rest of the app needs to know about each.

A *source* is a service with its own catalogue, API keys and request rules: Earth Data Hub (direct reads of
Zarr chunks, a monthly request quota) or the Copernicus Climate Data Store (requests wait in a queue, each one
bounded by a cost limit). Jobs, keys and the UI carry a source id; everything source-specific is looked up here.
"""

from dataclasses import asdict, dataclass

EDH = "edh"
CDS = "cds"


@dataclass(frozen=True)
class SourceInfo:
    id: str
    title: str  # full name, for headings
    short: str  # short name, for chips and selects
    key_url: str  # where a user gets an API key
    key_hint: str  # where else the app finds a key automatically
    has_quota: bool  # a monthly request allowance the app counts locally
    check_cost: str  # what validating a key costs, shown next to the "check it" switch
    jobs: bool  # this version can plan and run jobs from this source

    def to_dict(self) -> dict:
        return asdict(self)


SOURCES: dict[str, SourceInfo] = {
    EDH: SourceInfo(
        id=EDH, title="Earth Data Hub", short="EDH",
        key_url="https://earthdatahub.destine.eu/quota-api-keys",
        key_hint="`~/.netrc` (machine data.earthdatahub.destine.eu) is used automatically.",
        has_quota=True, check_cost="1 request", jobs=True,
    ),
    CDS: SourceInfo(
        id=CDS, title="Copernicus Climate Data Store", short="CDS",
        key_url="https://cds.climate.copernicus.eu/profile",
        key_hint="`~/.cdsapirc` (the file the `cdsapi` package uses) is used automatically.",
        has_quota=False, check_cost="free", jobs=True,
    ),
}


def get(source: str | None) -> SourceInfo:
    """The source with this id; ``None`` means Earth Data Hub (jobs and keys written before sources existed)."""
    try:
        return SOURCES[source or EDH]
    except KeyError:
        raise ValueError(f"unknown data source {source!r}; known: {', '.join(SOURCES)}") from None


def require_jobs(source: str | None) -> SourceInfo:
    s = get(source)
    if not s.jobs:
        raise ValueError(f"downloads from {s.title} are not available in this version yet")
    return s
