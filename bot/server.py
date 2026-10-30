"""
QUOTEX1 Dashboard — Flask + SocketIO backend
"""

import logging
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO, emit

BASE_DIR     = Path(__file__).parent.parent          # QUOTEX1 root
_ASSETS_DIR  = BASE_DIR / "dashboard"               # HTML/CSS/JS live here

# The ONE config location, shared with bot/config.py so the dashboard and the
# bot can never read different files. Override with QUOTEX_CONFIG_PATH (or
# DATA_DIR) to keep settings on a volume instead of inside the image.
from bot.config import (Config, load_json_file,
                        DEFAULT_CONFIG_FILE as CONFIG_FILE)
sys.path.insert(0, str(BASE_DIR))

app = Flask(
    __name__,
    template_folder=str(_ASSETS_DIR / "templates"),
    static_folder=str(_ASSETS_DIR / "static"),
)
app.config["SECRET_KEY"] = "qx-dashboard-2026"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ═══════════════════════════════════════════════════════════
#  SHARED ASYNC RUNTIME
#  ONE event loop for the entire process. Every async thing — the Quotex
#  session, the Telegram auth client, the trading bot — lives on it.
#  That matters for two reasons:
#    1. A Quotex session opened from Settings is the SAME live object the bot
#       later uses. aiohttp/websocket connections are bound to the loop that
#       created them, so a per-request loop could never be handed over; the
#       old code had to disconnect right after testing, forcing the bot to log
#       in again (and re-trigger the email PIN).
#    2. Per-request loops were created, run once and abandoned while their
#       tasks were still scheduled — which is exactly what printed
#       "Task was destroyed but it is pending!". This loop is never closed.
# ═══════════════════════════════════════════════════════════

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock  = threading.Lock()
_loop_ready = threading.Event()


def _runtime() -> asyncio.AbstractEventLoop:
    """Return the shared event loop, starting its thread on first use."""
    global _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        _loop_ready.clear()

        def _runner():
            global _loop
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop = loop
            _loop_ready.set()
            loop.run_forever()

        threading.Thread(target=_runner, daemon=True, name="async-runtime").start()
        if not _loop_ready.wait(timeout=10):
            raise RuntimeError("async runtime failed to start")
        return _loop


def submit(coro):
    """Schedule a coroutine on the shared loop; returns a concurrent Future."""
    return asyncio.run_coroutine_threadsafe(coro, _runtime())


def run_sync(coro, timeout: float = 30):
    """Run a coroutine on the shared loop and wait for its result."""
    return submit(coro).result(timeout=timeout)


# ─── Shared state ────────────────────────────────────────────

_tg_client_store: dict = {}   # holds the live Telethon client during auth

# Live reference to the TradingBot — set by main.py when the bot starts.
_bot_instance = None

# The running TradingBot.start() task (a concurrent.futures.Future).
_bot_task = None

# The Quotex session opened from Settings. Kept ALIVE and handed to the bot.
_shared_quotex = None

# Fallback store when bot is not running — tracks alert and last known Quotex connection
_server_state: dict = {"alert": None, "quotex_connected": False}

# ── OTP / PIN coordination ─────────────────────────────────
# When Quotex emails a PIN, pyquotex awaits our callback (it supports async
# callbacks). We await a Future instead of blocking a thread, so the shared
# loop keeps serving the bot and the balance monitor while the user types.
_otp_future: Optional[asyncio.Future] = None


MAX_OTP_ATTEMPTS = 3


