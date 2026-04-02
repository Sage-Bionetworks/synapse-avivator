"""CLI entry point for synapse-avivator."""

import argparse
import os
import threading
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

    from synapse_avivator.proxy import set_synapse_client
    set_synapse_client(syn)

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
