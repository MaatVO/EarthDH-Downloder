#!/usr/bin/env bash
# Build the standalone single-file executable dist/edh-dl for the platform it runs on (Linux, macOS, Windows:
# PyInstaller does not cross-compile; .github/workflows/build.yml builds all three). No Python needed to run it.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --dev
# CI passes the release number (e.g. 0.2.7); the app shows it and compares it with the latest release to self-update.
# A local build gets "<pyproject version>+local": it never replaces itself with a release at startup (it would
# otherwise see the release as newer and swap itself out before you could try your changes).
if [ -n "${EDH_DL_VERSION:-}" ]; then
  printf '__version__ = "%s"\n' "$EDH_DL_VERSION" > src/edh_downloader/_version.py
else
  base=$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')
  printf '__version__ = "%s+local"\n' "$base" > src/edh_downloader/_version.py
fi
uv run python -m edh_downloader.bundle packaging/agent.zip
uv run pyinstaller --noconfirm --clean --onefile --name edh-dl \
  --additional-hooks-dir "$PWD/packaging/hooks" --collect-all icechunk --collect-all numcodecs --collect-all zarr \
  --collect-submodules edh_downloader --collect-submodules asyncssh \
  --copy-metadata zarr --copy-metadata xarray --copy-metadata numcodecs --copy-metadata icechunk \
  --copy-metadata nicegui --add-data "$PWD/src/edh_downloader/gui/search_select.js:edh_downloader/gui" --exclude-module uvloop --exclude-module watchfiles --add-data "$PWD/packaging/agent.zip:." \
  --distpath dist --workpath build --specpath build \
  packaging/entry.py
ls -lh dist/edh-dl
