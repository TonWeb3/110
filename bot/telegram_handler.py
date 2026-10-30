"""
Telegram integration for signal reception.

Supported channel format (single message, all fields inline):

    🚀 One Minute Trade (1 MINT) 🚀

    🀄 EURJPY-OTC
    ⚡️ TIME ZONE UTC +5:30
    ⌚ 02:51:00 ENTRY TIME
    🔴 DOWN 🔴

    💎 Premium Signal 💎
    ━━━━━━━━━━━━━━━
    (promo footer — ignored)

The message may use stylised Unicode (bold / sans-serif) letters and digits;
they are normalised to plain ASCII before parsing. Only the block ABOVE the
first heavy divider (━) is parsed, so the promo footer can never be mistaken
for signal data. Fields extracted:
  • Pair       → two 3-letter groups, optional "-OTC"      (EURJPY-OTC)
  • Timeframe  → "(N MIN…)" → expiry, default 1 minute     (1 → 00:01:00)
  • Timezone   → "UTC +5:30"  (required to schedule the entry, no UTC fallback)
  • Entry time → "HH:MM[:SS] ENTRY TIME"  (waits until this moment, tz-adjusted)
  • Direction  → UP/CALL/BUY → call,  DOWN/PUT/SELL → put  (🟢/🔴 also accepted)

Multiple channels can be enabled simultaneously via telegram.channels in config.json.
"""

import asyncio
import logging
import re
import os
import json
import time
import math
import unicodedata
from datetime import datetime
import pytz
from typing import Optional, Dict, Any, List
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.tl.types import Channel, Chat, DocumentAttributeSticker
from .quotex_handler import QuotexHandler
from .config import Config, ChannelConfig


KNOWN_CURRENCIES = {
    'EUR', 'USD', 'GBP', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD',
    'BDT', 'INR', 'BRL', 'PKR', 'TRY', 'IDR', 'MXN', 'ARS',
    'EGP', 'NGN', 'KES', 'ZAR', 'AED', 'SAR', 'COP', 'VND',
    'RUB', 'CNY', 'SGD', 'HKD', 'SEK', 'NOK', 'DKK', 'PLN',
    'CZK', 'HUF', 'ILS', 'MYR', 'PHP', 'THB',
    'BTC', 'ETH', 'SOL', 'XRP', 'LTC', 'BCH', 'DOG', 'ADA', 'BNB',
    'XAU', 'XAG'
}


