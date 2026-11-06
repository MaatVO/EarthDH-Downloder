"""Where the app keeps its configuration, state, cache and jobs (XDG locations, overridable)."""

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    p = Path(os.environ.get("EDH_DL_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config") / "edh-dl")
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    p = Path(os.environ.get("EDH_DL_DATA_DIR") or _xdg("XDG_DATA_HOME", ".local/share") / "edh-dl")
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = Path(os.environ.get("EDH_DL_CACHE_DIR") or _xdg("XDG_CACHE_HOME", ".cache") / "edh-dl")
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_dir() -> Path:
    p = data_dir() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p
