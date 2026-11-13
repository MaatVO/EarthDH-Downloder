# Climate Data Store pipeline

The [Copernicus Climate Data Store](https://cds.climate.copernicus.eu/) (CDS) works the other way round from
Earth Data Hub. Nothing is read directly: every request goes into a queue, waits its turn (seconds to hours), is
produced by the CDS, and only then can be downloaded. Each request is bounded by a cost limit, and accounts that
send many requests are slowed down. A 1-field request has been seen waiting almost three hours and then running
for two seconds.

So the price of a CDS request is mostly **its place in the queue**, and the pipeline is built to send **as few
requests as possible, never the same one twice**.

![The CDS engine: plan for free, record each request in the ledger, keep a bounded queue on the CDS, poll all
requests with one call, download results as soon as they are ready, process GRIB or NetCDF into Icechunk and
Zarr; files that are not a grid are kept as delivered](diagrams/cds-engine.svg)

```text
sources/cds/catalog.py -> planner.py -> engine.py (+ client.py) -> grib.py / netcdf.py -> output.py -> store.py
 forms, constraints       requests      ledger, queue, downloads    reading the files      layout       Icechunk
```

## 1. Catalogue: forms and constraints

[sources/cds/catalog.py](../src/edh_downloader/sources/cds/catalog.py) reads the public catalogue (no token):

- `form.json`, the widgets of a dataset's download form;
- `constraints.json`, the value combinations that exist.

`describe` turns a form into what the app needs: which fields are the variables, levels, time and area (the
app's own controls fill them), which are dataset options to show as they are (`product_type` and the like),
which licences must be accepted, and how far the app can take the data:

| Tier | Datasets | What the job does |
|---|---|---|
| `grib` | gridded, with GRIB delivery: ERA5, ERA5-Land, CERRA, CARRA, seasonal forecasts | the full pipeline |
| `netcdf` | gridded files without GRIB: derived products, projections (CMIP6), satellite data | the full pipeline, once every file is downloaded |
| `timeseries` | one series per point (the form has a `location` field) | files kept as delivered |
| `table` | in-situ observations | files kept as delivered |

The constraints are used locally and for free: `allowed` shows only the values that exist together with what is
already selected, `option_state` tells which options apply, `invalid` flags a request asking for something the
dataset does not have. Everything is cached for a day.

## 2. Plan: the fewest requests

[sources/cds/planner.py](../src/edh_downloader/sources/cds/planner.py)

A CDS request is a cartesian product: years × months × days × times × variables × levels. Its cost is its number
of fields, and each dataset has a limit (121,000 fields for ERA5 single levels, 60,000 for ERA5 pressure levels;
60,000 is assumed until the CDS tells the real one).

![Four years of five ERA5 variables: one request per month gives 48 requests, packing gives 2 requests of about
87,700 fields each; the planner tries whole years, then months of a year, then splits by variables, levels and
days](diagrams/cds-packing.svg)

The rules, in order:

1. All selected variables and levels go into the same request.
2. Whole months are packed together: several months of one year, then whole years, as long as the request stays
   under the cost limit, under `max_request_gb`, and fits what the job can process.
3. A request is split only when one month alone is too big: by variables first, then levels, then days.
4. When the variables do not fit together over many years, they are split in groups if that gives fewer
   requests (each group then spans more years).
5. Months only partly selected (the first and the last, a time-zone shift, every N hours) are asked for whole
   when that saves requests. The extra steps cost a little more data; a queue slot can cost hours. The processor
   drops them.
6. Only the hours the job needs are requested: all of them when aggregating, every N hours for "first" values.
7. The area is cut and the grid changed on the CDS side when the dataset allows it (the area snapped to the
   grid; `grid` sent as a string, since a list would count twice in the cost).

Packed months share one day list. Days a month does not have (31 April) are ignored by the CDS and not counted in
its cost, and the planner counts them the same way.

`cds.pack = "month"` keeps one month per request instead, as ECMWF advises in general for tape-archived data.

The size of a request does not depend on the memory of the machine: results are read in slabs (see below).

### Free checks before launch

Before the job starts, and again when it plans (`fit_costs`):

- **Costing:** `POST processes/{id}/costing` returns `{cost, limit}` and queues nothing. The largest request is
  priced first; if the dataset's real limit differs, the plan is made again with it. Then up to 12 distinct
  request sizes are priced; if the CDS counts more than the local estimate, the plan is made again with a
  proportionally smaller limit (up to three times).
- **Constraints:** every request is checked against the dataset's value combinations.
- **Licences:** the licences your account has accepted are compared with those the dataset needs. A job that
  would be rejected does not start, and the UI links to the dataset page where you accept them.

`edh-dl plan job.json` prints the requests, their cost and any problem, and costs nothing.

## 3. The request ledger

[sources/cds/engine.py](../src/edh_downloader/sources/cds/engine.py) keeps a **ledger**, `requests.json` in the
job folder, with every planned request and what happened to it. Each change is saved **before** the step it
leads to.

![The states of one request: planned, submitting, queued, ready, downloaded, done; a crash after the POST is
reconciled with the CDS instead of resent; throttling, a vanished job or an early failure go back to planned; an
expired result goes back to planned; a third failure stops the job](diagrams/cds-ledger.svg)

| State | Meaning |
|---|---|
| `planned` | In the plan, not sent (or to be sent again) |
| `submitting` | Saved just before `POST execution`; the answer (a `jobID`) is not known yet |
| `queued` | The CDS has it, with its `jobID`: accepted or running |
| `ready` | Successful; the result's link, size and checksum are known |
| `downloaded` | The result file is in the spool |
| `done` | Every unit that needed it is committed; the file was deleted |
| `failed` | Failed 3 times on the CDS for a reason other than a queue limit; the job stops |

What this buys:

- **A crash between the POST and the save** leaves a `submitting` entry. On the next run the engine lists the
  account's recent jobs and finds the one with the same request, so it is not sent again.
- **A paused or killed job** finds its `queued` requests again and simply polls them.
- **An expired result** (the CDS keeps results for a few days) goes back to `planned`. The new request is
  identical, and the CDS usually serves it from its cache at once.
- **Identical requests are recognised by the CDS:** requests are always built in one canonical form (keys
  sorted, list values sorted and deduplicated, except `area`, `grid` and `location`, which keep their order).
- **A resumed job asks for exactly the same requests:** the plan of the first run is kept in the ledger.

## 4. The queue

![How many requests may wait on the CDS over a job's life: the number halves when the CDS refuses one and comes
back up by one after three successes, never above the job's setting](diagrams/cds-queue.svg)

- At most `max_inflight` requests (4 by default) wait or run on the CDS at once. The job card's **Queue** menu
  changes it while the job runs.
- When the CDS refuses more (HTTP 429, or a job failing with "temporarily limited" or similar), the number is
  halved and the engine backs off: 60 seconds at first, doubling up to 30 minutes. The refused request goes back
  to `planned` without counting as a failure.
- After 3 successes the number grows by one again, up to the job's setting.
- **Polling:** all waiting requests are checked with one job-list call (individual calls only for what the list
  does not cover). The interval is 5 seconds after a change and grows by half each quiet round, up to 2 minutes.
- **Spool:** results on disk (or about to be) are kept under `max_spool_gb`. Requests still waiting take no disk,
  so they may add up to one more spool's worth: the queue keeps moving while units are processed.
- **Cancel** removes the job's waiting requests from the CDS (`DELETE jobs/{id}`).

## 5. Downloads

[sources/cds/client.py](../src/edh_downloader/sources/cds/client.py)

- Results are downloaded as soon as they are ready, 2 at a time.
- An interrupted download continues with an HTTP `Range` request from the `.part` file.
- Every file is checked against the CDS's `file:checksum`, the object store's ETag: the plain MD5 for a file
  uploaded in one part, the multipart MD5 for a large one.
- A failed download is retried after 30 seconds. After 5 failures the engine asks the CDS for a new link, and
  after 2 new links the job stops.
- Result links are public: the token is sent only to the CDS API's own origin, never to the file host, also
  across redirects (followed by hand).
- Network errors, 5xx and 429 are retried with exponential backoff and jitter, honouring `Retry-After`.

## 6. Processing

The output layout comes from the files themselves, so it is built once the first result is known and saved as
`output.json` and `grid.npz` in the job folder ([output.py](../src/edh_downloader/sources/cds/output.py)). A
resumed job writes into the same layout even after those files were deleted.

**GRIB** ([grib.py](../src/edh_downloader/sources/cds/grib.py)). A result holds one message per field (variable ×
level × time step). The file is indexed from message headers only (fast, nothing decoded), then read message by
message, one variable and level at a time, so a file of several GB never has to fit in memory. A slab of time
steps uses at most a sixth of `memory_gb`. GRIB parameters are matched to the form's variable names by their
words; matches learned in earlier jobs are remembered per dataset. ecCodes is loaded only here, so Earth Data Hub
jobs work without it.

**Units.** As for Earth Data Hub: output periods are grouped into units, each one Icechunk commit. For GRIB the
timeline is known from the plan alone, before any download, and a unit can start as soon as its requests are
downloaded. A file is deleted when the last unit that needs it is committed.

**NetCDF** ([netcdf.py](../src/edh_downloader/sources/cds/netcdf.py)). The time axis, the levels and the variable
names are only known from the files, so a NetCDF job downloads everything first, then converts:

1. *unpack:* a result may be a NetCDF file, or a zip or tar of them (recognised by their first bytes: names lie).
   Archive members are extracted as flat, numbered files, so nothing lands outside the job folder;
2. *scan:* read only metadata and time coordinates: which variables are gridded fields, on which grid, at which
   times and levels. Times in other calendars (`noleap`, `360_day`, as CMIP6 uses) are mapped date by date onto
   the standard calendar;
3. *convert:* create the store, then read, cut, aggregate and commit unit by unit, in slabs of time steps.

A field the app can store has a time axis, a horizontal grid (1-D latitude and longitude, or a projected grid with
2-D coordinates) and at most one more dimension longer than 1 (levels, depths, ensemble members). Anything else
(tables, station series, two products for the same variable and time) is **kept as delivered**.

**Projected grids.** Regional datasets such as CERRA (Lambert conformal, 1069 × 1069) are always delivered over
their whole domain. The area is cut locally, and the output keeps the 2-D latitude and longitude of every cell and
the projection attributes.

**Kept as delivered.** Time series, in-situ tables and NetCDF files that are not a grid are saved in
`<output>/<name>.files/` with a `manifest.json`: each file with its request, its dates and its size, and why
the files were not converted.
