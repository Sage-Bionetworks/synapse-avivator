# synapse-avivator Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the presigned URL proxy and a bundled Avivator frontend as a pip-installable CLI (`synapse-avivator`).

**Architecture:** Migrate existing `demo.py` and `proxy.py` into a `src/synapse_avivator/` package with proper imports. Add `cli.py` for argparse + auth + uvicorn startup + browser open. Bundle pre-built Avivator static files. Serve everything from a single FastAPI app on localhost.

**Tech Stack:** Python 3.10+, hatchling, FastAPI, uvicorn, synapseclient, httpx

---

## File Map

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata, build config, CLI entry point |
| `src/synapse_avivator/__init__.py` | Package marker with version |
| `src/synapse_avivator/__main__.py` | `python -m synapse_avivator` support |
| `src/synapse_avivator/refreshing_url.py` | `RefreshingUrl` class, `range_fetch()`, constants (from `demo.py`) |
| `src/synapse_avivator/proxy.py` | FastAPI app with caching, dedup, static file serving (from `proxy.py`) |
| `src/synapse_avivator/cli.py` | Argparse, Synapse auth, uvicorn start, browser open |
| `src/synapse_avivator/static/index.html` | Placeholder Avivator page (real build vendored separately) |
| `tests/__init__.py` | (already exists) |
| `tests/test_refreshing_url.py` | Tests migrated from `tests/test_demo.py` |
| `tests/test_cli.py` | CLI argument parsing tests |

---

### Task 1: Create package structure and `pyproject.toml`

**Files:**
- Create: `pyproject.toml`
- Create: `src/synapse_avivator/__init__.py`
- Create: `src/synapse_avivator/__main__.py`

- [ ] **Step 1: Create `pyproject.toml`**

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

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-mock>=3.12.0",
]

