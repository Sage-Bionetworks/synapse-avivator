"""CLI entry point for synapse-avivator."""

import argparse
import os
import threading
import webbrowser
from pathlib import Path
from urllib.parse import quote

import synapseclient
import uvicorn

_DEFAULT_GEN3_ENDPOINT = "https://nci-crdc.datacommons.io"
_DEFAULT_GEN3_CREDS = Path.home() / ".gen3" / "credentials.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="synapse-avivator",
        description="View Synapse or Gen3/DRS-hosted OME-TIFF images in Avivator",
    )
    parser.add_argument(
        "entity_id",
        nargs="?",
        default=None,
        help="Synapse entity ID (syn12345) or DRS URI (drs://host/object_id). Opens browser to that image.",
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
    parser.add_argument(
        "--gen3-endpoint",
        default=_DEFAULT_GEN3_ENDPOINT,
        help=f"Gen3 endpoint URL (default: {_DEFAULT_GEN3_ENDPOINT})",
    )
    parser.add_argument(
        "--gen3-creds",
        default=None,
        help=f"Path to Gen3 credentials.json (default: {_DEFAULT_GEN3_CREDS})",
    )
    parser.add_argument(
        "--hosted",
        action="store_true",
        default=False,
        help="Run in hosted mode with Synapse OAuth2 login (requires SYNAPSE_OAUTH_CLIENT_ID and SYNAPSE_OAUTH_CLIENT_SECRET env vars)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging to logs/ directory",
    )
    return parser.parse_args(argv)


def build_browser_url(port: int, entity_id: str | None) -> str:
    base = f"http://localhost:{port}"
    if entity_id is None:
        return f"{base}/"
    # DRS URI → URL path: drs://host/obj → /image/drs/host/obj.ome.tiff
    if entity_id.startswith("drs://"):
        path_part = entity_id[len("drs://"):]
        image_url = f"{base}/image/drs/{path_part}.ome.tiff"
    else:
        image_url = f"{base}/image/{entity_id}.ome.tiff"
    return f"{base}/?image_url={quote(image_url, safe='')}"


def authenticate_synapse(token: str | None) -> synapseclient.Synapse:
    syn = synapseclient.Synapse()
    auth_token = token or os.environ.get("SYNAPSE_AUTH_TOKEN")
    if auth_token:
        syn.login(authToken=auth_token, silent=True)
    else:
        syn.login(silent=True)
    return syn


def authenticate_gen3(endpoint: str, creds_path: Path | None):
    """Attempt Gen3 auth. Returns (endpoint, auth) or (None, None) if unavailable."""
    creds = Path(creds_path) if creds_path else _DEFAULT_GEN3_CREDS
    if not creds.exists():
        return None, None
    try:
        from gen3.auth import Gen3Auth
        auth = Gen3Auth(endpoint=endpoint, refresh_file=str(creds))
        return endpoint, auth
    except ImportError:
        return None, None
    except Exception as e:
        print(f"Gen3 auth failed ({e}), Gen3 sources unavailable")
        return None, None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    from synapse_avivator.proxy import set_synapse_client, set_gen3_client, set_verbose, set_oauth_config
    set_verbose(args.verbose)

    if args.hosted:
        # Hosted mode: OAuth2 login, no local Synapse client needed at startup
        from synapse_avivator.auth import OAuthConfig
        client_id = os.environ.get("SYNAPSE_OAUTH_CLIENT_ID")
        client_secret = os.environ.get("SYNAPSE_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            print("ERROR: --hosted requires SYNAPSE_OAUTH_CLIENT_ID and SYNAPSE_OAUTH_CLIENT_SECRET env vars")
            raise SystemExit(1)
        redirect_uri = f"http://localhost:{args.port}/auth/callback"
        set_oauth_config(OAuthConfig(client_id, client_secret, redirect_uri))
        print(f"Hosted mode: OAuth2 login enabled (callback: {redirect_uri})")
    else:
        # Local mode: authenticate with Synapse directly
        print("Authenticating with Synapse...")
        syn = authenticate_synapse(args.token)
        print(f"Logged in as {syn.credentials.owner_id}")
        set_synapse_client(syn)

    # Attempt Gen3 auth (optional — works if credentials exist)
    gen3_endpoint, gen3_auth = authenticate_gen3(args.gen3_endpoint, args.gen3_creds)
    if gen3_auth:
        set_gen3_client(gen3_endpoint, gen3_auth)
        print(f"Gen3 authenticated ({gen3_endpoint})")
    else:
        print("Gen3 not configured (install gen3 package + ~/.gen3/credentials.json to enable)")

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
