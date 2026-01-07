# MASTER.stream_.py

from __future__ import annotations

import asyncio
import aiohttp
import json
import time
import hmac
import hashlib
import random
from typing import *
from pprint import pprint

from a_config import BLACK_SYMBOLS

from c_utils import now, Utils
from .state_ import (
    SignalEvent,
    normalize_symbol,
    side_from_order_side,
    side_from_position_type,
)

if TYPE_CHECKING:
    from c_log import UnifiedLogger
    from .state_ import SignalCache


IS_SHOW_SIGNAL = False


class MasterSignalStream:
    """
    STREAM LEVEL ONLY.
    НИКАКОЙ high-level логики.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        signal_cache: "SignalCache",
        *,
        logger: "UnifiedLogger",
        stop_flag: Callable,
        proxy_url: Optional[str] = None,
        ws_url: str = "wss://contract.mexc.com/edge",
        # ws_url: str = "wss://contract.mexc.com/ws",
        quota_asset: str = "USDT",
    ):
        self.api_key = api_key
        self.api_secret = api_secret

        self.cache = signal_cache
        self.logger = logger

        self.proxy_url = None if not proxy_url or proxy_url == "0" else proxy_url
        self.ws_url = ws_url
        self.quota_asset = quota_asset.upper()

        self.session: Optional[aiohttp.ClientSession] = None
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None

        self.ready = False
        self.is_connected = False
        self.ping_interval = 12
        self.stop_flag = stop_flag

        self._external_stop = False
        self._ping_task: Optional[asyncio.Task] = None

        self.black_symbols = {x.upper() for x in BLACK_SYMBOLS if x and x.strip()}

    # --------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------

    def stop(self):
        self._external_stop = True
        self.ready = False
        self.logger.info("MasterSignalStream: stop requested")

    # --------------------------------------------------
    def _signature(self, ts_ms: int) -> str:
        payload = f"{self.api_key}{ts_ms}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    # --------------------------------------------------
    # CONNECTION
    # --------------------------------------------------
    async def _connect(self) -> bool:
        try:
            if not self.session or self.session.closed:
                self.session = aiohttp.ClientSession()

            self.websocket = await self.session.ws_connect(
                self.ws_url,
                proxy=self.proxy_url,
                autoping=False,
            )

            self.is_connected = True
            self.logger.info("MasterSignalStream: WS connected")
            return True

        except Exception as e:
            self.logger.warning(f"WS connect failed: {e}")
            return False
            
    async def _login(self) -> bool:
        ts = int(time.time() * 1000) - 1000  # 🔥 важно
        await self.websocket.send_json({
            "method": "login",
            "param": {
                "apiKey": self.api_key,
                "reqTime": ts,
                "signature": self._signature(ts),
            }
        })

        msg = await asyncio.wait_for(self.websocket.receive(), timeout=10)
        data = json.loads(msg.data)

        if data.get("channel") == "rs.login" and data.get("data") == "success":
            self.logger.info("MasterSignalStream: WS login success")
            return True

        self.logger.error(f"WS login failed: {data}")
        return False

    async def _ping_loop(self):
        while not self._external_stop and self.is_connected and not self.stop_flag():
            await asyncio.sleep(self.ping_interval)
            try:
                await self.websocket.send_json({"method": "ping"})
            except Exception:
                return

    async def _disconnect(self):
        self.is_connected = False
        self.ready = False

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()

        try:
            if self.websocket:
                await self.websocket.close()
            if self.session:
                await self.session.close()
        except Exception:
            pass

        self.websocket = None
        self.session = None
        self.logger.info("MasterSignalStream: WS disconnected")

    # --------------------------------------------------
    # RAW EVENT EMIT
    # --------------------------------------------------
    async def _emit(self, symbol, pos_side, etype, raw):
        if symbol and symbol.upper() in self.black_symbols:
            return
             
        ev = SignalEvent(
            symbol=symbol,
            pos_side=pos_side,
            event_type=etype,
            ts=now(),
            raw=raw,
        )
        # pprint(ev)
        if IS_SHOW_SIGNAL:
            self.logger.debug(f"RAW SIGNAL: {ev}")
        await self.cache.push_event(ev)

    async def _handle_order(self, data: dict):
        symbol = normalize_symbol(data.get("symbol"), self.quota_asset)
        side_code = int(data.get("side", 0))
        side = side_from_order_side(side_code)

        state = int(data.get("state", 0))
        order_type = int(data.get("orderType", 0))

        # print(
        #     "[ORDER]",
        #     "orderType=", order_type,
        #     "side=", side_code,
        #     "state=", state,
        #     "raw=", data
        # )

        # ---------------- TERMINAL ----------------
        if state in (4, 5):
            await self._emit(
                symbol,
                side,
                "order_cancelled" if state == 4 else "order_invalid",
                data,
            )
            return

        # ---------------- EXECUTION ----------------
        if state == 3:
            if order_type == 1:
                await self._emit(symbol, side, "limit_filled", data)
            elif order_type == 5:
                await self._emit(symbol, side, "market_filled", data)
            else:
                await self._emit(symbol, side, "trigger_filled", data)
            return

        # ---------------- INTENT ----------------
        if order_type == 1 and state == 2:
            await self._emit(symbol, side, "limit_placed", data)
            return

    async def _handle_order_deal(self, data):
        symbol = normalize_symbol(data.get("symbol"), self.quota_asset)
        side = side_from_order_side(int(data.get("side", 0)))
        await self._emit(symbol, side, "deal", data)

    async def _handle_position(self, data):
        symbol = normalize_symbol(data.get("symbol"), self.quota_asset)
        side = side_from_position_type(int(data.get("positionType", 0)))

        hold_vol = float(data.get("holdVol", 0))
        state = int(data.get("state", 0))

        etype = (
            "position_opened"
            if (state in (1, 2) and hold_vol > 0)
            else "position_closed"
        )
        await self._emit(symbol, side, etype, data)

    async def _handle_plan_order(self, data):
        symbol = normalize_symbol(data.get("symbol"), self.quota_asset)
        side = side_from_order_side(int(data.get("side", 0)))
        state = int(data.get("state", 0))

        etype = (
            "plan_order"
            if state == 1
            else "plan_executed"
            if state == 3
            else "plan_cancelled"
        )
        await self._emit(symbol, side, etype, data)

    async def _handle_stop_order(self, data):
        # pprint("_handle_stop_order")
        symbol = normalize_symbol(data.get("symbol"), self.quota_asset)
        side = side_from_order_side(int(data.get("side", 0)))

        await self._emit(
            symbol,
            side,
            "oco_attached",   # ⬅️ вместо stop_attached
            {
                "tp": Utils.safe_float(data.get("takeProfitPrice")),
                "sl": Utils.safe_float(data.get("stopLossPrice")),
            },
        )

    # --------------------------------------------------
    # MESSAGE LOOP
    # --------------------------------------------------

    async def _handle_messages(self):
        while not self._external_stop and not self.stop_flag():
            try:
                msg = await asyncio.wait_for(self.websocket.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            data = json.loads(msg.data)
            channel = data.get("channel")
            payload = data.get("data", {})

            if channel == "push.personal.order":
                await self._handle_order(payload)
            elif channel == "push.personal.order.deal":
                await self._handle_order_deal(payload)
            elif channel == "push.personal.position":
                await self._handle_position(payload)
            elif channel == "push.personal.plan.order":
                await self._handle_plan_order(payload)
            elif channel == "push.personal.stop.order":
                await self._handle_stop_order(payload)

    # --------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------
    async def start(self):
        self._external_stop = False

        while not self._external_stop and not self.stop_flag():
            if not await self._connect():
                await asyncio.sleep(1)
                continue

            if not await self._login():
                await self._disconnect()
                await asyncio.sleep(1)
                continue

            self.ready = True
            self._ping_task = asyncio.create_task(self._ping_loop())

            await self._handle_messages()
            await self._disconnect()

            if not self._external_stop:
                await asyncio.sleep(random.uniform(0.8, 1.5))
