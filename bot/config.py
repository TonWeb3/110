"""
Configuration management for the Quotex Telegram Trading Bot
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import logging
from typing import Optional, Union, List

# config.json lives beside main.py. Resolving it against the CWD instead meant
# that launching the bot from anywhere else found no file, silently fell back to
# DEFAULTS (risk_amount 1.0, max_daily_trades 10, martingale off) and wrote a
# stray config.json into whatever directory it was started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_config_path() -> Path:
    """
    Where config.json lives.

    In a container the code directory (/app) is rebuilt on every deploy, so a
    config.json written there is lost — and if the file never made it into the
    image, the dashboard has nothing to read. Point QUOTEX_CONFIG_PATH at a file
    (or DATA_DIR at a mounted volume) to keep settings outside the image.
    """
    explicit = os.getenv("QUOTEX_CONFIG_PATH") or os.getenv("CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "config.json"
    return PROJECT_ROOT / "config.json"


DEFAULT_CONFIG_FILE = resolve_config_path()


class ConfigParseError(Exception):
    """config.json exists but could not be parsed. Carries a precise reason."""


def load_json_file(path) -> dict:
    """
    Read a JSON config written by a human or uploaded from another machine.

    Handles what actually goes wrong in practice rather than assuming clean
    UTF-8: a BOM, a UTF-16 file (PowerShell's `>` redirect writes one), and the
    trailing commas left behind when lines are deleted by hand — e.g. removing
    the email/password lines from the "quotex" block leaves `"password": "",`
    dangling before `}`.

    Raises ConfigParseError naming the line, column and the offending text, so
    the failure can be fixed instead of guessed at.
    """
    path = Path(path)
    raw = path.read_bytes()
    if not raw.strip():
        raise ConfigParseError(f"{path} is empty (0 bytes of content).")

    text = None
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
        try:
            candidate = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if candidate.lstrip().startswith(("{", "[")):
            text = candidate
            break
    if text is None:
        raise ConfigParseError(
            f"{path} is not text this program can read — it is not UTF-8, "
            f"UTF-8 with BOM, or UTF-16. Re-save it as UTF-8."
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        # One repair attempt: trailing commas, the usual result of deleting
        # lines by hand. Anything else is reported, never guessed at.
        repaired = re.sub(r",(\s*[}\]])", r"\1", text)
        if repaired != text:
            try:
                data = json.loads(repaired)
                logging.getLogger(__name__).warning(
                    f"{path} had a trailing comma (line {first_error.lineno}); "
                    f"reading it anyway. Save from Settings to tidy the file."
                )
                return data
            except json.JSONDecodeError:
                pass

        lines = text.splitlines()
        offending = (lines[first_error.lineno - 1].strip()
                     if 0 < first_error.lineno <= len(lines) else "")
        raise ConfigParseError(
            f"{path} is not valid JSON: {first_error.msg} at line "
            f"{first_error.lineno}, column {first_error.colno}"
            + (f' — the text there is: {offending!r}' if offending else "")
        ) from first_error


# ── Tolerant readers ────────────────────────────────────────────────────────
# config.json is edited by hand, so a value may arrive as a string ("2"), and a
# blank or nonsense entry must fall back to the documented default rather than
# crashing the load and taking every other setting down with it.

def _num(value, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class ChannelConfig:
    """Configuration for a single monitored Telegram channel."""
    enabled: bool = True
    identifier: Union[str, int] = ""   # group name, @username, or numeric ID


@dataclass
class QuotexConfig:
    """Quotex platform configuration"""
    email: str = ""
    password: str = ""
    host: str = "quotex.com"
    early_entry_seconds: float = 0.5   # latency lead: fire this many seconds before the entry second
    # Grace period: if the order cannot be sent within this many seconds of the
    # intended firing moment, the signal is dropped instead of entered late.
    late_entry_grace_seconds: float = 5.0
    # Order style, used for every asset. TIMER = optionType 100, a relative
    # duration that runs from the fill (what the signals describe).
    time_mode: str = "TIMER"


@dataclass
class TelegramConfig:
    """Telegram configuration"""
    api_id: Optional[int] = None
    api_hash: str = ""
    session_name: str = "quotex_bot_session"
    channels: List[ChannelConfig] = field(default_factory=list)


@dataclass
class TradingConfig:
    """Trading configuration"""
    account_type: str = "demo"          # "demo" or "live"
    risk_mode: str = "fixed"            # "fixed" (dollar amount) or "percent" (% of balance)
    risk_amount: float = 1.0            # Dollar amount OR percentage value (e.g. 5 = 5%)
    max_daily_trades: int = 10
    max_daily_loss: float = 50.0
    max_daily_loss_enabled: bool = True   # when False, the daily-loss limit is not enforced
    max_concurrent_trades: int = 1
    martingale_enabled: bool = False
    martingale_multiplier: float = 2.0
    martingale_steps: int = 2      # trades per signal on the SAME pair (initial + auto recoveries)
    # expiry is intentionally absent — each signal format provides its own duration


@dataclass
class LoggingConfig:
    """Logging configuration"""
    log_level: str = "INFO"
    log_file: str = "quotex_bot.log"


class Config:
    """Main configuration class"""

    def __init__(self, config_file: Optional[str] = None):
        # A bare name is resolved against the project root, so the values the
        # user set in Settings are used no matter where the bot was launched.
        path = Path(config_file) if config_file else DEFAULT_CONFIG_FILE
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.config_file = str(path)
        self.logger = logging.getLogger(__name__)
        self.load_error: Optional[str] = None
        self._stamp_seen = None      # file fingerprint at the last successful read
        self._load_config()

    def _stamp(self):
        """Fingerprint the file so an external edit can be detected cheaply."""
        try:
            st = os.stat(self.config_file)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def refresh_if_changed(self) -> bool:
        """
        config.json is the single source of truth.

        If the file has changed since it was last read — edited by hand,
        replaced on a volume, written by the Settings screen — re-read it here
        so the very next decision uses the new values. Costs one os.stat() when
        nothing has changed, which is the normal case.

        Returns True if the file was re-read.
        """
        stamp = self._stamp()
        if stamp is None or stamp == self._stamp_seen:
            return False

        fields = ("risk_mode", "risk_amount", "max_daily_trades",
                  "max_daily_loss", "max_daily_loss_enabled",
                  "max_concurrent_trades", "martingale_enabled",
                  "martingale_multiplier", "martingale_steps", "account_type")
        before = {f: getattr(self.trading, f) for f in fields}

        if not self.reload():
            # Unreadable now: keep running on the values already in use rather
            # than dropping to defaults mid-session.
            return False

        changes = ", ".join(
            f"{f}: {before[f]} -> {getattr(self.trading, f)}"
            for f in fields if before[f] != getattr(self.trading, f)
        )
        if changes:
            self.logger.info(f"config.json changed — now using {changes}")
        return True

    def reload(self) -> bool:
        """
        Re-read config.json in place.

        Every component shares this one object, so mutating it makes a Settings
        change take effect on the RUNNING bot instead of waiting for a restart.
        """
        self.load_error = None
        self._load_config()
        return self.load_error is None

    def _load_config(self):
        """
        Start from the code defaults, then overlay whatever config.json provides.

        Only keys PRESENT in the file are applied, and each section is applied in
        isolation: one bad field logs a warning and skips that section, but never
        drops the other sections back to defaults. An unreadable file is a
        warning, not a stoppage — the bot keeps running on the values it has.
        """
        self.quotex   = QuotexConfig()
        self.telegram = TelegramConfig()
        self.trading  = TradingConfig()
        self.logging  = LoggingConfig()

        if not os.path.exists(self.config_file):
            self._create_default_config()
            self.logger.warning(f"Created default config file: {self.config_file}")
            return

        try:
            config_data = load_json_file(self.config_file)
        except Exception as e:
            # Keep the defaults already set above and leave the file alone.
            self.load_error = str(e)
            self.logger.warning(
                f"Could not read {self.config_file}: {e}. Using defaults for "
                f"this load; the file has NOT been changed."
            )
            return

        def _section(name, fn):
            try:
                fn()
            except Exception as e:
                self.logger.warning(f"config.json section '{name}' skipped ({e})")

        def _quotex():
            q = config_data["quotex"]
            if "email" in q:    self.quotex.email    = q["email"] or ""
            if "password" in q: self.quotex.password = q["password"] or ""
            if "host" in q and q["host"]: self.quotex.host = str(q["host"])
            if "early_entry_seconds" in q:
                self.quotex.early_entry_seconds = _num(q["early_entry_seconds"], 0.5)
            if "late_entry_grace_seconds" in q:
                self.quotex.late_entry_grace_seconds = _num(
                    q["late_entry_grace_seconds"], 5.0)
            if "time_mode" in q and q["time_mode"]:
                self.quotex.time_mode = str(q["time_mode"])
        if "quotex" in config_data: _section("quotex", _quotex)

        def _telegram():
            t = config_data["telegram"]
            if "api_id" in t:       self.telegram.api_id       = t["api_id"]
            if "api_hash" in t:     self.telegram.api_hash     = t["api_hash"] or ""
            if "session_name" in t and t["session_name"]:
                self.telegram.session_name = t["session_name"]
            if "channels" in t:
                self.telegram.channels = [
                    ChannelConfig(enabled=c.get("enabled", True),
                                  identifier=c.get("identifier", ""))
                    for c in (t["channels"] or [])
                ]
        if "telegram" in config_data: _section("telegram", _telegram)

        def _trading():
            # Coerced, because config.json is meant to be hand-editable: "2"
            # typed as a string must size a $2 trade.
            tr = config_data["trading"]
            if "account_type" in tr: self.trading.account_type = str(tr["account_type"])
            if "risk_mode" in tr:    self.trading.risk_mode    = str(tr["risk_mode"])
            if "risk_amount" in tr:
                self.trading.risk_amount = _num(tr["risk_amount"], 1.0)
            if "max_daily_trades" in tr:
                self.trading.max_daily_trades = _int(tr["max_daily_trades"], 10)
            if "max_daily_loss" in tr:
                self.trading.max_daily_loss = _num(tr["max_daily_loss"], 50.0)
            if "max_daily_loss_enabled" in tr:
                self.trading.max_daily_loss_enabled = _bool(
                    tr["max_daily_loss_enabled"], True)
            if "max_concurrent_trades" in tr:
                self.trading.max_concurrent_trades = _int(
                    tr["max_concurrent_trades"], 1)
            if "martingale_enabled" in tr:
                self.trading.martingale_enabled = _bool(
                    tr["martingale_enabled"], False)
            if "martingale_multiplier" in tr:
                self.trading.martingale_multiplier = _num(
                    tr["martingale_multiplier"], 2.0)
            if "martingale_steps" in tr:
                self.trading.martingale_steps = _int(tr["martingale_steps"], 2)
        if "trading" in config_data: _section("trading", _trading)

        def _logging():
            lo = config_data["logging"]
            if "log_level" in lo and lo["log_level"]:
                self.logging.log_level = str(lo["log_level"])
            if "log_file" in lo and lo["log_file"]:
                self.logging.log_file = str(lo["log_file"])
        if "logging" in config_data: _section("logging", _logging)

        self._stamp_seen = self._stamp()
        self.logger.info(f"Configuration loaded from {self.config_file}")

    def _create_default_config(self):
        """Only ever called when no config file exists at all."""
        self.quotex  = QuotexConfig()
        self.telegram = TelegramConfig()
        self.trading  = TradingConfig()
        self.logging  = LoggingConfig()
        self.save_config()

    def save_config(self):
        try:
            config_data = {
                'quotex': {
                    'email':              self.quotex.email,
                    'password':           self.quotex.password,
                    'host':               self.quotex.host,
                    'early_entry_seconds': self.quotex.early_entry_seconds,
                    'late_entry_grace_seconds': self.quotex.late_entry_grace_seconds,
                    'time_mode':           self.quotex.time_mode,
                },
                'telegram': {
                    'api_id':          self.telegram.api_id,
                    'api_hash':        self.telegram.api_hash,
                    'session_name':    self.telegram.session_name,
                    'channels': [
                        {
                            'enabled':    c.enabled,
                            'identifier': c.identifier,
                        }
                        for c in self.telegram.channels
                    ],
                },
                'trading': {
                    'account_type':          self.trading.account_type,
                    'risk_mode':             self.trading.risk_mode,
                    'risk_amount':           self.trading.risk_amount,
                    'max_daily_trades':         self.trading.max_daily_trades,
                    'max_daily_loss':           self.trading.max_daily_loss,
                    'max_daily_loss_enabled':   self.trading.max_daily_loss_enabled,
                    'max_concurrent_trades':    self.trading.max_concurrent_trades,
                    'martingale_enabled':    self.trading.martingale_enabled,
                    'martingale_multiplier': self.trading.martingale_multiplier,
                    'martingale_steps':      self.trading.martingale_steps,
                },
                'logging': self.logging.__dict__,
            }
            Path(self.config_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2)
            self.logger.info(f"Configuration saved to {self.config_file}")
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")

    def validate(self) -> bool:
        errors = []
        if self.telegram.api_id is None:
            errors.append("Telegram API ID is required")
        if not self.telegram.api_hash:
            errors.append("Telegram API Hash is required")
        enabled = [c for c in self.telegram.channels if c.enabled]
        if not enabled:
            errors.append("At least one channel must be enabled in telegram.channels")
        for error in errors:
            self.logger.error(f"Configuration error: {error}")
        return len(errors) == 0
