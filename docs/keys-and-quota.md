# Keys and quota

## Where keys come from

[keys.py](../src/edh_downloader/keys.py) keeps the API keys of every source. Each key belongs to one source, and
each source has its own default key.

| Source | Stored keys | Found automatically |
|---|---|---|
| Earth Data Hub | `~/.config/edh-dl/keys.json` | `~/.netrc`, entry `machine data.earthdatahub.destine.eu` (the password is the key) |
| Climate Data Store | `~/.config/edh-dl/keys.json` | `~/.cdsapirc` (or `$CDSAPI_RC`), or the `CDSAPI_URL` / `CDSAPI_KEY` variables: what the `cdsapi` package uses |

Keys are added in the **Keys** tab or with `edh-dl keys add NAME [--source cds]`. `keys.json` is written with
permissions 0600. A damaged `keys.json` is set aside as `keys.json.bad` instead of stopping the app.

A `keys.json` written before the app had sources has no source on its keys: they are Earth Data Hub keys, and its
`default` stays the Earth Data Hub default, so older versions keep reading it.

## How keys stay secret

- Everything outside `keys.py` refers to a key by its **name** and a **fingerprint**: the first 12 hex digits of
  its SHA-256, which cannot be turned back into the key.
- Job files, state files, logs and output attributes contain names and fingerprints, never keys.
- On a server, keys live on the server. The UI on your computer only receives names and fingerprints.
- The CDS token is sent only to the CDS API's own origin, never to the hosts that serve result files.

## Checking a key

| Source | What a check does | Cost |
|---|---|---|
| Earth Data Hub | reads `zarr.json` of the private test dataset | 1 request of the quota |
| Climate Data Store | reads the account profile | free, nothing queued |

## Earth Data Hub quota

Earth Data Hub gives each key a monthly allowance (500,000 requests by default; `edh-dl keys quota NAME N` sets
another) and does not tell API-key clients how much is left. So the app counts its own requests
([quota.py](../src/edh_downloader/quota.py)).

- **Where:** `~/.local/share/edh-dl/usage.json`, per key fingerprint and calendar month (UTC). The file is locked
  while it is updated, so every job on the machine shares the same count.
- **How:** each request is reserved before it is sent. Jobs flush their counts to the file in batches of 50 and
  read back what other jobs added.
- **Limit:** a job stops at `quota × quota_safety` (90% by default). With `wait_for_quota` it waits for the reset
  (`waiting_quota`) and continues; without it, it stops as `paused_quota`.
- **Calibrate:** requests made by other tools are not counted. Enter the "used" number shown on the
  [Earth Data Hub quota page](https://earthdatahub.destine.eu/) (Keys tab, or `edh-dl keys calibrate`) and the
  app adds the difference as an offset for the rest of the month.

What costs requests:

| Action | Requests |
|---|---|
| Browsing the catalogue, thumbnails | none (public STAC) |
| Opening a dataset (structure and coordinates) | a few, cached for a day |
| A sample | 12 by default, after confirmation |
| A job | one per source chunk the plan lists (`plan.n_requests`), each fetched once |

The Climate Data Store has no request quota: its limits are the cost limit of each request and the queue. See
[Climate Data Store](climate-data-store.md#4-the-queue).
