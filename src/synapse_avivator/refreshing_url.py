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
    """Presigned URL manager for Gen3/DRS objects.

    Accepts a full DRS URI (drs://host/object_id) or a bare object ID.
    The endpoint and auth can be provided explicitly or parsed from the URI.
    """

    def __init__(self, drs_uri: str, default_endpoint: str | None = None, default_auth=None):
        parsed = self.parse_drs_uri(drs_uri)
        if parsed:
            self._endpoint = f"https://{parsed[0]}"
            object_id = parsed[1]
        else:
            self._endpoint = default_endpoint
            object_id = drs_uri
        super().__init__(object_id, expiry_secs=3600)  # Gen3 URLs typically 1 hour
        self._auth = default_auth

    @staticmethod
    def parse_drs_uri(uri: str) -> tuple[str, str] | None:
        """Parse drs://host/object_id → (host, object_id) or None."""
        if not uri.startswith("drs://"):
            return None
        rest = uri[len("drs://"):]
        slash = rest.find("/")
        if slash < 0:
            return None
        return rest[:slash], rest[slash + 1:]

    def _fetch(self) -> str:
        from gen3.file import Gen3File

        print(f"[refresh] fetching new presigned URL for {self.object_id} (Gen3: {self._endpoint})")
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
