# Storage

Every gridded job writes into an [Icechunk](https://icechunk.io/) repository first, one transaction per unit.
When every unit is in, the repository is exported to a plain [Zarr v3](https://zarr.dev/) store in the layout you
chose. The code is in [store.py](../src/edh_downloader/store.py) and
[chunking.py](../src/edh_downloader/chunking.py).

![Committed units go into the Icechunk repository in the staging layout; after the last unit a local,
memory-bounded export writes the Zarr store in your layout; an Earth Data Hub job can also extend an existing
store](diagrams/storage.svg)

## Why two stores

- **Icechunk is the working copy.** Each unit is written in a writable session and committed in one transaction.
  A job killed in the middle of a unit leaves the repository at the previous commit, never half-written. Resuming
  starts from the first unit not in `units_done`.
- **Zarr is the deliverable.** A plain directory that Xarray, Dask and any Zarr v3 reader open without Icechunk.

| Option | Result |
|---|---|
| `export_zarr = true`, `keep_icechunk = true` | both `<name>.icechunk/` and `<name>.zarr/` |
| `export_zarr = true`, `keep_icechunk = false` | only `<name>.zarr/`: the repository is removed after a successful export |
| `export_zarr = false` | only `<name>.icechunk/` |

## The staging layout

The repository is always written with the **automatic** layout: one download unit of time per chunk. Each chunk is
then written exactly once, by the unit that owns it, so no unit ever rewrites another unit's chunks, and resuming
is safe. Any other layout, and sharding, is applied only to the exported Zarr.

## The export

`export_zarr` writes the committed state of `main`:

- with the staging layout and no sharding, chunk bytes are **copied as they are** (no decoding);
- otherwise the variables are **rechunked** into your layout, one block at a time. Blocks are aligned to the
  destination's write unit (a shard, or a chunk), so no destination chunk is written twice, and sized to stay
  within `memory_gb`.

The export goes to `<name>.zarr.partial` and is renamed to `<name>.zarr` only when complete, with consolidated
metadata when `consolidated` is on. A crash after the export only has to finish the cleanup; a crash during it
starts the export again.

Before the rename, the partial store is verified against the Icechunk repository: same arrays and groups, same
shape, dtype, dimension names, fill value and attributes, the consolidated metadata agrees with the real metadata,
and every value is identical (NaN equals NaN), compared block by block within `memory_gb`. On any mismatch the
partial store is deleted, an existing `<name>.zarr` is left untouched, the job fails, and the Icechunk repository
is never removed, even with `keep_icechunk = false`.

## Chunk layouts

![Three chunk layouts of the same variable: time series chunks are long in time so a point over all years is one
chunk; map chunks are wide in space so one date is one chunk; the automatic layout is balanced and download-aligned](diagrams/chunk-layouts.svg)

A chunk is the unit Zarr reads and decompresses. Good chunks hold a few MB to a few tens of MB (the target is
about 4 million values, ~16 MB of float32 before compression). Their *shape* decides which reads are cheap; the
download costs the same either way.

| Layout | Shape | Cheap to read |
|---|---|---|
| Automatic | aligned with the download units | balanced; the fastest to write |
| Time series | long in time (up to ~10 years of hourly steps), small in space | many years at a point or a small region |
| Maps | the whole area, few time steps | one date over a large area |
| Custom | your sizes (empty fields keep the automatic value) | whatever you need |

The form compares the layouts for the first time-dependent variable: chunk shape and size, number of chunks, and
the cost of reading one grid cell's time series or one map. It also recommends one for the selection.

**Sharding** groups many chunks into one shard file, so a store has far fewer files (useful on file systems that
dislike millions of small files). Automatic shard sizes aim at about 512 MB, growing in space first, since each
unit writes the whole area for its time slab.

## Encoding

| Setting | Values |
|---|---|
| `dtype` | `float32` (default) or `float64` |
| `compressor` | Blosc with Zstandard (default), LZ4, LZ4HC or zlib; Zstandard; gzip; none |
| `clevel` | compression level (Blosc 1–9, Zstandard 1–22, gzip 1–9) |
| `shuffle` | Blosc only: `shuffle`, `bitshuffle` or `noshuffle` |
| `keepbits` | bit-rounding: float32 mantissa bits kept (none = lossless). ERA5 on Earth Data Hub itself keeps 13 |

Bit-rounding zeroes mantissa bits below `keepbits`, the same as numcodecs' `BitRound`; the values compress much
better and lose precision the data does not carry.

## What the store contains

| Dimension or coordinate | Meaning |
|---|---|
| `valid_time` | time; the period start for aggregated data |
| `latitude`, `longitude` | 1-D for regular grids; 2-D for projected grids (CERRA, CARRA) |
| `isobaricInhPa` (or another level dimension) | pressure or other levels, when selected |
| `point`, `point_name` | for point extraction |

Output variables keep the source variable's name. When a variable gets more than one statistic, each one is a
separate variable with the statistic appended (`t2m_mean`, `t2m_max`).

Root attributes record where the data came from and how it was made: `title`, `source`, `source_dataset`,
`provider`, `license`, `Conventions` (`CF-1.8`), `history`, and the provenance attributes `edh_job` (the whole
job spec), `edh_plan` (the plan summary), `edh_job_id`, `edh_status`, `edh_downloader_version`, plus the
requests and bytes the job used. Your `extra_attrs` are added too (names starting with `edh_` are reserved).

## Extending a store

Earth Data Hub jobs can append to an existing store (`target_mode = "extend"`), **forward** (after its last time)
or **backward** (before its first). Before anything is resized, `check_extension` refuses a job that would not
fit: the store must have every variable of the job, the same area, grid, points and levels, and the new period
must not repeat any time. The repository is then resized along `valid_time`, the new units are committed as
usual, and the export writes the whole store again. A resumed job never extends the repository twice.

Climate Data Store jobs cannot extend a store yet.

## Reading the output

```python
import xarray as xr

ds = xr.open_zarr("~/edh-data/era5_subset.zarr")
```

```python
import icechunk
import xarray as xr

repo = icechunk.Repository.open(icechunk.local_filesystem_storage("~/edh-data/era5_subset.icechunk"))
ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False)
```

Each commit in the repository is one unit (`unit 3: 2021-04-01 .. 2021-04-30`), so its history shows how the job
progressed.
