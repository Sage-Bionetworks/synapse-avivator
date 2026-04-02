"""
Local proxy server that wraps Synapse presigned URL refresh for byte-range viewers.

Usage:
    uv run uvicorn proxy:app --port 8000 --workers 4

Then point Avivator at:
    http://localhost:8000/image/syn74307866.ome.tiff
"""
import asyncio
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
import synapseclient
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask

from demo import SYNAPSE_AUTH_TOKEN, RefreshingUrl

_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    _http = httpx.AsyncClient(
        follow_redirects=True,
        timeout=60,
        limits=limits,
        http2=True,  # multiplex many tile requests over fewer TLS connections
    )
    yield
    await _http.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["Range"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"],
)

_syn = synapseclient.Synapse()
if SYNAPSE_AUTH_TOKEN:
    _syn.login(authToken=SYNAPSE_AUTH_TOKEN, silent=True)
else:
    _syn.login(silent=True)

_getters: dict[str, RefreshingUrl] = {}


def _getter(entity_id: str) -> RefreshingUrl:
    if entity_id not in _getters:
        _getters[entity_id] = RefreshingUrl(entity_id, _syn)
    return _getters[entity_id]


_PASSTHROUGH_HEADERS = {
    "content-type", "content-length", "content-range", "accept-ranges", "etag",
}
_TIFF_SUFFIXES = (".ome.tiff", ".ome.tif", ".tiff", ".tif")
_SYN_ID_RE = re.compile(r"^syn\d+$")
_OFFSETS_SUFFIX = ".offsets.json"
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")

# ─── Two-tier range cache ─────────────────────────────────────────────
#
# Tier 1 — Block cache (aligned 256 KB blocks)
#   Absorbs all reads ≤ BLOCK_SIZE.  GeoTIFF.js does many 1-byte probes
#   followed by 64-128 KB re-reads at the same offset. By fetching one
#   aligned block up-front, the follow-up read is a memory hit.
#
# Tier 2 — Tile cache (exact range responses)
#   Caches tile-sized responses (> BLOCK_SIZE, ≤ TILE_CACHE_ENTRY_MAX)
#   keyed by exact range.  Revisiting a viewport serves from memory.
#
# Reads larger than TILE_CACHE_ENTRY_MAX stream through uncached.
# ──────────────────────────────────────────────────────────────────────

BLOCK_SIZE = 256 * 1024               # 256 KB aligned blocks
BLOCK_CACHE_MAX = 256 * 1024 * 1024   # 256 MB budget for blocks

TILE_CACHE_ENTRY_MAX = 2 * 1024 * 1024  # cache tiles up to 2 MB
TILE_CACHE_MAX = 256 * 1024 * 1024       # 256 MB budget for tiles

# OrderedDict gives us move-to-end for LRU + popitem(last=False) for eviction
_block_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_block_cache_bytes = 0

_tile_cache: OrderedDict[str, bytes] = OrderedDict()   # "eid:start-end" → bytes
_tile_cache_bytes = 0


