"""
Local proxy server that wraps Synapse presigned URL refresh for byte-range viewers.

Usage:
    uv run uvicorn proxy:app --port 8000 --workers 4

Then point Avivator at:
    http://localhost:8000/image/syn74307866.ome.tiff
"""
import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import synapseclient
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from synapse_avivator.refreshing_url import BaseRefreshingUrl, SynapseRefreshingUrl, Gen3RefreshingUrl

# ─── Session logging ──────────────────────────────────────────────────
# Quiet by default. Enable with --verbose flag to write session logs to logs/.
log = logging.getLogger("proxy")
log.setLevel(logging.WARNING)  # quiet until set_verbose(True)
_log_path: str | None = None


def set_verbose(enabled: bool) -> None:
    """Enable detailed logging to stdout + file. Called by CLI with --verbose."""
    global _log_path
    if not enabled:
        return
    log.setLevel(logging.DEBUG)
    _log_dir = Path("logs")
    _log_dir.mkdir(exist_ok=True)
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    _log_path = str(_log_dir / f"session-{session_id}.log")
    fh = logging.FileHandler(_log_path)
    fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(sh)
    log.info("session %s  log: %s", session_id, _log_path)

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
    allow_origins=["https://avivator.gehlenborglab.org", "http://localhost:3000"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["Range", "X-Synapse-Token"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"],
)


@app.middleware("http")
async def strip_token_from_log(request: Request, call_next):
    """Prevent tokens from appearing in uvicorn access logs.
    Saves the real token in request.state, then sanitizes the query string."""
    qs = request.scope.get("query_string", b"").decode()
    if "token=" in qs:
        # Parse and save the real token before sanitizing
        from urllib.parse import parse_qs
        params = parse_qs(qs)
        token_list = params.get("token", [])
        request.state.synapse_token = token_list[0] if token_list else None
        # Sanitize the query string so logs don't show the token
        cleaned = re.sub(r'token=[^&]+', 'token=***', qs)
        request.scope["query_string"] = cleaned.encode()
    else:
        request.state.synapse_token = None
    return await call_next(request)

_static_dir = Path(__file__).parent / "static"
_viewer_dir = _static_dir / "viewer"

if _static_dir.is_dir():
    # Landing page at /
    _no_cache = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/")
    async def index():
        return FileResponse(_static_dir / "index.html", headers=_no_cache)

    # Bundled Avivator viewer at /viewer
    if _viewer_dir.is_dir():
        @app.get("/viewer")
        @app.get("/viewer/")
        async def viewer():
            return FileResponse(_viewer_dir / "index.html", headers=_no_cache)

        # Serve viewer's static assets (JS/CSS) — mounted AFTER the explicit routes
        app.mount("/viewer/assets", StaticFiles(directory=_viewer_dir / "assets"), name="viewer-assets")

_syn: synapseclient.Synapse | None = None
_gen3_endpoint: str | None = None
_gen3_auth = None  # gen3.auth.Gen3Auth instance
_hosted_mode = False


def set_synapse_client(syn: synapseclient.Synapse) -> None:
    """Called by CLI (local mode) to inject the authenticated Synapse client."""
    global _syn
    _syn = syn


def set_gen3_client(endpoint: str, auth) -> None:
    """Called by CLI to inject Gen3 auth."""
    global _gen3_endpoint, _gen3_auth
    _gen3_endpoint = endpoint
    _gen3_auth = auth


def set_hosted_mode(enabled: bool) -> None:
    """Enable hosted mode — users provide tokens via X-Synapse-Token header."""
    global _hosted_mode
    _hosted_mode = enabled


def _extract_token(request: Request) -> str | None:
    """Extract Synapse token from header, middleware-saved state, or query param."""
    return (request.headers.get("x-synapse-token")
            or getattr(request.state, "synapse_token", None)
            or request.query_params.get("token"))


def _get_syn_for_request(request: Request) -> synapseclient.Synapse | None:
    """Get Synapse client. Local mode: shared client. Hosted mode: per-request from token."""
    if not _hosted_mode:
        return _syn
    token = _extract_token(request)
    if not token:
        return None
    syn = synapseclient.Synapse()
    syn.login(authToken=token, silent=True)
    return syn


# Per-token getters in hosted mode, shared in local mode
_getters: dict[str, BaseRefreshingUrl] = {}
_token_getters: dict[str, dict[str, BaseRefreshingUrl]] = {}


def _getter_for(object_id: str, request: Request) -> BaseRefreshingUrl:
    """Get or create a RefreshingUrl. In hosted mode, scoped per user token."""
    if _hosted_mode:
        token = _extract_token(request) or ""
        # Use first 8 chars of token as cache key (enough to separate users)
        token_key = token[:8] if token else ""
        if token_key not in _token_getters:
            _token_getters[token_key] = {}
        getters = _token_getters[token_key]
    else:
        getters = _getters

    if object_id not in getters:
        syn = _get_syn_for_request(request)
        if _SYN_ID_RE.match(object_id):
            if syn is None:
                raise ValueError("Not authenticated — provide a Synapse token")
            getters[object_id] = SynapseRefreshingUrl(object_id, syn)
        elif object_id.startswith("drs://"):
            if _gen3_auth is None:
                raise ValueError("Gen3 auth required for DRS URI")
            getters[object_id] = Gen3RefreshingUrl(object_id, _gen3_endpoint, _gen3_auth)
        else:
            raise ValueError(f"Unknown ID format: {object_id}")
    return getters[object_id]


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

TILE_CACHE_ENTRY_MAX = 5 * 1024 * 1024  # cache tiles up to 5 MB
TILE_CACHE_MAX = 512 * 1024 * 1024       # 512 MB budget for tiles

# OrderedDict gives us move-to-end for LRU + popitem(last=False) for eviction
_block_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_block_cache_bytes = 0

_tile_cache: OrderedDict[str, bytes] = OrderedDict()   # "eid:start-end" → bytes
_tile_cache_bytes = 0

# In-flight dedup: when multiple requests arrive for the same range before the
# first one completes, they all await the same Future instead of hitting S3 again.
_inflight: dict[str, asyncio.Future[bytes]] = {}

# Total file size per entity, learned from S3's Content-Range header
_file_sizes: dict[str, int] = {}
_CONTENT_RANGE_RE = re.compile(r"bytes \d+-\d+/(\d+)")


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


def _learn_file_size(entity_id: str, r: httpx.Response) -> None:
    """Extract total file size from S3's Content-Range header."""
    cr = r.headers.get("content-range", "")
    m = _CONTENT_RANGE_RE.match(cr)
    if m:
        _file_sizes[entity_id] = int(m.group(1))


@app.get("/stats")
async def stats():
    return {
        "log": _log_path,
        "block_cache": {"entries": len(_block_cache), "bytes": _block_cache_bytes},
        "tile_cache": {"entries": len(_tile_cache), "bytes": _tile_cache_bytes},
        "file_sizes": _file_sizes,
        "entities": list(_getters.keys()),
    }


# ─── Auth check endpoint ─────────────────────────────────────────────

@app.get("/auth/me")
async def auth_me(request: Request):
    """Report auth mode so the landing page knows whether to show the token input."""
    if not _hosted_mode:
        return {"mode": "local"}
    return {"mode": "hosted"}


async def _fetch_with_retry(method: str, url: str, headers: dict, getter, loop) -> httpx.Response:
    """Fetch from S3 with streaming, retry once on 403."""
    r = await _http.send(_http.build_request(method, url, headers=headers), stream=True)
    if r.status_code == 403:
        await r.aclose()
        getter.invalidate()
        url = await loop.run_in_executor(None, getter)
        r = await _http.send(_http.build_request(method, url, headers=headers), stream=True)
    return r


def _make_206(entity_id: str, data: bytes, start: int, end: int) -> Response:
    total = _file_sizes.get(entity_id, "*")
    return Response(
        content=data,
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Type": "image/tiff",
            "Content-Length": str(len(data)),
            "Accept-Ranges": "bytes",
        },
    )


