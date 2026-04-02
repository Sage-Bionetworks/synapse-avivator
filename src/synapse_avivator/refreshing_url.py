import json
import time
from abc import ABC, abstractmethod

import requests
import synapseclient

BUFFER_SECS = 60  # Refresh this many seconds before expiry


class BaseRefreshingUrl(ABC):
    """Base class for presigned URL managers with caching and auto-refresh."""

    def __init__(self, object_id: str, expiry_secs: int):
        self.object_id = object_id
        self._expiry_secs = expiry_secs
        self._url: str | None = None
        self._fetched_at: float = 0.0

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= (self._expiry_secs - BUFFER_SECS)

    @abstractmethod
    def _fetch(self) -> str: ...

    def get(self) -> str:
        if self._url is None or self._is_stale():
            self._url = self._fetch()
            self._fetched_at = time.monotonic()
        return self._url

    def invalidate(self):
        self._url = None

    def __call__(self) -> str:
        return self.get()


class SynapseRefreshingUrl(BaseRefreshingUrl):
    """Presigned URL manager for Synapse entities."""

    def __init__(self, entity_id: str, syn: synapseclient.Synapse):
        super().__init__(entity_id, expiry_secs=900)  # 15 min
        self.syn = syn

    def _fetch(self) -> str:
        print(f"[refresh] fetching new presigned URL for {self.object_id} (Synapse)")
        entity = self.syn.restGET(f"/entity/{self.object_id}")
        file_handle_id = entity["dataFileHandleId"]
        response = self.syn.restPOST(
            "/fileHandle/batch",
            body=json.dumps({
                "requestedFiles": [{
                    "fileHandleId": file_handle_id,
                    "associateObjectId": self.object_id,
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
                f"No presigned URL returned for {self.object_id}. "
                f"Check entity permissions and file handle status."
            )
        return files[0]["preSignedURL"]


class Gen3RefreshingUrl(BaseRefreshingUrl):
    """Presigned URL manager for Gen3/DRS objects."""

    def __init__(self, guid: str, gen3_endpoint: str, gen3_auth):
        super().__init__(guid, expiry_secs=3600)  # Gen3 URLs typically 1 hour
        self._endpoint = gen3_endpoint
        self._auth = gen3_auth

    def _fetch(self) -> str:
        from gen3.file import Gen3File

        print(f"[refresh] fetching new presigned URL for {self.object_id} (Gen3)")
        file_client = Gen3File(self._endpoint, self._auth)
        result = file_client.get_presigned_url(self.object_id, protocol="s3")
        if not result or "url" not in result:
            raise RuntimeError(
                f"No presigned URL returned for {self.object_id}. "
                f"Check Gen3 credentials and object permissions."
            )
        return result["url"]


# Backward-compatible alias — proxy and tests use this name
RefreshingUrl = SynapseRefreshingUrl


def range_fetch(getter: BaseRefreshingUrl, offset: int, length: int) -> bytes:
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
