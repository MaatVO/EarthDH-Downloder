#!/usr/bin/env bash
# Build the standalone single-file executable dist/edh-dl for the platform it runs on (Linux, macOS, Windows:
# PyInstaller does not cross-compile; .github/workflows/build.yml builds all three). No Python needed to run it.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --dev
uv run python -m edh_downloader.bundle packaging/agent.zip
uv run pyinstaller --noconfirm --clean --onefile --name edh-dl \
  --collect-all nicegui --collect-all icechunk --collect-all numcodecs --collect-all zarr \
  --collect-submodules edh_downloader --collect-submodules asyncssh \
  --copy-metadata zarr --copy-metadata xarray --copy-metadata numcodecs --copy-metadata icechunk \
  --copy-metadata nicegui --add-data "$PWD/packaging/agent.zip:." \
  --distpath dist --workpath build --specpath build \
  packaging/entry.py
ls -lh dist/edh-dl
