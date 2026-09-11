"""
session_manager.py — Robust SmartAPI session with auto-reconnect,
heartbeat monitoring, and exponential-backoff retry.

Replaces the simple auth.py login/logout pattern with a production-grade
session that:
  - Auto-reconnects on network drops or token expiry
  - Sends heartbeat pings every 60s to detect stale connections
  - Retries failed API calls up to MAX_RETRIES times
  - Thread-safe singleton session
"""

import os
import time
import json
import threading
import pyotp
from loguru import logger
from SmartApi import SmartConnect

import config

os.makedirs("/tmp/logs", exist_ok=True)
os.chdir("/tmp")
from SmartApi import SmartConnect  # noqa: F811
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ── SDK monkey-patch: fix persistent connection bug ───────────
#
# SmartAPI SDK v1.5.5 bug: _request() calls module-level requests.request()
# instead of self.reqsession, so every API call opens a NEW TCP connection.
# Angel One's server-side rate limiter triggers on rapid connection open/close
# cycles (not just request count), causing AB1021 even at very low call rates.
#
# Fix: replace _request() with a version that uses self.reqsession (the
# persistent requests.Session with connection pooling) so all API calls
# reuse the same TCP connection to Angel One's servers.
#
import requests as _requests
import urllib.parse as _urlparse

def _patched_request(self, route, method, parameters=None):
    """Patched _request that uses self.reqsession (persistent connection pool)."""
    params  = parameters.copy() if parameters else {}
    uri     = self._routes[route].format(**params)
    url     = _urlparse.urljoin(self.root, uri)
    headers = self.requestHeaders()

    if self.access_token:
        headers["Authorization"] = "Bearer {}".format(self.access_token)

    try:
        if method in ("POST", "PUT"):
            r = self.reqsession.request(
                method, url,
                data=json.dumps(params),
                headers=headers,
                verify=not self.disable_ssl,
                allow_redirects=True,
                timeout=self.timeout,
                proxies=self.proxies,
            )
        else:
            r = self.reqsession.request(
                method, url,
                params=json.dumps(params),
                headers=headers,
                verify=not self.disable_ssl,
                allow_redirects=True,
                timeout=self.timeout,
                proxies=self.proxies,
            )
    except Exception as exc:
        logger.error(f"HTTP {method} {url} failed: {exc}")
        raise exc

    if "json" in headers.get("Content-type", ""):
        try:
            data = json.loads(r.content.decode("utf8"))
        except ValueError:
            # Non-JSON response (rate-limit HTML/plain-text page)
            raise Exception(
                f"Couldn't parse the JSON response received from the server: {r.content}"
            )
        if data.get("status", False) is False:
            logger.error(
                f"API error {method} {url}: {data.get('message')} | {params}"
            )
        return data
    elif "csv" in headers.get("Content-type", ""):
        return r.content
    else:
        raise Exception(
            f"Unknown Content-type ({headers.get('Content-type')}) response: {r.content}"
        )

# Apply patch to the SmartConnect class (affects all instances)
SmartConnect._request = _patched_request
logger.debug("✅ SmartAPI SDK patched: _request() now uses persistent reqsession")

# ── Constants ─────────────────────────────────────────────────
MAX_RETRIES       = 5
RETRY_BASE_DELAY  = 2      # seconds — doubles each retry (2,4,8,16,32)
HEARTBEAT_INTERVAL = 60    # seconds between session health checks
TOKEN_REFRESH_SECS = 6 * 3600  # refresh JWT every 6 hours


