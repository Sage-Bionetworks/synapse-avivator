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