def _parse_image_path(full_path: str) -> str | None:
    """Extract a Synapse ID or DRS URI from the URL path. Returns None if invalid.

    Handles:
      syn12345.ome.tiff                          → syn12345
      syn12345.offsets.json                       → None (offsets handled separately)
      drs/nci-crdc.datacommons.io/dg.4DFC/UUID.ome.tiff → drs://nci-crdc.datacommons.io/dg.4DFC/UUID
    """
    # Strip TIFF suffix
    cleaned = full_path
    for suffix in _TIFF_SUFFIXES:
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    # DRS path: drs/{host}/{object_id...}
    if cleaned.startswith("drs/"):
        parts = cleaned[len("drs/"):]
        slash = parts.find("/")
        if slash > 0:
            return f"drs://{parts}"
        return None

    # Synapse ID: no slashes allowed
    if "/" in cleaned:
        return None
    if _SYN_ID_RE.match(cleaned):
        return cleaned
    return None


@app.api_route("/image/{full_path:path}", methods=["GET", "HEAD"])
async def proxy_image(full_path: str, request: Request) -> Response:
    # Serve pre-generated offsets sidecar if present on disk (Synapse only)
    if full_path.endswith(_OFFSETS_SUFFIX):
        entity_id = full_path[: -len(_OFFSETS_SUFFIX)]
        if "/" not in entity_id and _SYN_ID_RE.match(entity_id):
            try:
                with open(f"{entity_id}.offsets.json", "rb") as f:
                    data = f.read()
                log.info("[proxy] offsets %s  %dB", entity_id, len(data))
                return Response(content=data, media_type="application/json")
            except FileNotFoundError:
                pass
        return Response(status_code=404)

    object_id = _parse_image_path(full_path)
    if object_id is None:
        return Response(status_code=404)

    # Use object_id as the cache/getter key from here on
    entity_id = object_id

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
            log.info("[cache] TILE %s  bytes=%d-%d  %dB", entity_id, req_start, req_end, req_len)
            return _make_206(entity_id, hit, req_start, req_end)

        # Tier 1: block-aligned match (covers small reads including 1-byte probes)
        hit = _block_get(entity_id, req_start, req_len)
        if hit is not None:
            log.info("[cache] BLOCK %s  bytes=%d-%d  %dB", entity_id, req_start, req_end, req_len)
            return _make_206(entity_id, hit, req_start, req_end)

    # ─── Auth check (hosted mode) ──────────────────────────────
    if _hosted_mode:
        if not _extract_token(request):
            return Response(status_code=401, content="Provide token via X-Synapse-Token header or ?token= query param")

    # ─── Cache miss → fetch from S3 ──────────────────────────
    loop = asyncio.get_running_loop()
    getter = _getter_for(entity_id, request)
    url = await loop.run_in_executor(None, getter)

    # Decide fetch strategy based on request size
    if req_start is not None and req_len is not None and req_len <= 16:
        strategy = "block"
    elif req_start is not None and req_len is not None and req_len <= TILE_CACHE_ENTRY_MAX:
        strategy = "tile"
    else:
        strategy = "stream"

    # ─── Block strategy ──────────────────────────────────────────────
    if strategy == "block":
        block_start = (req_start // BLOCK_SIZE) * BLOCK_SIZE
        fetch_range = f"bytes={block_start}-{block_start + BLOCK_SIZE - 1}"
        t0 = time.monotonic()
        r = await _fetch_with_retry(request.method, url, {"Range": fetch_range}, getter, loop)
        elapsed = time.monotonic() - t0

        if request.method == "HEAD":
            resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
            await r.aclose()
            return Response(status_code=r.status_code, headers=resp_headers)

        if r.status_code in (200, 206):
            _learn_file_size(entity_id, r)
            block_data = await r.aread()
            _block_put(entity_id, block_start, block_data)
            off = req_start - block_start
            chunk = block_data[off: off + req_len]
            log.info("[S3→block] %s  bytes=%d-%d  fetch=%dKB  %dms", entity_id, req_start, req_end, BLOCK_SIZE // 1024, elapsed * 1000)
            return _make_206(entity_id, chunk, req_start, req_end)

    # ─── Tile strategy with inflight dedup ───────────────────────────
    if strategy == "tile":
        dedup_key = f"{entity_id}:{req_start}-{req_end}"

        # Another request for the same range is already in flight — wait for it
        if dedup_key in _inflight:
            log.info("[dedup] %s  bytes=%d-%d  waiting", entity_id, req_start, req_end)
            data = await _inflight[dedup_key]
            return _make_206(entity_id, data, req_start, req_end)

        # We're the first — create a Future so concurrent requests can wait on us
        fut: asyncio.Future[bytes] = loop.create_future()
        _inflight[dedup_key] = fut
        try:
            t0 = time.monotonic()
            r = await _fetch_with_retry(request.method, url, {"Range": raw_range}, getter, loop)
            elapsed = time.monotonic() - t0

            if request.method == "HEAD":
                resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
                await r.aclose()
                return Response(status_code=r.status_code, headers=resp_headers)

            if r.status_code in (200, 206):
                _learn_file_size(entity_id, r)
                tile_data = await r.aread()
                _tile_put(entity_id, req_start, req_end, tile_data)
                log.info("[S3→tile] %s  bytes=%d-%d  %dB  %dms", entity_id, req_start, req_end, len(tile_data), elapsed * 1000)
                fut.set_result(tile_data)
                return _make_206(entity_id, tile_data, req_start, req_end)
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            _inflight.pop(dedup_key, None)

    # ─── Stream strategy (large reads) with dedup ────────────────────
    dedup_key = f"{entity_id}:{raw_range}" if raw_range else None

    if dedup_key and dedup_key in _inflight:
        log.info("[dedup] %s  %s  waiting", entity_id, raw_range)
        data = await _inflight[dedup_key]
        total = _file_sizes.get(entity_id, "*")
        return Response(content=data, status_code=206,
                        headers={"Content-Range": f"bytes {req_start}-{req_end}/{total}",
                                 "Content-Type": "image/tiff",
                                 "Content-Length": str(len(data))})

    fetch_headers = {"Range": raw_range} if raw_range else {}
    if dedup_key:
        fut = loop.create_future()
        _inflight[dedup_key] = fut

    try:
        t0 = time.monotonic()
        r = await _fetch_with_retry(request.method, url, fetch_headers, getter, loop)
        elapsed = time.monotonic() - t0

        if request.method == "HEAD":
            resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
            await r.aclose()
            return Response(status_code=r.status_code, headers=resp_headers)

        _learn_file_size(entity_id, r)

        # Buffer large reads too so we can dedup — they're at most a few MB
        data = await r.aread()
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
        cl = len(data)
        log.info("[S3→large] %s  %s  -> %d  %dB  %dms", entity_id, raw_range or "full", r.status_code, cl, elapsed * 1000)

        if dedup_key:
            fut.set_result(data)

        return Response(content=data, status_code=r.status_code, headers=resp_headers)
    except Exception as exc:
        if dedup_key:
            fut.set_exception(exc)
        raise
    finally:
        if dedup_key:
            _inflight.pop(dedup_key, None)