[project.scripts]
synapse-avivator = "synapse_avivator.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_avivator"]
```

- [ ] **Step 2: Create `src/synapse_avivator/__init__.py`**

```python
"""synapse-avivator: View Synapse-hosted OME-TIFF images in Avivator."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create `src/synapse_avivator/__main__.py`**

```python
"""Allow `python -m synapse_avivator`."""

from synapse_avivator.cli import main

main()
```

- [ ] **Step 4: Verify the directory structure**

Run: `ls -R src/synapse_avivator/`
Expected: `__init__.py` and `__main__.py` present.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/
git commit -m "chore: create package structure with pyproject.toml"
```

---

### Task 2: Migrate `RefreshingUrl` and `range_fetch` to package

**Files:**
- Create: `src/synapse_avivator/refreshing_url.py`
- Modify: `tests/test_demo.py` → rename to `tests/test_refreshing_url.py`

- [ ] **Step 1: Create `src/synapse_avivator/refreshing_url.py`**

```python
import json
import time

import requests
import synapseclient

EXPIRY_SECS = 900   # Synapse presigned URL lifetime (15 min)
BUFFER_SECS = 60    # Refresh this many seconds before expiry


class RefreshingUrl:
    """Wraps a Synapse entity, caches its presigned URL, and refreshes before expiry."""

    def __init__(self, entity_id: str, syn: synapseclient.Synapse):
        self.entity_id = entity_id
        self.syn = syn
        self._url: str | None = None
        self._fetched_at: float = 0.0

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= (EXPIRY_SECS - BUFFER_SECS)

    def _fetch(self) -> str:
        print(f"[refresh] fetching new presigned URL for {self.entity_id}")
        entity = self.syn.restGET(f"/entity/{self.entity_id}")
        file_handle_id = entity["dataFileHandleId"]
        response = self.syn.restPOST(
            "/fileHandle/batch",
            body=json.dumps({
                "requestedFiles": [{
                    "fileHandleId": file_handle_id,
                    "associateObjectId": self.entity_id,
                    "associateObjectType": "FileEntity",
                }],
                "includePreSignedURLs": True,
                "includeFileHandles": False,
                "includePreviewPreSignedURLs": False,
            }),
            endpoint=self.syn.fileHandleEndpoint,
        )
        files = response.get("requestedFiles", [])
        if not files or "preSignedURL" not in files[0]:
            raise RuntimeError(
                f"No presigned URL returned for {self.entity_id}. "
                f"Check entity permissions and file handle status."
            )
        return files[0]["preSignedURL"]

    def get(self) -> str:
        if self._url is None or self._is_stale():
            self._url = self._fetch()
            self._fetched_at = time.monotonic()
        return self._url

    def invalidate(self):
        self._url = None

    def __call__(self) -> str:
        return self.get()


def range_fetch(getter: RefreshingUrl, offset: int, length: int) -> bytes:
    """Issue a single byte-range GET. Retries once on 403 with a fresh URL."""
    url = getter()
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 403:
        getter.invalidate()
        url = getter()
        r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content
```

- [ ] **Step 2: Rename and update test file**

Rename `tests/test_demo.py` to `tests/test_refreshing_url.py`. Update all imports from `from demo import ...` to `from synapse_avivator.refreshing_url import ...`:

```bash
mv tests/test_demo.py tests/test_refreshing_url.py
```

Then replace all occurrences in `tests/test_refreshing_url.py`:

Replace `from demo import RefreshingUrl` with `from synapse_avivator.refreshing_url import RefreshingUrl`

Replace `from demo import RefreshingUrl, range_fetch` with `from synapse_avivator.refreshing_url import RefreshingUrl, range_fetch`

Also replace `with patch("requests.get"` with `with patch("synapse_avivator.refreshing_url.requests.get"` in the three range_fetch tests.

- [ ] **Step 3: Run tests**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/test_refreshing_url.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/synapse_avivator/refreshing_url.py tests/test_refreshing_url.py
git rm tests/test_demo.py
git commit -m "refactor: migrate RefreshingUrl and range_fetch to synapse_avivator package"
```

---

### Task 3: Migrate proxy to package

**Files:**
- Create: `src/synapse_avivator/proxy.py`

- [ ] **Step 1: Create `src/synapse_avivator/proxy.py`**

Copy the full content of the current `proxy.py` with these changes:

1. Replace the import line:
   ```python
   from demo import SYNAPSE_AUTH_TOKEN, RefreshingUrl
   ```
   with:
   ```python
   from synapse_avivator.refreshing_url import RefreshingUrl
   ```

2. Remove the module-level Synapse client creation (lines 67-71):
   ```python
   _syn = synapseclient.Synapse()
   if SYNAPSE_AUTH_TOKEN:
       _syn.login(authToken=SYNAPSE_AUTH_TOKEN, silent=True)
   else:
       _syn.login(silent=True)
   ```

3. Replace it with a module-level variable set by the CLI:
   ```python
   _syn: synapseclient.Synapse | None = None
   ```

4. Add a function for the CLI to inject the authenticated client:
   ```python
   def set_synapse_client(syn: synapseclient.Synapse) -> None:
       global _syn
       _syn = syn
   ```

5. Add static file serving. After the CORS middleware block, add:
   ```python
   from starlette.staticfiles import StaticFiles
   from pathlib import Path

   _static_dir = Path(__file__).parent / "static"
   if _static_dir.is_dir():
       app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="assets")

       @app.get("/")
       async def index():
           return FileResponse(_static_dir / "index.html")
   ```
   Also add `from starlette.responses import FileResponse` to the imports.

6. Update the log directory to use a `logs/` subdir in the current working directory (unchanged behavior, but use `Path`):
   ```python
   _log_dir = Path("logs")
   _log_dir.mkdir(exist_ok=True)
   ```

All caching, dedup, route logic, `_fetch_with_retry`, `_make_206`, `_learn_file_size`, `/stats`, and `/image/{full_path}` route stay exactly as-is.

- [ ] **Step 2: Verify import works**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -c "from synapse_avivator.proxy import app; print('ok')"
```

Expected: prints `ok` (plus the session log line).

- [ ] **Step 3: Commit**

```bash
git add src/synapse_avivator/proxy.py
git commit -m "refactor: migrate proxy to synapse_avivator package with injectable Synapse client"
```

---

### Task 4: Create placeholder static files

**Files:**
- Create: `src/synapse_avivator/static/index.html`
- Create: `src/synapse_avivator/static/assets/.gitkeep`

The real Avivator build will be vendored later. For now, a placeholder page that redirects to the hosted Avivator with the correct URL lets us test the full CLI flow.

- [ ] **Step 1: Create placeholder `index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>synapse-avivator</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; line-height: 1.6; }
    input { width: 100%; padding: 8px; font-size: 16px; margin: 8px 0; }
    button { padding: 8px 24px; font-size: 16px; cursor: pointer; }
    .status { color: #666; font-size: 14px; margin-top: 16px; }
  </style>
</head>
<body>
  <h1>synapse-avivator</h1>
  <p>Enter a Synapse entity ID to view in Avivator:</p>
  <input id="entity" type="text" placeholder="syn51671125" />
  <button onclick="go()">Open in Avivator</button>
  <div class="status" id="status"></div>
  <script>
    // Auto-load if image_url is in query params (set by CLI)
    const params = new URLSearchParams(window.location.search);
    const imageUrl = params.get("image_url");
    if (imageUrl) {
      window.location.href = "https://avivator.gehlenborglab.org/?image_url=" + encodeURIComponent(imageUrl);
    }

    function go() {
      const eid = document.getElementById("entity").value.trim();
      if (!eid.match(/^syn\d+$/)) {
        document.getElementById("status").textContent = "Enter a valid Synapse ID (e.g., syn51671125)";
        return;
      }
      const url = window.location.origin + "/image/" + eid + ".ome.tiff";
      window.location.href = "https://avivator.gehlenborglab.org/?image_url=" + encodeURIComponent(url);
    }

    document.getElementById("entity").addEventListener("keydown", e => {
      if (e.key === "Enter") go();
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Create assets dir placeholder**

```bash
mkdir -p src/synapse_avivator/static/assets
touch src/synapse_avivator/static/assets/.gitkeep
```

- [ ] **Step 3: Commit**

```bash
git add src/synapse_avivator/static/
git commit -m "feat: add placeholder Avivator landing page with entity ID input"
```

---

### Task 5: Implement CLI

**Files:**
- Create: `src/synapse_avivator/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests for CLI argument parsing**

Create `tests/test_cli.py`:

```python
from unittest.mock import patch, MagicMock


def test_cli_parses_entity_id():
    from synapse_avivator.cli import parse_args
    args = parse_args(["syn12345"])
    assert args.entity_id == "syn12345"
    assert args.port == 8000
    assert args.token is None


def test_cli_parses_port_and_token():
    from synapse_avivator.cli import parse_args
    args = parse_args(["--port", "9000", "--token", "abc", "syn99999"])
    assert args.entity_id == "syn99999"
    assert args.port == 9000
    assert args.token == "abc"


def test_cli_no_entity_id():
    from synapse_avivator.cli import parse_args
    args = parse_args([])
    assert args.entity_id is None


def test_cli_builds_url_with_entity():
    from synapse_avivator.cli import build_browser_url
    url = build_browser_url(port=8000, entity_id="syn51671125")
    assert "image_url=" in url
    assert "syn51671125.ome.tiff" in url
    assert "localhost:8000" in url


def test_cli_builds_url_without_entity():
    from synapse_avivator.cli import build_browser_url
    url = build_browser_url(port=8000, entity_id=None)
    assert url == "http://localhost:8000/"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/test_cli.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `cli.py`**

```python
"""CLI entry point for synapse-avivator."""

import argparse
import os
import threading
import time
import webbrowser
from urllib.parse import quote

import synapseclient
import uvicorn


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="synapse-avivator",
        description="View Synapse-hosted OME-TIFF images in Avivator",
    )
    parser.add_argument(
        "entity_id",
        nargs="?",
        default=None,
        help="Synapse entity ID (e.g., syn51671125). Opens browser to that image.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local server (default: 8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Synapse personal access token. Falls back to SYNAPSE_AUTH_TOKEN env var, then ~/.synapseConfig.",
    )
    return parser.parse_args(argv)


def build_browser_url(port: int, entity_id: str | None) -> str:
    base = f"http://localhost:{port}"
    if entity_id is None:
        return f"{base}/"
    image_url = f"{base}/image/{entity_id}.ome.tiff"
    return f"{base}/?image_url={quote(image_url, safe='')}"


def authenticate(token: str | None) -> synapseclient.Synapse:
    syn = synapseclient.Synapse()
    auth_token = token or os.environ.get("SYNAPSE_AUTH_TOKEN")
    if auth_token:
        syn.login(authToken=auth_token, silent=True)
    else:
        syn.login(silent=True)
    return syn


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    print("Authenticating with Synapse...")
    syn = authenticate(args.token)
    print(f"Logged in as {syn.credentials.owner_id}")

    # Inject the authenticated client into the proxy module
    from synapse_avivator.proxy import set_synapse_client
    set_synapse_client(syn)

    # Open browser after a short delay to let uvicorn start
    url = build_browser_url(args.port, args.entity_id)
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"Starting server on http://localhost:{args.port}")
    if args.entity_id:
        print(f"Opening {args.entity_id} in Avivator...")
    else:
        print("Opening Avivator (enter an entity ID in the UI)...")

    uvicorn.run(
        "synapse_avivator.proxy:app",
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    )
```

- [ ] **Step 4: Run CLI tests**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/test_cli.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Run all tests**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/ -v
```

Expected: all 13 tests PASS (8 refreshing_url + 5 cli).

- [ ] **Step 6: Commit**

```bash
git add src/synapse_avivator/cli.py tests/test_cli.py
git commit -m "feat: add CLI with argparse, auth, browser open, uvicorn start"
```

---

### Task 6: Wire up `pip install` and smoke test

**Files:**
- Modify: `pyproject.toml` (may need tweaks)
- No new files

- [ ] **Step 1: Install the package in dev mode**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv pip install -e ".[dev]"
```

Expected: installs successfully, `synapse-avivator` command becomes available.

- [ ] **Step 2: Verify the CLI entry point exists**

```bash
synapse-avivator --help
```

Expected: shows usage with `entity_id`, `--port`, `--token` options.

- [ ] **Step 3: Run all tests via the installed package**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/ -v
```

Expected: all 13 tests PASS.

- [ ] **Step 4: Smoke test (manual, requires Synapse auth)**

```bash
synapse-avivator syn51671125
```

Expected:
- Terminal shows "Authenticating with Synapse..." then "Logged in as ..."
- Terminal shows "Starting server on http://localhost:8000"
- Browser opens to `http://localhost:8000/?image_url=...`
- The placeholder page redirects to hosted Avivator with the proxy URL
- Image loads in Avivator via the proxy

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "chore: verify pip install and CLI smoke test"
```

---

### Task 7: Clean up old flat files

**Files:**
- Remove: `demo.py` (migrated to `src/synapse_avivator/refreshing_url.py`)
- Remove: `proxy.py` (migrated to `src/synapse_avivator/proxy.py`)
- Remove: `requirements.txt` (replaced by `pyproject.toml`)
- Keep: `generate_offsets.py` (standalone utility, stays at repo root)
- Keep: `docs/` (specs and plans)

- [ ] **Step 1: Update `generate_offsets.py` to import from new location**

Replace:
```python
from demo import ENTITY_ID, SYNAPSE_AUTH_TOKEN, RefreshingUrl, range_fetch
```
with:
```python
from synapse_avivator.refreshing_url import RefreshingUrl, range_fetch

ENTITY_ID = "syn74307866"  # default entity ID
SYNAPSE_AUTH_TOKEN = None
```

- [ ] **Step 2: Remove old files**

```bash
git rm demo.py proxy.py requirements.txt
```

- [ ] **Step 3: Run all tests one final time**

```bash
cd /Users/ataylor/Documents/projects/htan2/synapse-presigned-url-refresh && uv run python -m pytest tests/ -v
```

Expected: all 13 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add generate_offsets.py
git rm demo.py proxy.py requirements.txt
git commit -m "chore: remove old flat files, update generate_offsets.py imports"
```
