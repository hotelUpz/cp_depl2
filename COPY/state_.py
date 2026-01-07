# COPY.state_.py

from __future__ import annotations

import asyncio
import copy
import random
import math
from typing import *
from dataclasses import dataclass

from a_config import SESSION_TTL, FALLBACK_LEVERAGE, FALLBACK_MARGIN_MODE
from b_context import COPY_RUNTIME_STATE

from c_utils import now, Utils
from b_network import NetworkManager
from API.MX.client import MexcClient

if TYPE_CHECKING:
    from b_context import MainContext
    from c_log import UnifiedLogger
    from MASTER.payload_ import MasterEvent


class CopyState:
    """
    Управляет ЖИЗНЕННЫМ ЦИКЛОМ copy-runtime.

    Инварианты:
    • для одного cid может существовать ТОЛЬКО ОДИН runtime-init
    • ensure_copy_state НИКОГДА ничего не создаёт
    • init / shutdown атомарны per-cid
    """

    def __init__(
        self,
        mc: "MainContext",
        logger: "UnifiedLogger",
        stop_flag: Callable[[], bool],
    ):
        self.mc = mc
        self.logger = logger
        self.stop_flag = stop_flag

        # 🔒 per-cid init locks (ключевой момент)
        self._init_locks: Dict[int, asyncio.Lock] = {}

    # ==================================================
    # PUBLIC API
    # ==================================================
    async def activate_copy(self, cid: int) -> bool:
        """
        Полная подготовка copy-аккаунта.
        Вызывается ТОЛЬКО из UI / команды.
        """

        lock = self._init_locks.setdefault(cid, asyncio.Lock())

        async with lock:
            rt = self.mc.copy_runtime_states.get(cid)
            if rt and rt.get("init_state") == "READY" and rt.get("network_ready"):
                self.logger.info(f"[CopyState:{cid}] already READY")
                return True

            self.logger.info(f"[CopyState:{cid}] activate_copy")

            rt = await self._init_copy_runtime(cid)
            if not rt:
                self.logger.error(f"[CopyState:{cid}] activation failed")
                return False

            self.mc.active_copy_ids.add(cid)

            self.logger.info(f"[CopyState:{cid}] READY")
            return True

    async def deactivate_copy(self, cid: int):
        """
        Полное выключение copy-аккаунта.
        """

        lock = self._init_locks.setdefault(cid, asyncio.Lock())

        async with lock:
            self.logger.info(f"[CopyState:{cid}] deactivate_copy")

            self.mc.active_copy_ids.discard(cid)
            await self.shutdown_runtime(cid)

    def ensure_copy_state(self, cid: int) -> Optional[Dict[str, Any]]:
        """
        БОЕВОЙ МЕТОД.

        • ничего не создаёт
        • ничего не чинит
        • возвращает только READY runtime
        """
        rt = self.mc.copy_runtime_states.get(cid)
        if not rt:
            return None

        if rt.get("init_state") != "READY":
            return None

        if not rt.get("network_ready"):
            return None

        return rt

    # ==================================================
    # INTERNAL INIT
    # ==================================================

    async def _init_copy_runtime(self, cid: int) -> Optional[Dict[str, Any]]:
        """
        Вся тяжёлая инициализация здесь.
        АТОМАРНО per-cid.
        """

        cfg = self.mc.copy_configs.get(cid)
        if not cfg:
            self.logger.error(f"[CopyState:{cid}] no config")
            return None

        if not cfg.get("enabled"):
            self.logger.warning(f"[CopyState:{cid}] not enabled")
            return None

        # ---- EXISTING RUNTIME CHECK ----
        rt = self.mc.copy_runtime_states.get(cid)
        if rt:
            if rt.get("init_state") == "READY":
                return rt
            if rt.get("init_state") == "INIT":
                # другой поток уже инициализирует
                self.logger.warning(f"[CopyState:{cid}] init already in progress")
                return None
            if rt.get("init_state") == "FAILED":
                # разрешаем повторную попытку
                self.mc.copy_runtime_states.pop(cid, None)

        # ---- CREATE RUNTIME SKELETON ----
        rt = copy.deepcopy(COPY_RUNTIME_STATE)
        rt["id"] = cid
        rt["init_state"] = "INIT"
        rt["network_ready"] = False

        self.mc.copy_runtime_states[cid] = rt

        ex = cfg.get("exchange", {})
        api_key = ex.get("api_key")
        api_secret = ex.get("api_secret")
        uid = ex.get("uid")
        proxy = ex.get("proxy")

        try:
            # ---- NETWORK ----
            connector = NetworkManager(
                logger=self.logger,
                proxy_url=proxy,
                stop_flag=self.stop_flag,
            )
            self.logger.wrap_object_methods(connector)

            # 🔑 ЯВНО СОЗДАЁМ СЕССИЮ
            await connector.initialize_session()

            # потом уже мониторинг
            connector.start_ping_loop()

            rt["connector"] = connector

            ok = await connector.wait_for_session()
            if not ok:
                raise RuntimeError("session timeout")

            # ---- CLIENT ----
            mc_client = MexcClient(
                connector=connector,
                logger=self.logger,
                api_key=api_key,
                api_secret=api_secret,
                token=uid,
            )
            self.logger.wrap_object_methods(mc_client)

            rt["mc_client"] = mc_client
            rt["network_ready"] = True
            rt["init_state"] = "READY"

            return rt

        except Exception as e:
            self.logger.exception(f"[CopyState:{cid}] init failed", e)
            rt["init_state"] = "FAILED"

            # аккуратно подчистим за собой
            await self.shutdown_runtime(cid)
            return None

    # ==================================================
    # SHUTDOWN
    # ==================================================
    async def shutdown_runtime(self, cid: int):
        """
        Полное уничтожение runtime + network.
        Вызывается ТОЛЬКО под init-lock.
        """

        rt = self.mc.copy_runtime_states.pop(cid, None)
        if not rt:
            return

        self.logger.info(f"[CopyState:{cid}] shutdown_runtime")

        conn = rt.get("connector")
        if conn:
            try:
                await conn.shutdown_session()
            except Exception:
                self.logger.exception(
                    f"[CopyState:{cid}] shutdown_session failed"
                )

        self.logger.info(f"[CopyState:{cid}] runtime destroyed")