class SignalParser:
    """Parses both single-message signals and multi-message (setup + trigger sticker) signals."""

    def __init__(self, config: Config):
        self.logger = logging.getLogger(__name__)
        self.config = config

    @staticmethod
    def _normalize(text: str) -> str:
        """
        Convert stylised Unicode (mathematical bold / sans-serif) letters and
        digits back to plain ASCII so the field regexes match.
        NFKC maps e.g. 𝗢→O, 𝐓→T, 𝟭→1, 𝟓→5.
        """
        if not text:
            return ""
        return unicodedata.normalize('NFKC', text)

    @classmethod
    def extract_asset(cls, text: str) -> Optional[str]:
        """
        Extract currency pair / asset from text.
        Handles markdown styling, 'ASSET NZD /USD OTC', 'EURJPY-OTC', 'EUR/USD', etc.
        """
        if not text:
            return None
        norm = cls._normalize(text)
        # Strip markdown bold/italic formatting (*, _, `, ~)
        clean = re.sub(r'[*_`~]', '', norm)

        # 1. Look for explicit ASSET / PAIR line
        asset_m = re.search(r'(?:ASSET|PAIR)\s*[:\-]?\s*([A-Za-z/ -]+)', clean, re.IGNORECASE)
        if asset_m:
            line = asset_m.group(1).upper()
            m = re.search(r'([A-Z]{3})\s*/?\s*([A-Z]{3})\b\s*(-?\s*OTC\b)?', line)
            if m:
                asset = (m.group(1) + m.group(2)).upper()
                if m.group(3) or 'OTC' in line:
                    asset += '-OTC'
                return asset

        # 2. Look for slash-separated pairs (e.g. NZD/USD, EUR/USD-OTC)
        slash_m = re.search(r'\b([A-Z]{3})\s*/\s*([A-Z]{3})\b\s*(-?\s*OTC\b)?', clean, re.IGNORECASE)
        if slash_m:
            asset = (slash_m.group(1) + slash_m.group(2)).upper()
            if slash_m.group(3):
                asset += '-OTC'
            return asset

        # 3. Look for pair joined or space-separated, matching KNOWN_CURRENCIES
        for m in re.finditer(r'\b([A-Z]{3})\s*/?\s*([A-Z]{3})\b\s*(-?\s*OTC\b)?', clean):
            c1, c2 = m.group(1).upper(), m.group(2).upper()
            if c1 in KNOWN_CURRENCIES and c2 in KNOWN_CURRENCIES:
                asset = c1 + c2
                if m.group(3):
                    asset += '-OTC'
                return asset

        return None

    @classmethod
    def extract_expiry(cls, text: str) -> str:
        """Extract timeframe/expiry, defaulting to '00:01:00'."""
        if not text:
            return '00:01:00'
        norm = cls._normalize(text)
        tf_m = re.search(r'(\d+)\s*(?:MIN|MINT|MINUTE|M\b)', norm, re.IGNORECASE)
        if tf_m:
            mins = int(tf_m.group(1))
            hours = mins // 60
            rem_mins = mins % 60
            return f'{hours:02d}:{rem_mins:02d}:00'
        return '00:01:00'

    @classmethod
    def extract_entry_time(cls, text: str) -> Optional[str]:
        """Extract entry time: 'HH:MM[:SS] ENTRY TIME' or 'ENTRY TIME: HH:MM[:SS]'."""
        if not text:
            return None
        norm = cls._normalize(text)
        ent_m = (
            re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)\s*ENTRY', norm, re.IGNORECASE)
            or re.search(r'ENTRY\s*(?:TIME\s*)?[:\-at\s]+\s*(\d{1,2}:\d{2}(?::\d{2})?)', norm, re.IGNORECASE)
        )
        if ent_m:
            t = ent_m.group(1)
            return t if t.count(':') == 2 else t + ':00'
        return None

    @classmethod
    def extract_timezone(cls, text: str) -> Optional[str]:
        """Extract timezone string (e.g. 'UTC+5:30', 'UTC:(+5:30)')."""
        if not text:
            return None
        norm = cls._normalize(text)
        tz_m = re.search(
            r'(?:UTC\s*[:\-]?\s*\(?\s*([+-]\s*\d{1,2}:\d{2})\s*\)?|UTC\s*([+-]\s*\d{1,2}:\d{2})|([+-]\d{1,2}:\d{2}))',
            norm, re.IGNORECASE
        )
        if tz_m:
            raw = (tz_m.group(1) or tz_m.group(2) or tz_m.group(3)).replace(' ', '')
            return f'UTC{raw}' if not raw.upper().startswith('UTC') else raw.upper()
        return None

    @classmethod
    def extract_direction_from_text(cls, text: str) -> Optional[str]:
        """
        Extract trade direction from text.
        Excludes lines containing 'ASSET' or 'PAIR' so bullet emojis (like '⬇️ ASSET')
        are not confused with trade direction.
        """
        if not text:
            return None
        norm = cls._normalize(text)
        head = norm.split('━')[0]
        # Ignore lines with ASSET/PAIR to prevent bullet emojis from triggering direction
        filtered_lines = [
            l for l in head.splitlines()
            if not re.search(r'\b(ASSET|PAIR|CURRENCY)\b', l, re.IGNORECASE)
        ]
        content = '\n'.join(filtered_lines)

        if re.search(r'\b(UP|CALL|BUY)\b', content, re.IGNORECASE) or any(c in content for c in ['🟢', '🟩', '⬆', '🔼', '📈', '👍']):
            return 'call'
        if re.search(r'\b(DOWN|PUT|SELL)\b', content, re.IGNORECASE) or any(c in content for c in ['🔴', '🟥', '⬇', '🔽', '📉', '👎']):
            return 'put'
        return None

    @staticmethod
    def get_sticker_emoji(msg: Any) -> Optional[str]:
        """Extract the emoji representation of a Telegram sticker message."""
        if getattr(msg, 'sticker', False):
            emoji = getattr(msg.file, 'emoji', None)
            if emoji:
                return emoji
        doc = getattr(msg, 'document', None)
        if not doc and getattr(msg, 'media', None):
            doc = getattr(msg.media, 'document', None)
        if doc:
            for attr in getattr(doc, 'attributes', []):
                if isinstance(attr, DocumentAttributeSticker) and getattr(attr, 'alt', None):
                    return attr.alt
        return None

    @staticmethod
    def extract_direction_from_sticker(emoji: Optional[str]) -> Optional[str]:
        """Map sticker emoji to call or put."""
        if not emoji:
            return None
        if any(c in emoji for c in ['⬆', '🔼', '🟢', '🟩', '📈', '👍', '🆙']):
            return 'call'
        if any(c in emoji for c in ['⬇', '🔽', '🔴', '🟥', '📉', '👎']):
            return 'put'
        return None

    def parse_complete_signal(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse a complete single-message signal containing both asset and direction.
        """
        if not text:
            return None
        norm = self._normalize(text)
        head = norm.split('━')[0]

        asset = self.extract_asset(head)
        if not asset:
            return None

        direction = self.extract_direction_from_text(head)
        if not direction:
            return None

        expiry = self.extract_expiry(head)
        entry_time = self.extract_entry_time(head)
        timezone_str = self.extract_timezone(head)

        signal = {
            'type':       'trade',
            'asset':      asset,
            'direction':  direction,
            'expiry':     expiry,
            'entry_time': entry_time,
            'timezone':   timezone_str,
            'parsed_at':  datetime.now().isoformat(),
        }
        self.logger.info(
            f"[signal] Complete signal: {asset} {direction.upper()} "
            f"entry: {entry_time} ({timezone_str}) expiry: {expiry}"
        )
        return signal

    def parse_setup(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse a signal setup message that specifies the asset/timeframe/timezone
        but no direction yet (awaiting direction sticker in subsequent message).
        """
        if not text:
            return None
        norm = self._normalize(text)
        head = norm.split('━')[0]

        asset = self.extract_asset(head)
        if not asset:
            return None

        # If it already has direction, it's a complete signal, not a pure setup
        if self.extract_direction_from_text(head):
            return None

        expiry = self.extract_expiry(head)
        entry_time = self.extract_entry_time(head)
        timezone_str = self.extract_timezone(head)

        return {
            'asset':      asset,
            'expiry':     expiry,
            'entry_time': entry_time,
            'timezone':   timezone_str,
            'timestamp':  time.time(),
        }

    def parse_signal(self, text: str) -> Optional[Dict[str, Any]]:
        """Backward compatible single-message parser call."""
        return self.parse_complete_signal(text)


class SignalExecutor:
    """Executes trade signals — time-scheduled when the signal carries an entry time."""

    def __init__(self, config: Config, quotex_handler: QuotexHandler):
        self.config          = config
        self.quotex_handler  = quotex_handler
        self.logger          = logging.getLogger(__name__)
        self.pending_signals: Dict[str, Any] = {}

    async def schedule_signal(self, signal: Dict[str, Any]) -> bool:
        try:
            if signal['type'] == 'trade':
                sid = f"trade_{signal['asset']}_{signal['direction']}_{signal['parsed_at']}"
                self.pending_signals[sid] = {**signal, 'executed': False}
                await self._execute(sid)
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error scheduling signal: {e}")
            return False

    async def _execute(self, sid: str):
        try:
            signal = self.pending_signals.get(sid)
            if not signal or signal.get('executed'):
                return

            asset     = signal['asset']
            direction = signal['direction']
            expiry    = signal.get('expiry')

            # ── Step 1: Select the pair immediately ───────────────
            try:
                await self.quotex_handler.select_asset(asset)
            except Exception as e:
                self.logger.error(f"Could not select asset {asset}: {e}")

            # ── Step 1b: For timed signals or candle alignment, calculate wait ──
            wait_secs = None
            not_after = None      # deadline for sending order
            target_epoch = signal.get('target_epoch')
            display_entry = signal.get('entry_time')

            if target_epoch is not None:
                wait_secs = target_epoch - time.time()
                display_entry = datetime.fromtimestamp(target_epoch).strftime('%H:%M:%S')
            elif signal.get('entry_time'):
                wait_secs = self._calc_wait(signal['entry_time'], signal.get('timezone'))
                if wait_secs is None:
                    self.logger.error(
                        f"Skipping {asset} {direction.upper()} — entry time "
                        f"'{signal['entry_time']}' cannot be scheduled "
                        f"(missing or invalid timezone in signal)."
                    )
                    return
                if wait_secs < -30:
                    self.logger.warning(
                        f"Entry time already passed by {abs(wait_secs):.0f}s — skipping."
                    )
                    return
                target_epoch = time.time() + wait_secs

            # ── Step 1c: Pre-set amount + expiry ──────────────────
            # If pre-configured during the setup phase, use it; otherwise configure now.
            pre_configured_amount = signal.get('pre_configured_amount')
            if pre_configured_amount is None and target_epoch is not None:
                try:
                    pre_configured_amount = await self.quotex_handler.pre_configure_trade(expiry)
                except Exception as e:
                    self.logger.error(f"Pre-configure failed — will retry at entry time: {e}")

            # ── Step 2: Wait until candle open (minus the latency lead) ──────
            if target_epoch is not None:
                early = max(0.0, float(self.config.quotex.early_entry_seconds))
                self.logger.info(
                    f"Pair {asset} armed. Waiting {max(0.0, wait_secs - early):.1f}s "
                    f"for next candle {display_entry} (lead {early:.2f}s)..."
                )
                while True:
                    remaining = target_epoch - time.time()
                    delay = remaining - early
                    if delay <= 0.0:
                        break
                    await asyncio.sleep(delay if delay < 1.0 else min(delay - 0.5, 30.0))

                remaining = target_epoch - time.time()
                grace = max(0.0, float(
                    getattr(self.config.quotex, 'late_entry_grace_seconds', 5.0)
                ))
                fire_at   = time.time() + remaining - early
                not_after = fire_at + grace
                late      = -remaining

                if time.time() > not_after:
                    self.logger.warning(
                        f"Skipping {asset} {direction.upper()} — entry {display_entry} "
                        f"was missed by {time.time() - fire_at:.1f}s (grace {grace:.1f}s)."
                    )
                    return

                self.logger.info(
                    f"Firing now — {abs(late):.2f}s "
                    f"{'after' if late > 0 else 'before'} entry {display_entry} "
                    f"(must send within {not_after - time.time():.1f}s)."
                )

            # ── Step 4: Place trade ───────────────────────────────
            self.logger.info(f"Executing: {asset} {direction.upper()}")
            success = await self.quotex_handler.perform_trade(
                asset, direction, expiry=expiry,
                pre_configured_amount=pre_configured_amount,
                not_after=not_after,
            )

            if success:
                signal['executed'] = True
                self.logger.info(f"Trade executed: {asset} {direction.upper()}")
                logging.getLogger('trades').info(
                    f"QUOTEX_TRADE_EXECUTED | {json.dumps(signal)}"
                )
            else:
                # The reason was already logged by QuotexHandler (gate hit,
                # asset unavailable, or an unconfirmed order with its diagnosis).
                self.logger.error(
                    f"Trade execution failed for {asset} {direction.upper()} "
                    f"(entry {signal.get('entry_time') or 'immediate'}) — see the "
                    f"reason logged just above."
                )

        except Exception as e:
            self.logger.error(f"Error executing trade {sid}: {e}")
        finally:
            self.pending_signals.pop(sid, None)

    def _calc_wait(self, entry_time_str: str, timezone_str: Optional[str]) -> Optional[float]:
        """
        Returns seconds until entry time. Negative means it already passed.

        The timezone MUST be supplied by the signal — there is NO UTC fallback.
        Returns None when the timezone is missing/unparseable or the entry time
        cannot be parsed; the caller then skips the trade so it is never fired at
        the wrong moment based on an assumed timezone.
        """
        if not timezone_str:
            self.logger.error(
                f"No timezone in signal for entry '{entry_time_str}' — "
                f"cannot schedule (UTC fallback removed). Skipping."
            )
            return None

        try:
            tz_clean = timezone_str.replace(' ', '')
            if tz_clean.upper().startswith('UTC'):
                tz_clean = tz_clean[3:]
            m = re.match(r'^([+-])(\d{1,2}):(\d{2})$', tz_clean)
            if not m:
                self.logger.error(
                    f"Unparseable timezone '{timezone_str}' for entry "
                    f"'{entry_time_str}' — skipping."
                )
                return None
            sign       = 1 if m.group(1) == '+' else -1
            offset_min = sign * (int(m.group(2)) * 60 + int(m.group(3)))
            signal_tz  = pytz.FixedOffset(offset_min)

            # Build the entry instant on TODAY'S DATE IN THE SIGNAL'S TIMEZONE.
            # Using UTC's date is wrong: when the UTC date differs from the
            # signal-tz date (e.g. early-morning IST is still the previous UTC
            # day), the entry lands ~24h off — the "passed by 86248s" bug.
            now_tz = datetime.now(signal_tz)
            t      = datetime.strptime(entry_time_str, '%H:%M:%S')
            entry  = now_tz.replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            )

            delta = (entry - now_tz).total_seconds()
            # Near-midnight wrap: an entry that looks more than 12h in the past is
            # really the next day's occurrence (e.g. 00:05 entry sent at 23:58).
            if delta < -43200:
                delta += 86400
            return delta

        except Exception as e:
            self.logger.error(f"Could not calculate wait time for '{entry_time_str}': {e}")
            return None


