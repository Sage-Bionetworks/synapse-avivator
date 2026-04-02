# Presigned URL Refresh Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single Python script that proves Synapse presigned URLs can be transparently refreshed during a byte-range image reading session.

**Architecture:** `RefreshingUrl` manages the cached URL and expiry logic; `range_fetch()` issues Range-header HTTP requests and retries once on 403; `main()` orchestrates a four-step demo that artificially expires the URL mid-run to prove the refresh fires.

**Tech Stack:** Python 3.9+, `synapseclient`, `requests`, `pytest`

---

## File Map

| File | Purpose |
|------|---------|
| `requirements.txt` | Pin `synapseclient` and `requests` |
| `demo.py` | `EXPIRY_SECS`, `BUFFER_SECS`, `ENTITY_ID`, `SYNAPSE_AUTH_TOKEN`, `RefreshingUrl`, `range_fetch()`, `main()` |
| `tests/__init__.py` | Empty — makes `tests/` a package |
| `tests/test_demo.py` | Unit tests for `RefreshingUrl` and `range_fetch()` using mocks |

---

## Task 1: Scaffold the project

**Files:**
- Create: `requirements.txt`
- Create: `tests/__init__.py`

- [ ] **Step 1: Write `requirements.txt`**

```
synapseclient>=4.0.0
requests>=2.31.0
pytest>=8.0.0
pytest-mock>=3.12.0
```

- [ ] **Step 2: Create empty test package**

Create `tests/__init__.py` with no content.

- [ ] **Step 3: Verify pytest can discover tests**

Run: `pytest tests/ --collect-only`
Expected: `no tests ran` (or `0 tests collected`) with exit code 0 (or 5 for "no tests", which is fine).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt tests/__init__.py
git commit -m "chore: scaffold project with requirements and test package"
```

---

## Task 2: `RefreshingUrl` class

**Files:**
- Create: `demo.py`
- Create: `tests/test_demo.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_demo.py`:

```python
import time
from unittest.mock import MagicMock, patch
import pytest


def make_syn(url="https://s3.example.com/file.tiff?X-Amz-Signature=abc"):
    syn = MagicMock()
    entity = MagicMock()
    entity.id = "syn123"
    entity._file_handle = {"id": "99999"}
    syn.get.return_value = entity
    syn.restPOST.return_value = {
        "requestedFiles": [{"preSignedURL": url}]
    }
    return syn


def test_refreshing_url_returns_url_on_first_call():
    from demo import RefreshingUrl
    syn = make_syn("https://example.com/first")
    ru = RefreshingUrl("syn123", syn)
    assert ru() == "https://example.com/first"


def test_refreshing_url_caches_url_when_fresh():
    from demo import RefreshingUrl
    syn = make_syn("https://example.com/cached")
    ru = RefreshingUrl("syn123", syn)
    ru()  # prime the cache
    ru()  # should use cache
    assert syn.restPOST.call_count == 1


def test_refreshing_url_refreshes_when_stale():
    from demo import RefreshingUrl
    syn = make_syn("https://example.com/stale")
    ru = RefreshingUrl("syn123", syn)
    ru()                   # prime cache
    ru._fetched_at = 0.0   # force stale
    ru()                   # should refresh
    assert syn.restPOST.call_count == 2


def test_refreshing_url_invalidate_forces_refresh():
    from demo import RefreshingUrl
    syn = make_syn("https://example.com/invalidated")
    ru = RefreshingUrl("syn123", syn)
    ru()
    ru.invalidate()
    ru()
    assert syn.restPOST.call_count == 2


def test_refreshing_url_prints_on_refresh(capsys):
    from demo import RefreshingUrl
    syn = make_syn()
    ru = RefreshingUrl("syn123", syn)
    ru()
    captured = capsys.readouterr()
    assert "[refresh]" in captured.out
    assert "syn123" in captured.out
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_demo.py -v
```

Expected: `ModuleNotFoundError: No module named 'demo'` or similar — confirms tests are wired up correctly.

- [ ] **Step 3: Write minimal `demo.py` with `RefreshingUrl`**

Create `demo.py`:

```python
import json
import time

import requests
import synapseclient

# --- Configuration ---
ENTITY_ID = "syn..."           # Replace with a real OME-TIFF Synapse entity ID
SYNAPSE_AUTH_TOKEN = None      # Set explicitly or leave None to use ~/.synapseConfig

# --- Constants ---
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
        entity = self.syn.get(self.entity_id, downloadFile=False)
        file_handle_id = entity._file_handle["id"]
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
        )
        return response["requestedFiles"][0]["preSignedURL"]

    def get(self) -> str:
        if self._url is None or self._is_stale():
            self._url = self._fetch()
            self._fetched_at = time.monotonic()
        return self._url

    def invalidate(self):
        self._url = None

    def __call__(self) -> str:
        return self.get()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_demo.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add demo.py tests/test_demo.py
git commit -m "feat: add RefreshingUrl with expiry and invalidation"
```

---

## Task 3: `range_fetch()` function

**Files:**
- Modify: `demo.py` — append `range_fetch()` after `RefreshingUrl`
- Modify: `tests/test_demo.py` — append new tests

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_demo.py`:

