# synapse-avivator — Pip-Installable CLI Design

**Date:** 2026-04-02
**Status:** Approved

---

## Problem

The presigned URL refresh proxy works but requires users to manually run uvicorn, open Avivator in a browser, and paste a localhost URL. Packaging this as a single-command CLI makes it accessible to researchers who just want to view a Synapse-hosted OME-TIFF.

---

## Goal

A pip-installable Python package (`synapse-avivator`) that bundles a pre-built Avivator frontend. One command starts the proxy, serves the viewer, and opens the browser.

```bash
synapse-avivator syn51671125        # opens browser to that image
synapse-avivator                    # opens browser, user enters entity IDs in UI
synapse-avivator --token abc123     # explicit auth token
```

---

## Package Layout

```
synapse-avivator/
├── pyproject.toml
├── src/
│   └── synapse_avivator/
│       ├── __init__.py
│       ├── __main__.py           # python -m synapse_avivator support
│       ├── cli.py                # argparse, auth, start server, open browser
│       ├── refreshing_url.py     # RefreshingUrl class + range_fetch
│       ├── proxy.py              # FastAPI app (image proxy + static file serving)
│       └── static/               # Pre-built Avivator (~5MB, committed to git)
│           ├── index.html
│           ├── assets/
│           └── ...
├── tests/
│   ├── __init__.py
│   └── test_refreshing_url.py
└── generate_offsets.py           # standalone utility, not part of package
```

---

## CLI (`cli.py`)

```
synapse-avivator [ENTITY_ID] [--port PORT] [--token TOKEN]
```

**Arguments:**
- `ENTITY_ID` (optional) — Synapse entity ID (e.g., `syn51671125`). If provided, opens browser directly to that image.
- `--port` (default: `8000`) — port for the local server.
- `--token` — Synapse personal access token. Falls back to `SYNAPSE_AUTH_TOKEN` env var, then `~/.synapseConfig`.

**Behavior:**
1. Parse arguments.
2. Create `synapseclient.Synapse()` and authenticate (token priority: `--token` > env var > `~/.synapseConfig`).
3. Store the authenticated client in the FastAPI app state.
4. Start uvicorn (single worker, same process).
5. Open browser:
   - With entity: `http://localhost:{port}/?image_url=http://localhost:{port}/image/{entity_id}.ome.tiff`
   - Without entity: `http://localhost:{port}/`

---

## Proxy Routes (`proxy.py`)

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serve `static/index.html` |
| `/assets/*` | GET | Serve Avivator static assets |
| `/image/{full_path}` | GET, HEAD | Presigned URL proxy with caching (existing logic) |
| `/stats` | GET | Cache diagnostics JSON |

Static files served via Starlette's `StaticFiles` mount.

---

## Avivator Fork

One change to the Avivator source: on load, check for an `image_url` query parameter. If present, auto-load that URL instead of showing the load dialog.

Build the fork once locally (`npm run build`), commit the output to `src/synapse_avivator/static/`. The fork is a separate git clone used only for building — the built files are vendored into this repo.

---

## Existing Code Migration

| Current file | Becomes |
|-------------|---------|
| `demo.py` (RefreshingUrl, range_fetch, constants) | `src/synapse_avivator/refreshing_url.py` |
| `proxy.py` (FastAPI app, caches, routes) | `src/synapse_avivator/proxy.py` |
| `tests/test_demo.py` | `tests/test_refreshing_url.py` |
| `generate_offsets.py` | stays at repo root (standalone utility) |

The proxy module imports `RefreshingUrl` from `refreshing_url` instead of `demo`. The proxy creates the Synapse client from app state (passed in by CLI) instead of at module level.

---

## `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "synapse-avivator"
version = "0.1.0"
description = "View Synapse-hosted OME-TIFF images in Avivator with transparent presigned URL refresh"
requires-python = ">=3.10"
dependencies = [
    "synapseclient>=4.0.0",
    "requests>=2.31.0",
    "fastapi>=0.111.0",
    "uvicorn[standard]>=0.29.0",
    "httpx>=0.27.0",
]

[project.scripts]
synapse-avivator = "synapse_avivator.cli:main"
```

---

## Authentication Priority

1. `--token` CLI flag
2. `SYNAPSE_AUTH_TOKEN` environment variable
3. `~/.synapseConfig` file (synapseclient default)

All three already work with `synapseclient.Synapse().login()`. The CLI just needs to pass the token if provided explicitly.

---

## Success Criteria

- `pip install .` works
- `synapse-avivator syn51671125` starts server, opens browser, image loads
- `synapse-avivator` starts server, opens browser to Avivator load dialog
- Presigned URL refresh works transparently mid-session
- Tile cache serves revisited tiles from memory
- Tests pass (`pytest tests/`)

---

## Out of Scope

- PyPI publication (later, after testing)
- GitHub Pages deployment
- Hosted proxy
- Avivator UI customization beyond the `image_url` query param
- `generate_offsets.py` integration into the CLI
