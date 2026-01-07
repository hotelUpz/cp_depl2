# MASTER.state_.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections import deque
from typing import *
from b_context import PosVarTemplate
from c_utils import Utils

PosSide = Literal["LONG", "SHORT"]


def normalize_symbol(raw_symbol: str, quota_asset: str = "USDT") -> str:
    if not raw_symbol:
        return ""
    qa = quota_asset.upper()
    s = raw_symbol.upper().replace("-", "").replace("_", "").replace(" ", "")
    return s.replace(qa, f"_{qa}")


def side_from_order_side(code: int) -> Optional[PosSide]:
    return (
        "LONG" if code in (1, 4)
        else "SHORT" if code in (2, 3)
        else None
    )


def side_from_position_type(code: int) -> Optional[PosSide]:
    return "LONG" if code == 1 else "SHORT" if code == 2 else None


SignalEventType = Literal[
    # --- execution ---
    "open_market", "open_limit",
    "close_market", "close_limit",

    # --- pending ---
    "open_pending", "close_pending",

    # --- triggers ---
    "plan_order", "plan_executed", "plan_cancelled",

    # --- position ---
    "position_opened", "position_closed",

    # --- misc ---
    "order_cancelled", "order_invalid",
    "deal",

    # 🔑 NEW (НЕ сигнал, а STATE)
    "oco_attached",      # <-- ВАЖНО
]



@dataclass
class SignalEvent:
    symbol: str
    pos_side: Optional[PosSide]
    event_type: SignalEventType
    ts: int
    raw: Dict[str, Any] = field(default_factory=dict)


class SignalCache:
    """
    RAW-only cache.
    Единственная обязанность:
        • принять SignalEvent
        • разбудить consumer (MasterPayload)
    """

    def __init__(self):
        self._events: Deque[SignalEvent] = deque()
        self._last_raw: Dict[Tuple[str, PosSide], Dict[str, Any]] = {}

        self._lock = asyncio.Lock()
        self._event_notify = asyncio.Event()

    async def push_event(self, ev: SignalEvent):
        async with self._lock:
            self._events.append(ev)
            if ev.pos_side:
                self._last_raw[(ev.symbol, ev.pos_side)] = ev.raw
            self._event_notify.set()   # ← КРИТИЧЕСКИ ВАЖНО

    async def pop_events(self) -> list[SignalEvent]:
        async with self._lock:
            out = list(self._events)
            self._events.clear()
            self._event_notify.clear()
            return out

    def get_last_raw(self, symbol: str, side: PosSide) -> Optional[Dict[str, Any]]:
        return self._last_raw.get((symbol, side))
    

class PosVarSetup():
    @staticmethod
    def pos_vars_root_template():
        pv = PosVarTemplate.base_template()
        pv.update({
            "_pending_buy": False,
            "_last_exec_source": None,
            "_attached_tp": None,
            "_attached_sl": None,
        })
        return pv

    @staticmethod
    def set_pos_defaults(
        position_vars: Dict[str, Dict[str, Any]],
        symbol: str,
        pos_side: str,
        instruments_data: Optional[List[Dict[str, Any]]] = None,
        reset_flag: bool = False,
    ) -> Dict[str, Dict[str, Any]] | bool:
        """
        Безопасная инициализация структуры данных контроля позиций.
        """

        if symbol not in position_vars:
            position_vars[symbol] = {}

        # -------- SPEC --------
        specs = {}
        if instruments_data and "spec" not in position_vars[symbol]:
            try:
                specs = Utils.parse_precision(
                    symbols_info=instruments_data,
                    symbol=symbol,
                )
                if not specs or not all(v is not None for v in specs.values()):
                    print(
                        f"Нет нужных инструментов для монеты {symbol}. "
                        f"Возможно токен недоступен для торговли."
                    )
            except Exception as e:
                print(f"⚠️ [ERROR] при получении инструментов для {symbol}: {e}")

            position_vars[symbol]["spec"] = specs

        # -------- SIDE INIT --------
        if pos_side not in position_vars[symbol] or reset_flag:
            position_vars[symbol][pos_side] = PosVarSetup.pos_vars_root_template()

        return position_vars
