"""Reduce source time steps into output periods (vectorised, NaN-aware)."""

import numpy as np


def reduce_periods(arr: np.ndarray, starts: np.ndarray, stat: str) -> np.ndarray:
    """Reduce ``arr`` along axis 0 over periods beginning at ``starts`` (offsets into arr, ascending,
    first == 0; each period runs to the next start or the end). NaNs are ignored; all-NaN -> NaN."""
    if stat == "none":
        return arr
    if stat == "first":
        return arr[starts]
    if stat in ("min", "max"):
        f = np.fmin if stat == "min" else np.fmax
        return f.reduceat(arr, starts, axis=0)
    valid = ~np.isnan(arr)
    total = np.add.reduceat(np.where(valid, arr, 0).astype(np.float64), starts, axis=0)
    count = np.add.reduceat(valid, starts, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        if stat == "sum":
            out = np.where(count > 0, total, np.nan)
        elif stat == "mean":
            out = total / count
        else:
            raise ValueError(f"unknown statistic {stat}")
    return out  # float64; cast to the output dtype when writing


def bitround(a: np.ndarray, keepbits: int | None) -> np.ndarray:
    """Round float32 mantissas to ``keepbits`` bits (same as numcodecs BitRound), in place."""
    if keepbits is None or keepbits >= 23 or a.dtype != np.float32:
        return a
    a = np.ascontiguousarray(a)
    b = a.view(np.uint32)
    maskbits = 23 - keepbits
    mask = np.uint32((0xFFFFFFFF >> maskbits) << maskbits)
    half = np.uint32((1 << (maskbits - 1)) - 1)
    nan = np.isnan(a)
    b += ((b >> np.uint32(maskbits)) & np.uint32(1)) + half
    b &= mask
    a[nan] = np.nan
    return a