def _block_get(entity_id: str, start: int, length: int) -> bytes | None:
    """Try to serve a range from an aligned block."""
    block_start = (start // BLOCK_SIZE) * BLOCK_SIZE
    key = (entity_id, block_start)
    block = _block_cache.get(key)
    if block is None:
        return None
    _block_cache.move_to_end(key)   # LRU touch
    off = start - block_start
    chunk = block[off: off + length]
    return chunk if len(chunk) == length else None


def _block_put(entity_id: str, block_start: int, data: bytes) -> None:
    global _block_cache_bytes
    key = (entity_id, block_start)
    if key in _block_cache:
        return
    _block_cache[key] = data
    _block_cache_bytes += len(data)
    while _block_cache_bytes > BLOCK_CACHE_MAX:
        _, evicted = _block_cache.popitem(last=False)
        _block_cache_bytes -= len(evicted)


def _tile_get(entity_id: str, start: int, end: int) -> bytes | None:
    key = f"{entity_id}:{start}-{end}"
    data = _tile_cache.get(key)
    if data is not None:
        _tile_cache.move_to_end(key)
    return data


def _tile_put(entity_id: str, start: int, end: int, data: bytes) -> None:
    global _tile_cache_bytes
    key = f"{entity_id}:{start}-{end}"
    if key in _tile_cache:
        return
    _tile_cache[key] = data
    _tile_cache_bytes += len(data)
    while _tile_cache_bytes > TILE_CACHE_MAX:
        _, evicted = _tile_cache.popitem(last=False)
        _tile_cache_bytes -= len(evicted)


def _make_206(data: bytes, start: int, end: int) -> Response:
    return Response(
        content=data,
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/*",
            "Content-Type": "image/tiff",
            "Content-Length": str(len(data)),
            "Accept-Ranges": "bytes",
        },
    )


@app.api_route("/image/{full_path:path}", methods=["GET", "HEAD"])
async def proxy_image(full_path: str, request: Request) -> Response:
    if "/" in full_path:
        return Response(status_code=404)

    # Serve pre-generated offsets sidecar if present on disk
    if full_path.endswith(_OFFSETS_SUFFIX):
        entity_id = full_path[: -len(_OFFSETS_SUFFIX)]
        try:
            with open(f"{entity_id}.offsets.json", "rb") as f:
                data = f.read()
            print(f"[proxy] offsets {entity_id}  {len(data)}B", flush=True)
            return Response(content=data, media_type="application/json")
        except FileNotFoundError:
            return Response(status_code=404)

    entity_id = full_path
    for suffix in _TIFF_SUFFIXES:
        if entity_id.lower().endswith(suffix):
            entity_id = entity_id[: -len(suffix)]
            break

    if not _SYN_ID_RE.match(entity_id):
        return Response(status_code=404)

    raw_range = request.headers.get("range", "")
    m = _RANGE_RE.match(raw_range) if raw_range else None
    req_start = int(m.group(1)) if m else None
    req_end   = int(m.group(2)) if m else None
    req_len   = (req_end - req_start + 1) if m else None

    # ─── Cache lookup (GET only) ──────────────────────────────
    if req_start is not None and req_len is not None and request.method == "GET":
        # Tier 2: exact tile match
        hit = _tile_get(entity_id, req_start, req_end)
        if hit is not None:
            print(f"[cache] TILE {entity_id}  bytes={req_start}-{req_end}  {req_len}B", flush=True)
            return _make_206(hit, req_start, req_end)

        # Tier 1: block-aligned match (covers small reads including 1-byte probes)
        hit = _block_get(entity_id, req_start, req_len)
        if hit is not None:
            print(f"[cache] BLOCK {entity_id}  bytes={req_start}-{req_end}  {req_len}B", flush=True)
            return _make_206(hit, req_start, req_end)

    # ─── Cache miss → fetch from S3 ──────────────────────────
    loop = asyncio.get_running_loop()
    getter = _getter(entity_id)
    url = await loop.run_in_executor(None, getter)

    # Decide fetch strategy based on request size
    if req_start is not None and req_len is not None and req_len <= BLOCK_SIZE:
        # Small read → inflate to aligned block
        block_start = (req_start // BLOCK_SIZE) * BLOCK_SIZE
        fetch_range = f"bytes={block_start}-{block_start + BLOCK_SIZE - 1}"
        strategy = "block"
    elif req_start is not None and req_len is not None and req_len <= TILE_CACHE_ENTRY_MAX:
        # Tile-sized → fetch exact, cache for revisits
        fetch_range = raw_range
        strategy = "tile"
    else:
        # Large or no range → stream through
        fetch_range = raw_range if raw_range else ""
        strategy = "stream"

    fetch_headers = {"Range": fetch_range} if fetch_range else {}

    t0 = time.monotonic()
    r = await _http.send(_http.build_request(request.method, url, headers=fetch_headers), stream=True)

    if r.status_code == 403:
        await r.aclose()
        getter.invalidate()
        url = await loop.run_in_executor(None, getter)
        r = await _http.send(_http.build_request(request.method, url, headers=fetch_headers), stream=True)

    elapsed = time.monotonic() - t0

    if request.method == "HEAD":
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
        await r.aclose()
        return Response(status_code=r.status_code, headers=resp_headers)

    # ─── Block strategy: buffer, cache block, serve requested slice ──
    if strategy == "block" and r.status_code in (200, 206):
        block_data = await r.aread()
        block_start = (req_start // BLOCK_SIZE) * BLOCK_SIZE
        _block_put(entity_id, block_start, block_data)
        off = req_start - block_start
        chunk = block_data[off: off + req_len]
        print(f"[S3→block] {entity_id}  bytes={req_start}-{req_end}  fetch={BLOCK_SIZE//1024}KB  {elapsed*1000:.0f}ms", flush=True)
        return _make_206(chunk, req_start, req_end)

    # ─── Tile strategy: buffer, cache exact range, serve ─────────────
    if strategy == "tile" and r.status_code in (200, 206):
        tile_data = await r.aread()
        _tile_put(entity_id, req_start, req_end, tile_data)
        print(f"[S3→tile] {entity_id}  bytes={req_start}-{req_end}  {len(tile_data)}B  {elapsed*1000:.0f}ms", flush=True)
        return _make_206(tile_data, req_start, req_end)

    # ─── Stream strategy: pass through ───────────────────────────────
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
    cl = resp_headers.get("content-length", "?")
    print(f"[S3→stream] {entity_id}  {raw_range or 'full'}  -> {r.status_code}  {cl}B  {elapsed*1000:.0f}ms", flush=True)

    return StreamingResponse(
        r.aiter_bytes(chunk_size=65536),
        status_code=r.status_code,
        headers=resp_headers,
        background=BackgroundTask(r.aclose),
    )
