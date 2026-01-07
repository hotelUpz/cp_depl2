# API.MX.client.py

import aiohttp
import time
from typing import *

from .mx_bypass.mexcTypes import (
    CreateOrderRequest,
    OpenType,
    OrderSide,
    OrderType,
    # PositionType,
    # PositionMode,
    ExecuteCycle,
    TriggerPriceType,
    TriggerType,
    TriggerOrderRequest
)
from .mx_bypass.api import MexcFuturesAPI, ApiResponse

if TYPE_CHECKING:
    from b_network import NetworkManager
    from c_log import UnifiedLogger


class OrderValidator:
    @staticmethod
    def validate_and_log(
        result: ApiResponse,
        debug_label: str,
        debug: bool = True,
    ) -> dict:
        """
        Универсальный валидатор ответа MEXC.

        Возвращает:
        {
            success: bool
            order_id: str | None
            order_ids: list[str] | None
            ts: int
            code: int | None
            reason: str | None
            raw: ApiResponse
            raw_data: Any
        }
        """

        ts = int(time.time() * 1000)

        if result is None:
            return {
                "success": False,
                "order_id": None,
                "order_ids": None,
                "ts": ts,
                "code": None,
                "reason": "Empty response from exchange",
                "raw": None,
                "raw_data": None,
            }

        raw_data = result.data
        code = getattr(result, "code", None)
        message = getattr(result, "message", None)

        # ----------------------------
        # SUCCESS CASE
        # ----------------------------
        if result.success and code == 0:
            order_id = None
            order_ids = None

            # create_order / trigger_order
            if hasattr(raw_data, "orderId"):
                order_id = raw_data.orderId

            # cancel_orders (list)
            elif isinstance(raw_data, list):
                order_ids = [
                    x.get("orderId")
                    for x in raw_data
                    if isinstance(x, dict) and "orderId" in x
                ]

            # fallback
            elif isinstance(raw_data, (int, str)):
                order_id = raw_data

            if debug:
                pass  # лог уже обернут через logger.wrap_foreign_methods

            return {
                "success": True,
                "order_id": order_id,
                "order_ids": order_ids,
                "ts": ts,
                "code": code,
                "reason": None,
                "raw": result,
                "raw_data": raw_data,
            }

        # ----------------------------
        # ERROR CASE
        # ----------------------------
        reason = message or "Unknown exchange error"

        return {
            "success": False,
            "order_id": None,
            "order_ids": None,
            "ts": ts,
            "code": code,
            "reason": reason,
            "raw": result,
            "raw_data": raw_data,
        }


