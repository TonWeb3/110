"""
Quotex trading handler using pyquotex — no browser required.

Credentials resolved in this priority order:
  1. Environment variables  QUOTEX_EMAIL  /  QUOTEX_PASSWORD
  2. config.json → quotex.email  /  quotex.password  (set via Settings dashboard)

Install:
  pip install git+https://github.com/cleitonleonel/pyquotex.git
"""

import asyncio
import os
import time
import logging
from datetime import datetime
from typing import Optional, Any
from .config import Config

try:
    from pyquotex.stable_api import Quotex  # type: ignore[import]
    from pyquotex.network.login import Login  # type: ignore[import]
except ImportError:
    Quotex = None
    Login = None


def _patch_pyquotex_login():
    """Ensure pyquotex Login uses api.host instead of defunct qxbroker.com, and patch curl_cffi Response."""
    try:
        import curl_cffi.requests
        for cls in (getattr(curl_cffi.requests, "Response", None),
                    getattr(getattr(curl_cffi.requests, "models", None), "Response", None)):
            if cls and not hasattr(cls, "reason_phrase"):
                cls.reason_phrase = property(lambda self: getattr(self, "reason", None) or "")
    except Exception:
        pass

    if Login is None or getattr(Login, "_qx_host_patched", False):
        return
    orig_init = Login.__init__

    def _new_init(self, api, *args, **kwargs):
        orig_init(self, api, *args, **kwargs)
        target_host = getattr(api, "host", "quotex.com") or "quotex.com"
        self.base_url = target_host
        self.https_base_url = f"https://{target_host}"
        self.full_url = f"{self.https_base_url}/{api.lang}"

    Login.__init__ = _new_init
    Login._qx_host_patched = True

    # Patch pyquotex Quotex._check_connect: the default 2-second timeout is too short for
    # Cloudflare TLS handshakes + WebSocket SSID authorization, causing premature timeouts
    # that wipe the session token.
    if Quotex is not None and not getattr(Quotex, "_qx_connect_patched", False):
        @staticmethod
        async def _patient_check_connect(state):
            from pyquotex._api._waits import wait_until
            from pyquotex.global_value import AuthStatus
            try:
                await wait_until(
                    lambda: state.auth_status == AuthStatus.AUTHENTICATED,
                    timeout=10,
                    poll_interval=0.1,
                )
                return True
            except asyncio.TimeoutError:
                return state.auth_status == AuthStatus.AUTHENTICATED

        Quotex._check_connect = _patient_check_connect
        Quotex._qx_connect_patched = True

_patch_pyquotex_login()

# Route the WebSocket through a real browser TLS fingerprint. Without this
# Cloudflare rejects the upgrade with HTTP 403 and NOTHING else works — see
# bot/cf_transport.py for the measurements.
from . import cf_transport

# How long to wait for Quotex to acknowledge an order. pyquotex itself waits
# duration+5s, but a 1-minute signal is worthless by then, so we cap it. NOTE:
# the order is transmitted before this wait — a timeout means "unconfirmed",
# not "not sent".
BUY_CONFIRM_TIMEOUT = 20

# Where the day's opening balance and counters are persisted, so the daily
# limits survive a restart instead of silently starting over.
DAILY_STATE_FILE = "daily_state.json"


