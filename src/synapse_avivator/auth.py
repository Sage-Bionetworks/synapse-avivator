"""Synapse OAuth2 authentication for hosted mode."""

import secrets
from dataclasses import dataclass, field

import httpx

SYNAPSE_AUTHORIZE_URL = "https://signin.synapse.org"
SYNAPSE_TOKEN_URL = "https://repo-prod.prod.sagebase.org/auth/v1/oauth2/token"
SYNAPSE_USERINFO_URL = "https://repo-prod.prod.sagebase.org/auth/v1/oauth2/userinfo"
SYNAPSE_SCOPES = "openid view download"


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str  # e.g. https://your-app.com/auth/callback


@dataclass
class UserSession:
    access_token: str
    refresh_token: str | None = None
    user_id: str | None = None
    username: str | None = None


# Server-side session store: session_id → UserSession
_sessions: dict[str, UserSession] = {}


def create_session(user_session: UserSession) -> str:
    """Store a session server-side, return the session ID."""
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = user_session
    return session_id


def get_session(session_id: str | None) -> UserSession | None:
    if session_id is None:
        return None
    return _sessions.get(session_id)


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def build_authorize_url(config: OAuthConfig, state: str) -> str:
    """Build the Synapse OAuth2 authorization URL."""
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "scope": SYNAPSE_SCOPES,
        "state": state,
        "claims": '{"id_token":{"userid":null},"userinfo":{"userid":null}}',
    }
    qs = "&".join(f"{k}={httpx.URL('', params={k: v}).params}" for k, v in params.items())
    # Use httpx for proper URL encoding
    from urllib.parse import urlencode
    return f"{SYNAPSE_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(config: OAuthConfig, code: str) -> UserSession:
    """Exchange an authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNAPSE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        tokens = resp.json()

        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")

        # Fetch user info
        info_resp = await client.get(
            SYNAPSE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_info = info_resp.json() if info_resp.status_code == 200 else {}

        return UserSession(
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=user_info.get("userid"),
            username=user_info.get("userid"),
        )
