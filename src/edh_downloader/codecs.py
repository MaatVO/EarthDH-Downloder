"""Decode raw Zarr v3 chunks fetched over HTTP, following the array's codec chain."""

import numpy as np
import numcodecs

_BYTES_CODECS = {
    "blosc": lambda c: numcodecs.Blosc(),  # decoding needs no parameters
    "zstd": lambda c: numcodecs.Zstd(),
    "gzip": lambda c: numcodecs.GZip(),
    "numcodecs.zlib": lambda c: numcodecs.Zlib(),
    "numcodecs.lz4": lambda c: numcodecs.LZ4(),
}
_NOOP_ON_DECODE = {"numcodecs.bitround", "numcodecs.quantize"}  # lossy filters: decoding is identity

DTYPES = {
    "float32": "f4", "float64": "f8", "int8": "i1", "int16": "i2", "int32": "i4", "int64": "i8",
    "uint8": "u1", "uint16": "u2", "uint32": "u4", "uint64": "u8",
}


class ChunkDecoder:
    def __init__(self, meta: dict):
        self.shape = tuple(meta["chunk_grid"]["configuration"]["chunk_shape"])
        dt = meta["data_type"]
        if dt not in DTYPES:
            raise ValueError(f"unsupported data type {dt}")
        endian = "<"
        self.bytes_codecs = []
        seen_bytes = False
        for c in meta["codecs"]:
            name = c["name"]
            if name == "bytes":
                endian = "<" if c.get("configuration", {}).get("endian", "little") == "little" else ">"
                seen_bytes = True
            elif name in _BYTES_CODECS:
                self.bytes_codecs.append(_BYTES_CODECS[name](c.get("configuration", {})))
            elif name in _NOOP_ON_DECODE and not seen_bytes:
                pass
            else:
                raise ValueError(f"unsupported codec {name}")
        self.dtype = np.dtype(endian + DTYPES[dt])
        fv = meta.get("fill_value")
        self.fill = np.nan if fv in ("NaN", None) else fv

    def decode(self, raw: bytes) -> np.ndarray:
        buf = raw
        for codec in reversed(self.bytes_codecs):
            buf = codec.decode(buf)
        return np.frombuffer(buf, dtype=self.dtype).reshape(self.shape)

    def empty(self) -> np.ndarray:
        return np.full(self.shape, self.fill, dtype=self.dtype)
