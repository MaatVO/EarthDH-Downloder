# Architecture

The app has four layers:

1. **Interface:** the NiceGUI web UI and the `edh-dl` command line.
2. **Service:** plain functions, JSON in and JSON out, that do everything the interface needs.
3. **Sources:** Earth Data Hub and the Climate Data Store, each with its own catalogue, planner and engine.
4. **Shared output:** decoding, aggregation, the Icechunk repository and the Zarr export.

The interface never touches keys, quota, catalogues, jobs or output stores directly. It always goes through the
service. That one rule is what lets the interface run on your computer while the work runs on a server.

![The interface on your computer reaches the service through a Local or Remote backend; on the server an agent
answers over SSH, the service launches detached job workers and keeps all files](diagrams/where-it-runs.svg)

## Where the code runs

| Situation | Interface | Service and jobs |
|---|---|---|
| `edh-dl` on your computer, "This computer" | your computer | the same process (`backend.Local`) |
| `edh-dl` on your computer, a server tab | your computer | the server, through `edh-dl agent` over SSH (`backend.Remote`) |
| `edh-dl serve` on a server, opened through the launcher or a tunnel | the server, shown in your browser | the server, same process |
| `edh-dl run job.json --detach` | none | the machine where you type it |

`backend.Local` and `backend.Remote` have the same interface (`await call(method, **kwargs)`) and return the same
JSON, so the UI code does not know which one it is talking to. See [Remote use](remote.md) for the SSH side.

## Processes

| Process | Started by | Lifetime | Code |
|---|---|---|---|
| App (connect shell) | `edh-dl` / `edh-dl app` | until you quit it | [gui/connect.py](../src/edh_downloader/gui/connect.py) |
| Web UI | the app (one page per connection) or `edh-dl serve` | as long as the page is served | [gui/app.py](../src/edh_downloader/gui/app.py) |
| Agent | the app, over SSH | until the connection closes | [agent.py](../src/edh_downloader/agent.py) |
| Job worker | `runner.launch` (Start, Resume, `edh-dl run --detach`) | until the job stops | [runner.py](../src/edh_downloader/runner.py), [engine.py](../src/edh_downloader/engine.py), [sources/cds/engine.py](../src/edh_downloader/sources/cds/engine.py) |

A job worker is the same executable started as `edh-dl _worker <job folder>`, in a new session (`setsid`), with
no terminal. It does not depend on the process that started it, so it survives a closed browser, a closed SSH
session and a restarted app. The UI and the worker share only files: the worker writes `state.json` and reads
`control.json`. See [Jobs](jobs.md).

## Module map

| Area | Modules | Role |
|---|---|---|
| Interface | `gui/connect.py`, `gui/app.py`, `gui/structure.py`, `gui/tour.py`, `gui/theme.py`, `gui/logo.py`, `cli.py` | Servers and tabs, the job form, the jobs list, the guided tour, the command line |
| Service | `service.py`, `backend.py`, `agent.py` | Everything the interface asks for, in process or over SSH |
| Jobs | `job.py`, `runner.py` | The job specification (`job.json`), job folders, detached workers, pause and cancel |
| Sources | `sources/__init__.py` | The registry: what each source can browse and download |
| Earth Data Hub | `catalog.py`, `planner.py`, `engine.py`, `http.py`, `codecs.py`, `sample.py`, `quota.py` | STAC catalogue, chunk plan, async engine, authenticated HTTP, Zarr chunk decoding, sampling, request accounting |
| Climate Data Store | `sources/cds/catalog.py`, `planner.py`, `client.py`, `engine.py`, `output.py`, `grib.py`, `netcdf.py` | Forms and constraints, request packing, retrieve API client, ledger and queue, output layout, GRIB and NetCDF reading |
| Output | `aggregate.py`, `chunking.py`, `store.py` | Time statistics and bit-rounding, chunk layouts, Icechunk and Zarr |
| Remote | `remote.py`, `sshconfig.py`, `bundle.py`, `launcher.py` | SSH sessions, `~/.ssh/config`, the agent zip, the launcher scripts |
| Keys | `keys.py` | API keys of every source |
| Releases | `update.py`, `packaging/` | Self-update and the standalone build |

## Files on disk

Everything lives under three folders (XDG locations, each of them can be moved with an environment variable):

| Folder | Default | Override | Holds |
|---|---|---|---|
| Config | `~/.config/edh-dl` | `EDH_DL_CONFIG_DIR` | `keys.json` (0600), `servers.json` (0600, never a password) |
| Data | `~/.local/share/edh-dl` | `EDH_DL_DATA_DIR` | `jobs/<id>/`, `usage.json`; on a server also the agent's `venv-<deps id>/` and `agent/<build>.zip` |
| Cache | `~/.cache/edh-dl` | `EDH_DL_CACHE_DIR` | catalogues, coordinates, CDS forms and constraints, sample chunks |

The output stores go wherever the job says (`output.directory`, `~/edh-data` by default).

The folder names and the `edh-dl` command keep the project's first name (*EDH Downloader*), so existing keys,
jobs and servers keep working.

## Adding a source

A source is registered in [sources/__init__.py](../src/edh_downloader/sources/__init__.py) with what the rest of
the app needs to know: its id and names, where to get a key, whether it has a monthly quota, what checking a
key costs, and whether it can run jobs. Jobs, keys and UI state
carry the source id. A new source needs:

- a catalogue (datasets and their variables),
- a planner that turns a `JobSpec` into units of work,
- an engine with the same folder, state, control and stop handling as `Engine` (the CDS engine reuses them),
- an output object with the interface `store.py` and `chunking.py` expect: periods, units, dimensions, chunks.
  The CDS `CdsOutput` is the example to follow.