def _make_otp_callback():
    """
    Async on_otp_callback for pyquotex — resolved by POST /api/quotex/pin.

    Attempts are capped. pyquotex's awaiting_pin() re-invokes this callback by
    RECURSION on any empty or non-digit code, with no limit and no delay
    (network/login.py:91-93), so returning "" forever would spin the login
    until it blew the recursion limit. After MAX_OTP_ATTEMPTS we raise instead,
    which unwinds that recursion and fails the connect cleanly.
    """
    attempts = {"n": 0}

    async def callback(message: str) -> str:
        global _otp_future
        attempts["n"] += 1
        if attempts["n"] > MAX_OTP_ATTEMPTS:
            socketio.emit("quotex_otp_failed",
                          {"message": "PIN not provided — login aborted."})
            raise RuntimeError(
                f"Quotex PIN not supplied after {MAX_OTP_ATTEMPTS} prompts — aborting login."
            )

        _otp_future = asyncio.get_running_loop().create_future()
        socketio.emit("quotex_otp_required",
                      {"message": str(message), "attempt": attempts["n"]})
        try:
            return await asyncio.wait_for(asyncio.shield(_otp_future), timeout=300)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return ""
        finally:
            _otp_future = None

    return callback


# ─── Helpers ─────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    """
    Read a JSON file. Tolerates a UTF-8 BOM (Windows editors and PowerShell add
    one). A malformed file is LOGGED rather than swallowed: returning {} in
    silence made the dashboard show placeholder defaults as if that were the
    real configuration.
    """
    try:
        if path.exists():
            return load_json_file(path)
    except Exception as e:
        logging.getLogger(__name__).error(f"Could not read {path}: {e}")
    return default if default is not None else {}


def _config_error() -> Optional[str]:
    """The precise reason config.json cannot be read, or None if it is fine."""
    try:
        if not CONFIG_FILE.exists():
            return f"{CONFIG_FILE} does not exist."
        load_json_file(CONFIG_FILE)
        return None
    except Exception as e:
        return str(e)


def _normalize_phone(raw) -> str:
    """
    Normalize any user-entered phone number to digits only (no +, spaces, dashes
    or parentheses), so every format works — including a US number typed plainly
    as 12342342345, or +1 (234) 234-2345, or 1-234-234-2345. The country code
    MUST be included (e.g. leading 1 for the US). A leading '00' international
    prefix is converted to nothing extra (kept as digits; Telegram accepts the
    country code form). Returns '' if there are no digits.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    # Drop a leading international call prefix like 00 (e.g. 0012342342345 -> 12342342345)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base. Nested dicts are merged; lists and
    scalars in override replace those in base. This lets the dashboard send only
    the fields it edits while everything else already in config.json (e.g.
    session_name) is preserved exactly.
    """
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _pending_signals(th) -> list:
    """Pending (scheduled) signals with a live countdown for the dashboard."""
    out = []
    if not (th and th.executor):
        return out
    for sig in list(th.executor.pending_signals.values()):
        item = {
            "asset":        sig.get("asset"),
            "direction":    sig.get("direction"),
            "expiry":       sig.get("expiry"),
            "entry_time":   sig.get("entry_time"),
            "seconds_left": None,
        }
        if sig.get("entry_time"):
            try:
                left = th.executor._calc_wait(sig["entry_time"], sig.get("timezone"))
                item["seconds_left"] = round(left, 1) if left is not None else None
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda i: (i["seconds_left"] is None, i["seconds_left"]))
    return out


def _quotex_is_connected() -> bool:
    """True when a live Quotex session exists — the bot's, or the shared one."""
    if _bot_instance is not None and _bot_instance.quotex_handler is not None:
        return bool(_bot_instance.quotex_handler.is_connected)
    if _shared_quotex is not None:
        return bool(_shared_quotex.is_connected)
    return bool(_server_state.get("quotex_connected", False))


