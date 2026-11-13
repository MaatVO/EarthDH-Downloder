# Jobs

A *job* is one download: one selection, one output store. The interface builds it, a detached worker process
runs it, and a folder on disk is the only link between the two.

## The job file

[job.py](../src/edh_downloader/job.py) defines `JobSpec`, everything the user chose, serialised as `job.json`.
The GUI only builds it and the engine only reads it, so a job file made on one machine runs on another
(`edh-dl run job.json`).

| Section | Fields (main ones) |
|---|---|
| top level | `source` (`edh` or `cds`), `dataset`, `collection`, `variables`, `levels` |
| `time` | `start`, `end`, `start_hour`, `end_hour`, `freq` (`none`, `hours`, `day`, `month`, `year`), `hours`, `tz_offset`, `stats` per variable |
| `area` | `mode` (`global`, `bbox`, `points`), `west`, `south`, `east`, `north`, `points`, `lon_convention` |
| `output` | `directory`, `name`, `export_zarr`, `keep_icechunk`, chunk layout and sizes, `dtype`, `compressor`, `clevel`, `shuffle`, sharding, `keepbits`, `extra_attrs`, `target_mode` (`create` or `extend`) |
| `download` | `key` (a key **name**), `connections`, `max_spool_gb`, `min_free_gb`, `quota_safety`, `wait_for_quota`, `memory_gb` |
| `cds` | `grid`, `options` (other form fields), `max_inflight`, `pack` (`auto` or `month`), `max_request_gb` |

The job file never contains an API key, only the key's name. Unknown fields are ignored and missing ones take
their default, so a job written by an older or newer version still loads.

## The job folder

Each job lives in `<data>/jobs/<id>/` (`~/.local/share/edh-dl/jobs/` by default):

| File | Written by | Purpose |
|---|---|---|
| `job.json` | `runner.create_job` | The spec, the job id and its creation time |
| `state.json` | the worker | Status, progress, completed units, rates, the current activity, errors |
| `control.json` | the UI and CLI | Requests to the worker: `connections`, `max_inflight`, `action` (`pause`, `cancel`) |
| `log.txt` | the worker | The job's log (the job card's **Log**, `edh-dl log ID`) |
| `worker.out` | the worker | Standard output and errors of the process (useful if it dies before logging) |
| `run.lock`, `launch.lock` | runner | One worker per job, even after two clicks on Resume |
| `requests.json` | CDS engine | The request ledger (see [Climate Data Store](climate-data-store.md)) |
| `output.json`, `grid.npz` | CDS engine | The output layout learned from the first files, so a resumed job writes the same layout |

The temporary files (downloaded chunks or result files waiting to be processed) are not in the job folder: they go
to `<output folder>/tmp/<job id>/`, on the same disk as the output. `download.spool_dir` moves them to
`<spool_dir>/<job id>/`. They are deleted when the job completes (and the `tmp/` folder with them, once empty), and
when the job is deleted. A job started by an older version keeps using its `<job folder>/spool/`, so a resume
does not download again. A paused, interrupted or failed job keeps them so a resume does not download again.

`state.json` and `control.json` are always written atomically (temporary file, then rename), so a reader never
sees half a file.

## States

![Job states: created, starting, running, exporting, completed; running can wait for the quota, or stop as paused,
paused_quota, cancelled, failed or interrupted; Resume starts a new worker that continues](diagrams/job-states.svg)

| Status | Meaning |
|---|---|
| `created` | The folder exists, no worker has run yet |
| `starting` | A worker was launched and is planning |
| `running` | Downloading and committing units |
| `waiting_quota` | Earth Data Hub only: the local monthly limit was reached and `wait_for_quota` is on; the job sleeps until the reset |
| `exporting` | Every unit is committed; the Icechunk repository is being exported to Zarr |
| `completed` | Done. Outputs are listed in `state.json` |
| `paused` | Stopped by Pause (or Ctrl+C / SIGTERM) after the unit in progress |
| `paused_quota` | Stopped at the quota limit because `wait_for_quota` is off |
| `cancelled` | Stopped by Cancel; a CDS job also removes its requests still waiting on the CDS |
| `failed` | An error the job cannot get past; the reason is in `state.json` and the log |
| `interrupted` | Not written by the worker: the UI finds a job marked active whose process is gone (killed, machine rebooted) |

Active states are `starting`, `running`, `waiting_quota` and `exporting`. A job just launched counts as alive for
30 seconds even before its process can be recognised, so the UI never shows a fresh job as interrupted.

## Pause, resume, cancel

- **Pause** and **Cancel** write `action` into `control.json`. The worker checks it between steps and stops after
  the unit it is processing, so the repository is always at a unit boundary.
- **Connections** (Earth Data Hub) and **Queue** (CDS) also go through `control.json` and apply while the job runs.
- **Resume** starts a new worker on the same folder. It plans again (a CDS job reuses the requests saved in its
  ledger, so it asks for exactly the same ones), skips the units in `units_done`, reuses chunks or files still in
  the spool, and carries on. If the job crashed after the export, it only finishes the cleanup.

What makes resuming safe:

- a unit is committed to Icechunk in one transaction, then recorded in `units_done`;
- each source chunk (EDH) or request (CDS) is fetched once, and spool files are deleted only when no later unit
  needs them;
- the CDS ledger records each request **before** it is sent (see
  [Life of one CDS request](climate-data-store.md#3-the-request-ledger)).

## Command line

```bash
edh-dl run job.json --detach     # create the job folder and start a worker
edh-dl jobs                      # list jobs
edh-dl status ID                 # state.json, readable
edh-dl log ID                    # the log
edh-dl pause ID | resume ID | cancel ID
edh-dl resume ID --connections 8
edh-dl export ID                 # export a job's repository to Zarr again
```

`ID` can be the job folder, the full id, or any beginning or end of the id that matches one job only.
