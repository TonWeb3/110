"""
Cloudflare-proof WebSocket transport for pyquotex.

Why this exists
---------------
Quotex sits behind Cloudflare bot management, which scores the TLS/JA3
fingerprint of the client. Python's `websockets` library has an unmistakably
non-browser fingerprint, so the WebSocket UPGRADE is rejected with HTTP 403
before any authorization frame is ever sent. Measured on 2026-09-02:

    GET /api/v1/cabinets/digest   -> 200  (JSON API allowed)
    GET /pt/trade                 -> 403  (browser route blocked)
    WSS ws2.qxbroker.com          -> 403  (every host, every User-Agent)

The session itself was perfectly valid throughout - the same cookies returned
a live balance over HTTP. Re-logging in cannot fix this and only burns the
e-mail PIN; the block is on the transport, not the account.

curl_cffi replays a real browser's TLS + HTTP/2 fingerprint. With it the same
cookies, same URL and same headers connect first time:

    chrome      page=200  WS=OK -> 0{"sid":"cldLlwsHSQ3CWT5MLpGl",...}
    chrome124   page=200  WS=OK
    firefox133  page=200  WS=OK

So this module swaps ONLY the socket transport underneath pyquotex. Every
other part of the flow the bot already relies on - _on_open, _on_message,
the SSID authorization, the stale watchdog, subscription replay, reconnect
backoff - is untouched and still runs.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Fingerprints known to pass, in preference order (measured, see docstring).
IMPERSONATE_CHAIN = ("chrome", "chrome124", "firefox133")

# Headers curl/the impersonation profile must own. Sending our own would
# contradict the fingerprint and hand Cloudflare the mismatch it looks for.
_STRIP = {
    "user-agent", "accept-encoding", "sec-websocket-extensions",
    "sec-websocket-key", "sec-websocket-version", "upgrade", "connection",
    "cache-control", "pragma",
}


class _CurlWebSocket:
    """
    Adapts curl_cffi's AsyncWebSocket to the small slice of the `websockets`
    API that pyquotex's WebsocketClient actually uses: `.state`, `.send()`,
    `.close(code=, reason=)` and async iteration.
    """

    def __init__(self, ws, session, state_enum):
        self._ws = ws
        self._session = session
        self._State = state_enum
        self._closed = False

    @property
    def state(self):
        return self._State.CLOSED if self._closed else self._State.OPEN

    async def send(self, data, *args, **kwargs):
        from curl_cffi.const import CurlWsFlag
        if isinstance(data, str):
            data = data.encode("utf-8")
        # socket.io frames are TEXT; the default BINARY flag makes the
        # server ignore them silently. Accept flags kwarg from newer pyquotex versions.
        flags = kwargs.get("flags", CurlWsFlag.TEXT)
        await self._ws.send(data, flags)

    async def close(self, code: int = 1000, reason: str = ""):
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass
        try:
            await self._session.close()
        except Exception:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            frame = await self._ws.recv()
        except Exception as e:
            self._closed = True
            logger.info("WebSocket receive ended: %s: %s", type(e).__name__, e)
            raise StopAsyncIteration
        if frame is None:
            self._closed = True
            raise StopAsyncIteration
        return frame[0] if isinstance(frame, tuple) else frame


async def _open_socket(url: str, headers: dict, impersonate: str):
    """One handshake attempt. Returns (ws, session) or raises."""
    from curl_cffi.requests import AsyncSession

    session = AsyncSession(impersonate=impersonate)
    try:
        ws = await session.ws_connect(url, headers=headers, timeout=30)
        return ws, session
    except Exception:
        # A 403 here usually clears after fetching the ordinary trade page
        # with the SAME fingerprint, which is what earns the Cloudflare
        # clearance cookies a browser would already hold.
        try:
            host = "quotex.com"
            if "://" in url:
                netloc = url.split("://", 1)[1].split("/", 1)[0]
                host = netloc.replace("ws2.", "").replace("ws.", "")
            await session.get(f"https://{host}/pt/trade",
                              headers=headers, timeout=30)
            ws = await session.ws_connect(url, headers=headers, timeout=30)
            return ws, session
        except Exception:
            try:
                await session.close()
            except Exception:
                pass
            raise


def install(preferred: str | None = None) -> bool:
    """
    Patch pyquotex's WebsocketClient to connect through curl_cffi.

    Returns True if the patch is in place. Safe to call repeatedly.
    """
    try:
        from curl_cffi.requests import AsyncSession  # noqa: F401
        import curl_cffi.requests
        for cls in (getattr(curl_cffi.requests, "Response", None),
                    getattr(getattr(curl_cffi.requests, "models", None), "Response", None)):
            if cls and not hasattr(cls, "reason_phrase"):
                cls.reason_phrase = property(lambda self: getattr(self, "reason", None) or "")
    except ImportError:
        logger.error(
            "curl_cffi is not installed, so the WebSocket handshake will be "
            "made by `websockets` and Cloudflare will reject it with HTTP 403. "
            "Install it with:  pip install curl_cffi"
        )
        return False

    from pyquotex.ws import client as ws_client
    from websockets.protocol import State

    if getattr(ws_client.WebsocketClient, "_qx1_cf_patched", False):
        return True

    chain = ([preferred] if preferred else []) + [
        i for i in IMPERSONATE_CHAIN if i != preferred
    ]

    async def _connect_once(self, url, extra_headers, ssl):
        headers = {
            k: v for k, v in (extra_headers or {}).items()
            if k.lower() not in _STRIP
        }

        ws = session = None
        last_error = None
        for imp in chain:
            try:
                ws, session = await _open_socket(url, headers, imp)
                if imp != chain[0]:
                    logger.info("WebSocket connected using the %s "
                                "fingerprint.", imp)
                break
            except Exception as e:
                last_error = e
                logger.debug("Handshake with %s failed: %s", imp, e)
        if ws is None:
            raise ConnectionError(
                f"WebSocket handshake refused with every browser fingerprint "
                f"({', '.join(chain)}): {last_error}"
            )

        shim = _CurlWebSocket(ws, session, State)
        self._ws = shim
        self.api.last_message_at = time.monotonic()
        await self.api._on_open()
        self._open_count += 1
        if self._open_count > 1:
            asyncio.create_task(self._replay_subscriptions())

        self._start_watchdog()
        try:
            async for raw in shim:
                await self.api._on_message(raw)
        finally:
            self._stop_watchdog()
            await shim.close()
            # run_forever's reconnect loop takes it from here.
            try:
                self.api._on_close(1006, "transport closed")
            except Exception:
                pass

    ws_client.WebsocketClient._connect_once = _connect_once
    ws_client.WebsocketClient._qx1_cf_patched = True
    logger.info("Cloudflare-proof WebSocket transport installed "
                "(curl_cffi, %s).", chain[0])
    return True