@dataclass
class CopyOrderIntent:
    # --- required ---
    symbol: str
    side: str                 # BUY / SELL
    position_side: str        # LONG / SHORT
    contracts: float
    method: Literal["MARKET", "LIMIT", "TRIGGER"]

    # --- optional ---
    leverage: Optional[int] = None
    open_type: Optional[int] = None

    price: Optional[str] = None
    trigger_price: Optional[str] = None
    sl_price: Optional[str] = None
    tp_price: Optional[str] = None

    delay_ms: int = 0


class CopyOrderIntentFactory:
    """
    Единственная точка кастомизации ИНИЦИИРУЮЩИХ ордеров.
    CLOSE здесь не существует.
    """

    def __init__(self, mc: "MainContext"):
        self.mc = mc

    # --------------------------------------------------
    def _log_drop(self, cid, mev, reason: str, **extra):
        parts = [
            f"{mev.symbol} {mev.pos_side}",
            f"INTENT DROP",
            f"reason={reason}",
            f"event={mev.event}",
            f"method={mev.method}",
        ]

        if extra:
            extras = ", ".join(f"{k}={v}" for k, v in extra.items())
            parts.append(extras)

        self.mc.log_events.append(
            (cid, " :: ".join(parts))
            # (cid, " :: ".join(parts), now())
        )

    # --------------------------------------------------
    def _fmt_price(self, value, precision):
        if value is None:
            return None
        raw = Utils.safe_float(value)
        if raw is None:
            return None
        if precision is not None:
            raw = round(raw, precision)
        return Utils.to_human_digit(raw)

    # --------------------------------------------------
    @staticmethod
    def _clamp_by_max_margin(
        *,
        contracts: float,
        max_margin: float,
        price: Optional[float],
        leverage: int,
        coef: float,
        rnd: float,
        spec: dict,
    ) -> float:

        if not isinstance(contracts, (int, float)) or not math.isfinite(contracts):
            return 0.0
        if not price or price <= 0 or not leverage or leverage <= 0:
            return contracts
        
        if not spec:
            return contracts
        
        contract_size = spec.get("contract_size")
        vol_unit = spec.get("vol_unit")
        precision = spec.get("contract_precision")

        if not contract_size or not vol_unit or precision is None:
            return contracts

        margin = (contracts * contract_size * price) / leverage

        # --------------------------------------------------
        # 1️⃣ COEF
        # --------------------------------------------------            
        if coef and coef not in (0, 1):
            margin *= abs(coef)

        # --------------------------------------------------
        # 2️⃣ RANDOM SIZE
        # --------------------------------------------------
        
        if rnd and rnd not in (0, 100):
            margin *= abs(rnd / 100)

        if margin and max_margin and margin >= max_margin:
            margin = abs(float(max_margin))

        elif not margin:
            return 0.0 

        # считаем объём в базовой валюте
        base_qty = (margin * leverage) / price
        # переводим в контракты
        contracts = base_qty / contract_size

        # contracts = round(contracts / vol_unit) * vol_unit
        contracts = math.floor(contracts / vol_unit) * vol_unit
        contracts = round(contracts, precision)


        if not math.isfinite(contracts) or contracts <= 0:
            return 0.0

        return contracts

    # --------------------------------------------------
    def build(
        self,
        cfg: Dict,
        mev: "MasterEvent",
        copy_pv: Dict,
        spec: Dict
    ) -> Optional[CopyOrderIntent]:

        payload = mev.payload or {}
        cid = cfg.get("cid", "?")

        # --------------------------------------------------
        # 3️⃣ LEVERAGE (SAFE)
        # --------------------------------------------------
        if not mev.closed:
            leverage = (
                cfg.get("leverage")
                or payload.get("leverage")
                or copy_pv.get("leverage")
                or FALLBACK_LEVERAGE
            )
        else:
            leverage = (
                copy_pv.get("leverage")
                or payload.get("leverage")
                or cfg.get("leverage")
                or FALLBACK_LEVERAGE
            )

        try:
            leverage = int(leverage)
        except (TypeError, ValueError):
            # leverage = FALLBACK_LEVERAGE
            return

        max_lev = spec.get("max_leverage")
        if max_lev:
            leverage = min(leverage, int(max_lev))

        if not mev.closed:
            open_type = (
                cfg.get("margin_mode")
                or payload.get("open_type")
                or copy_pv.get("margin_mode")
                or FALLBACK_MARGIN_MODE
            )
        else:
            open_type = (
                copy_pv.get("margin_mode")
                or payload.get("open_type")
                or cfg.get("margin_mode")
                or FALLBACK_MARGIN_MODE
            )

        try:
            open_type = int(open_type)
        except (TypeError, ValueError):
            # open_type = FALLBACK_MARGIN_MODE
            return

        sl_price = tp_price = price = trigger_price = None

        max_margin = Utils.safe_float(cfg.get("max_position_size"))

        coef = Utils.safe_float(cfg.get("coef", 1.0)) or 1.0
        lo, hi = Utils.safe_float(cfg.get("random_size_pct", [0.0, 0.0])[0]), Utils.safe_float(cfg.get("random_size_pct", [0.0, 0.0])[1])
        rnd = 100
        if lo or hi and hi > lo:
            rnd = random.uniform(lo, hi)

        qty = None
        payload_qty = Utils.safe_float(payload.get("qty"))
        copy_pv_qty = Utils.safe_float(copy_pv.get("qty"))
        changing_qty_flag = (coef not in (0, 1, None)) or lo or hi or max_margin

        # --------------------------------------------------
        # 6️⃣ DELAY
        # --------------------------------------------------        
        delay_ms = 0

        if mev.sig_type != "manual":
            delay_cfg = cfg.get("delay_ms", [0, 0])

            if isinstance(delay_cfg, (list, tuple)) and len(delay_cfg) == 2:
                lo, hi = Utils.safe_float(delay_cfg[0]), Utils.safe_float(delay_cfg[1])
                lo, hi = abs(lo), abs(hi)
                if hi > lo:
                    delay_ms = int(random.uniform(lo, hi))
            elif isinstance(delay_cfg, (int, float)):
                delay_ms = int(delay_cfg)

        # --------------------------------------------------
        # 6️⃣ PROCE
        # --------------------------------------------------
        price_precision = spec.get("price_precision") if spec else None
        price = self._fmt_price(payload.get("price"), price_precision)

        # ==================================================
        # CLOSE
        # ==================================================
        if mev.closed:
            qty = payload_qty if not changing_qty_flag else copy_pv_qty
            if not qty or qty <= 0:
                self._log_drop(cid, mev, "CLOSE_QTY_INVALID", qty=qty)
                return None

        # ==================================================
        # OPEN / MODIFY
        # ==================================================
        else:    
            qty = payload_qty        
            if not qty or qty <= 0:
                self._log_drop(cid, mev, "QTY_PAYLOAD_INVALID", qty=qty, payload=payload)
                return None

            # --------------------------------------------------
            # CLAMP (если БЫЛИ изменения)
            # --------------------------------------------------
            if changing_qty_flag:
                raw_price = payload.get("price") or copy_pv.get("entry_price")
                price_f = Utils.safe_float(raw_price)

                qty = self._clamp_by_max_margin(
                    contracts=qty,
                    max_margin=max_margin,
                    price=price_f,
                    leverage=leverage,
                    coef=coef,
                    rnd=rnd,
                    spec=spec,
                )

                if not qty or qty <= 0:
                    self._log_drop(
                        cid, mev,
                        "QTY_AFTER_CLAMP_INVALID",
                        qty=qty,
                        price=price_f,
                        max_margin=cfg.get("max_position_size"),
                        vol_unit=spec.get("vol_unit"),
                    )
                    return None

            # --------------------------------------------------
            # PRICES
            # --------------------------------------------------
            trigger_price = self._fmt_price(payload.get("trigger_price"), price_precision)
            sl_price = self._fmt_price(payload.get("sl_price"), price_precision)
            tp_price = self._fmt_price(payload.get("tp_price"), price_precision)

        return CopyOrderIntent(
            symbol=mev.symbol,
            side="BUY" if mev.event == "buy" else "SELL",
            position_side=mev.pos_side,
            contracts=qty,
            method=mev.method.upper(),
            price=price,
            trigger_price=trigger_price,
            leverage=leverage,
            open_type=open_type,
            sl_price=sl_price,
            tp_price=tp_price,
            delay_ms=delay_ms,
        )