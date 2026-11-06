import numpy as np

from edh_downloader.aggregate import bitround, reduce_periods


def test_reduce_daily_and_uneven():
    a = np.arange(48, dtype=np.float32).reshape(48, 1)
    starts = np.array([0, 24])
    assert np.allclose(reduce_periods(a, starts, "mean")[:, 0], [11.5, 35.5])
    assert np.allclose(reduce_periods(a, starts, "sum")[:, 0], [276, 852])
    assert np.allclose(reduce_periods(a, starts, "min")[:, 0], [0, 24])
    assert np.allclose(reduce_periods(a, starts, "max")[:, 0], [23, 47])
    assert np.allclose(reduce_periods(a, starts, "first")[:, 0], [0, 24])
    assert np.allclose(reduce_periods(a, np.array([0, 10]), "mean")[:, 0], [4.5, 28.5])


def test_reduce_nan():
    a = np.array([[np.nan, 1], [np.nan, 3]], dtype=np.float32)
    for stat in ("mean", "sum", "min", "max"):
        r = reduce_periods(a, np.array([0]), stat)
        assert np.isnan(r[0, 0])
    assert reduce_periods(a, np.array([0]), "mean")[0, 1] == 2


def test_bitround():
    a = np.array([1.2345678, np.nan, -3.3], dtype=np.float32)
    r = bitround(a.copy(), 13)
    assert np.isnan(r[1]) and abs(r[0] - 1.2345678) < 1e-3
