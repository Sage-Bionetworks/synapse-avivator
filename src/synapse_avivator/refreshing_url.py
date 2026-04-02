import json
import time

import requests
import synapseclient

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
