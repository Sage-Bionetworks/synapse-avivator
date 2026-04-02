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
