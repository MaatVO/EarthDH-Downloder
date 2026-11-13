# How Weather Dataset Downloader works

These pages explain what happens between "I want these variables over this area and period" and a finished
Zarr store: the pieces of the app, the two download pipelines, and the choices behind them. They are written for
anyone who wants to understand, change or debug the app. For installing and using it, see the
[main README](../README.md).

![From a selection to a dataset: you describe the data, a job file records it, the source's planner and engine
fetch it, a shared processor writes it to Icechunk, and an export produces the final Zarr store](diagrams/overview.svg)

## Pages

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | The parts of the app, where each one runs, and how they talk to each other |
| [Jobs](jobs.md) | The job file, the job folder, job states, pause, resume and cancel |
| [Earth Data Hub pipeline](earth-data-hub.md) | Catalogue, planning in source chunks and units, the async engine, sampling |
| [Climate Data Store pipeline](climate-data-store.md) | Dataset forms, request packing, the request ledger, the adaptive queue, GRIB and NetCDF |
| [Storage](storage.md) | Icechunk commits, the Zarr export, chunk layouts, compression, extending a store |
| [Remote use over SSH](remote.md) | Connecting to a server, the agent, the code bundle, the launcher |
| [Keys and quota](keys-and-quota.md) | Where API keys come from, how they are kept secret, Earth Data Hub request accounting |
| [Releases and self-update](releases.md) | The CI build, release numbering, and how the executable updates itself |

## Three ideas behind the app

1. **Plan first, download once.** Every job is turned into an exact plan before anything is fetched: the source
   chunks or CDS requests it needs, their cost and size. Each piece is fetched exactly once, even across pauses,
   crashes and resumes.
2. **Commit in units.** A long period is cut into *units*. Each unit is decoded, cut, aggregated and committed
   to Icechunk as one transaction. A unit is also the checkpoint a stopped job resumes from.
3. **The work stays where the data is.** The interface can run on your laptop while keys, jobs and output stay
   on a server. Jobs run as detached processes, so closing the browser or the SSH session never stops them.

## Diagrams

The diagrams are SVG files drawn by [diagrams/build.py](diagrams/build.py), with no dependencies. To change one,
edit its function and run:

```bash
python docs/diagrams/build.py
```

Colours mean the same thing in every diagram:

| Colour | Meaning |
|---|---|
| Blue | You and the interface |
| Orange | Earth Data Hub |
| Teal | Climate Data Store |
| Purple | Processing |
| Green | Storage (Icechunk, Zarr) |
| Grey | Files on disk, outside services |
| Red | Failures and fallbacks |
