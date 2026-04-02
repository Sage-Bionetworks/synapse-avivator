"""
Local proxy server that wraps Synapse presigned URL refresh for byte-range viewers.

Usage:
    uv run uvicorn proxy:app --port 8000

Then point Avivator at:
    http://localhost:8000/image/syn74307866.ome.tiff
"""
import asyncio
import re
import time
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
    _http = httpx.AsyncClient(follow_redirects=True, timeout=60, limits=limits)
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

# Block cache: absorbs GeoTIFF.js's 1-byte probe + immediate re-read pattern.
# When a tiny read comes in, we fetch a full BLOCK from S3 and cache it.
# The follow-up read at the same offset is then a local memory hit.
# Keyed by (entity_id, block_start). LRU eviction at CACHE_MAX_BLOCKS entries.
BLOCK_SIZE = 131072          # 128 KB per cache block
CACHE_MAX_BLOCKS = 512       # ~64 MB total cache

_block_cache: dict[tuple[str, int], bytes] = {}
_block_cache_order: list[tuple[str, int]] = []  # insertion order for LRU eviction


def _cache_get(entity_id: str, start: int, length: int) -> bytes | None:
    block_start = (start // BLOCK_SIZE) * BLOCK_SIZE
    key = (entity_id, block_start)
    block = _block_cache.get(key)
    if block is None:
        return None
    off = start - block_start
    chunk = block[off: off + length]
    return chunk if len(chunk) == length else None


def _cache_put(entity_id: str, block_start: int, data: bytes) -> None:
    key = (entity_id, block_start)
    if key in _block_cache:
        return  # already cached
    _block_cache[key] = data
    _block_cache_order.append(key)
    while len(_block_cache_order) > CACHE_MAX_BLOCKS:
        evict = _block_cache_order.pop(0)
        _block_cache.pop(evict, None)


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

    # --- Cache read path ---
    if req_start is not None and req_len is not None and request.method == "GET":
        cached = _cache_get(entity_id, req_start, req_len)
        if cached is not None:
            print(f"[cache] HIT  {entity_id}  bytes={req_start}-{req_end}  {req_len}B", flush=True)
            headers = {
                "Content-Range": f"bytes {req_start}-{req_end}/*",
                "Content-Type":  "image/tiff",
                "Content-Length": str(len(cached)),
            }
            return Response(content=cached, status_code=206, headers=headers)

    loop = asyncio.get_running_loop()
    getter = _getter(entity_id)
    url = await loop.run_in_executor(None, getter)

    # When request is tiny (≤16 bytes), inflate to a full BLOCK so the
    # follow-up read at the same offset is served from cache.
    if req_start is not None and req_len is not None and req_len <= 16:
        block_start = (req_start // BLOCK_SIZE) * BLOCK_SIZE
        fetch_range = f"bytes={block_start}-{block_start + BLOCK_SIZE - 1}"
        fetch_headers = {"Range": fetch_range}
        inflate = True
    else:
        fetch_headers = {"Range": raw_range} if raw_range else {}
        inflate = False

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

    if inflate and r.status_code in (200, 206):
        # Buffer the block, cache it, then serve just the requested slice
        block_data = await r.aread()
        block_start = (req_start // BLOCK_SIZE) * BLOCK_SIZE
        _cache_put(entity_id, block_start, block_data)
        off = req_start - block_start
        chunk = block_data[off: off + req_len]
        print(f"[proxy] GET {entity_id}  bytes={req_start}-{req_end}  inflated→{BLOCK_SIZE//1024}KB  {elapsed*1000:.0f}ms  cached", flush=True)
        headers = {
            "Content-Range":  f"bytes {req_start}-{req_end}/*",
            "Content-Type":   "image/tiff",
            "Content-Length": str(len(chunk)),
        }
        return Response(content=chunk, status_code=206, headers=headers)

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
    rng = raw_range or "full"
    cl  = resp_headers.get("content-length", "?")
    print(f"[proxy] GET {entity_id}  {rng}  -> {r.status_code}  {cl}B  {elapsed*1000:.0f}ms", flush=True)

    return StreamingResponse(
        r.aiter_bytes(chunk_size=65536),
        status_code=r.status_code,
        headers=resp_headers,
        background=BackgroundTask(r.aclose),
    )
