<div align="center">

# EDH Downloader

### A powerful, resumable downloader for Earth Data Hub datasets.

Download, subset, aggregate and store large-scale climate and meteorological datasets in **Icechunk and Zarr v3**, through an interactive web interface or command-line workflow.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Zarr](https://img.shields.io/badge/Zarr-v3-orange)](https://zarr.dev/)
[![Icechunk](https://img.shields.io/badge/Storage-Icechunk-blue)](https://icechunk.io/)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
[![Build](https://img.shields.io/github/actions/workflow/status/MaatVO/EarthDH-Downloder/build.yml?label=build)](.github/workflows/build.yml)
[![GitHub Release](https://img.shields.io/github/v/release/MaatVO/EarthDH-Downloder)](https://github.com/MaatVO/EarthDH-Downloder/releases)

[Features](#features) · [Installation](#installation) · [Getting Started](#getting-started) · [CLI](#command-line-interface) · [Output](#output-formats) · [Architecture](#architecture)

</div>

---

## Overview

**EDH Downloader** is a Python application designed to make downloading and preparing large-scale Earth observation and climate datasets from [Earth Data Hub](https://earthdatahub.destine.eu/) straightforward, efficient and reproducible.

It provides an interactive web interface, a command-line interface and a remote execution workflow over SSH.

Rather than downloading entire datasets, users can precisely define their data requirements, estimate download costs, select spatial and temporal subsets, configure aggregation strategies and choose how their data should be stored.

Downloads run as independent background jobs. They can be interrupted, resumed or monitored without keeping the browser open or maintaining an active SSH connection.

### Why EDH Downloader?

Working with large meteorological datasets often involves more than retrieving files. It requires managing API quotas, selecting relevant variables, extracting spatial subsets, handling temporal resolutions and organizing data into formats suitable for scientific computing.

EDH Downloader brings these operations together in a single workflow.

## Features

### 🌍 Dataset discovery and selection

- Browse available datasets directly from the Earth Data Hub catalogue.
- Discover variables and pressure levels from live STAC metadata and Zarr store structures.
- Support for ERA5 Single Levels, ERA5 Pressure Levels, ERA5-Land and monthly/daily products.
- Inspect available temporal and spatial coordinates before downloading.

### 📅 Flexible temporal selection

- Define custom start and end dates.
- Retrieve data at its original temporal resolution or resample it.
- Supported output frequencies:
  - Original resolution
  - Every N hours
  - Daily
  - Monthly
  - Yearly
- Configure aggregation independently for each variable:
  - Mean
  - Minimum
  - Maximum
  - Sum
  - First value
- Configure the day boundary using a custom UTC offset.

### 🗺️ Spatial subsetting

Choose exactly which geographical data you need:

- **Global:** download the entire spatial domain.
- **Bounding box:** select a latitude/longitude region.
- **Interactive map:** draw the geographical selection directly on the map.
- **Point extraction:** retrieve the nearest grid cell for a list of geographical coordinates.

Bounding-box selection is antimeridian-safe, making it suitable for regions crossing the ±180° longitude boundary.

### 📊 Download planning and cost estimation

Understand the cost of a download before starting it.

The planning engine estimates:

- Number of API requests (source chunks).
- Expected download size.
- Expected output size.
- Remaining monthly API quota.
- Estimated storage requirements.

An optional sampling operation downloads a selection of real source chunks to measure actual transfer speeds, compression ratios and output sizes.

It can also recommend a suitable number of parallel connections.

Sampling uses 12 real API requests and requires explicit confirmation.

### ⚡ Resumable background downloads

Downloads are designed to be robust, even when working with large datasets or unreliable connections.

- Jobs execute independently of the web interface.
- Close your browser or disconnect from SSH without stopping a download.
- Pause, resume and cancel running jobs.
- Monitor progress, transfer rates, estimated completion time and API usage.
- Dynamically change the number of parallel connections.
- Recover interrupted jobs without downloading already committed chunks again.

Each download is organized into processing units aligned with the source data's chunk structure.

Every completed unit is committed to Icechunk, providing a reliable checkpoint for recovery.

### 🧊 Modern scientific storage

EDH Downloader uses [Icechunk](https://icechunk.io/) as its transactional storage layer and supports exporting datasets to standard [Zarr v3](https://zarr.dev/) stores.

Features include:

- Chunk-aligned downloads.
- Transactional commits.
- Resumable processing.
- Configurable chunk layouts.
- Optional sharding.
- Multiple compression codecs.
- Float32 and Float64 output.
- Optional bit-rounding for reduced storage requirements.
- Consolidated metadata for convenient Xarray access.

### 🔌 Remote execution over SSH

Run the application on a remote Linux server and access it through your local browser.

The integrated SSH workflow provides:

- SSH key, agent and password authentication.
- Compatibility with existing `~/.ssh/config` aliases.
- Support for `ProxyJump`, custom ports and certificates.
- Automatic SSH tunnelling and port selection.
- Remote background job management.
- Independent execution on multiple servers.

The interface runs locally on your computer, while computation, API keys, downloads and output files remain on the remote machine.

### 🖥️ Cross-platform standalone application

EDH Downloader can be distributed as a standalone executable.

Pre-built releases are provided through GitHub Releases for:

- Linux
- macOS
- Windows

The packaged application supports automatic updates, with release checksums verified before installation.

---

## Installation

### Option 1 — Standalone application (recommended)

Download the latest executable for your operating system from:

**[GitHub Releases](https://github.com/MaatVO/EarthDH-Downloder/releases)**

Launch the application:

```bash
./edh-dl
```

On Windows, launch the downloaded executable.

The application opens a connection page in your browser, where you can select a remote server or run everything locally.

### Option 2 — Install from source

Requirements:

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/).

Clone the repository:

```bash
git clone https://github.com/MaatVO/EarthDH-Downloder.git

cd EarthDH-Downloder
```

Install dependencies:

```bash
uv sync
```

Start the application:

```bash
uv run edh-dl serve
```

### Option 3 — Install as a Python tool

If you already have the repository locally:

```bash
uv tool install /path/to/EarthDH-Downloder
```

Then launch it using:

```bash
edh-dl
```

---

## Getting Started

The first time you open the application, it offers a short guided tour of the whole procedure, from the SSH connection to the download. You can skip it, and replay it at any time from **Tour** in the top bar.

### 1. Configure your Earth Data Hub API key

Obtain an API key from [Earth Data Hub](https://earthdatahub.destine.eu/).

You can configure it through the application's **Keys** tab or using the CLI:

```bash
edh-dl keys add my-key
```

Keys are stored locally in:

```text
~/.config/edh-dl/keys.json
```

The application also supports reading credentials from `~/.netrc`:

```text
machine data.earthdatahub.destine.eu
login YOUR_USERNAME
password YOUR_API_KEY
```

API keys are never written into job specifications or output metadata. Only the associated key name is recorded.

### 2. Select your dataset

Open the application and browse the available Earth Data Hub datasets.

Select the dataset and the variables you want to retrieve.

For pressure-level products, you can also select the required pressure levels.

### 3. Configure your selection

Define:

- Temporal range.
- Temporal resolution and aggregation.
- Spatial domain.
- Output variables and pressure levels.

The application will retrieve the necessary catalogue metadata and prepare a download plan.

### 4. Review the download plan

Before starting, inspect:

- Estimated number of requests.
- Expected download size.
- Expected output size.
- API quota consumption.

Optionally run a sampling operation to benchmark actual transfer performance.

### 5. Configure storage

Select your preferred output format and configure:

- Chunk layout.
- Compression codec.
- Compression level.
- Sharding.
- Data type.
- Optional bit-rounding.
- Additional global metadata.

### 6. Start and monitor the job

Start the download and monitor its progress through the **Jobs** tab.

You can pause, resume, cancel or adjust the parallel connections of a running job.

Jobs remain available when you reconnect to the application.

---

## Remote Execution

EDH Downloader supports two approaches to remote execution.

### A. Using the desktop application

The recommended workflow is to run the standalone application on your local computer.

1. Launch `edh-dl`.
2. Open the connection page.
3. Select an SSH host or enter a server address.
4. Authenticate using your SSH key, agent or password.
5. Start working through the browser interface.

The first connection automatically prepares the required Python environment on the remote server.

The server does not need a manually installed copy of EDH Downloader.

The application creates an environment under:

```text
~/.local/share/edh-dl
```

It installs the required dependencies once and subsequently transfers only the application code when updates are necessary.

Existing SSH configuration is respected, including aliases, users, ports, keys and ProxyJump settings.

### B. Using the SSH launcher

For environments where you prefer not to run the desktop application, EDH Downloader provides a lightweight SSH launcher.

On macOS, Linux or WSL:

```bash
ssh me@server '/path/to/edh-dl launcher sh' > edh-connect

chmod +x edh-connect

./edh-connect
```

You can also specify another SSH destination:

```bash
./edh-connect other@host
```

On Windows 10/11:

```powershell
ssh me@server "/path/to/edh-dl launcher ps1" > edh-connect.ps1

powershell -ExecutionPolicy Bypass -File edh-connect.ps1
```

The launcher automatically:

- Establishes the SSH tunnel.
- Starts the remote web interface.
- Selects available local and remote ports.
- Transfers a fresh access token through the SSH connection.
- Opens the application in your browser.

No manual port forwarding is required.

Closing the launcher terminates the connection and remote interface, but **does not terminate running download jobs**.

---

## API Quota Management

Earth Data Hub provides free API access with a monthly allowance of **500,000 requests**.

Each source chunk retrieved counts as one request.

EDH Downloader includes its own quota accounting system to help manage consumption.

Usage is tracked per API key and calendar month in:

```text
~/.local/share/edh-dl/usage.json
```

The application:

- Tracks requests across download jobs.
- Calculates remaining quota based on locally recorded usage.
- Supports manual calibration against the official quota page.
- Allows configuring a maximum quota utilization threshold.
- Can pause jobs or wait for the next monthly reset when the threshold is reached.

The default stopping threshold is 90%.

**Important:** Earth Data Hub does not expose the remaining quota directly to API-key clients. Consequently, the application maintains its own usage records. Requests made by other tools are not automatically accounted for.

You can calibrate usage using the official [Earth Data Hub quota page](https://earthdatahub.destine.eu/).

---

## Command-Line Interface

In addition to the web interface, EDH Downloader provides a CLI for scripting, automation and headless workflows.

### Dataset discovery

List available datasets:

```bash
edh-dl datasets
```

Inspect the variables, pressure levels and temporal range of a dataset:

```bash
edh-dl variables era5-pressure-levels
```

### Download planning

Create a job specification through the web interface and save it as `job.json`.

Estimate the cost of the operation:

```bash
edh-dl plan job.json
```

Optionally benchmark the download using real source chunks:

```bash
edh-dl plan job.json --sample
```

This operation uses 12 API requests and asks for confirmation.

### Execute downloads

Start a detached download:

```bash
edh-dl run job.json --detach
```

### Job management

The CLI supports the following operations:

```bash
edh-dl jobs

edh-dl status ID

edh-dl log ID

edh-dl pause ID

edh-dl resume ID

edh-dl resume ID --connections 8

edh-dl cancel ID

edh-dl export ID
```

### Development and local server

Start the web interface:

```bash
edh-dl serve
```

Generate a standalone executable:

```bash
packaging/build.sh
```

---

## Output Formats

EDH Downloader supports two complementary storage formats.

### Icechunk

Icechunk is the default storage backend for CLI downloads.

A typical output directory contains:

```text
output/
└── dataset.icechunk/
```

Icechunk provides transactional commits and allows the downloader to process datasets incrementally.

Each completed processing unit is committed independently, making interrupted jobs resumable without repeating completed downloads.

### Zarr v3

The application can also export the resulting dataset into a standard Zarr v3 store:

```text
output/
├── dataset.icechunk/
└── dataset.zarr/
```

The exported Zarr store can be accessed directly using Xarray.

The export process supports configurable chunking, sharding, compression and data types.

When Zarr export is selected, the temporary Icechunk repository is removed after successful export.

If only Icechunk output is selected, the repository is retained.

### Reading the output with Xarray

```python
import xarray as xr

ds = xr.open_zarr("~/edh-data/dataset.zarr")

print(ds)
```

### Reading directly from Icechunk

```python
import icechunk
import xarray as xr

storage = icechunk.local_filesystem_storage(
    "~/edh-data/dataset.icechunk"
)

repo = icechunk.Repository.open(storage)

session = repo.readonly_session("main")

ds = xr.open_zarr(
    session.store,
    consolidated=False
)

print(ds)
```

### Dataset structure

Depending on the selected dataset and extraction strategy, the output contains:

| Dimension / Coordinate | Description |
|---|---|
| `valid_time` | Time coordinate; period start for aggregated data |
| `latitude` | Latitude coordinate |
| `longitude` | Longitude coordinate |
| `isobaricInhPa` | Optional pressure-level dimension |
| `point` | Optional dimension for point extraction |
| `point_name` | Optional coordinate identifying extracted points |

Data values are stored as `float32` by default.

The application also records the original selection and processing configuration in the root attributes:

- `edh_job`
- `edh_plan`

This provides useful provenance information for downstream scientific workflows.

---

## Chunking and Compression

Choosing an appropriate chunk layout is important when working with large multidimensional datasets.

EDH Downloader includes a chunk-layout comparison step that evaluates four strategies:

| Strategy | Recommended use |
|---|---|
| Time Series | Point data or small spatial areas over long periods |
| Maps | Large geographical areas over relatively few time steps |
| Automatic | General-purpose workloads |
| Custom | User-defined chunk dimensions |

The comparison includes:

- Chunk dimensions and sizes.
- Total number of chunks.
- Estimated cost of reading a complete grid-cell time series.
- Estimated cost of reading a geographical map.

The Icechunk repository always uses the Automatic download-aligned layout.

This ensures each source-aligned chunk is written only once, avoiding unnecessary rewrites during incremental processing.

The final Zarr export can use a different layout through a local, memory-bounded rechunking operation.

### Supported compression codecs

- Blosc + Zstandard
- Blosc + LZ4
- Blosc + LZ4HC
- Blosc + zlib
- Zstandard
- gzip
- No compression

Additional options include compression levels, shuffle filters, optional sharding and configurable floating-point precision.

---

## Architecture

EDH Downloader is organized around a modular download and processing pipeline.

```text
                  ┌──────────────────────┐
                  │    User Interface    │
                  │      CLI / Web       │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │       Catalog        │
                  │  STAC + Zarr Metadata│
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │       Planner        │
                  │ Selection & Chunking │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │        Engine        │
                  │ Async Chunk Download │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │      Processor       │
                  │ Decode / Cut /       │
                  │ Aggregate / Write    │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │       Icechunk       │
                  │ Transactional Store  │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │        Export        │
                  │      Zarr v3         │
                  └──────────────────────┘
```

### Main components

| Component | Responsibility |
|---|---|
| `catalog` | Reads STAC catalogue information and consolidated Zarr metadata. |
| `planner` | Translates user selections into source chunk indices and processing units. |
| `engine` | Downloads source chunks asynchronously using configurable parallel connections. |
| `processor` | Decodes, subsets, aggregates and writes data into Icechunk. |
| `store` | Handles storage operations and Zarr export. |
| `runner` | Launches and manages detached background workers. |

The download engine uses bounded temporary storage and prefetches a limited number of processing units ahead of the processor.

This allows large downloads to run without requiring the entire dataset to fit in memory.

---

## Technology Stack

| Technology | Purpose |
|---|---|
| Python 3.12+ | Core application |
| [NiceGUI](https://nicegui.io/) | Interactive web interface |
| [aiohttp](https://docs.aiohttp.org/) | Asynchronous HTTP requests |
| [AsyncSSH](https://asyncssh.readthedocs.io/) | SSH connections and remote execution |
| [Icechunk](https://icechunk.io/) | Transactional scientific data storage |
| [Zarr](https://zarr.dev/) | Chunked multidimensional array storage |
| [Xarray](https://docs.xarray.dev/) | Multidimensional scientific data |
| [NumPy](https://numpy.org/) | Numerical operations |
| [Pandas](https://pandas.pydata.org/) | Tabular data operations |
| [Numcodecs](https://numcodecs.readthedocs.io/) | Compression and encoding |
| [uv](https://docs.astral.sh/uv/) | Dependency and environment management |
| [PyInstaller](https://pyinstaller.org/) | Standalone executable packaging |
| [pytest](https://docs.pytest.org/) | Automated testing |

---

## Testing

The project includes automated tests using a local mock Earth Data Hub implementation.

Run the test suite with:

```bash
uv run pytest
```

Tests do not consume Earth Data Hub API quota.

For offline development, set:

```bash
export EDH_DL_OFFLINE=1
```

This prevents the application from performing uncached metadata requests, making it useful when developing the interface without accessing the remote catalogue.

---

## Notes and Limitations

- Supported Zarr layouts include `(time, lat, lon)`, `(time, level, lat, lon)` with a level chunk size of 1, and static `(lat, lon)` arrays.
- One download job writes to one Icechunk repository. Do not run multiple jobs targeting the same output repository.
- Daily sums of accumulated ERA5 variables use the observations whose `valid_time` falls within the selected day. This corresponds to hours 00–23 UTC, adjusted for the configured UTC offset. ECMWF daily totals conventionally use hours 01–24, so an appropriate offset should be applied when reproducing that convention.
- API quota tracking is maintained locally and cannot automatically account for requests made by external applications.
- The desktop application requires SSH access to remote servers. The remote environment requires Python 3.12 or newer, with `uv` used when available or `python3 -m venv` as a fallback.

---

## Credits and Acknowledgements

EDH Downloader builds upon the open scientific data ecosystem and the infrastructure provided by:

- **[Earth Data Hub](https://earthdatahub.destine.eu/)** — data access, catalogue services and underlying meteorological datasets.
- **[Zarr](https://zarr.dev/)** — scalable storage for multidimensional scientific data.
- **[Icechunk](https://icechunk.io/)** — transactional storage and versioning for chunked scientific datasets.
- **[Xarray](https://xarray.dev/)** — tools for working with labelled multidimensional arrays.

The project is independently developed and is not an official Earth Data Hub application.

## License

This project is distributed under the **Apache License 2.0**.

See [LICENSE](LICENSE) for the full license text.

---

<div align="center">

**Built for efficient access to large-scale Earth data.**

If you find EDH Downloader useful, consider giving the project a ⭐ on GitHub.

[Repository](https://github.com/MaatVO/EarthDH-Downloder) · [Issues](https://github.com/MaatVO/EarthDH-Downloder/issues) · [Releases](https://github.com/MaatVO/EarthDH-Downloder/releases)

</div>
