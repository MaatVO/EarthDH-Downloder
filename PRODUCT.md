# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Climate and weather researchers who need a subset of a weather or climate dataset from Earth Data Hub or the Copernicus Climate Data Store (ERA5, CERRA and related products) for analysis in Xarray. They understand variables, pressure levels, grids and time resolution, and often work on a remote Linux server over SSH. Downloading is a chore on the way to their actual science, not their job.

## Product Purpose

Weather Dataset Downloader turns "I need these variables, over this region and period, at this resolution" into an analysis-ready Icechunk or Zarr v3 store, without pulling whole datasets and without gambling the monthly API allowance.

Success: a researcher configures one large job correctly the first time, knows what it will cost before it starts, walks away, and finds a complete, correct dataset when they come back.

## Positioning

Three promises define the product, in no fixed order:

- **Know the cost before spending.** Requests, download size, output size and quota impact are visible before anything starts. An optional sampling run measures real transfer speed and compression using 12 real API requests, only after explicit confirmation.
- **Jobs survive anything.** Downloads run as detached background jobs, independent of the browser and the SSH session. Every completed processing unit is committed to Icechunk, so interrupted jobs resume without re-downloading committed chunks.
- **Only the data you need.** Precise selection of variables, pressure levels, time range, spatial domain (global, bounding box, drawn on a map, or points) and per-variable aggregation, instead of whole-dataset downloads.

Remote execution over SSH is an important capability that supports these promises; it is not one of the headline promises.

## Operating Context

The typical session is configure once, then check back: an occasional, careful setup of one large job that runs for hours or days, followed by brief check-ins on progress. The setup is high-stakes and attentive; the check-ins are short and need to answer "is it fine, and how long is left?" quickly.

Workflow: configure the API key, select dataset and variables, set time and space selection, review the download plan, configure storage, start the job, monitor it in the Jobs tab.

Users arrive through the standalone executable (which opens a connection page in the browser to pick an SSH host or run locally), through `edh-dl serve`, or through the SSH launcher. The browser may be closed and reopened at any point while jobs continue.

## Capabilities and Constraints

- Built with Python 3.12+ and NiceGUI; the interface is a browser UI served by the application, including in the packaged Linux, macOS and Windows executables.
- Earth Data Hub allows 500,000 API requests per month, and each source chunk retrieved counts as one request. Earth Data Hub does not expose remaining quota to API-key clients, so usage is tracked locally per key and calendar month. Requests from other tools are not counted automatically; users can calibrate manually against the official quota page. Quota figures must be presented as locally tracked, not authoritative.
- The default quota stopping threshold is 90%. Jobs can pause or wait for the monthly reset when it is reached.
- Jobs can be paused, resumed and cancelled, and parallel connections can be changed while running.
- Bounding-box selection is antimeridian-safe.
- Output is Icechunk, with optional export to Zarr v3. Zarr export offers a chunk-layout comparison (Time Series, Maps, Automatic, Custom), compression codecs, sharding, data type and bit-rounding. When Zarr export is selected, the temporary Icechunk repository is removed after a successful export.
- One job writes to one Icechunk repository; multiple jobs must not target the same output repository.
- API keys are never written into job specifications or output metadata; only the key name is recorded. On remote use, keys, computation and output stay on the server.
- Terminology to preserve: source chunks, processing units, job, plan, sampling, quota, parallel connections, Icechunk, Zarr v3.

## Brand Commitments

- Product name: Weather Dataset Downloader (formerly EDH Downloader). CLI command: `edh-dl` (kept for compatibility).
- The project is independently developed and must never be presented as an official Earth Data Hub or Copernicus application.
- Licensed under Apache 2.0.

Voice, personality and visual identity are undecided.

## Evidence on Hand

- `README.md` is the factual source for features and workflows.
- `tests/` runs against a local mock Earth Data Hub, without using API quota.
- There are no testimonials, user counts, adoption figures, or published performance benchmarks. Do not fabricate them.

## Product Principles

1. **No surprises with quota.** Nothing that consumes real API requests happens without the user seeing the cost and choosing it.
2. **Get the one big setup right.** Favor clarity and validation during configuration over speed of repeat entry; a wrong job can waste days and a large share of the monthly allowance.
3. **Reward the glance.** A returning user should learn job health, progress and time remaining at once, and trust that closing the page changes nothing.
4. **Speak the researcher's language.** Use the terms of the data (variables, levels, grids, chunks) precisely, and explain storage and download mechanics where they affect the result.
5. **Be honest about what is known.** Distinguish estimates from measurements and locally tracked figures from official ones.
