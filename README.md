# EDH Downloader

Download subsets of [Earth Data Hub](https://earthdatahub.destine.eu) datasets (ERA5 single levels, pressure
levels, ERA5-Land, monthly/daily products, …) into an **Icechunk** repository and a plain **Zarr v3** store, from a
**web UI** you reach over SSH. Downloads run as **detached background jobs**: close the browser, stop the UI or
drop the SSH connection and they keep going.

* pick dataset, variables and pressure levels from the live catalogue (STAC + each store's `zarr.json`);
* time range plus an optional time step: as stored, every N hours, daily, monthly or yearly, with per-variable
  statistics (mean, min, max, sum, first) and a configurable day boundary (UTC offset);
* global, a lat/lon box (antimeridian-safe, drawn on a map or typed) or a list of points (nearest grid cell);
* exact cost before you start: number of requests vs. your remaining monthly quota, download size, output size;
* every source chunk is fetched **once**; jobs commit to Icechunk unit by unit, so they pause/resume/crash-recover
  without re-downloading;
* optional sample (a dozen real chunks of the job, counted in your quota) that measures download size, Zarr size
  and line speed, and *suggests* a number of parallel connections (you choose the number);
* output metadata records the full job, source, dataset version, app version and request count.

## Run it

**The app on your computer (recommended).** Run `edh-dl` on your computer with no arguments (or double-click
it): it opens a **Connect** page in your browser. Enter the server (an `~/.ssh/config` alias works, as do
ProxyJump and certificates) and log in with your SSH key/agent or a password. You get the interface in a tab;
connect to more servers and each gets its own tab. The interface always runs on your computer, in the version you
are running. The server only does the work: keys, quota, downloads, jobs and files stay there.

* First time on a server: it shows the host-key fingerprint and asks before trusting it (like ssh).
* The server needs no edh-dl install. On first connection the app creates a Python environment in
  `~/.local/share/edh-dl` (numpy, zarr, icechunk … from PyPI, about 300 MB, once; uses `uv` when present, else
  `python3 -m venv` with Python 3.12 or newer). After that it only sends the app's Python code, a zip of a few
  hundred KB, when it changed.
* Servers are remembered in `~/.config/edh-dl/servers.json`, passwords never.
* Hosts in `~/.ssh/config` (and its `Include`s) are offered as presets; pick one and its HostName, User, Port,
  ProxyJump and keys apply as with ssh (leave User/Port empty to use them).
* No server? **This computer** on the Connect page opens the same UI locally in its own tab: jobs, files and
  keys stay on your computer.
* Closing a tab (×) disconnects that server, after asking. Closing the browser tab, or **Quit**, closes the
  app and all its connections (the browser asks first). Download jobs are separate processes: they keep
  running, here or on the servers, and show up in **Jobs** again when you reconnect.

Executables are per platform (`packaging/build.sh` builds for the machine it runs on; the GitHub workflow in
`.github/workflows/build.yml` builds Linux, macOS and Windows on every push to `main` and publishes them under
[Releases](../../releases), versioned `v<major>.<minor>.<run number>`; the major.minor comes from `pyproject.toml`). The packaged
executable updates itself: every time the app starts it checks the latest release, and if a newer one exists it
downloads it (checked against the release's `.sha256`), replaces itself and restarts. Skip with `--no-update` or
`EDH_DL_NO_UPDATE=1`; `edh-dl update` checks on demand. (Not for source / `uv tool install`.) Without an
executable for your computer: `uv tool install /path/to/edh-downloader` (any OS), then `edh-dl`.

**Without the app, only ssh** (no manual port forwarding either). Get the launcher once, with ssh, then just run it: it opens the SSH tunnel, starts the UI on the server and
opens your browser. Ctrl+C closes it; download jobs keep running on the server.

```bash
# macOS / Linux / WSL (needs only ssh)
ssh me@server '/path/to/edh-dl launcher sh' > edh-connect && chmod +x edh-connect
./edh-connect                     # or ./edh-connect other@host

# Windows 10/11 (built-in OpenSSH)
ssh me@server "/path/to/edh-dl launcher ps1" > edh-connect.ps1
powershell -ExecutionPolicy Bypass -File edh-connect.ps1

# a Linux machine with edh-dl installed
edh-dl connect me@server --cmd /path/to/edh-dl
```

The launcher picks free ports, sends a fresh access token over the SSH session (never on a command line), and
the server exits when the launcher closes. Manually: `./edh-dl serve` on the server prints the link (and the
launcher commands); `ssh -L 8765:localhost:8765 server` on your machine. Run on your own computer, `edh-dl serve`
opens the browser itself.

The UI listens on `127.0.0.1` only and requires the token (shared servers: other users can reach localhost ports).

From source (Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
uv run edh-dl serve
```

Build the executable: `packaging/build.sh` → `dist/edh-dl` (~88 MB).

## API keys and quota

Add a key in the **Keys** tab (or `edh-dl keys add NAME`). Keys are stored in `~/.config/edh-dl/keys.json`
(mode 600); a key in `~/.netrc` (`machine data.earthdatahub.destine.eu`) is used automatically. Job files and
outputs only ever contain the key's name.

Free access gives **500,000 requests per month** (resets on the 1st, UTC); one chunk = one request. Earth Data Hub
does not expose the remaining quota to API-key clients, so the app counts its own requests per key and month in
`~/.local/share/edh-dl/usage.json` (shared by all jobs). Use **calibrate** with the "used" number from the
[quota page](https://earthdatahub.destine.eu/quota-api-keys) to include requests made by other tools. Jobs stop at
a configurable share of the quota (default 90%) and then either wait for the monthly reset and continue, or pause.
The ↗ next to the quota in the header and **Check on Earth Data Hub** in Keys open that page.

Catalogue reads are cheap: a dataset's structure and coordinates cost ~5 requests and are cached for 3 days.

## Jobs

A job is a folder `~/.local/share/edh-dl/jobs/<id>/` with `job.json` (spec), `state.json` (progress), `log.txt`,
`control.json` (connections / pause / cancel) and a temporary directory of raw chunks (deleted as soon as they are processed).
The **Jobs** tab shows progress, rate, ETA, requests; you can change connections of a running job, pause, resume,
cancel, read the log, or clone a job into a new one. A completed job's **Structure** button draws the store as
written, read from disk (real chunks, codecs, sizes); long variable lists open in full with **See all**.

Headless equivalents:

```bash
edh-dl datasets                       # list ERA5 datasets
edh-dl variables era5-pressure-levels # variables, levels, time range
edh-dl plan job.json                  # cost of a job file (save one from the UI with "job.json")
edh-dl run job.json --detach          # start in the background
edh-dl jobs | status ID | log ID | pause ID | resume ID [--connections N] | cancel ID | export ID
edh-dl plan job.json --sample             # measure on 12 real chunks (USES 12 requests; asks first)
```

## Output

Format **Zarr + Icechunk** (CLI default): `<directory>/<name>.zarr`, a plain Zarr v3 store, plus the Icechunk
repository `<directory>/<name>.icechunk` the download goes through (one commit per unit: that is what makes jobs
resumable). **Zarr** deletes the repository after the export; **Icechunk only** (default) skips the export.
Consolidated metadata is written so `xr.open_zarr(path)` opens without warnings (it is optional in Zarr v3).

```python
import xarray as xr
ds = xr.open_zarr("~/edh-data/era5_subset.zarr")
# or the Icechunk repository:
import icechunk
repo = icechunk.Repository.open(icechunk.local_filesystem_storage("~/edh-data/era5_subset.icechunk"))
ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False)
```

Dimensions: `valid_time` (period start for aggregated data), optional level dimension (e.g. `isobaricInhPa`),
`latitude`/`longitude` (or `point` with `latitude`, `longitude`, `point_name` coordinates). Values are float32,
lossless by default (optionally bit-rounded, e.g. to the 13 mantissa bits ERA5 has on Earth Data Hub, for smaller files).
Root attributes `edh_job` / `edh_plan` hold everything that was selected.

## Chunking and Zarr encoding

The **Output** step compares four chunk layouts with real numbers for your selection (chunk shape and size,
number of chunks, cost of reading one grid cell's full time series and of reading one map) and recommends one:
*Time series* for points or small areas over long periods, *Maps* for large areas over few time steps,
*Automatic* otherwise, or *Custom* sizes.

The Icechunk repository is always written in the *Automatic* (download-aligned) layout, so each chunk is written
exactly once and an interrupted job resumes safely; Icechunk keeps old chunk versions, so rewriting long chunks
unit after unit would multiply its size. The chosen layout, optional **sharding** (chunks grouped into ~512 MB
shard files, useful when there would be tens of thousands of chunk files), compressor (Blosc+Zstd/LZ4/LZ4HC/zlib,
Zstd, gzip, none), level, shuffle, data type (float32/float64) and extra global attributes
are applied to the exported Zarr: a direct byte copy for the Automatic layout, otherwise one local, memory-bounded
rechunking pass (no extra downloads).

## How it works

`catalog` reads STAC and the consolidated `zarr.json`; `planner` maps the selection to source chunk indices,
groups output periods into units aligned with the source time chunks, and spatial tiles into memory-bounded
groups; `engine` downloads chunks with N async connections into a temporary directory a couple of units ahead (bounded by its
size and free disk); a processor decodes (bitround/bytes/blosc/zstd codecs), cuts, aggregates and region-writes
each unit to Icechunk and commits; `store` exports to Zarr; `runner` launches detached workers (`setsid`).

## Notes and limitations

* Daily sums of accumulated ERA5 variables use the hours whose `valid_time` falls in the day (00–23 UTC, shifted by
  the UTC offset). ECMWF's own daily totals use 01–24; shift with the offset if you need that convention.
* Supported layouts: `(time, lat, lon)`, `(time, level, lat, lon)` with level chunk 1, and static `(lat, lon)`
  arrays in Zarr v3 stores with consolidated metadata (all ERA5 datasets on Earth Data Hub).
* One job writes to one Icechunk repository; do not run two jobs into the same output.
* Tests: `uv run pytest` (runs against a local fake Earth Data Hub; uses no quota). Set `EDH_DL_OFFLINE=1` to
  make the app refuse any uncached metadata fetch (handy for UI work without spending requests).