class SessionManager:
    """
    Thread-safe SmartAPI session manager.
    Use SessionManager.get() to get the active session anywhere in the app.
    """

    _instance: "SessionManager | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self._smart: SmartConnect | None = None
        self._jwt: str | None = None
        self._refresh_token: str | None = None
        self._last_login: float = 0.0
        self._last_refresh: float = 0.0
        self._session_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False

    @classmethod
    def get(cls) -> "SessionManager":
        """Return the singleton SessionManager instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Public API ────────────────────────────────────────────

    def connect(self) -> SmartConnect:
        """Login only. Call start_heartbeat() separately after warmup."""
        with self._session_lock:
            self._login_with_retry()
        return self._smart

    def start_heartbeat(self) -> None:
        """Start the background heartbeat thread. Call after warmup is complete."""
        with self._session_lock:
            self._start_heartbeat()

    def smart(self) -> SmartConnect:
        """Get active session, reconnecting if needed."""
        if self._smart is None:
            self.connect()
        return self._smart

    def call(self, fn, *args, **kwargs):
        """
        Execute a SmartAPI call with automatic retry + reconnect on failure.
        Usage: session_manager.call(smart.ltpData, exchange=..., ...)
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                err = str(exc).lower()
                is_auth_error = any(x in err for x in [
                    "unauthorized", "token", "session", "jwt", "login"
                ])
                if is_auth_error or attempt == MAX_RETRIES:
                    logger.warning(
                        f"API call failed (attempt {attempt}/{MAX_RETRIES}): {exc}"
                    )
                    if is_auth_error:
                        logger.info("Session expired — reconnecting...")
                        self._relogin()
                else:
                    delay = RETRY_BASE_DELAY ** attempt
                    logger.warning(
                        f"API call failed (attempt {attempt}), retrying in {delay}s: {exc}"
                    )
                    time.sleep(delay)
        raise RuntimeError(f"API call failed after {MAX_RETRIES} retries")

    def disconnect(self):
        """Logout and stop heartbeat."""
        self._running = False
        if self._smart:
            try:
                self._smart.terminateSession(config.CLIENT_ID)
                logger.info("👋 SmartAPI session terminated.")
            except Exception as e:
                logger.warning(f"Session termination warning: {e}")
            finally:
                self._smart = None
                self._jwt = None

    # ── Internal ──────────────────────────────────────────────

    def _login_with_retry(self):
        """Login with exponential backoff retry."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._do_login()
                return
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    raise
                delay = RETRY_BASE_DELAY ** attempt
                logger.warning(
                    f"Login attempt {attempt}/{MAX_RETRIES} failed: {exc}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)

    def _do_login(self):
        totp = pyotp.TOTP(config.TOTP_SECRET).now()
        obj  = SmartConnect(api_key=config.API_KEY)
        data = obj.generateSession(config.CLIENT_ID, config.PASSWORD, totp)
        if not data.get("status"):
            raise ConnectionError(f"Login failed: {data.get('message')}")
        self._smart         = obj
        self._jwt           = data["data"]["jwtToken"]
        self._refresh_token = data["data"]["refreshToken"]
        self._last_login    = time.time()
        self._last_refresh  = time.time()
        logger.info(f"✅ SmartAPI connected | Mode: {config.TRADING_MODE.upper()}")

    def _relogin(self):
        """Force a fresh login (used after session expiry)."""
        with self._session_lock:
            self._smart = None
            self._login_with_retry()

    def _refresh_jwt(self):
        """Refresh JWT token without full re-login."""
        try:
            data = self._smart.generateToken(self._refresh_token)
            if data.get("status"):
                self._jwt          = data["data"]["jwtToken"]
                self._last_refresh = time.time()
                logger.debug("🔄 JWT token refreshed.")
            else:
                raise ValueError("Token refresh returned status=False")
        except Exception as exc:
            logger.warning(f"JWT refresh failed: {exc} — doing full re-login")
            self._relogin()

    def _start_heartbeat(self):
        """Start background thread that monitors session health."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="HeartbeatThread"
        )
        self._heartbeat_thread.start()
        logger.info("💓 Heartbeat monitor started.")

    def _heartbeat_loop(self):
        """Background loop: refresh token + detect dead sessions."""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._running:
                break
            try:
                now = time.time()
                # Refresh token every 6 hours
                if now - self._last_refresh > TOKEN_REFRESH_SECS:
                    self._refresh_jwt()
                # Ping with a lightweight LTP call to verify connection.
                # Use the shared _API_LOCK so this doesn't race the main tick.
                if self._smart:
                    import market_data as _md
                    with _md._API_LOCK:
                        _md._api_throttle()
                        resp = self._smart.ltpData(
                            exchange="NSE",
                            tradingsymbol="Nifty 50",
                            symboltoken="99926000"
                        )
                    if not resp.get("status"):
                        raise ValueError("Heartbeat ping returned status=False")
                    logger.debug(f"💓 Heartbeat OK | Nifty={resp['data']['ltp']}")
            except Exception as exc:
                logger.error(f"💔 Heartbeat failed: {exc} — reconnecting...")
                try:
                    self._relogin()
                except Exception as re:
                    logger.critical(f"Reconnect failed: {re}")


# ── Module-level convenience ──────────────────────────────────

def get_smart() -> SmartConnect:
    """Shortcut: get active SmartConnect object."""
    return SessionManager.get().smart()


def api_call(fn, *args, **kwargs):
    """Shortcut: execute API call with retry + reconnect."""
    return SessionManager.get().call(fn, *args, **kwargs)
