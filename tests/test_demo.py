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
    result = ru()  # should use cache
    assert syn.restPOST.call_count == 1
    assert result == "https://example.com/cached"


def test_refreshing_url_refreshes_when_stale():
    from demo import RefreshingUrl
    syn = MagicMock()
    entity = MagicMock()
    entity.id = "syn123"
    entity._file_handle = {"id": "99999"}
    syn.get.return_value = entity
    syn.restPOST.side_effect = [
        {"requestedFiles": [{"preSignedURL": "https://example.com/v1"}]},
        {"requestedFiles": [{"preSignedURL": "https://example.com/v2"}]},
    ]
    ru = RefreshingUrl("syn123", syn)
    assert ru() == "https://example.com/v1"
    ru._fetched_at = 0.0
    assert ru() == "https://example.com/v2"
    assert syn.restPOST.call_count == 2


def test_refreshing_url_invalidate_forces_refresh():
    from demo import RefreshingUrl
    syn = MagicMock()
    entity = MagicMock()
    entity.id = "syn123"
    entity._file_handle = {"id": "99999"}
    syn.get.return_value = entity
    syn.restPOST.side_effect = [
        {"requestedFiles": [{"preSignedURL": "https://example.com/v1"}]},
        {"requestedFiles": [{"preSignedURL": "https://example.com/v2"}]},
    ]
    ru = RefreshingUrl("syn123", syn)
    assert ru() == "https://example.com/v1"
    ru.invalidate()
    assert ru() == "https://example.com/v2"
    assert syn.restPOST.call_count == 2


def test_refreshing_url_prints_on_refresh(capsys):
    from demo import RefreshingUrl
    syn = make_syn()
    ru = RefreshingUrl("syn123", syn)
    ru()
    captured = capsys.readouterr()
    assert "[refresh]" in captured.out
    assert "syn123" in captured.out
