"""
Generate a .offsets.json sidecar for a Synapse OME-TIFF entity.

Without this file, GeoTIFF.js/Viv parses the IFD chain sequentially —
each IFD contains a pointer to the next, so they cannot be parallelized.
For a 50 GB file this takes several minutes.

With this file, all IFD positions are known upfront and can be batch-fetched
on first load, making Avivator interactive immediately.

Usage:
    uv run python generate_offsets.py              # uses ENTITY_ID from demo.py
    uv run python generate_offsets.py syn74307866  # explicit entity ID

Output:
    {entity_id}.offsets.json  — serve this via the proxy
"""
import io
import json
import sys

import requests
import synapseclient
import tifffile

from synapse_avivator.refreshing_url import RefreshingUrl, range_fetch

ENTITY_ID = "syn74307866"  # default entity ID
SYNAPSE_AUTH_TOKEN = None


class RangeFile(io.RawIOBase):
    """Synchronous file-like object backed by HTTP byte-range requests."""

    def __init__(self, getter: RefreshingUrl, size: int):
        self._getter = getter
        self._size = size
        self._pos = 0

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        elif whence == 2:
            self._pos = self._size + pos
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def readinto(self, b: bytearray) -> int:
        if self._pos >= self._size:
            return 0
        length = min(len(b), self._size - self._pos)
        data = range_fetch(self._getter, self._pos, length)
        n = len(data)
        b[:n] = data
        self._pos += n
        return n


def get_file_size(getter: RefreshingUrl) -> int:
    url = getter()
    r = requests.head(url, timeout=30)
    r.raise_for_status()
    return int(r.headers["content-length"])


def generate(entity_id: str) -> None:
    syn = synapseclient.Synapse()
    if SYNAPSE_AUTH_TOKEN:
        syn.login(authToken=SYNAPSE_AUTH_TOKEN, silent=True)
    else:
        syn.login(silent=True)

    getter = RefreshingUrl(entity_id, syn)

    print(f"Getting file size for {entity_id}...")
    size = get_file_size(getter)
    print(f"  {size:,} bytes  ({size / 1e9:.1f} GB)")

    print("Reading TIFF IFDs via range requests...")
    print("  (sequential IFD chain traversal — this will take a while for large files)")

    # 4 MB read buffer reduces round-trip count during tifffile's IFD scan
    rf = io.BufferedReader(RangeFile(getter, size), buffer_size=4 * 1024 * 1024)

    offsets = []
    with tifffile.TiffFile(rf) as tif:
        total = len(tif.pages)
        for i, page in enumerate(tif.pages):
            offsets.append(int(page.offset))
            if i % 500 == 0:
                print(f"  {i}/{total} IFDs...", flush=True)

    out_path = f"{entity_id}.offsets.json"
    with open(out_path, "w") as f:
        json.dump(offsets, f)

    print(f"Done. {len(offsets)} IFD offsets written to {out_path}")
    print(f"Restart the proxy — it will serve this file automatically.")


if __name__ == "__main__":
    entity_id = sys.argv[1] if len(sys.argv) > 1 else ENTITY_ID
    generate(entity_id)
