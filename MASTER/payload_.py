# MASTER.payload_.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import *

from c_utils import Utils, now
from MASTER.state_ import PosVarSetup

if TYPE_CHECKING:
    from MASTER.state_ import SignalCache, SignalEvent
    from b_context import MainContext
    from c_log import UnifiedLogger


# =====================================================================
# HL PROTOCOL
# =====================================================================
HL_EVENT = Literal["buy", "sell", "canceled"]
METHOD = Literal["market", "limit", "trigger"]


# =====================================================================
# MASTER EVENT
# =====================================================================
@dataclass
class MasterEvent:
    event: HL_EVENT
    method: METHOD
    symbol: str
    pos_side: str
    closed: bool
    payload: Dict[str, Any]
    sig_type: Literal["copy", "manual"]
    ts: int = field(default_factory=now)


# ==================================================
def _extract_exchange_ts(ev_raw: "SignalEvent") -> Optional[int]:
    if not ev_raw:
        return None
    raw = ev_raw.raw or {}
    for key in ("updateTime", "createTime", "timestamp", "time", "ts"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and val > 0:
            if val < 10_000_000_000:
                val *= 1000
            return int(val)
    return None


# =====================================================================
# MASTER PAYLOAD (EXECUTION-ONLY + INTENT OCO)
# =====================================================================
class MasterPayload:
    """
    EXECUTION-ONLY PAYLOAD

    ИСТИНЫ:
    • Источник сигналов — ТОЛЬКО execution reports
    • LIMIT placed = intent
    • LIMIT filled = execution
    • OCO (TP/SL) — STATE, а не сигнал
    • WS / snapshot НЕ используются для OCO
    """

    def __init__(
        self,
        cache: "SignalCache",
        mc: "MainContext",
        logger: "UnifiedLogger",
        stop_flag: Callable[[], bool],
    ):
        self.cache = cache
        self.mc = mc
        self.logger = logger
        self.stop_flag = stop_flag

        self._pending: list[MasterEvent] = []
        self._stop = False

        self.out_queue = asyncio.Queue(maxsize=1000)

        # anti-double fire для limit
        self._limit_intents: set[str] = set()

    # ==================================================
    def stop(self):
        self._stop = True
        Utils.clear_runtime_positions(pos_vars_root=self.mc.pos_vars_root)
        self.logger.info("MasterPayload: stop requested")

    # ==================================================
    async def run(self):
        self.logger.info("MasterPayload READY")

        while not self._stop and not self.stop_flag():
            await self.cache._event_notify.wait()
            events = await self.cache.pop_events()

            for ev in events:
                self._route(ev)

            for mev in self._pending:
                await self.out_queue.put(mev)

            self._pending.clear()

        self.logger.info("MasterPayload STOPPED")

    # ==================================================
    def _ensure_pv(self, symbol: str, pos_side: str) -> dict:
        PosVarSetup.set_pos_defaults(
            self.mc.pos_vars_root,
            symbol,
            pos_side,
            instruments_data=self.mc.instruments_data,
        )
        return self.mc.pos_vars_root[symbol][pos_side]

    # ==================================================
    def _route(self, ev: "SignalEvent"):
        et = ev.event_type
        symbol, pos_side = ev.symbol, ev.pos_side
        if not symbol or not pos_side:
            return

        raw = ev.raw or {}

        # ==================================================
        # OCO STATE (НЕ СИГНАЛ)
        # ==================================================
        if et == "oco_attached":
            pv = self._ensure_pv(symbol, pos_side)

            tp = Utils.safe_float(raw.get("tp"))
            sl = Utils.safe_float(raw.get("sl"))

            if tp is not None:
                pv["_attached_tp"] = tp
            if sl is not None:
                pv["_attached_sl"] = sl

            return

        # ==================================================
        # MARKET FILLED
        # ==================================================
        if et == "market_filled":
            reduce_only = bool(raw.get("reduceOnly"))
            is_close = reduce_only

            emit_side = pos_side
            if is_close:
                emit_side = {"LONG": "SHORT", "SHORT": "LONG"}[pos_side]

            payload = self._base_payload(raw)

            self._inject_oco_from_intent(payload, symbol, emit_side)

            self._emit(
                event="sell" if is_close else "buy",
                method="market",
                symbol=symbol,
                pos_side=emit_side,
                closed=is_close,
                payload=payload,
                ev_raw=ev,
            )
            return

        # ==================================================
        # LIMIT FILLED
        # ==================================================
        if et == "limit_filled":
            oid = raw.get("orderId")

            # intent → не сигнал
            if oid in self._limit_intents:
                self._limit_intents.discard(oid)
                return

            payload = self._base_payload(raw)
            self._inject_oco_from_intent(payload, symbol, pos_side)

            self._emit(
                event="buy",
                method="limit",
                symbol=symbol,
                pos_side=pos_side,
                closed=False,
                payload=payload,
                ev_raw=ev,
            )
            return

        # ==================================================
        # LIMIT PLACED (INTENT)
        # ==================================================
        if et == "limit_placed":
            oid = raw.get("orderId")
            if oid:
                self._limit_intents.add(oid)

            self._emit(
                event="buy",
                method="limit",
                symbol=symbol,
                pos_side=pos_side,
                closed=False,
                payload=self._base_payload(raw),
                ev_raw=ev,
            )
            return

        # ==================================================
        # TRIGGER FILLED
        # ==================================================
        if et == "trigger_filled":
            reduce_only = bool(raw.get("reduceOnly"))
            is_sell = raw.get("side") not in (1, 3)

            emit_side = pos_side
            if reduce_only:
                emit_side = {"LONG": "SHORT", "SHORT": "LONG"}[pos_side]

            payload = self._base_payload(raw)
            self._inject_oco_from_intent(payload, symbol, emit_side)

            self._emit(
                event="sell" if is_sell else "buy",
                method="trigger",
                symbol=symbol,
                pos_side=emit_side,
                closed=reduce_only,
                payload=payload,
                ev_raw=ev,
            )
            return

        # ==================================================
        # CANCEL
        # ==================================================
        if et in ("order_cancelled", "order_invalid"):
            oid = raw.get("orderId")
            if oid:
                self._limit_intents.discard(oid)

            self._emit(
                event="canceled",
                method="limit",
                symbol=symbol,
                pos_side=pos_side,
                closed=False,
                payload={"order_id": oid},
                ev_raw=ev,
            )
            return

    # ==================================================
    def _inject_oco_from_intent(self, payload: dict, symbol: str, pos_side: str):
        """
        🔑 Единственный источник TP/SL — локальный INTENT (pos_vars).
        Подмешивается ОДИН РАЗ.
        """

        pv = self.mc.pos_vars_root.get(symbol, {}).get(pos_side)
        if not pv:
            return

        tp = pv.get("_attached_tp")
        sl = pv.get("_attached_sl")

        if tp is not None:
            payload["tp_price"] = tp
        if sl is not None:
            payload["sl_price"] = sl

        # consume once
        pv["_attached_tp"] = None
        pv["_attached_sl"] = None

    # ==================================================
    @staticmethod
    def _base_payload(raw: dict) -> dict:
        return {
            "order_id": raw.get("orderId"),
            "qty": Utils.safe_float(raw.get("vol")),
            "price": Utils.safe_float(
                raw.get("price")
                or raw.get("dealAvgPrice")
                or raw.get("avgPrice")
            ),
            "leverage": raw.get("leverage"),
            "open_type": raw.get("openType"),
            "reduce_only": bool(raw.get("reduceOnly")),
        }

    # ==================================================
    def _emit(
        self,
        *,
        event: HL_EVENT,
        method: METHOD,
        symbol: str,
        pos_side: str,
        payload: Dict[str, Any],
        ev_raw: Optional["SignalEvent"],
        closed: bool = False,
        sig_type: Literal["copy", "manual"] = "copy",
    ):  
        ts = now()   
        exec_ts = _extract_exchange_ts(ev_raw=ev_raw)
        tech_ts = ev_raw.ts if ev_raw else now()
        if exec_ts and tech_ts: ts = min(exec_ts, tech_ts)

        payload = dict(payload)
        payload["exec_ts"] = exec_ts

        self._pending.append(
            MasterEvent(
                event=event,
                method=method,
                symbol=symbol,
                pos_side=pos_side,
                closed=closed,
                payload=payload,
                sig_type=sig_type,
                ts=ts,
            )
        )