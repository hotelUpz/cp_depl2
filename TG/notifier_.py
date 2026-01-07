# TG.notifier_.py

from __future__ import annotations

import asyncio
import random
from typing import *

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramNetworkError,
)

from c_utils import Utils
from MASTER.payload_ import MasterEvent

if TYPE_CHECKING:
    from c_log import UnifiedLogger


class FormatUILogs:
    """
    UI-only форматирование для Telegram.
    • Никакого HTML / Markdown
    • parse_mode=None
    • Только строки
    """

    # ==========================================================
    # GENERAL SUMMARY (AGGREGATED)
    # ==========================================================

    @staticmethod
    def format_general_summary(rows: List[Dict]) -> str:
        """
        Общий итог по закрытым позициям.
        """
        total_usdt: float = 0.0
        count: int = 0

        for r in rows:
            pnl = r.get("pnl_usdt")
            if pnl is None:
                continue
            total_usdt += pnl
            count += 1

        sign = "+" if total_usdt > 0 else ""

        return (
            "📊 GENERAL SUMMARY\n\n"
            f"Closed positions: {count}\n"
            f"PNL: {sign}{Utils.to_human_digit(total_usdt)} $"
        )

    # ==========================================================
    # GENERAL REPORT (PER POSITION)
    # ==========================================================

    @staticmethod
    def format_general_report(rows: List[Dict]) -> List[str]:
        """
        Детальный отчет по закрытым позициям.
        Каждый элемент списка = один UI-блок.
        """

        texts: List[str] = []
        if not rows:
            return texts

        total = len(rows)

        for i, r in enumerate(rows):
            symbol = r.get("symbol", "?")
            side = r.get("pos_side", "?")

            pnl = r.get("pnl_usdt")
            if pnl is None:
                pnl_text = "PNL: N/A"
            else:
                sign = "+" if pnl > 0 else ""
                pnl_text = f"PNL: {sign}{Utils.to_human_digit(pnl)} $"

            entry_ts = r.get("entry_ts")
            exit_ts = r.get("exit_ts")

            if entry_ts and exit_ts:
                duration = Utils.format_duration(max(0, exit_ts - entry_ts))
                close_time = Utils.milliseconds_to_datetime(exit_ts)
            else:
                duration = "N/A"
                close_time = "N/A"
            
            cid = r.get("cid", "?")
            block = (
                f"🆔 Copy ID: {cid}\n"
                f"📌 {symbol} {side}\n"
                f"💰 {pnl_text}\n"
                f"⏱ Duration: {duration}\n"
                f"🔒 Closed at: {close_time}"
            )

            # визуальный разделитель между позициями
            if i < total - 1:
                block += "\n" + ("—" * 16)

            texts.append(block)

        return texts    

    # ==========================================================
    # MASTER / COPY EVENT (SINGLE)
    # ==========================================================

    @staticmethod
    def format_master_log_event(cid: int, mev: "MasterEvent") -> str:
        """
        Формат одного MasterEvent для UI.
        """
        p = mev.payload or {}

        lines: List[str] = [
            f"🧾 {'MASTER' if cid == 0 else f'COPY #{cid}'}",
            f"{mev.symbol} {mev.pos_side}",
            f"event: {mev.event.upper()}",
            f"method: {mev.method}",
            f"type: {mev.sig_type}",
        ]

        # if mev.partially:
        #     lines.append("partial execution")

        if mev.closed:
            lines.append("position CLOSED")

        if "price" in p and p["price"] is not None:
            lines.append(f"price: {Utils.to_human_digit(float(p['price']))}")

        if "tp_price" in p and p["tp_price"] is not None:
            lines.append(f"tp_price: {Utils.to_human_digit(float(p['tp_price']))}")

        if "sl_price" in p and p["sl_price"] is not None:
            lines.append(f"sl_price: {Utils.to_human_digit(float(p['sl_price']))}")

        if "qty" in p and p["qty"] is not None:
            lines.append(f"qty: {Utils.to_human_digit(float(p['qty']))}")

        if "trigger_price" in p and p["trigger_price"] is not None:
            lines.append(f"trigger price: {Utils.to_human_digit(float(p['trigger_price']))}")

        # if "latency_ms" in p and p["latency_ms"] is not None:
        #     lines.append(f"latency: {p['latency_ms']} ms")

        return "\n".join(lines)

    # ==========================================================
    # FLUSH HELPERS
    # ==========================================================

    @staticmethod
    def flush_log_events(log_events: Iterable) -> List[str]:
        """
        Форматирует и очищает накопленные лог-события.

        Поддерживает:
        • (cid, MasterEvent)
        • (cid, str)
        • (cid, dict)
        """
        if not log_events:
            return []
        
        try:
            texts: List[str] = []
            for cid, payload in log_events:

                # ---------------- MasterEvent ----------------
                if isinstance(payload, MasterEvent):
                    texts.append(
                        FormatUILogs.format_master_log_event(cid, payload)
                    )
                    continue

                # ---------------- Plain string ----------------
                if isinstance(payload, str):
                    header = "🧾 MASTER" if cid == 0 else f"🧾 COPY #{cid}"
                    texts.append(f"{header}\n{payload}")
                    continue

                # ---------------- dict fallback ----------------
                if isinstance(payload, dict):
                    header = "🧾 MASTER" if cid == 0 else f"🧾 COPY #{cid}"
                    lines = [header]
                    for k, v in payload.items():
                        lines.append(f"{k}: {v}")
                    texts.append("\n".join(lines))
                    continue

                # ---------------- Unknown ----------------
                texts.append(
                    f"🧾 COPY #{cid}\n<unsupported log payload>"
                )

            return texts
        
        finally:
            log_events.clear()
      


