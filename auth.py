"""
auth.py — Angel One SmartAPI authentication with TOTP.
Returns an authenticated SmartConnect session object.
"""

import os
import time
import pyotp
from loguru import logger
import config

# SmartAPI hardcodes os.path.join("logs", date) relative to CWD in EVERY call.
# Keep CWD permanently at /tmp so all SmartAPI log writes go to /tmp/logs/
# (always writable) instead of /app/logs/ which is a root-owned volume mount.
os.makedirs("/tmp/logs", exist_ok=True)
os.chdir("/tmp")

from SmartApi import SmartConnect  # noqa: E402  (must come after chdir)


_session: SmartConnect | None = None
_auth_token: str | None = None
_refresh_token: str | None = None


def login() -> SmartConnect:
    """
    Authenticate with Angel One SmartAPI using Client ID, MPIN, and TOTP.
    Returns the authenticated SmartConnect object.
    Re-uses existing session if already logged in.
    """
    global _session, _auth_token, _refresh_token

    if _session is not None:
        return _session

    obj = SmartConnect(api_key=config.API_KEY)
    totp_code = pyotp.TOTP(config.TOTP_SECRET).now()

    logger.info(f"🔐 Logging in as {config.CLIENT_ID} ...")
    data = obj.generateSession(config.CLIENT_ID, config.PASSWORD, totp_code)

    if not data.get("status"):
        raise ConnectionError(
            f"SmartAPI login failed: {data.get('message', 'Unknown error')}"
        )

    _session       = obj
    _auth_token    = data["data"]["jwtToken"]
    _refresh_token = data["data"]["refreshToken"]

    logger.info(f"✅ Logged in to Angel One SmartAPI | Mode: {config.TRADING_MODE.upper()}")
    return _session


def get_session() -> SmartConnect:
    """
    Return active session.
    Delegates to SessionManager (the production session manager) so that
    market_data.py and auth.py both use the single shared SmartConnect
    object — avoiding a second login() on the first API call.
    """
    try:
        from session_manager import SessionManager
        sm = SessionManager.get()
        if sm._smart is not None:
            return sm._smart
    except Exception:
        pass
    # Fallback to local login (used in tests / standalone scripts)
    return _session if _session else login()


def logout() -> None:
    """Terminate the SmartAPI session gracefully."""
    global _session, _auth_token, _refresh_token
    if _session:
        try:
            _session.terminateSession(config.CLIENT_ID)
            logger.info("👋 SmartAPI session terminated.")
        except Exception as exc:
            logger.warning(f"Session termination warning: {exc}")
        finally:
            _session = _auth_token = _refresh_token = None


def refresh_session() -> None:
    """Refresh JWT token before it expires (~daily)."""
    global _session, _auth_token
    if _session and _refresh_token:
        try:
            data = _session.generateToken(_refresh_token)
            if data.get("status"):
                _auth_token = data["data"]["jwtToken"]
                logger.info("🔄 SmartAPI token refreshed.")
        except Exception as exc:
            logger.warning(f"Token refresh failed, re-logging in: {exc}")
            _session = None
            login()