class QuotexHandler:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.client: Optional[Any] = None
        self.is_connected = False
        self.last_place_latency: Optional[float] = None  # measured seconds from buy() call to confirmation
        # Only ONE order may be in flight through pyquotex at a time — see the
        # comment in _place_one(). Settlement (check_win) stays concurrent.
        self._buy_lock = asyncio.Lock()
        self.balance: Optional[float] = None              # last known account balance (dashboard)
        self.last_trade: Optional[dict] = None            # last placed trade + its result (dashboard)
        self.daily_trades = 0
        self.daily_loss = 0.0          # realized drawdown today (opening balance − current)
        self.wins = 0                  # settled winning trades today
        self.losses = 0                # settled losing trades today
        self.daily_pnl = 0.0           # realized profit/loss today (from check_win)
        self.active_trades = 0
        self.martingale_cycle: int = 1   # 2-step 2-cycle Martingale (Cycle 1: Trades 1 & 2, Cycle 2: Trades 3 & 4)
        self._trade_date = None
        self._day_start_balance: Optional[float] = None   # balance snapshot at 00:00
        self.last_error: Optional[str] = None             # detailed reason for connection failure
        # The day's counters are persisted, so restarting the bot mid-day does
        # NOT hand you a fresh set of limits. Loaded per account type.
        self._load_daily_state()

    # ─────────────────────────────────────────────────────────
    #  CONNECTION
    # ─────────────────────────────────────────────────────────

    async def connect(self, otp_callback=None) -> bool:
        if Quotex is None:
            self.logger.error(
                "pyquotex is not installed. "
                "Run: pip install git+https://github.com/cleitonleonel/pyquotex.git"
            )
            return False

        email    = os.getenv("QUOTEX_EMAIL")    or self.config.quotex.email
        password = os.getenv("QUOTEX_PASSWORD") or self.config.quotex.password

        # Check if an existing session token is cached in session.json for this email
        has_session_token = False
        try:
            import json as _json
            from pathlib import Path as _Path
            _sf = _Path("session.json")
            if _sf.exists():
                _sdata = _json.loads(_sf.read_text(encoding="utf-8"))
                if email in _sdata and _sdata[email].get("token"):
                    has_session_token = True
        except Exception:
            pass

        if not email:
            self.last_error = "Quotex email is required."
            self.logger.error(self.last_error)
            return False

        if not password and not has_session_token:
            self.last_error = (
                "Quotex credentials missing. Enter email and password in Settings, "
                "or import session cookies & token directly."
            )
            self.logger.error(self.last_error)
            return False

        # Must be installed before any socket is opened, or Cloudflare 403s the
        # handshake and every later step (auth, orders, balance) fails with it.
        if not cf_transport.install():
            self.last_error = "Cannot open a Quotex WebSocket without curl_cffi — run: pip install curl_cffi"
            self.logger.error(self.last_error)
            return False

        try:
            # Retire any previous client FIRST. Its websocket has its own
            # reconnect loop that keeps retrying forever on its own; leaving it
            # orphaned is how the log accumulated 138 rejected handshakes while
            # a newer client was already in use.
            await self._retire_client()

            _patch_pyquotex_login()
            host = getattr(self.config.quotex, "host", "quotex.com") or "quotex.com"
            self.logger.info(f"Connecting to Quotex as {email} (host={host})...")
            self.client = Quotex(
                email=email,
                password=password or "",
                host=host,
                on_otp_callback=otp_callback,
            )
            check, reason = await self.client.connect()

            if not check:
                self.logger.info("Waiting up to 8 s for WebSocket auth to settle...")
                for _ in range(8):
                    await asyncio.sleep(1)
                    if await self.client.check_connect():
                        check = True
                        break

            if not check:
                # Direct HTTP login or cached token failed.
                if not password:
                    self.last_error = (
                        "The imported session token was rejected by Quotex. "
                        "Please export a fresh token from your browser, or enter your password."
                    )
                    self.logger.error(self.last_error)
                    return False

                # Launch the browser authenticator (undetected_chromedriver) to solve Cloudflare Turnstile,
                # grab the fresh session cookies and token, and save to session.json.
                self.logger.info(
                    "Direct authentication failed or token expired. "
                    "Launching browser authenticator to bypass Cloudflare and refresh session..."
                )
                from .browser_auth import async_login_with_browser
                browser_ok, browser_msg = await async_login_with_browser(
                    email=email,
                    password=password,
                    host=host,
                    otp_callback=otp_callback,
                )
                if browser_ok:
                    self.logger.info("Browser authentication succeeded! Reconnecting Quotex headless client with fresh session...")
                    await self._retire_client()
                    self.client = Quotex(
                        email=email,
                        password=password,
                        host=host,
                        on_otp_callback=otp_callback,
                    )
                    check, reason = await self.client.connect()
                    if not check:
                        for _ in range(8):
                            await asyncio.sleep(1)
                            if await self.client.check_connect():
                                check = True
                                break
                else:
                    self.last_error = browser_msg
                    self.logger.error(f"Browser authentication failed: {browser_msg}")
                    return False

            if not check:
                self.last_error = f"Quotex login failed: {reason}"
                self.logger.error(f"Quotex login failed: {reason}")
                return False

            # Switch demo / live account
            account_map = {"demo": "PRACTICE", "live": "REAL"}
            target = account_map.get(self.config.trading.account_type.lower(), "PRACTICE")
            await self.client.change_account(target)
            self.logger.info(f"Account type: {target}")

            # Every later socket (the watchdog recycles an idle one every 60 s)
            # must re-authorize itself, or orders go into an anonymous socket.
            self._install_reauth_hook()
            self._start_keepalive()

            balance = await self.client.get_balance()
            self.logger.info(f"Connected. Balance: ${balance:.2f}")
            self.is_connected = True
            await self._refresh_balance_stats()   # snapshot the day's opening balance
            return True

        except Exception as e:
            self.logger.error(f"Error connecting to Quotex: {e}")
            return False

    def _clear_session_token(self, email: str):
        """
        Remove the stale WebSocket authorization token from pyquotex's session.json.
        Leaves HTTP cookies intact so the next connect can re-login silently.
        """
        import json
        from pathlib import Path
        session_file = Path("session.json")
        if not session_file.exists():
            return
        try:
            sessions = json.loads(session_file.read_text())
            if email in sessions:
                sessions[email]["token"] = None
                cookies = str(sessions[email].get("cookies") or "")
                if "qxbroker.com" in cookies:
                    sessions[email]["cookies"] = None
                session_file.write_text(json.dumps(sessions, indent=4))
                self.logger.info(f"Cleared stale WebSocket token for {email}")
        except Exception as e:
            self.logger.warning(f"Could not clear session token: {e}")

    def _has_credentials(self) -> bool:
        """Return True if email + password are available (env var or config)."""
        email    = os.getenv("QUOTEX_EMAIL")    or self.config.quotex.email
        password = os.getenv("QUOTEX_PASSWORD") or self.config.quotex.password
        return bool(email and password)

    # ─────────────────────────────────────────────────────────
    #  WEBSOCKET AUTHORIZATION
    # ─────────────────────────────────────────────────────────
    #
    # pyquotex sends the authorization frame (42["authorization", ...]) from
    # ONE place only: api.connect() — the FIRST connection. Its auto-reconnect
    # loop calls api._on_open(), which sends a heartbeat and a few list
    # requests and never re-authorizes (api.py:231-258, send_ssid called only
    # at api.py:943). The socket also recycles itself every time it goes 60 s
    # without an inbound message ("watchdog-stale"), which for a bot that only
    # waits for signals is constantly.
    #
    # So after the first recycle the socket is ANONYMOUS: public data
    # (instruments, candles) still arrives, so every health check looks fine,
    # but "orders/open" is silently discarded — no confirmation, no error.
    # That is the 20-second timeout, on every asset, OTC or not.

    def _install_reauth_hook(self):
        """Re-send the authorization frame on EVERY socket open, not just the first."""
        api = getattr(self.client, "api", None)
        if api is None or getattr(api, "_qx1_reauth_installed", False):
            return
        original_on_open = api._on_open
        handler = self

        async def _on_open_with_reauth():
            await original_on_open()
            try:
                from pyquotex.global_value import AuthStatus
                # The new socket is unauthenticated until the server says
                # otherwise — pyquotex leaves the old AUTHENTICATED flag set,
                # which is what made every health check lie.
                api.state.auth_status = AuthStatus.NOT_AUTHENTICATED
            except Exception:
                pass
            try:
                if await api.send_ssid():
                    handler.logger.info("WebSocket re-opened — authorization re-sent.")
                else:
                    handler.logger.warning(
                        "WebSocket re-opened but there is no session token to "
                        "re-authorize with — a fresh login is needed."
                    )
            except Exception as e:
                handler.logger.error(f"Could not re-authorize the new WebSocket: {e}")
            handler._start_keepalive()

        api._on_open = _on_open_with_reauth
        api._qx1_reauth_installed = True
        self.logger.info("Re-authorization hook installed on the Quotex WebSocket.")

    # pyquotex's own heartbeat sends 42["tick"] every 5 s, which the server
    # NEVER answers (measured: 0 replies in 6 s, likewise instruments/get and
    # chart_notification/get). Only the socket.io protocol ping "2" gets a "3"
    # pong back. Since the watchdog measures INBOUND silence, an unanswered
    # heartbeat is worthless: a bot waiting for signals subscribes to nothing,
    # inbound bursts arrive 40+ s apart, and the socket is torn down and rebuilt
    # every minute forever. One "2" every 20 s (the server's own pingInterval is
    # 25 s) keeps it genuinely alive and stops the churn.
    KEEPALIVE_SECONDS = 20

    def _start_keepalive(self):
        """Keep the socket demonstrably alive so the watchdog stops recycling it."""
        task = getattr(self, "_keepalive_task", None)
        if task is not None and not task.done():
            task.cancel()

        api = getattr(self.client, "api", None)
        if api is None:
            return

        async def _ping_loop():
            socket = api.websocket          # this socket only; a new one re-arms
            try:
                while socket is not None and api.websocket is socket:
                    await asyncio.sleep(self.KEEPALIVE_SECONDS)
                    if api.websocket is not socket:
                        return
                    try:
                        await socket.send("2")
                    except Exception as e:
                        self.logger.debug(f"Keepalive ping stopped: {e}")
                        return
            except asyncio.CancelledError:
                pass

        self._keepalive_task = asyncio.ensure_future(_ping_loop())

    async def _ensure_authorized(self, timeout: float = 6.0) -> bool:
        """
        Make sure the CURRENT socket is authorized, re-sending the token if not.

        Cheap when the session is healthy (one flag read). Called before every
        order so a recycled socket can never swallow a trade.
        """
        api = getattr(self.client, "api", None)
        if api is None:
            return False
        try:
            from pyquotex.global_value import AuthStatus
            from pyquotex._api._waits import wait_until
        except Exception:
            return True                      # can't verify — don't block the trade

        if api.state.auth_status == AuthStatus.AUTHENTICATED:
            return True
        if api.websocket is None:
            self.logger.warning("No live WebSocket to authorize.")
            return False
        try:
            await api.send_ssid()
            await wait_until(
                lambda: api.state.auth_status == AuthStatus.AUTHENTICATED,
                timeout=timeout, poll_interval=0.05,
            )
            self.logger.info("WebSocket re-authorized.")
            return True
        except Exception as e:
            self.logger.warning(f"WebSocket authorization failed: {e}")
            return False

    async def _connection_alive(self) -> bool:
        """
        True only if the CURRENT socket is open and the broker still accepts it.

        This is an active round trip — reset the auth flag, re-send the token,
        wait for the server's reply — because every passive check lies:
        pyquotex's check_connect() just reads state.auth_status, a flag set once
        at the first login and never cleared. With that flag alone the monitor
        reported "Quotex OK" while the socket was being rejected (HTTP 403) on
        138 consecutive reconnect attempts and no order could be placed.
        """
        if not (self.is_connected and self.client is not None):
            return False
        api = getattr(self.client, "api", None)
        if api is None or api.websocket is None:
            self.is_connected = False
            return False
        try:
            from pyquotex.global_value import AuthStatus
            api.state.auth_status = AuthStatus.NOT_AUTHENTICATED
        except Exception:
            pass
        alive = await self._ensure_authorized(timeout=8.0)
        if not alive:
            self.logger.warning(
                "Quotex socket is not authorized — flagging for a fresh login."
            )
            self.is_connected = False      # let the health monitor reconnect
        return alive

    def _time_mode(self, asset_name: str = "") -> str:
        """
        Order style. TIMER for every asset, as requested.

        TIMER sends optionType 100 with a RELATIVE duration, so the trade runs
        exactly its stated duration from the fill rather than to the next candle
        boundary — which is what the signals describe.

        There was previously an "AUTO" mode that sent TURBO on non-OTC pairs, on
        the theory that Quotex rejects TIMER off OTC. That theory was WRONG: the
        24h log shows OTC pairs (USDBDT_otc, NZDCAD_otc, CADCHF_otc, ...) failing
        identically. The real cause was an unauthorized WebSocket, fixed in
        _ensure_authorized(). Asset class never mattered.
        """
        mode = str(getattr(self.config.quotex, "time_mode", "TIMER") or "TIMER").upper()
        return mode if mode in ("TIMER", "TIME") else "TIMER"

    async def _retire_client(self):
        """Shut down the current client so its reconnect loop cannot outlive it."""
        task = getattr(self, "_keepalive_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._keepalive_task = None

        client, self.client = self.client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(client.close(), timeout=10)
            self.logger.info("Previous Quotex client closed.")
        except Exception as e:
            self.logger.debug(f"Closing the previous Quotex client: {e}")

    async def disconnect(self):
        task = getattr(self, "_keepalive_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._keepalive_task = None
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.is_connected = False
        self.logger.info("Quotex disconnected.")

    # ─────────────────────────────────────────────────────────
    #  ASSET SELECTION  (no-op — asset is specified in buy() at trade time)
    # ─────────────────────────────────────────────────────────

    async def select_asset(self, asset: str):
        """
        Pre-warm the price stream for the asset.
        pyquotex's buy() waits for realtime_price[asset] to be populated.
        Starting the candle stream here gives the server time to begin
        delivering ticks before the trade button is pressed.
        """
        formatted = self._format_asset(asset)
        self.logger.info(f"Asset queued: {formatted} — pre-subscribing price stream")
        try:
            await self.client.start_candles_stream(formatted, 60)
        except Exception as e:
            self.logger.warning(f"Pre-subscribe failed for {formatted}: {e}")

    async def _is_asset_open(self, asset_name: str) -> bool:
        """
        Return True only if Quotex currently lists this asset as tradable.
        check_asset_open() returns (raw, (id, name, is_open)); is_open is the flag.

        Fails CLOSED. The previous version returned True when the check itself
        errored, on the theory that buy()'s timeout was a backstop — but that
        "backstop" costs 20 s AND sends a real order into the void, because
        buy() transmits orders/open before it waits for the confirmation. If we
        cannot confirm the asset is tradable, we do not trade it.
        """
        try:
            _, status_info = await asyncio.wait_for(
                self.client.check_asset_open(asset_name), timeout=15
            )
        except Exception as e:
            self.logger.error(
                f"Could not verify whether {asset_name} is tradable ({e}) — "
                f"not trading it."
            )
            return False

        if status_info and status_info[2]:
            return True

        # Not tradable — say WHY, so the log answers the question next time.
        try:
            names = [str(i[1]) for i in (self.client.api.instruments or [])]
        except Exception:
            names = []

        if not names:
            self.logger.error(
                f"Quotex returned no instrument list — the WebSocket is not "
                f"authenticated, so {asset_name} cannot be verified."
            )
        elif asset_name not in names:
            base = asset_name.replace("_otc", "")[:3].upper()
            near = [n for n in names if n.upper().startswith(base)][:8]
            self.logger.error(
                f"{asset_name} is NOT offered by Quotex on this account "
                f"({len(names)} instruments listed). Closest names: {near or 'none'}"
            )
        else:
            self.logger.warning(f"{asset_name} is listed but CLOSED right now.")
        return False

    def _late_grace(self) -> float:
        """Seconds past the intended entry moment that an order may still go out."""
        try:
            return max(0.0, float(self.config.quotex.late_entry_grace_seconds))
        except Exception:
            return 5.0

    def _too_late_to_enter(self, not_after: Optional[float],
                           asset_name: str, direction: str) -> bool:
        """
        True when the entry moment has passed by more than the grace period, so
        the order must NOT be sent.

        A signal is only worth trading at its stated entry. Anything that eats
        the intervening seconds — a slow asset check, queueing behind another
        order's confirmation, a stalled machine — makes the trade a different
        bet from the one the signal described, so it is dropped rather than
        entered late.
        """
        if not_after is None:
            return False
        overdue = time.time() - not_after
        if overdue <= 0:
            return False
        self.logger.warning(
            f"TOO LATE — skipping {asset_name} {direction.upper()}: the entry moment "
            f"passed {overdue + self._late_grace():.1f}s ago, beyond the "
            f"{self._late_grace():.1f}s grace. No order sent."
        )
        return True

    def _clear_pending_order_state(self):
        """
        Forget a half-finished order. pyquotex keeps only one pending-order id,
        so a confirmation arriving late (after we gave up) would otherwise be
        adopted by the NEXT trade as if it were its own.
        """
        try:
            self.client.api.buy_id = None
            self.client.api.slots.buy_confirm.clear()
        except Exception as e:
            self.logger.debug(f"Could not clear pending order state: {e}")

    async def _diagnose_no_confirmation(self, asset_name: str) -> str:
        """
        Explain a missing order confirmation.

        Note buy() sends orders/open BEFORE waiting, so a timeout does not prove
        the order was rejected — it may be live at the broker but untracked here.
        """
        bits = []
        api = getattr(self.client, "api", None)

        # Report the CURRENT socket, not pyquotex's sticky auth flag: that flag
        # is set once at the first login and never cleared, so it happily read
        # "authenticated" through 138 rejected reconnects.
        sock_open = bool(api is not None and api.websocket is not None)
        bits.append(f"ws_open={sock_open}")
        try:
            from pyquotex.global_value import AuthStatus
            status = api.state.auth_status if api else None
            bits.append(f"ws_auth={getattr(status, 'name', status)}")
            authed = status == AuthStatus.AUTHENTICATED
        except Exception:
            authed = False
            bits.append("ws_auth=unknown")

        if not (sock_open and authed):
            self.is_connected = False        # health monitor will re-login
            bits.append("socket unusable — flagged for reconnect")

        try:
            state = api.state
            if getattr(state, "check_websocket_if_error", False):
                bits.append(f"ws_error={getattr(state, 'websocket_error_reason', '')!r}")
        except Exception:
            pass
        bits.append(f"time_mode={self._time_mode(asset_name)}")
        bits.append("the order MAY still have reached Quotex — check the platform")
        return " | ".join(bits)

    # ─────────────────────────────────────────────────────────
    #  PRE-CONFIGURE  (timed signals)
    # ─────────────────────────────────────────────────────────

    async def pre_configure_trade(self, expiry: Optional[str] = None) -> float:
        """
        Called immediately after asset selection for timed signals.
        With the API there is no UI to pre-fill; just calculate and return the
        amount so it is ready the moment the entry time arrives.
        """
        base = await self._calc_amount()
        if self.config.trading.martingale_enabled:
            amount = self.get_cycle_trade_amount(base, step=1)
        else:
            amount = base

        # Warm the HTTPS path buy() uses. pyquotex's buy() calls get_server_time()
        # -> get_profile() -> GET /api/v1/cabinets/digest on EVERY order; doing one
        # here, while there is still time to spare, leaves a live keep-alive TLS
        # connection so the call at entry time is a warm round trip, not a
        # cold handshake. Failures are ignored — this is purely an optimisation.
        try:
            t0 = time.monotonic()
            await self.client.get_server_time()
            self.logger.info(f"Order path pre-warmed in {time.monotonic() - t0:.2f}s")
        except Exception as e:
            self.logger.debug(f"Pre-warm skipped: {e}")

        # Confirm the socket is still authorized while there is time to fix it.
        # An idle socket gets recycled every ~60 s and comes back anonymous.
        if not await self._ensure_authorized():
            self.logger.warning(
                "Quotex socket is NOT authorized while arming this signal — "
                "the health monitor will re-login; the entry may be missed."
            )
            self.is_connected = False

        self.logger.info(
            f"Pre-configured: ${amount:.2f} | duration: {expiry or '00:01:00'} "
            f"— ready, waiting for entry time."
        )
        return amount

    # ─────────────────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────────────────

    async def _calc_amount(self) -> float:
        """
        Calculate the BASE trade amount: percent of balance, or fixed dollars.
        Martingale step sizing (doubling) is handled by the recovery loop in
        perform_trade(), not here — this always returns the step-1 base.

        Re-reads config.json first if it changed, so the stake is always the one
        the file currently specifies.
        """
        self.config.refresh_if_changed()
        if self.config.trading.risk_mode == "percent":
            try:
                balance = await self.client.get_balance()
                if balance:
                    amount = round(balance * (self.config.trading.risk_amount / 100), 2)
                    self.logger.info(
                        f"Balance: ${balance:.2f} | "
                        f"{self.config.trading.risk_amount}% = ${amount:.2f}"
                    )
                    return amount
            except Exception:
                pass
            self.logger.warning("Could not read balance — using fixed risk_amount.")

        return self.config.trading.risk_amount

    def get_cycle_trade_amount(self, base_amount: float, step: int) -> float:
        """
        Calculate stake for 2-step 2-cycle Martingale:
        Cycle 1:
          Step 1 (Trade 1): base * 1   (e.g. $2)
          Step 2 (Trade 2): base * 2   (e.g. $4)
        Cycle 2:
          Step 1 (Trade 3): base * 4   (e.g. $8)
          Step 2 (Trade 4): base * 8   (e.g. $16)
        """
        mult = float(self.config.trading.martingale_multiplier or 2.0)
        cycle = max(1, getattr(self, 'martingale_cycle', 1))
        power = (cycle - 1) * 2 + (step - 1)
        return round(base_amount * (mult ** power), 2)

    @staticmethod
    def _format_asset(asset: str) -> str:
        """
        Convert signal asset name to pyquotex format.
          EURUSD     → EURUSD
          EURUSD-OTC → EURUSD_otc
        """
        if asset.upper().endswith("-OTC"):
            return asset[:-4].upper() + "_otc"
        return asset.upper()

    @staticmethod
    def _expiry_to_seconds(expiry: str) -> int:
        """'00:05:00' → 300"""
        try:
            h, m, s = expiry.split(":")
            return int(h) * 3600 + int(m) * 60 + int(s)
        except Exception:
            return 60

    # ─────────────────────────────────────────────────────────
    #  DAILY STATE  (persisted — survives restarts)
    # ─────────────────────────────────────────────────────────

    def _daily_state_path(self):
        from pathlib import Path
        return Path(__file__).parent.parent / DAILY_STATE_FILE

    def _account_key(self) -> str:
        """Demo and live have separate balances, so they get separate day state."""
        return "live" if self.config.trading.account_type.lower() == "live" else "demo"

    def _load_daily_state(self):
        """
        Restore today's opening balance and counters from disk.

        Without this, restarting the bot at any point re-snapshotted the opening
        balance from the CURRENT balance and zeroed the trade count — so a
        max_daily_loss you had already hit was forgiven, and max_daily_trades
        started over. Anything saved under an older date is ignored (new day).
        """
        import json as _json
        try:
            path = self._daily_state_path()
            if not path.exists():
                return
            saved = _json.loads(path.read_text(encoding="utf-8")).get(self._account_key())
            if not saved:
                return
            if saved.get("date") != datetime.now().date().isoformat():
                return                        # stale — a new day starts clean
            self._trade_date        = datetime.now().date()
            self._day_start_balance = saved.get("open_balance")
            self.daily_trades       = int(saved.get("trades", 0))
            self.wins               = int(saved.get("wins", 0))
            self.losses             = int(saved.get("losses", 0))
            self.martingale_cycle   = int(saved.get("martingale_cycle", 1))
            self.logger.info(
                f"Resumed today's {self._account_key()} state: open balance "
                f"${self._day_start_balance}, {self.daily_trades} trades, "
                f"{self.wins}W/{self.losses}L, Martingale Cycle {self.martingale_cycle}"
            )
        except Exception as e:
            self.logger.warning(f"Could not load daily state: {e}")

    def _save_daily_state(self):
        """Persist today's opening balance + counters (other accounts preserved)."""
        import json as _json
        try:
            path = self._daily_state_path()
            data = {}
            if path.exists():
                try:
                    data = _json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[self._account_key()] = {
                "date":         (self._trade_date or datetime.now().date()).isoformat(),
                "open_balance": self._day_start_balance,
                "trades":           self.daily_trades,
                "wins":             self.wins,
                "losses":           self.losses,
                "martingale_cycle": getattr(self, "martingale_cycle", 1),
                "saved_at":         datetime.now().isoformat(timespec="seconds"),
            }
            path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"Could not save daily state: {e}")

    def _reset_daily_counters_if_new_day(self):
        today = datetime.now().date()
        if self._trade_date != today:
            if self._trade_date is not None:
                self.logger.info(
                    f"New trading day ({today}) — resetting counters; the opening "
                    f"balance is re-snapshotted on the next balance read."
                )
            self.daily_trades       = 0
            self.daily_loss         = 0.0
            self.wins               = 0
            self.losses             = 0
            self.daily_pnl          = 0.0
            self.martingale_cycle   = 1
            self._day_start_balance = None   # re-snapshot opening balance on the new day
            self._trade_date        = today
            self._save_daily_state()

    async def _read_balance(self, authoritative: bool = False) -> Optional[float]:
        """
        Current account balance from the authenticated Quotex connection.
        Prioritizes the live WebSocket account balance which is event-driven.
        If an authoritative profile read is requested, validates that the profile
        was actually returned by Quotex (profile_id is set) before trusting it.
        """
        if authoritative and self.client:
            try:
                profile = await self.client.get_profile()
                # Profile is only valid if Quotex actually returned profile data (profile_id is set).
                # If the HTTP endpoint returned 401 Unauthenticated, profile_id is None and
                # balances default to 0.0 — which must NEVER be accepted as a real balance!
                if profile is not None and getattr(profile, "profile_id", None) is not None:
                    live = self.config.trading.account_type.lower() == "live"
                    value = profile.live_balance if live else profile.demo_balance
                    if value is not None and float(value) >= 0:
                        return float(value)
            except Exception as e:
                self.logger.debug(f"Authoritative balance read failed: {e}")

        # Live WebSocket balance — always connected and updated by Quotex
        try:
            if self.client:
                bal = await self.client.get_balance()
                if bal is not None:
                    return float(bal)
        except Exception as e:
            self.logger.warning(f"Could not read balance from WebSocket: {e}")

        return None

    async def _refresh_balance_stats(self, authoritative: bool = False):
        """
        Snapshot the day's opening balance at 00:00, then derive realized daily
        P&L and drawdown from the live account balance:
            daily_pnl  = current_balance − opening_balance
            daily_loss = max(0, −daily_pnl)     ← what max_daily_loss is checked against
        Both are sourced from the balance so they reflect actual money, not stakes.
        """
        self._reset_daily_counters_if_new_day()
        balance = await self._read_balance(authoritative=authoritative)
        if balance is None:
            return

        # Sanity check: if opening balance is already established (> 0) and balance suddenly reads 0.0,
        # ignore the suspicious zero read to prevent falsely wiping the balance and triggering max_daily_loss.
        if balance == 0.0 and self._day_start_balance is not None and self._day_start_balance > 0.0:
            self.logger.warning(
                f"Ignored suspicious $0.00 balance read (opening baseline is ${self._day_start_balance:.2f})."
            )
            return

        self.balance = balance
        if self._day_start_balance is None:
            # First reading of the day — this is the figure every daily limit is
            # measured against, so it is written to disk immediately.
            self._day_start_balance = balance
            self._save_daily_state()
            self.logger.info(
                f"Opening balance for {self._trade_date}: ${balance:.2f} "
                f"({self._account_key()}) — saved as today's baseline."
            )

        # In percent risk mode, express P&L / loss as a % of the opening balance
        # so the max_daily_loss limit is also read as a percentage. Otherwise dollars.
        gross = balance - self._day_start_balance
        if self.config.trading.risk_mode == "percent" and self._day_start_balance:
            self.daily_pnl = round(gross / self._day_start_balance * 100, 2)
        else:
            self.daily_pnl = round(gross, 2)
        self.daily_loss = max(0.0, -self.daily_pnl)

    # ─────────────────────────────────────────────────────────
    #  MARTINGALE
    # ─────────────────────────────────────────────────────────

    async def _get_trade_result(self, order_id: Any, duration_secs: int):
        """
        Uses check_win() which waits event-driven until the trade closes
        (up to 300s).
        Returns a (won, profit) tuple where won is True = win, False = loss,
        None = unknown, and profit is the realized P&L for the trade
        (positive on a win, negative on a loss, 0.0 if unknown).
        """
        try:
            win_status, profit = await self.client.check_win(str(order_id), duration_secs)
            profit = float(profit or 0.0)
            won = True if win_status == "win" else False if win_status else None
            self.logger.info(f"Trade result: {win_status}  profit={profit:.2f}")
            return won, profit
        except Exception as e:
            self.logger.error(f"Could not read trade result: {e}")
            return None, 0.0

    def _record_result(self, won: Optional[bool], profit: float):
        """
        Update win/loss counters from a settled trade.
        Daily P&L / loss are NOT accumulated here — they are derived from the live
        balance in _refresh_balance_stats() (dollars, or % in percent risk mode).
        """
        self._reset_daily_counters_if_new_day()
        if won is True:
            self.wins += 1
        elif won is False:
            self.losses += 1
        self._save_daily_state()
        if self.last_trade is not None:
            self.last_trade["result"] = "win" if won is True else "loss" if won is False else "unknown"
            self.last_trade["profit"] = round(profit, 2)
        self.logger.info(
            f"Stats: {self.wins}W / {self.losses}L  |  last trade profit: ${profit:.2f}"
        )

    async def _settle_and_record(self, order_id: Any, duration_secs: int):
        """Background settle: wait for the trade to close, record win/loss + P&L."""
        won, profit = await self._get_trade_result(order_id, duration_secs)
        self._record_result(won, profit)
        await self._refresh_balance_stats(authoritative=True)

    async def _place_one(
        self,
        asset_name: str,
        direction: str,
        period_secs: int,
        amount: float,
        expiry: Optional[str],
        wait_result: bool,
        not_after: Optional[float] = None,
    ):
        """
        Place a single trade and optionally wait for its result.
        Returns (placed, won):
          placed = False        → the order never went on (timeout / rejected / too late)
          won    = True/False/None when wait_result is True; always None otherwise

        not_after: wall-clock epoch after which this entry is stale and must NOT
        be sent (see _too_late_to_enter).
        """
        mode = self._time_mode(asset_name)
        self.logger.info(
            f"Placing trade: {asset_name} {direction.upper()} "
            f"${amount:.2f}  duration: {period_secs}s  mode: {mode} "
            f"({'runs ' + str(period_secs) + 's from the fill' if mode == 'TIMER' else 'candle-aligned expiry'})"
        )

        # ── Orders MUST be sent one at a time ─────────────────────────────
        # pyquotex tracks the pending order in a SINGLE shared field
        # (api.buy_id) with ONE shared confirmation slot (api.slots.buy_confirm),
        # and buy() clears both as it starts (api.py:505-522). Two overlapping
        # buy() calls therefore steal each other's confirmation: ids get
        # duplicated across trades, results are attributed to the wrong order,
        # and later orders sit unconfirmed until they time out. Serialise the
        # send+confirm window. check_win() afterwards is keyed per order id and
        # stays fully concurrent, so this only costs the round trip.
        async with self._buy_lock:
            # Waiting for that lock is itself a way to become late, so the
            # freshness check happens HERE — the last moment before the order
            # goes out — not before the queue.
            if self._too_late_to_enter(not_after, asset_name, direction):
                return False, None

            # Final guarantee: the socket carrying this order is authorized.
            # Without this an order can be sent into a socket the watchdog
            # recycled seconds ago, which the server accepts and ignores.
            if not await self._ensure_authorized(timeout=4.0):
                self.logger.error(
                    f"NOT sending {asset_name} {direction.upper()} — the WebSocket "
                    f"is not authorized (the broker would silently drop it)."
                )
                self.is_connected = False
                return False, None

            # Signals come from Telegram, not from price data, so the realtime
            # price stream is NOT needed to decide a trade. buy() would otherwise
            # block in start_realtime_price() waiting for live ticks and time out
            # valid trades. Seed one price entry so that check passes instantly.
            # (buy() ignores the value — the broker fills at its own price.)
            try:
                self.client.api.realtime_price.setdefault(asset_name, []).append(
                    {"time": 0, "price": 0}
                )
            except Exception as _e:
                self.logger.debug(f"Could not seed price stream: {_e}")

            # time_mode="TIMER": duration option (optionType 100, expiry sent as
            # a RELATIVE number of seconds). The trade runs for exactly
            # `period_secs` from the moment the broker accepts it — it is NOT
            # snapped to the next candle boundary, so a 1-minute signal is always
            # a full minute and a late placement can never land on the wrong
            # candle. Bounded timeout so a stuck buy() can't hold the lock.
            fired_at = time.monotonic()
            try:
                status, buy_info = await asyncio.wait_for(
                    self.client.buy(amount, asset_name, direction, period_secs,
                                    time_mode=mode),
                    timeout=BUY_CONFIRM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Drop the half-finished order's state before releasing the lock,
                # so the NEXT order cannot inherit this one's id.
                self._clear_pending_order_state()
                self.logger.error(
                    f"No order confirmation for {asset_name} within {BUY_CONFIRM_TIMEOUT}s. "
                    f"{await self._diagnose_no_confirmation(asset_name)}"
                )
                return False, None

        if not status:
            self.logger.error(f"Trade rejected by Quotex — NOT placed: {buy_info}")
            return False, None

        self.last_place_latency = time.monotonic() - fired_at
        self.daily_trades += 1
        self._save_daily_state()      # the trade count must survive a restart
        order_id = buy_info.get("id") if isinstance(buy_info, dict) else None
        self.last_trade = {
            "asset":     asset_name,
            "direction": direction,
            "amount":    amount,
            "duration":  period_secs,
            "latency":   round(self.last_place_latency, 2),
            "at":        datetime.now().strftime("%H:%M:%S"),
            "result":    None,     # filled in by _record_result when it settles
            "profit":    None,
        }
        self.logger.info(
            f"Trade #{self.daily_trades} placed — {asset_name} {direction.upper()} "
            f"${amount:.2f} | id={order_id} | fill latency {self.last_place_latency:.2f}s "
            f"| Daily: {self.daily_trades}/{self.config.trading.max_daily_trades}"
        )
        import json as _json, logging as _logging
        _logging.getLogger('trades').info(
            f"QUOTEX_TRADE_EXECUTED | {_json.dumps({'asset': asset_name, 'direction': direction, 'amount': amount, 'expiry': expiry, 'order_id': str(order_id)})}"
        )

        if order_id is None:
            self.logger.warning(
                "Trade placed but broker returned no order id — win/loss not trackable."
            )
            return True, None

        # check_win timeout: the option closes exactly period_secs after the fill.
        result_timeout = period_secs + 30
        if wait_result:
            won, profit = await self._get_trade_result(order_id, result_timeout)
            self._record_result(won, profit)
            await self._refresh_balance_stats(authoritative=True)
            return True, won
        # Non-martingale: record in the background so the next signal isn't delayed.
        asyncio.ensure_future(self._settle_and_record(order_id, result_timeout))
        return True, None

    # ─────────────────────────────────────────────────────────
    #  MAIN TRADE FLOW
    # ─────────────────────────────────────────────────────────

    async def perform_trade(
        self,
        symbol: str,
        direction: str,
        expiry: Optional[str] = None,
        pre_configured_amount: Optional[float] = None,
        not_after: Optional[float] = None,
    ) -> bool:
        """
        not_after: wall-clock epoch after which this entry is stale. Applies to
        the FIRST order only — a martingale recovery deliberately happens a
        minute later and must not be judged against the original entry time.
        """
        if not self.is_connected or not self.client:
            self.logger.error("QuotexHandler not connected.")
            return False

        # Cheap early exit before the gates spend time on balance/asset checks.
        if self._too_late_to_enter(not_after, self._format_asset(symbol), direction):
            return False

        # config.json is the source of truth: pick up any edit made since the
        # last trade (Settings screen, hand edit, or a replaced file on a
        # volume) so THIS trade is gated and sized by what the file says now.
        self.config.refresh_if_changed()

        # Refresh realized daily P&L / drawdown from the live balance before gating.
        await self._refresh_balance_stats()
        loss_unit = "%" if self.config.trading.risk_mode == "percent" else "$"

        if self.daily_trades >= self.config.trading.max_daily_trades:
            self.logger.warning(
                f"Daily trade limit reached ({self.config.trading.max_daily_trades}). Skipping."
            )
            return False

        if (self.config.trading.max_daily_loss_enabled
                and self.daily_loss >= self.config.trading.max_daily_loss):
            self.logger.warning(
                f"Daily loss limit reached ({self.daily_loss:.2f}{loss_unit} "
                f">= {self.config.trading.max_daily_loss:.2f}{loss_unit}). Skipping."
            )
            return False

        if self.active_trades >= self.config.trading.max_concurrent_trades:
            self.logger.warning(
                f"Max concurrent trades reached ({self.config.trading.max_concurrent_trades}). Skipping."
            )
            return False

        asset_name = self._format_asset(symbol)

        # Verify the asset is actually tradable RIGHT NOW before claiming a slot.
        # Otherwise buy() blocks waiting for a realtime price that never arrives
        # (e.g. a non-OTC pair when its market is closed), holding the concurrency
        # slot and silently blocking every later trade. Fail fast and loudly here.
        if not await self._is_asset_open(asset_name):
            self.logger.error(
                f"Asset {asset_name} is CLOSED / unavailable right now — "
                f"trade NOT placed, skipping (slot not held)."
            )
            return False

        # Claim slot before any await so concurrent callers see the correct count
        self.active_trades += 1

        try:
            base_amount = await self._calc_amount()
            period_secs = self._expiry_to_seconds(expiry or "00:01:00")   # M1=60, M5=300

            # ── No martingale: place once, settle in the background ───────────
            if not self.config.trading.martingale_enabled:
                amount = pre_configured_amount if pre_configured_amount is not None else base_amount
                placed, _ = await self._place_one(
                    asset_name, direction, period_secs, amount, expiry,
                    wait_result=False, not_after=not_after,
                )
                return placed

            # ── 2-Step 2-Cycle Auto-Martingale ────────────────────────────────
            # Cycle 1: Trade 1 ($2) -> if loss -> Trade 2 ($4) on next candle.
            #   If either wins -> resets to Cycle 1, Trade 1 ($2).
            #   If Trade 2 loses -> advances to Cycle 2 for NEXT Telegram signal.
            # Cycle 2 (on next signal): Trade 3 ($8) -> if loss -> Trade 4 ($16).
            #   If Trade 3 or Trade 4 wins -> recovers loss, resets to Cycle 1 ($2).
            #   If Trade 4 loses -> resets to Cycle 1 ($2).
            cycle = max(1, getattr(self, 'martingale_cycle', 1))
            steps = max(1, int(self.config.trading.martingale_steps or 2))

            for step in range(1, steps + 1):
                trade_num = (cycle - 1) * 2 + step
                # Step 1 can use pre_configured_amount if available, otherwise get_cycle_trade_amount
                if step == 1 and pre_configured_amount is not None:
                    amount = pre_configured_amount
                else:
                    amount = self.get_cycle_trade_amount(base_amount, step)

                self.logger.info(
                    f"[martingale] Placing Cycle {cycle} Trade {trade_num} at ${amount:.2f} "
                    f"on {asset_name} {direction.upper()} (duration {period_secs}s)"
                )

                placed, won = await self._place_one(
                    asset_name, direction, period_secs, amount, expiry,
                    wait_result=True,
                    # Only the first order answers to the signal's entry time; a
                    # recovery is meant to be a candle later.
                    not_after=not_after if step == 1 else None,
                )
                if not placed:
                    # Couldn't place — nothing to carry, the sequence just ends.
                    return step > 1

                if won is True:
                    self.logger.info(
                        f"Martingale: WIN at ${amount:.2f} on Cycle {cycle} Trade {trade_num} — "
                        f"sequence complete, resetting to Cycle 1 (base ${base_amount:.2f})."
                    )
                    self.martingale_cycle = 1
                    self._save_daily_state()
                    return True

                if won is None:
                    self.logger.warning(
                        "Martingale: result unknown — ending this sequence "
                        "(no recovery trade on an unread result)."
                    )
                    return True

                # ── Loss ──
                if step >= steps:
                    # Final step of current cycle lost
                    if cycle == 1:
                        self.logger.warning(
                            f"Martingale: Cycle 1 lost (both Trade 1 and Trade 2 lost). "
                            f"Transitioning to CYCLE 2 for next Telegram signal (Trade 3: $8, Trade 4: $16)!"
                        )
                        self.martingale_cycle = 2
                    else:
                        self.logger.warning(
                            f"Martingale: Cycle 2 lost on Trade 4 (${amount:.2f}). Sequence over — "
                            f"resetting back to Cycle 1 (base ${base_amount:.2f})."
                        )
                        self.martingale_cycle = 1
                    self._save_daily_state()
                    return True

                # Daily caps are checked before every recovery trade.
                if self.daily_trades >= self.config.trading.max_daily_trades:
                    self.logger.warning(
                        f"Martingale: daily trade limit reached — recovery abandoned."
                    )
                    return True
                if (self.config.trading.max_daily_loss_enabled
                        and self.daily_loss >= self.config.trading.max_daily_loss):
                    self.logger.warning(
                        f"Martingale: daily loss limit reached — recovery abandoned."
                    )
                    return True

                next_trade_num = (cycle - 1) * 2 + (step + 1)
                next_amount = self.get_cycle_trade_amount(base_amount, step + 1)
                self.logger.info(
                    f"Martingale: LOSS — re-entering {asset_name} {direction.upper()} "
                    f"at ${next_amount:.2f} on next candle (Cycle {cycle} Trade {next_trade_num})."
                )

        except Exception as e:
            self.logger.error(f"Error placing trade on {symbol}: {e}")
            return False
        finally:
            self.active_trades = max(0, self.active_trades - 1)
