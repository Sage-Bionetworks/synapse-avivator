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