def get_bot_state() -> dict:
    """
    Live bot state. Figures come from whichever QuotexHandler is alive — the
    bot's while it runs, otherwise the shared session opened from Settings —
    so the dashboard keeps showing the balance with the bot stopped.
    """
    bot = _bot_instance
    cfg = _read_json(CONFIG_FILE)
    trading = cfg.get("trading", {})

    qh      = (bot.quotex_handler if bot is not None else None) or _shared_quotex
    pending = _pending_signals(bot.telegram_handler) if bot is not None else []

    return {
        "status":          "running" if (bot is not None and bot.running) else "stopped",
        "daily_trades":    qh.daily_trades if qh else 0,
        "wins":            qh.wins if qh else 0,
        "losses":          qh.losses if qh else 0,
        "daily_pnl":       qh.daily_pnl if qh else 0.0,
        "daily_loss":      qh.daily_loss if qh else 0.0,
        "balance":         qh.balance if qh else None,
        "day_open_balance": qh._day_start_balance if qh else None,
        "account_type":    (qh.config.trading.account_type if qh
                            else trading.get("account_type", "demo")),
        "risk_mode":       (qh.config.trading.risk_mode if qh
                            else (trading.get("risk_mode") or "fixed")),
        "max_daily_trades":       trading.get("max_daily_trades", 0),
        "max_daily_loss":         trading.get("max_daily_loss", 0),
        "max_daily_loss_enabled": trading.get("max_daily_loss_enabled", True),
        # Whether the deployment supplies Quotex credentials via the
        # environment, so config.json can be uploaded without them. A boolean
        # only — never the values themselves.
        "quotex_env_credentials": bool(os.getenv("QUOTEX_EMAIL")
                                       and os.getenv("QUOTEX_PASSWORD")),
        "active_trades":          qh.active_trades if qh else 0,
        "max_concurrent_trades":  trading.get("max_concurrent_trades", 1),
        "last_trade":      qh.last_trade if qh else None,
        "pending":         pending,
        "active_signals":  len(pending),
        "alert":           bot.alert if bot is not None else _server_state.get("alert"),
        "quotex_connected": _quotex_is_connected(),
        "bot_running":     _bot_is_running(),
    }


def get_connection_status() -> dict:
    """Return live connection status — reads directly from bot instance when running."""
    config = _read_json(CONFIG_FILE)
    session_name = (config.get("telegram", {}).get("session_name") or "quotex_bot_session")
    session_data = _read_json(BASE_DIR / f"{session_name}.json")
    telegram_ok  = bool(session_data.get("session_string"))

    return {"telegram": telegram_ok, "quotex": _quotex_is_connected()}


# ─── Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    return jsonify(state)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    # no-store so the dashboard always reflects the live config.json and never a
    # cached copy — otherwise edits made directly on disk wouldn't show up.
    if not CONFIG_FILE.exists():
        # A fresh deployment (or a volume mounted over the image) has no config
        # yet. Create one from the defaults so Settings is usable and the user
        # can fill it in — this is NOT the same as overwriting a file that
        # exists but cannot be parsed, which stays forbidden.
        logging.getLogger(__name__).warning(
            f"No config file at {CONFIG_FILE} — creating one from defaults."
        )
        try:
            Config(str(CONFIG_FILE))
        except Exception as e:
            logging.getLogger(__name__).error(f"Could not create {CONFIG_FILE}: {e}")

    cfg = _read_json(CONFIG_FILE)
    if not cfg.get("trading"):
        # The file is missing sections or could not be parsed. Serve the
        # EFFECTIVE settings (defaults overlaid with whatever did parse) so the
        # form is always usable, and attach a warning explaining what happened.
        # Never block the dashboard on a bad file.
        effective = Config(str(CONFIG_FILE))
        cfg = {
            "quotex": {
                "email": effective.quotex.email,
                "password": effective.quotex.password,
                "early_entry_seconds": effective.quotex.early_entry_seconds,
                "late_entry_grace_seconds": effective.quotex.late_entry_grace_seconds,
                "time_mode": effective.quotex.time_mode,
            },
            "telegram": {
                "api_id": effective.telegram.api_id,
                "api_hash": effective.telegram.api_hash,
                "session_name": effective.telegram.session_name,
                "channels": [{"enabled": c.enabled, "identifier": c.identifier}
                             for c in effective.telegram.channels],
            },
            "trading": effective.trading.__dict__,
            "logging": effective.logging.__dict__,
        }
        problem = _config_error()
        if problem:
            cfg["warning"] = (f"{problem} Showing default settings — saving "
                              f"will replace the unreadable file.")
    resp = jsonify(cfg)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/settings", methods=["POST"])