class TelegramNotifier:
    """
    ДЕТЕРМИНИРОВАННЫЙ TG notifier

    • никаких очередей
    • никаких фоновых циклов
    • один вызов = одно сообщение
    • retry / rate-limit внутри
    """

    def __init__(
        self,
        bot: Bot,
        logger: "UnifiedLogger",
        chat_id: int,
        stop_bot: Callable[[], bool],
    ):
        self.bot = bot
        self.logger = logger
        self.stop_bot = stop_bot
        self.chat_id = chat_id

    # ==========================================================
    # PUBLIC API
    # ==========================================================

    async def send(
        self,
        text: str,
    ) -> Optional[int]:
        """
        Отправка одного сообщения.
        Возвращает message_id или None.
        """
        return await self._send_message(self.chat_id, text)

    async def send_block(
        self,
        texts: Iterable[str],
        separator: str = "\n\n",
    ) -> Optional[int]:
        """
        Отправка нескольких сообщений ОДНИМ блоком.
        """
        block = separator.join(t for t in texts if t)
        if not block:
            return None
        return await self._send_message(self.chat_id, block)

    # ==========================================================
    # INTERNAL
    # ==========================================================

    async def _send_message(
        self,
        chat_id: int,
        text: str,
    ) -> Optional[int]:

        while not self.stop_bot():
            try:
                msg = await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=None,
                )
                return msg.message_id

            except TelegramRetryAfter as e:
                wait = int(getattr(e, "retry_after", 5))
                self.logger.warning(
                    f"[TG][{chat_id}] rate limit → wait {wait}s"
                )
                await asyncio.sleep(wait)

            except TelegramNetworkError as e:
                wait = random.uniform(1.0, 3.0)
                self.logger.warning(
                    f"[TG][{chat_id}] network error → retry in {wait:.1f}s: {e}"
                )
                await asyncio.sleep(wait)

            except TelegramForbiddenError:
                self.logger.info(
                    f"[TG][{chat_id}] bot blocked by user"
                )
                return None

            except TelegramAPIError as e:
                self.logger.error(
                    f"[TG][{chat_id}] API error: {e}"
                )
                return None

            except Exception as e:
                self.logger.exception(
                    f"[TG][{chat_id}] unexpected error",
                    e,
                )
                return None

        return None
