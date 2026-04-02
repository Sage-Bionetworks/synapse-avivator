"""
Local proxy server that wraps Synapse presigned URL refresh for byte-range viewers.

Usage:
    uv run uvicorn proxy:app --port 8000

Then point Avivator at:
    http://localhost:8000/image/syn74307866.ome.tiff
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager

import httpx
import synapseclient
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask

from demo import SYNAPSE_AUTH_TOKEN, RefreshingUrl

log = logging.getLogger("proxy")

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


@app.api_route("/image/{full_path:path}", methods=["GET", "HEAD"])
async def proxy_image(full_path: str, request: Request) -> Response:
    if "/" in full_path:
        return Response(status_code=404)

    entity_id = full_path
    for suffix in _TIFF_SUFFIXES:
        if entity_id.lower().endswith(suffix):
            entity_id = entity_id[: -len(suffix)]
            break

    if not _SYN_ID_RE.match(entity_id):
        return Response(status_code=404)

    loop = asyncio.get_running_loop()
    getter = _getter(entity_id)
    url = await loop.run_in_executor(None, getter)

    forward: dict[str, str] = {}
    if "range" in request.headers:
        forward["Range"] = request.headers["range"]

    # Stream the response — don't buffer the full body before sending
    r = await _http.send(_http.build_request(request.method, url, headers=forward), stream=True)

    if r.status_code == 403:
        await r.aclose()
        getter.invalidate()
        url = await loop.run_in_executor(None, getter)
        r = await _http.send(_http.build_request(request.method, url, headers=forward), stream=True)

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
    log.info("%s %s range=%s -> %s %s",
             request.method, entity_id,
             forward.get("Range", "none"), r.status_code,
             resp_headers.get("content-length", "?") + "B")

    if request.method == "HEAD":
        await r.aclose()
        return Response(status_code=r.status_code, headers=resp_headers)

    return StreamingResponse(
        r.aiter_bytes(chunk_size=65536),
        status_code=r.status_code,
        headers=resp_headers,
        background=BackgroundTask(r.aclose),
    )
