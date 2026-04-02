# Synapse Presigned URL Refresh — Demo Script Design

**Date:** 2026-04-02  
**Status:** Approved

---

## Problem

Byte-range–based image viewers (Viv, Vitessce, GeoTIFF.js) issue many small HTTP requests over the lifetime of a session. Synapse presigned URLs expire after 15 minutes. A URL baked in at load time will start returning 403s mid-session, breaking tile reads silently or loudly.

This demo proves the refresh mechanism works before integrating it into a full viewer stack.

---

## Goal

A single Python script (`demo.py`) that:

1. Obtains a Synapse presigned URL for an OME-TIFF entity (no download)
2. Issues byte-range HTTP requests against it (simulating what Viv does)
3. Transparently refreshes the URL before it expires
4. Demonstrates the refresh by artificially expiring the URL mid-run

---

## Architecture

Single file, three components:

```
demo.py
  ├── RefreshingUrl          # URL lifecycle management
  ├── range_fetch()          # byte-range HTTP with 403 retry
  └── main()                 # demo orchestration + printed narrative
```

---

## Components

### `RefreshingUrl`

Wraps a `synapseclient` instance and an entity ID. Manages one cached presigned URL.

**State:**
- `entity_id: str`
- `syn: synapseclient.Synapse`
- `_url: str | None` — current presigned URL
- `_fetched_at: float` — `time.monotonic()` timestamp of last fetch
- `_pending: threading.Event | None` — coalesces concurrent refresh calls

**Method `get() -> str`:**
- If `_url` is set and `time.monotonic() - _fetched_at < EXPIRY_SECS - BUFFER_SECS`, return `_url`
- Otherwise call `syn.get_presigned_url(entity_id)` (or equivalent), update `_url` and `_fetched_at`, print `"[refresh] fetching new presigned URL for {entity_id}"`
- Return `_url`

**Constants (top of file):**
```python
EXPIRY_SECS = 900   # Synapse presigned URL lifetime
BUFFER_SECS = 60    # refresh this many seconds before expiry
```

### `range_fetch(getter, offset, length) -> bytes`

```python
def range_fetch(getter, offset, length):
    url = getter()
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 403:
        # force refresh and retry once
        getter._url = None
        url = getter()
        r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content
```

Returns raw bytes. Caller is responsible for interpretation.

### `main()`

Runs four steps with printed output:

| Step | Action | Expected output |
|------|---------|-----------------|
| 1 | Read bytes 0–7 (TIFF magic) | `b'II'` or `b'MM'` prefix confirmed |
| 2 | Read 64 KB at offset 0 | byte count printed, no error |
| 3 | Force expiry (`getter._fetched_at = 0`) | — |
| 4 | Read 64 KB again | `[refresh]` line printed, then byte count |

---

## Configuration

At the top of `demo.py`:

```python
ENTITY_ID = "syn..."          # OME-TIFF entity on Synapse
SYNAPSE_AUTH_TOKEN = None     # or set explicitly; falls back to ~/.synapseConfig
```

---

## Dependencies

```
synapseclient
requests
```

No other dependencies. `tifffile`, `zarr`, and `fsspec` are explicitly out of scope for this demo.

---

## Success Criteria

- Script runs to completion with no exceptions
- Step 1 confirms valid TIFF magic bytes
- Step 4 prints a `[refresh]` line before succeeding, proving the URL was re-fetched

---

## Out of Scope

- Jupyter notebook UI
- Vitessce/Viv integration
- Service worker or proxy approaches
- Concurrent request coalescing (threading.Event) — kept simple for the demo