def save_settings():
    try:
        data = request.get_json(force=True)
        # Merge over the existing file so fields the dashboard doesn't send are
        # preserved exactly (e.g. session_name). Whatever the form does send
        # overwrites the matching keys — so frontend edits land in config.json.
        existing = _read_json(CONFIG_FILE, {})
        merged = _deep_merge(existing, data)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(merged, indent=2),
                               encoding="utf-8")

        # Push the new values into whatever is already running. Every component
        # shares one Config object, so reloading it in place means a Settings
        # change applies immediately instead of being ignored until a restart.
        applied = []
        for holder in (_bot_instance, _shared_quotex):
            cfg = getattr(holder, "config", None)
            if cfg is not None and cfg not in applied:
                try:
                    cfg.reload()
                    applied.append(cfg)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        f"Could not apply settings to the running bot: {e}")
        if applied:
            t = applied[0].trading
            logging.getLogger(__name__).info(
                f"Settings applied live — risk_mode={t.risk_mode} "
                f"risk_amount={t.risk_amount:g} max_daily_trades={t.max_daily_trades} "
                f"martingale={'on' if t.martingale_enabled else 'off'}"
            )
        return jsonify({"success": True, "applied_live": bool(applied)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ─── Bot start / stop ─────────────────────────────────────────

def _bot_is_running() -> bool:
    return _bot_task is not None and not _bot_task.done()


async def _run_trading_bot():
    """
    Run TradingBot.start() on the shared loop, handing it the Quotex session
    that Settings already opened (if any) so it is NOT re-established.
    """
    global _bot_instance
    from main import TradingBot
    bot = TradingBot(quotex_handler=_shared_quotex)
    try:
        await bot.start()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[bot] Fatal error: {e}")
    finally:
        _bot_instance = None
        _server_state["alert"] = None
        socketio.emit("bot_status", {"running": False})


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global _bot_task
    if _bot_is_running():
        return jsonify({"success": False, "message": "Bot already running"})

    _server_state["alert"] = None
    _bot_task = submit(_run_trading_bot())
    return jsonify({"success": True})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    """
    Stop the bot cleanly: ask it to shut down (which disconnects Telegram and
    cancels its background tasks), then wait briefly for start() to unwind.
    The Quotex session opened from Settings is deliberately left connected.
    """
    global _bot_task
    bot = _bot_instance
    if not (bot and _bot_is_running()):
        return jsonify({"success": False, "message": "Bot not running"})

    try:
        run_sync(bot.shutdown(), timeout=15)
    except Exception as e:
        print(f"[bot] shutdown error: {e}")

    task = _bot_task
    if task is not None:
        try:
            task.result(timeout=10)      # start() returns once Telegram disconnects
        except Exception:
            task.cancel()                # last resort — never leave it pending
    _bot_task = None

    socketio.emit("bot_status", {"running": False})
    return jsonify({"success": True})


def _patch_state(patch: dict):
    """
    Update mutable state fields.
    - alert: stored on bot instance (if running) or _server_state fallback
    - quotex_connected: stored in _server_state for badge display when bot not running
    """
    if "alert" in patch:
        if _bot_instance is not None:
            _bot_instance.alert = patch["alert"]
        else:
            _server_state["alert"] = patch["alert"]
    if "quotex_connected" in patch:
        _server_state["quotex_connected"] = bool(patch["quotex_connected"])


# ─── Log file: history, live tail, download ───────────────────

def _log_path() -> Path:
    """The log file the bot is actually writing to (honours logging.log_file)."""
    cfg  = _read_json(CONFIG_FILE)
    name = (cfg.get("logging", {}) or {}).get("log_file") or "quotex_bot.log"
    return BASE_DIR / "logs" / name


def _parse_log_line(line: str) -> dict:
    """Split a written log line into the shape the dashboard renders."""
    level = "ERROR" if "ERROR" in line else "WARNING" if "WARNING" in line else "INFO"
    # File format: "YYYY-MM-DD HH:MM:SS | logger | LEVEL | message"
    stamp = line[11:19] if (len(line) > 19 and line[4] == "-" and line[13] == ":") else ""
    return {
        "message": line,
        "level":   level,
        "time":    stamp or datetime.now().strftime("%H:%M:%S"),
    }


@app.route("/api/logs")
def api_logs():
    """
    Recent log lines, so reloading the page restores history instead of showing
    an empty console. The live tail below only streams what arrives afterwards.
    """
    try:
        limit = max(1, min(int(request.args.get("lines", 400)), 5000))
    except (TypeError, ValueError):
        limit = 400

    path  = _log_path()
    lines = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.readlines()[-limit:]
            lines = [_parse_log_line(l.rstrip("\n")) for l in raw if l.strip()]
        except Exception as e:
            lines = [{"message": f"Could not read {path.name}: {e}",
                      "level": "ERROR", "time": datetime.now().strftime("%H:%M:%S")}]

    resp = jsonify({"file": path.name, "exists": path.exists(), "lines": lines})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/logs/download")
def api_logs_download():
    """Download the full log file."""
    path = _log_path()
    if not path.exists():
        return jsonify({"success": False, "message": "No log file yet."}), 404
    return send_file(
        path,
        as_attachment=True,
        download_name=f"quotex1-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log",
        mimetype="text/plain",
    )


def _tail_log_file():
    """
    Stream newly written log lines to connected dashboards.
    Starts at the CURRENT end of the file — history is served by /api/logs, so
    a restart must not replay the whole file into everyone's console.
    """
    last_path = None
    last_size = 0
    while True:
        try:
            log_path = _log_path()
            if log_path != last_path:                 # log_file changed in Settings
                last_path = log_path
                last_size = log_path.stat().st_size if log_path.exists() else 0

            if log_path.exists():
                size = log_path.stat().st_size
                if size < last_size:                  # rotated or truncated
                    last_size = 0
                if size > last_size:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                socketio.emit("log", _parse_log_line(line))
                    last_size = size
        except Exception:
            pass
        time.sleep(0.5)


# ─── State broadcasting ───────────────────────────────────────

def _broadcast_state():
    """Push state updates to all connected clients every 3 seconds."""
    last_balance_poll = 0.0
    while True:
        time.sleep(3)
        try:
            # With the bot stopped, nothing else refreshes the shared session's
            # balance — do it here (every 10 s) so Settings → Connect immediately
            # shows real figures. The bot's own monitor covers the running case.
            now = time.monotonic()
            if (_bot_instance is None and _shared_quotex is not None
                    and _shared_quotex.is_connected and now - last_balance_poll > 10):
                last_balance_poll = now
                submit(_shared_quotex._refresh_balance_stats(authoritative=True))

            state = get_bot_state()
            state["connections"] = get_connection_status()
            socketio.emit("state_update", state)
        except Exception:
            pass


# ─── Telegram auth ────────────────────────────────────────────

@app.route("/api/telegram/connect", methods=["POST"])
def telegram_connect():
    data = request.get_json(force=True)
    phone = _normalize_phone(data.get("phone"))
    if len(phone) < 7:
        return jsonify({
            "success": False,
            "message": "Enter a valid phone number WITH country code "
                       "(e.g. 12342342345 for the US).",
        }), 400

    config = _read_json(CONFIG_FILE)
    api_id   = config.get("telegram", {}).get("api_id")
    api_hash = config.get("telegram", {}).get("api_hash", "")

    async def _send():
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            await client.send_code_request(phone)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        # The client stays alive on the shared loop between the code/2FA steps.
        _tg_client_store["client"] = client
        _tg_client_store["phone"]  = phone

    try:
        run_sync(_send(), timeout=30)
        return jsonify({"success": True, "message": ""})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


def _save_tg_session(client, phone):
    """Persist the authorized Telethon StringSession, then release the client."""
    session_str  = client.session.save()
    config       = _read_json(CONFIG_FILE)
    session_name = config.get("telegram", {}).get("session_name", "quotex_bot_session")
    (BASE_DIR / f"{session_name}.json").write_text(json.dumps({
        "session_string": session_str,
        "phone_number":   phone,
        "saved_at":       datetime.now().isoformat(),
    }))


@app.route("/api/telegram/verify", methods=["POST"])
def telegram_verify():
    data   = request.get_json(force=True)
    code   = (data.get("code") or "").strip()
    client = _tg_client_store.get("client")
    phone  = _tg_client_store.get("phone")

    if not (client and phone):
        return jsonify({"success": False, "message": "No auth session active — send code first"}), 400

    async def _verify() -> dict:
        try:
            await client.sign_in(phone, code)
        except Exception as e:
            if "password" in str(e).lower():
                return {"success": False, "needs_password": True,
                        "message": "2FA password required"}
            return {"success": False, "message": str(e)}
        _save_tg_session(client, phone)
        await client.disconnect()
        _tg_client_store.clear()
        return {"success": True, "message": ""}

    try:
        result = run_sync(_verify(), timeout=30)
    except Exception as e:
        result = {"success": False, "message": str(e)}

    if result.get("success"):
        socketio.emit("connection_update", {"telegram": True})
    return jsonify(result)


@app.route("/api/telegram/password", methods=["POST"])
def telegram_password():
    """Handle Telegram 2FA password."""
    data     = request.get_json(force=True)
    password = data.get("password", "")
    client   = _tg_client_store.get("client")
    phone    = _tg_client_store.get("phone")

    if not client:
        return jsonify({"success": False, "message": "No session active"}), 400

    async def _pw() -> dict:
        try:
            await client.sign_in(password=password)
            _save_tg_session(client, phone)
            await client.disconnect()
            _tg_client_store.clear()
            return {"success": True, "message": ""}
        except Exception as e:
            return {"success": False, "message": str(e)}

    try:
        result = run_sync(_pw(), timeout=30)
    except Exception as e:
        result = {"success": False, "message": str(e)}

    if result.get("success"):
        socketio.emit("connection_update", {"telegram": True})
    return jsonify(result)


@app.route("/api/telegram/disconnect", methods=["POST"])
def telegram_disconnect():
    config       = _read_json(CONFIG_FILE)
    session_name = config.get("telegram", {}).get("session_name", "quotex_bot_session")
    session_file = BASE_DIR / f"{session_name}.json"
    if session_file.exists():
        session_file.unlink()
    socketio.emit("connection_update", {"telegram": False})
    return jsonify({"success": True})


# ─── Quotex connection ────────────────────────────────────────

@app.route("/api/quotex/connect", methods=["POST"])
def quotex_connect():
    """
    Save Quotex credentials from the dashboard form, then verify the connection.
    If Quotex sends a PIN to the user's email, emits quotex_otp_required via
    SocketIO and waits for the user to submit it via /api/quotex/pin.
    """
    data     = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()

    # Credentials may live in the environment instead of the file — the
    # supported way to deploy without putting them in config.json at all.
    # Blank form fields mean "use the environment", not "no credentials".
    from_env = False
    if not email or not password:
        email    = email    or os.getenv("QUOTEX_EMAIL", "")
        password = password or os.getenv("QUOTEX_PASSWORD", "")
        from_env = True

    if not email or not password:
        return jsonify({"success": False, "message":
                        "Email and password are required — enter them here, or "
                        "set QUOTEX_EMAIL and QUOTEX_PASSWORD in the "
                        "environment."}), 400

    if not from_env:
        # Only write credentials the user actually typed. Copying environment
        # values into config.json would defeat the point of keeping them out
        # of the file.
        cfg = _read_json(CONFIG_FILE)
        cfg.setdefault("quotex", {})["email"]    = email
        cfg.setdefault("quotex", {})["password"] = password
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            return jsonify({"success": False,
                            "message": f"Could not save config: {e}"}), 500

    async def _connect() -> tuple:
        """Open (or keep) the shared Quotex session on the shared loop."""
        global _shared_quotex
        from bot.config import Config
        from bot.quotex_handler import QuotexHandler

        # Already live on these credentials? Reuse it — no second login, no PIN.
        existing = _shared_quotex
        if existing is not None and existing.is_connected:
            # Compare RESOLVED credentials: with env-based auth the config
            # fields are blank, so comparing them directly would never match
            # and every Connect click would tear down a working session.
            live_email = (os.getenv("QUOTEX_EMAIL")
                          or existing.config.quotex.email)
            live_pass  = (os.getenv("QUOTEX_PASSWORD")
                          or existing.config.quotex.password)
            if live_email == email and live_pass == password:
                return True, "Already connected — reusing the live session."
            await existing.disconnect()      # credentials changed: replace it
            _shared_quotex = None

        handler = QuotexHandler(Config(str(CONFIG_FILE)))
        if await handler.connect(otp_callback=_make_otp_callback()):
            _shared_quotex = handler         # KEEP it alive for the bot to adopt
            return True, ""
        await handler.disconnect()
        _shared_quotex = None
        return False, "Login failed — check your email/password."

    try:
        # Generous timeout: the user may need minutes to fetch the emailed PIN.
        ok, message = run_sync(_connect(), timeout=600)
    except Exception as e:
        _patch_state({"quotex_connected": False})
        return jsonify({"success": False, "message": f"Connection error: {e}"})

    if ok:
        _patch_state({"quotex_connected": True, "alert": None})
        socketio.emit("connection_update", {"quotex": True})
    else:
        _patch_state({"quotex_connected": False})
    return jsonify({"success": ok, "message": message})


@app.route("/api/quotex/pin", methods=["POST"])
def quotex_pin():
    """Receive the PIN the user typed and resolve the awaiting OTP future."""
    data = request.get_json(force=True)
    pin  = (data.get("pin") or "").strip()
    if not pin:
        return jsonify({"success": False, "message": "PIN is required."}), 400

    fut = _otp_future
    if fut is None or fut.done():
        return jsonify({"success": False, "message": "No PIN request is waiting."}), 400

    # The future belongs to the shared loop — resolve it from that loop's thread.
    _runtime().call_soon_threadsafe(lambda: fut.done() or fut.set_result(pin))
    return jsonify({"success": True})


@app.route("/api/quotex/disconnect", methods=["POST"])
def quotex_disconnect():
    """Close the live Quotex session and clear the saved credentials."""
    global _shared_quotex

    if _shared_quotex is not None:
        try:
            run_sync(_shared_quotex.disconnect(), timeout=15)
        except Exception:
            pass
        _shared_quotex = None

    cfg = _read_json(CONFIG_FILE)
    cfg.setdefault("quotex", {})["email"]    = ""
    cfg.setdefault("quotex", {})["password"] = ""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    _patch_state({"quotex_connected": False})
    socketio.emit("connection_update", {"quotex": False})
    return jsonify({"success": True})


# ─── SocketIO events ──────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    emit("state_update", state)


# ─── Entry point ─────────────────────────────────────────────

def start_server(host="0.0.0.0", port=5000):
    _runtime()          # start the shared event loop before serving requests
    threading.Thread(target=_tail_log_file,    daemon=True).start()
    threading.Thread(target=_broadcast_state,  daemon=True).start()
    print(f"\n  QUOTEX1 Dashboard → http://localhost:{port}\n")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
