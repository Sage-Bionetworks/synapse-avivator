"""
Local proxy server that wraps Synapse presigned URL refresh for byte-range viewers.

Usage:
    uv run uvicorn proxy:app --port 8000

Then point Avivator at:
    http://localhost:8000/image/syn74307866
"""
import asyncio
import re
from contextlib import asynccontextmanager

import httpx
import synapseclient
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from demo import SYNAPSE_AUTH_TOKEN, RefreshingUrl

_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http
    _http = httpx.AsyncClient(follow_redirects=True, timeout=60)
    yield
    await _http.aclose()


app = FastAPI(lifespan=lifespan)

# Allow browser-based viewers (Avivator, Vitessce) to make cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["Range"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"],
)

# Shared Synapse client — authenticated once at startup
_syn = synapseclient.Synapse()
if SYNAPSE_AUTH_TOKEN:
    _syn.login(authToken=SYNAPSE_AUTH_TOKEN, silent=True)
else:
    _syn.login(silent=True)

# One RefreshingUrl per entity, created on first request
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


@app.api_route("/image/{full_path:path}", methods=["GET", "HEAD"])
async def proxy_image(full_path: str, request: Request) -> Response:
    # Reject sub-path probes like "syn123/METADATA.ome.xml"
    if "/" in full_path:
        return Response(status_code=404)

    # Strip TIFF extension hint so Avivator can declare the format in the URL
    entity_id = full_path
    for suffix in _TIFF_SUFFIXES:
        if entity_id.lower().endswith(suffix):
            entity_id = entity_id[: -len(suffix)]
            break

    # Reject anything that isn't a bare Synapse ID (e.g. .offsets.json probes)
    if not _SYN_ID_RE.match(entity_id):
        return Response(status_code=404)

    getter = _getter(entity_id)
    # synapseclient is synchronous — run in thread pool to avoid blocking the event loop
    loop = asyncio.get_running_loop()
    url = await loop.run_in_executor(None, getter)

    forward: dict[str, str] = {}
    if "range" in request.headers:
        forward["Range"] = request.headers["range"]

    r = await _http.request(request.method, url, headers=forward)

    if r.status_code == 403:
        getter.invalidate()
        url = await loop.run_in_executor(None, getter)
        r = await _http.request(request.method, url, headers=forward)

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}

    # HEAD: return headers only, no body
    if request.method == "HEAD":
        return Response(status_code=r.status_code, headers=resp_headers)

    return Response(content=r.content, status_code=r.status_code, headers=resp_headers)