# ----------------------------
class MexcClient:
    def __init__(
            self,
            connector: "NetworkManager",
            logger: "UnifiedLogger",
            api_key: str = None,
            api_secret: str = None,
            token: str = None,
            
        ):      
        self.session: Optional[aiohttp.ClientSession] = connector.session
        self.logger = logger

        self.api_key = api_key
        self.api_secret = api_secret        

        self.api = MexcFuturesAPI(token, testnet=False)
        # self._is_wrapped = False

    # POST
    async def make_order(
        self,
        symbol: str,
        contract: float,
        side: str,
        position_side: str,
        leverage: int,
        open_type: int,
        price: Optional[str] = None,
        stopLossPrice: Optional[str] = None,
        takeProfitPrice: Optional[str] = None,
        market_type: str = "MARKET",
        debug: bool = True,
    ) -> dict:

        # -------- market type
        if market_type == "MARKET":
            order_type = OrderType.MarketOrder
        elif market_type == "LIMIT":
            order_type = OrderType.PriceLimited
        else:
            return OrderValidator.validate_and_log(
                None, "MAKE_ORDER", debug
            )

        # -------- side
        if position_side.upper() == "LONG":
            order_side = OrderSide.OpenLong if side.upper() == "BUY" else OrderSide.CloseLong
        elif position_side.upper() == "SHORT":
            order_side = OrderSide.OpenShort if side.upper() == "BUY" else OrderSide.CloseShort
        else:
            return OrderValidator.validate_and_log(
                None, "MAKE_ORDER", debug
            )

        # -------- open type
        if open_type == 1:
            openType = OpenType.Isolated
        elif open_type == 2:
            openType = OpenType.Cross
        else:
            return OrderValidator.validate_and_log(
                None, "MAKE_ORDER", debug
            )

        if openType == OpenType.Isolated and not leverage:
            return OrderValidator.validate_and_log(
                None, "MAKE_ORDER", debug
            )

        # -------- API call
        result = await self.api.create_order(
            order_request=CreateOrderRequest(
                symbol=symbol,
                side=order_side,
                vol=contract,
                leverage=leverage,
                openType=openType,
                type=order_type,
                price=price,
                stopLossPrice=stopLossPrice,
                takeProfitPrice=takeProfitPrice,
            ),
            session=self.session,
        )

        return OrderValidator.validate_and_log(
            result=result,
            debug_label="MAKE_ORDER",
            debug=debug,
        )
        
    async def make_trigger_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],          # торговая сторона
        position_side: Literal["LONG", "SHORT"],
        contract: float,
        trigger_price: str,
        leverage: int,
        open_type: int,
        order_type: int = 2,                   # 1=LIMIT, 2=MARKET
        debug: bool = False,
    ) -> dict:
        """
        Универсальный trigger-ордер (open / close).

        BUY  -> trigger on price <= trigger_price
        SELL -> trigger on price >= trigger_price
        """

        # --------------------------------------------------
        # ORDER SIDE (open / close)
        # --------------------------------------------------
        if position_side == "LONG":
            order_side = (
                OrderSide.OpenLong if side == "BUY"
                else OrderSide.CloseLong
            )
        elif position_side == "SHORT":
            order_side = (
                OrderSide.OpenShort if side == "BUY"
                else OrderSide.CloseShort
            )
        else:
            return OrderValidator.validate_and_log(
                None, "TRIGGER_ORDER", debug
            )

        # --------------------------------------------------
        # TRIGGER TYPE (ключевая часть)
        # --------------------------------------------------
        trigger_type = (
            TriggerType.LessThanOrEqual
            # if (side == "BUY" and position_side == "LONG") or (side == "SELL" and position_side == "SHORT")
            if order_side in (OrderSide.OpenLong, OrderSide.CloseShort)
            else TriggerType.GreaterThanOrEqual
        )

        # --------------------------------------------------
        # OPEN TYPE
        # --------------------------------------------------
        if open_type == 1:
            openType = OpenType.Isolated
        elif open_type == 2:
            openType = OpenType.Cross
        else:
            return OrderValidator.validate_and_log(
                None, "TRIGGER_ORDER", debug
            )

        if openType == OpenType.Isolated and not leverage:
            return OrderValidator.validate_and_log(
                None, "TRIGGER_ORDER", debug
            )

        # --------------------------------------------------
        # EXEC ORDER TYPE
        # --------------------------------------------------
        exec_order_type = (
            OrderType.PriceLimited
            if order_type == 1
            else OrderType.MarketOrder
        )

        # --------------------------------------------------
        # API CALL
        # --------------------------------------------------
        trigger_request = TriggerOrderRequest(
            symbol=symbol,
            side=order_side,
            vol=contract,
            leverage=leverage,
            openType=openType,
            orderType=exec_order_type,
            executeCycle=ExecuteCycle.UntilCanceled,
            trend=TriggerPriceType.LatestPrice,
            triggerPrice=trigger_price,
            triggerType=trigger_type,
        )

        result = await self.api.create_trigger_order(
            trigger_order_request=trigger_request,
            session=self.session,
        )

        return OrderValidator.validate_and_log(
            result=result,
            debug_label="TRIGGER_ORDER",
            debug=debug,
        )
    
    # DELETE
    async def cancel_trigger_order(
        self,
        order_id_list: List[str],
        symbol: str,
        debug: bool = True,
    ) -> dict:
        """
        Отмена trigger / plan ордеров
        """

        if not order_id_list:
            return OrderValidator.validate_and_log(
                None, "CANCEL_TRIGGER", debug
            )

        order_list = [{"orderId": oid, "symbol": symbol} for oid in order_id_list]

        result = await self.api.cancel_trigger_orders(
            orders=order_list,
            session=self.session,
        )

        return OrderValidator.validate_and_log(
            result=result,
            debug_label="CANCEL_TRIGGER",
            debug=debug,
        )
    
    async def cancel_limit_orders(
        self,
        order_id_list: List[str],
        debug: bool = True,
    ) -> dict:
        """
        Отмена обычных (не trigger) лимитных ордеров по orderId.
        """
        if not order_id_list:
            return {
                "success": False,
                "order_ids": [],
                "reason": "order_id_list is empty",
                "raw": None,
                "ts": int(time.time() * 1000),
            }

        result = await self.api.cancel_orders(
            order_ids=order_id_list,
            session=self.session
        )

        return OrderValidator.validate_and_log(
            result=result,
            debug_label="CANCEL_LIMIT",
            debug=debug
        )
    
    async def cancel_all_orders(
        self,
        symbol: str,
        debug: bool = True
    ) -> dict:
        """
        Отмена ВСЕХ ордеров (limit + trigger) по символу.
        """
        result = await self.api.cancel_all_orders(
            symbol=symbol,
            session=self.session
        )

        return OrderValidator.validate_and_log(
            result=result,
            debug_label="CANCEL_ALL",
            debug=debug
        )
        
    async def cancel_orders_bulk(
        self,
        *,
        limit_order_ids: Optional[List[str]] = None,
        trigger_order_ids: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        debug: bool = True,
    ) -> dict:
        """
        Универсальная массовая отмена ордеров по спискам ID.

        • limit_order_ids  → обычные лимитные ордера
        • trigger_order_ids → trigger / plan ордера (TP / SL)
        • symbol обязателен, если есть trigger_order_ids
        """

        results = {
            "limit": None,
            "trigger": None,
            "errors": [],
        }

        # --------------------------------------------------
        # LIMIT ORDERS
        # --------------------------------------------------
        if limit_order_ids:
            try:
                res = await self.cancel_limit_orders(
                    order_id_list=limit_order_ids,
                    debug=debug,
                )
                results["limit"] = res
                if not res.get("success"):
                    results["errors"].append(("limit", res.get("reason")))
            except Exception as e:
                results["errors"].append(("limit", str(e)))

        # --------------------------------------------------
        # TRIGGER ORDERS
        # --------------------------------------------------
        if trigger_order_ids:
            if not symbol:
                results["errors"].append(
                    ("trigger", "symbol is required for trigger order cancel")
                )
            else:
                try:
                    res = await self.cancel_trigger_order(
                        order_id_list=trigger_order_ids,
                        symbol=symbol,
                        debug=debug,
                    )
                    results["trigger"] = res
                    if not res.get("success"):
                        results["errors"].append(("trigger", res.get("reason")))
                except Exception as e:
                    results["errors"].append(("trigger", str(e)))

        success = not results["errors"]

        return {
            "success": success,
            "details": results,
        }

    # # GET
    async def get_realized_pnl_batch(
        self,
        *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[Tuple[str, int], dict]:
        """
        Batch PnL fetch.

        Возвращает:
        {
            (symbol, direction): {
                "pnl_usdt": float,
                "pnl_pct": float
            }
        }
        """

        async def _fetch():
            resp = await self.api.get_historical_orders_report(
                session=self.session
            )
            if resp and getattr(resp, "success", False) and resp.data:
                return resp.data
            return []

        try:
            rows = await _fetch()
        except Exception:
            try:
                rows = await _fetch()
            except Exception:
                return {}

        if not rows:
            return {}

        acc: Dict[Tuple[str, int], dict] = {}

        for row in rows:
            try:
                ts = int(row.get("updateTime", 0))
                if start_time and ts < start_time:
                    continue
                if end_time and ts > end_time:
                    continue

                symbol = row.get("symbol")
                direction = row.get("positionType")  # 1 LONG / 2 SHORT
                if not symbol or not direction:
                    continue

                key = (symbol, direction)
                rec = acc.setdefault(key, {
                    "pnl_usdt": 0.0,
                    "pnl_pct": 0.0,
                    "matched": False,
                })

                rec["pnl_usdt"] += float(row.get("realised", 0.0))

                pr = row.get("profitRatio")
                if pr is not None:
                    rec["pnl_pct"] += float(pr) * 100

                rec["matched"] = True

            except Exception:
                continue

        # normalize
        out = {}
        for k, v in acc.items():
            if not v["matched"]:
                continue
            out[k] = {
                "pnl_usdt": round(v["pnl_usdt"], 6),
                "pnl_pct": round(v["pnl_pct"], 4),
            }

        return out
    
    async def get_realized_pnl(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        direction: Optional[int] = None,  # 1=LONG, 2=SHORT
    ) -> dict:
        """
        Считает реализованный PnL за период по символу.

        Возвращает:
            {
                "pnl_usdt": float | None,
                "pnl_pct": float | None
            }

        Делает 1 реконнект при сетевой ошибке.
        """

        async def _fetch():
            resp = await self.api.get_historical_orders_report(
                symbol=symbol,
                session=self.session
            )
            if resp and getattr(resp, "success", False) and resp.data:
                return resp.data
            return []

        # ---------- FETCH WITH ONE RETRY ----------
        try:
            rows = await _fetch()
        except Exception as e:
            self.logger.exception(
                f"[get_realized_pnl] fetch error, retrying once: {e}",
                is_print=True,
            )
            try:
                rows = await _fetch()
            except Exception as e2:
                self.logger.exception(
                    f"[get_realized_pnl] retry failed: {e2}",
                    is_print=True,
                )
                return {"pnl_usdt": None, "pnl_pct": None}

        if not rows:
            return {"pnl_usdt": None, "pnl_pct": None}

        # ---------- CALC ----------
        pnl_usdt = 0.0
        pnl_pct = 0.0
        matched = False

        for row in rows:
            try:
                ts = int(row.get("updateTime", 0))

                if start_time and ts < start_time:
                    continue
                if end_time and ts > end_time:
                    continue
                if direction and row.get("positionType") != direction:
                    continue

                pnl_usdt += float(row.get("realised", 0.0))

                pr = row.get("profitRatio")
                if pr is not None:
                    pnl_pct += float(pr) * 100

                matched = True

            except Exception:
                continue

        if not matched:
            return {"pnl_usdt": None, "pnl_pct": None}

        return {
            "pnl_usdt": round(pnl_usdt, 6),
            "pnl_pct": round(pnl_pct, 4),
        }
    
    # ----------------------------
    async def fetch_positions(self):
        # path = "/private/position/open_positions"
        response = await self.api.get_open_positions(symbol=None, session=self.session)
        if response and getattr(response, "success", False) and response.data:
            return response.data
        return []