```python
def test_range_fetch_returns_bytes():
    from demo import RefreshingUrl, range_fetch
    syn = make_syn("https://example.com/img.tiff")
    ru = RefreshingUrl("syn123", syn)

    mock_response = MagicMock()
    mock_response.status_code = 206
    mock_response.content = b"\x49\x49\x2a\x00"  # TIFF little-endian magic
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response) as mock_get:
        data = range_fetch(ru, offset=0, length=4)

    assert data == b"\x49\x49\x2a\x00"
    call_headers = mock_get.call_args[1]["headers"]
    assert call_headers["Range"] == "bytes=0-3"


def test_range_fetch_retries_on_403():
    from demo import RefreshingUrl, range_fetch
    syn = make_syn("https://example.com/img.tiff")
    ru = RefreshingUrl("syn123", syn)
    ru()  # prime cache

    fail_response = MagicMock()
    fail_response.status_code = 403
    fail_response.content = b""

    ok_response = MagicMock()
    ok_response.status_code = 206
    ok_response.content = b"TIFF"
    ok_response.raise_for_status = MagicMock()

    with patch("requests.get", side_effect=[fail_response, ok_response]):
        data = range_fetch(ru, offset=0, length=4)

    assert data == b"TIFF"
    assert ru._url is not None  # refreshed


def test_range_fetch_sends_correct_range_header():
    from demo import RefreshingUrl, range_fetch
    syn = make_syn("https://example.com/img.tiff")
    ru = RefreshingUrl("syn123", syn)

    mock_response = MagicMock()
    mock_response.status_code = 206
    mock_response.content = b"x" * 65536
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response) as mock_get:
        range_fetch(ru, offset=1024, length=65536)

    call_headers = mock_get.call_args[1]["headers"]
    assert call_headers["Range"] == "bytes=1024-66559"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_demo.py::test_range_fetch_returns_bytes -v
```

Expected: `ImportError` — `range_fetch` not defined yet.

- [ ] **Step 3: Add `range_fetch()` to `demo.py`**

Append after the `RefreshingUrl` class:

```python
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

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_demo.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add demo.py tests/test_demo.py
git commit -m "feat: add range_fetch with 403 retry"
```

---

## Task 4: `main()` demo orchestration

**Files:**
- Modify: `demo.py` — append `main()` and `if __name__ == "__main__"` block

- [ ] **Step 1: Add `main()` to `demo.py`**

Append to the end of `demo.py`:

```python
def main():
    # --- Login ---
    syn = synapseclient.Synapse()
    if SYNAPSE_AUTH_TOKEN:
        syn.login(authToken=SYNAPSE_AUTH_TOKEN)
    else:
        syn.login(silent=True)

    getter = RefreshingUrl(ENTITY_ID, syn)

    # Step 1: Read TIFF magic bytes (offset 0, length 8)
    print("\n[step 1] Reading TIFF magic bytes (offset=0, length=8)...")
    magic = range_fetch(getter, offset=0, length=8)
    print(f"  -> got {len(magic)} bytes: {magic!r}")
    assert magic[:2] in (b"II", b"MM"), f"Not a TIFF! Got {magic[:2]!r}"
    print("  -> valid TIFF magic confirmed")

    # Step 2: Read a 64 KB chunk (simulating a tile fetch)
    print("\n[step 2] Reading 64 KB tile (offset=0, length=65536)...")
    chunk = range_fetch(getter, offset=0, length=65536)
    print(f"  -> got {len(chunk)} bytes")

    # Step 3: Artificially expire the URL
    print("\n[step 3] Forcing URL expiry (setting _fetched_at = 0)...")
    getter._fetched_at = 0.0
    print("  -> URL marked as stale")

    # Step 4: Read again — should trigger a refresh
    print("\n[step 4] Reading 64 KB again (refresh should fire)...")
    chunk2 = range_fetch(getter, offset=0, length=65536)
    print(f"  -> got {len(chunk2)} bytes")

    print("\n[done] Demo complete. URL refresh mechanism works.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run existing tests to confirm nothing broke**

```bash
pytest tests/test_demo.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add demo.py
git commit -m "feat: add main() demo with four-step refresh narrative"
```

---

## Task 5: Manual smoke test

This task cannot be automated without real Synapse credentials. It is a manual step.

- [ ] **Step 1: Set `ENTITY_ID` in `demo.py`**

Open `demo.py` and replace:
```python
ENTITY_ID = "syn..."
```
with a real Synapse entity ID for an OME-TIFF file you have read access to (e.g. `"syn12345678"`).

- [ ] **Step 2: Run the demo**

```bash
python demo.py
```

Expected output (exact URLs will differ):

```
[refresh] fetching new presigned URL for syn12345678

[step 1] Reading TIFF magic bytes (offset=0, length=8)...
[refresh] fetching new presigned URL for syn12345678
  -> got 8 bytes: b'II*\x00...'
  -> valid TIFF magic confirmed

[step 2] Reading 64 KB tile (offset=0, length=65536)...
  -> got 65536 bytes

[step 3] Forcing URL expiry (setting _fetched_at = 0)...
  -> URL marked as stale

[step 4] Reading 64 KB again (refresh should fire)...
[refresh] fetching new presigned URL for syn12345678
  -> got 65536 bytes

[done] Demo complete. URL refresh mechanism works.
```

The `[refresh]` line in step 4 output is the proof that the mechanism works.

- [ ] **Step 3: Commit with entity ID redacted**

Revert `ENTITY_ID` back to `"syn..."` before committing:

```bash
git add demo.py
git commit -m "chore: restore placeholder ENTITY_ID after smoke test"
```