class TelegramHandler:
    """Complete Telegram integration — monitors multiple channels simultaneously."""

    def __init__(self, config):
        self.config          = config
        self.logger          = logging.getLogger(__name__)
        self.client          = None
        self.parser          = SignalParser(self.config)
        self.executor        = None
        self.signal_callback = None
        self.session_string  = None
        self.api_id          = self.config.telegram.api_id
        self.api_hash        = self.config.telegram.api_hash
        self.phone_number    = None
        self.pending_setups: Dict[int, Dict[str, Any]] = {}

    async def initialize(self, quotex_handler: QuotexHandler):
        try:
            self.logger.info("Initializing Telegram client...")
            self._load_session()

            if not (self.session_string and self.phone_number):
                # The bot runs inside the dashboard's event loop — there is no
                # console to prompt on, and blocking here would hang the runtime.
                self.logger.error(
                    "Telegram is not signed in. Open the dashboard -> Settings -> "
                    "Connections -> Telegram and connect, then start the bot again."
                )
                return False

            session = StringSession(self.session_string) if self.session_string else StringSession()
            self.client = TelegramClient(session, self.api_id, self.api_hash)
            await self.client.start(phone=self.phone_number)

            self.session_string = self.client.session.save()
            self._save_session()
            await self._list_chats()

            self.executor = SignalExecutor(self.config, quotex_handler)
            self.logger.info("Telegram client initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram client: {e}")
            return False

    def _load_session(self):
        try:
            session_file = f"{self.config.telegram.session_name}.json"
            if os.path.exists(session_file):
                with open(session_file, 'r') as f:
                    data = json.load(f)
                self.session_string = data.get('session_string')
                self.phone_number   = data.get('phone_number')
                self.logger.info("Telegram session loaded")
        except Exception as e:
            self.logger.debug(f"Could not load session: {e}")

    def _save_session(self):
        try:
            session_file = f"{self.config.telegram.session_name}.json"
            with open(session_file, 'w') as f:
                json.dump({
                    'session_string': self.session_string,
                    'phone_number':   self.phone_number,
                    'saved_at':       datetime.now().isoformat(),
                }, f)
            self.logger.info("Telegram session saved")
        except Exception as e:
            self.logger.error(f"Could not save session: {e}")

    async def _list_chats(self):
        try:
            dialogs = await self.client.get_dialogs(limit=None)
            groups  = [d for d in dialogs if isinstance(d.entity, (Channel, Chat))]
            print("\n" + "=" * 65)
            print("  AVAILABLE GROUPS & CHANNELS")
            print("=" * 65)
            print(f"  {'TYPE':<12} {'ID (use in config.json)':<25} NAME")
            print("  " + "-" * 62)
            for d in groups:
                pid   = get_peer_id(d.entity)
                etype = "Channel" if isinstance(d.entity, Channel) else "Group"
                title = getattr(d.entity, 'title', 'Unknown')
                print(f"  {etype:<12} {str(pid):<25} {title}")
                self.logger.info(f"Chat: {etype} | {pid} | {title}")
            print("=" * 65)
            enabled = [c for c in self.config.telegram.channels if c.enabled]
            print(f"  Enabled channels: {[c.identifier for c in enabled]}")
            print("=" * 65 + "\n")
        except Exception as e:
            self.logger.error(f"Error listing chats: {e}")

    async def _resolve_channel(self, identifier):
        """Resolve a channel identifier (name, @username, or numeric ID) to a Telethon entity."""
        is_plain = isinstance(identifier, str) and ' ' in identifier.strip()

        if not is_plain:
            try:
                return await self.client.get_entity(identifier)
            except Exception:
                pass
            if isinstance(identifier, str) and identifier.strip().lstrip('-').isdigit():
                try:
                    return await self.client.get_entity(int(identifier.strip()))
                except Exception:
                    pass

        dialogs  = await self.client.get_dialogs(limit=None)
        id_lower = str(identifier).strip().lower()
        for d in dialogs:
            title = getattr(d.entity, 'title', '') or getattr(d.entity, 'first_name', '') or ''
            if title.strip().lower() == id_lower:
                self.logger.info(f"Found '{identifier}' (ID: {get_peer_id(d.entity)})")
                return d.entity

        raise ValueError(f"Could not find channel/group '{identifier}'.")

    async def start_monitoring(self, signal_callback):
        try:
            if not self.client:
                raise Exception("Telegram client not initialized")

            self.signal_callback = signal_callback

            enabled: List[ChannelConfig] = [
                c for c in self.config.telegram.channels if c.enabled
            ]
            if not enabled:
                self.logger.error("No channels enabled. Set enabled=true in telegram.channels.")
                return

            watch_ids: List[int] = []

            for ch in enabled:
                try:
                    entity = await self._resolve_channel(ch.identifier)
                    watch_ids.append(entity.id)
                    self.logger.info(
                        f"Watching: {getattr(entity, 'title', ch.identifier)} (ID: {entity.id})"
                    )
                    async for msg in self.client.iter_messages(entity, limit=1):
                        preview = msg.text[:80] if msg.text else '[media]'
                        self.logger.info(f"  Last message (ID: {msg.id}): '{preview}'")
                except Exception as e:
                    self.logger.error(f"Could not resolve channel '{ch.identifier}': {e}")

            if not watch_ids:
                self.logger.error("No channels could be resolved. Check config.")
                return

            @self.client.on(events.NewMessage(chats=watch_ids))
            async def on_message(event):
                await self._handle_message(event)

            await self.client.run_until_disconnected()

        except Exception as e:
            self.logger.error(f"Error monitoring channels: {e}")

    async def _prewarm_setup(self, chat_id: int, setup: Dict[str, Any]):
        """Pre-select asset and pre-configure trade amount so execution is instantaneous on trigger."""
        try:
            qh = self.executor.quotex_handler if self.executor else None
            if qh and qh.is_connected:
                asset = setup['asset']
                expiry = setup['expiry']
                self.logger.info(f"[pre-warm] Pre-selecting asset {asset} on Quotex...")
                await qh.select_asset(asset)
                pre_amt = await qh.pre_configure_trade(expiry)
                if chat_id in self.pending_setups and self.pending_setups[chat_id] is setup:
                    setup['pre_configured_amount'] = pre_amt
                    self.logger.info(f"[pre-warm] Asset {asset} armed; pre-configured amount: ${pre_amt:.2f}")
        except Exception as e:
            self.logger.debug(f"[pre-warm] Pre-warming encountered error: {e}")

    async def _handle_message(self, event):
        """
        Handle incoming Telegram messages from monitored channels.
        Supports:
          1. Complete single-message signals (asset + direction inline).
          2. Multi-message signals:
             - Message 1: Setup message (asset, expiry, timezone).
             - Message 2: Trigger sticker or message (direction ⬆️/⬇️).
        """
        msg = event.message
        chat_id = event.chat_id
        raw_text = msg.text or ""
        sticker_emoji = self.parser.get_sticker_emoji(msg)

        self.logger.info(
            f"New message ID: {msg.id} from chat {chat_id} "
            f"({'sticker: ' + sticker_emoji if sticker_emoji else 'text: ' + repr(raw_text[:80])})"
        )

        try:
            # ── 1. Check for complete single-message signal ─────────
            if raw_text:
                complete_signal = self.parser.parse_complete_signal(raw_text)
                if complete_signal:
                    self.pending_setups.pop(chat_id, None)
                    if self.signal_callback:
                        await self.signal_callback(complete_signal)
                    return

            # ── 2. Check for trigger (Sticker or standalone direction) ──
            trigger_dir = None
            if sticker_emoji:
                trigger_dir = self.parser.extract_direction_from_sticker(sticker_emoji)
            elif raw_text and not self.parser.extract_asset(raw_text):
                trigger_dir = self.parser.extract_direction_from_text(raw_text)

            if trigger_dir:
                setup = self.pending_setups.get(chat_id)
                if setup:
                    age = time.time() - setup['timestamp']
                    if age <= 180:  # 3 minutes valid window
                        self.pending_setups.pop(chat_id, None)

                        # Calculate next candle boundary (epoch seconds)
                        expiry = setup['expiry']
                        period_secs = QuotexHandler._expiry_to_seconds(expiry)
                        now = time.time()
                        next_candle_epoch = math.ceil(now / period_secs) * period_secs
                        if next_candle_epoch - now < 2.0:
                            next_candle_epoch += period_secs

                        signal = {
                            'type':                  'trade',
                            'asset':                 setup['asset'],
                            'direction':             trigger_dir,
                            'expiry':                setup['expiry'],
                            'entry_time':            setup.get('entry_time'),
                            'timezone':              setup.get('timezone'),
                            'target_epoch':          next_candle_epoch if not setup.get('entry_time') else None,
                            'pre_configured_amount': setup.get('pre_configured_amount'),
                            'parsed_at':             datetime.now().isoformat(),
                            'multi_message':         True,
                        }
                        display_tgt = (
                            setup.get('entry_time')
                            or datetime.fromtimestamp(next_candle_epoch).strftime('%H:%M:%S')
                        )
                        self.logger.info(
                            f"[signal] Multi-message signal triggered: {signal['asset']} {signal['direction'].upper()} "
                            f"(via {'sticker ' + sticker_emoji if sticker_emoji else 'text'}) "
                            f"expiry: {signal['expiry']} -> next candle at {display_tgt}"
                        )
                        if self.signal_callback:
                            await self.signal_callback(signal)
                        return
                    else:
                        self.logger.warning(
                            f"Pending setup for {setup['asset']} in chat {chat_id} "
                            f"expired ({age:.1f}s > 180s) — trigger ignored."
                        )
                        self.pending_setups.pop(chat_id, None)
                else:
                    self.logger.debug(
                        f"Direction trigger received ({trigger_dir}) but no pending setup found for chat {chat_id}."
                    )

            # ── 3. Check for setup message (Asset without direction) ──
            if raw_text:
                setup = self.parser.parse_setup(raw_text)
                if setup:
                    self.pending_setups[chat_id] = setup
                    self.logger.info(
                        f"[signal setup] Armed: {setup['asset']} expiry: {setup['expiry']} "
                        f"tz: {setup['timezone'] or 'none'} for chat {chat_id}. Awaiting direction sticker..."
                    )
                    # Pre-warm asset and amount asynchronously on Quotex
                    if self.executor and self.executor.quotex_handler:
                        asyncio.ensure_future(self._prewarm_setup(chat_id, setup))
                    return

        except Exception as e:
            self.logger.error(f"Error handling message {msg.id}: {e}", exc_info=True)

    async def disconnect(self):
        try:
            if self.client:
                await self.client.disconnect()
                self.logger.info("Telegram client disconnected")
        except Exception as e:
            self.logger.error(f"Error disconnecting: {e}")

    async def test_connection(self) -> bool:
        try:
            if not self.client:
                return False
            me = await self.client.get_me()
            self.logger.info(f"Connected as: {me.first_name}")
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return False
