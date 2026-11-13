# Earth Data Hub pipeline

[Earth Data Hub](https://earthdatahub.destine.eu/) publishes its datasets (ERA5 and related products) as Zarr
stores over HTTPS. The app reads the store's chunks directly: there is no queue and no server-side subsetting,
and every chunk read is one request counted against a monthly quota (500,000 by default). So the pipeline is
built around two questions: which chunks does the selection touch, and how do we fetch each one exactly once?

```text
catalog.py  ->  planner.py  ->  engine.py (+ http.py, codecs.py)  ->  aggregate.py  ->  store.py
 datasets        chunks, units     download, decode, cut               statistics       Icechunk, Zarr
```

## 1. Catalogue

[catalog.py](../src/edh_downloader/catalog.py)

- **Datasets** come from the public STAC API: collections, then their items. Catalogue images are public too and
  cost no quota.
- **Structure** (variables, dimensions, chunk shapes, codecs, attributes) comes from the store's consolidated
  `zarr.json`.
- **Coordinates** (time, latitude, longitude, levels) are fetched once and cached locally.

The structure and the coordinates cost a few requests per dataset per day of cache. `EDH_DL_OFFLINE=1` uses the
cache even when it is old and never fetches.

Each variable knows its default statistic: accumulated fields (precipitation, radiation and similar) are summed
over a period, the others averaged.

## 2. Plan

[planner.py](../src/edh_downloader/planner.py) turns a `JobSpec` and a dataset into an exact plan.

**Variables and levels.** The selected variables, and for pressure-level products the selected levels. A level
chunk holds one level, so each level is its own set of chunks.

**Space.**
- *Global:* every cell.
- *Bounding box:* the cells inside it. Longitudes are compared on −180..180, so a box across the antimeridian
  (west > east) works. The output keeps the convention you chose (−180..180 or 0..360).
- *Points:* the nearest grid cell of each point (longitude distance taken around the globe). The output has a
  `point` dimension and a `point_name` coordinate.

**Time.** Each source time step gets a period label: itself (`none`), its N-hour block, its day, its month or its
year, after adding `tz_offset` hours to UTC. The steps whose label falls between the start and the end make up
the output periods. The planner warns when the period was clipped to the data or when the first or last period
is incomplete.

**Units.** Output periods are grouped into units, aligned with the source's time chunks: a unit is the periods
that *start* inside one source time chunk. One unit is one Icechunk commit, and the point a stopped job resumes
from.

![Source time chunks, output periods shifted by a UTC offset, and the units built from them; a period crossing a
chunk boundary belongs to the unit where it starts, so that chunk serves two units](diagrams/edh-units.svg)

**Groups.** Within a unit, space is processed in groups of source tiles small enough for the memory budget
(`memory_gb`, of which a third goes to the groups in memory at once): whole rows of tiles when they fit, parts of
a row otherwise.

**The chunk list.** For each unit, the plan lists the chunks it needs as `(variable, time chunk, level, tile row,
tile column)` and remembers the last unit that needs each one. `plan.n_requests` is the number of distinct chunks:
the exact number of requests the job will make (plus a few for metadata).

## 3. Engine

[engine.py](../src/edh_downloader/engine.py) runs the plan in a detached worker.

![The asyncio side (producer, work queue, workers, monitor) fills a spool of chunk files from Earth Data Hub; the
processor thread waits for a unit, decodes, cuts and aggregates it, commits it and frees the spool](diagrams/edh-engine.svg)

**Download side (asyncio).**
- The **producer** walks the units in order and queues each chunk at the first unit that needs it. It stays at
  most 2 units ahead of the processor, keeps the spool under `max_spool_gb`, and keeps `min_free_gb` free on the
  spool and output disks. If a disk is full while the processor is waiting for the very unit being downloaded, the job
  stops with an error instead of waiting forever.
- **Workers** take chunks from the queue through a `Limiter`: a semaphore whose size is the number of connections
  and can change while workers wait. Each chunk is written to the spool as a file, through a temporary name and a
  rename. A 404 means the chunk is absent from the store (all fill value) and is recorded as an empty `.miss` file.
- The **monitor** runs every 2 seconds: it reads `control.json` (connections, pause, cancel), computes rates and
  the ETA, writes `state.json`, and handles the quota (below).

**Processing side (one thread).** For each unit in order:
1. wait until every chunk of the unit is in the spool;
2. for each variable and level, decode the chunks of each group, cut the selection, reduce time to the output
   periods (NaN-aware mean, min, max, sum or first), apply bit-rounding when asked, and write into the
   repository. Groups run in a small thread pool, a few at a time;
3. commit the unit to Icechunk in one transaction and add it to `units_done`;
4. delete the spool chunks no later unit needs.

When every unit is committed, the job sets the final attributes and exports to Zarr (see [Storage](storage.md)).

**HTTP.** [http.py](../src/edh_downloader/http.py) sends the key as a bearer token, follows the proxy
environment variables, and retries network errors, HTTP 429 and 5xx up to 6 times with exponential backoff and
jitter (at most 5 minutes between tries). 401 (bad key), 403 (access denied or server-side quota used up) and
404 on metadata stop the job: retrying cannot fix them.

**Chunk decoding.** [codecs.py](../src/edh_downloader/codecs.py) decodes raw Zarr v3 chunks by following the
array's own codec chain, so no Zarr store object is opened over HTTP.

## Quota

Every request is reserved before it is sent. When this month's count for the key would pass
`quota × quota_safety` (90% by default), the job either waits for the monthly reset (`waiting_quota`, when
`wait_for_quota` is on) or stops as `paused_quota`. Counts are shared by all jobs on the machine. See
[Keys and quota](keys-and-quota.md).

## Sampling

[sample.py](../src/edh_downloader/sample.py) is optional and costs real requests (12 by default, confirmed
first). It downloads chunks the job itself will need, spread over the variables and the period:

- the first 2 one at a time, then in parallel (up to 32 connections), to measure single-connection speed,
  latency and where throughput stops growing;
- the compressed size of a source chunk per variable, for the download size;
- the compression of **your** output encoding on the real values (cut, aggregated, rounded, encoded with your
  codec), for the Zarr size.

The decoded samples stay in the cache, so the Zarr size is estimated again offline whenever the output settings
change. The raw chunks are kept for 2 days: a job started in that time moves them into its spool instead of
downloading them again.

Without a sample, the plan estimates sizes from the dataset's metadata and a compression model.
