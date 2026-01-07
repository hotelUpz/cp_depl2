# COPY.pv_fsm_.py

from typing import *

from b_context import PosVarTemplate
from c_utils import Utils, now


if TYPE_CHECKING:
    from b_context import MainContext
    from c_log import UnifiedLogger
    from API.MX.client import MexcClient


class PreparePnlReport:    
    def __init__(
        self,
        mc: "MainContext",
        logger: "UnifiedLogger"
    ):
        self.mc = mc
        self.logger = logger

    async def pv_cleanup(
        self,
        get_realized_pnl: Callable,
        pv: PosVarTemplate,
        symbol: str,
        pos_side: str
    ) -> Optional[dict]:

        now_ts = now()
        entry_ts = pv.get("_entry_ts")

        if not isinstance(entry_ts, int) or entry_ts <= 1e10:
            self.logger.warning(
                f"[ResetPV] invalid entry_ts {entry_ts} for {symbol} {pos_side}"
            )
            return None

        pnl = await get_realized_pnl(
            symbol=symbol,
            start_time=entry_ts,
            end_time=now_ts,
            direction=1 if pos_side == "LONG" else 2,
        )
        if not pnl:
            return {
                "symbol": symbol,
                "pos_side": pos_side,
                "pnl_usdt": None,
                "entry_ts": entry_ts,
                "exit_ts": now_ts,
                "error": "PNL_FETCH_FAILED",
            }

        return {
            "symbol": symbol,
            "pos_side": pos_side,
            "pnl_usdt": pnl.get("pnl_usdt") if pnl else None,
            "entry_ts": entry_ts,
            "exit_ts": now_ts,
        }

    async def assum_positions(self, ids: List[int]):
        all_finish_results: List[dict] = []

        # ==================================================
        # 1) COLLECT ALL CLOSED_PENDING PV
        # ==================================================
        pv_items: List[Tuple[int, str, str, PosVarTemplate, "MexcClient"]] = []

        for cid, cfg in self.mc.copy_configs.items():
            if cid == 0 or not cfg or not cfg.get("enabled"):
                continue
            if cid not in ids:
                continue

            rt = self.mc.copy_runtime_states.get(cid)
            if not rt:
                continue

            position_vars = rt.get("position_vars") or {}
            mc_client: "MexcClient" = rt.get("mc_client")
            if not mc_client:
                continue

            for symbol, sides in position_vars.items():
                for pos_side, pv in sides.items():
                    if pv.get("_state") == "CLOSED_PENDING":
                        pv_items.append((cid, symbol, pos_side, pv, mc_client))
                        pv.pop("_state", None)

        if not pv_items:
            return []

        # ==================================================
        # 2) TIME RANGE
        # ==================================================
        entry_ts_list = [
            pv.get("_entry_ts")
            for _, _, _, pv, _ in pv_items
            if isinstance(pv.get("_entry_ts"), (int, float))
        ]

        if not entry_ts_list:
            self.logger.warning("[ResetPV] no valid entry_ts for batch pnl")
            return []

        start_ts = min(entry_ts_list)
        end_ts = now()

        # ==================================================
        # 3) BATCH FETCH (ONE REQUEST)
        # ==================================================
        # берем любой клиент (API одинаковый)
        _, _, _, _, mc_client_any = pv_items[0]

        batch_map = await mc_client_any.get_realized_pnl_batch(
            start_time=start_ts,
            end_time=end_ts,
        )

        # ==================================================
        # 4) APPLY RESULTS
        # ==================================================
        for cid, symbol, pos_side, pv, mc_client in pv_items:
            direction = 1 if pos_side == "LONG" else 2
            key = (symbol, direction)

            res: Optional[dict] = None

            # ---- batch hit ----
            if batch_map and key in batch_map:
                pnl = batch_map[key]
                res = {
                    "cid": cid,
                    "symbol": symbol,
                    "pos_side": pos_side,
                    "pnl_usdt": pnl.get("pnl_usdt"),
                    "entry_ts": pv.get("_entry_ts"),
                    "exit_ts": end_ts,
                }

                if "_entry_ts" in pv: pv.pop("_entry_ts", None)

            # ---- fallback (safe) ----
            else:
                res = await self.pv_cleanup(
                    mc_client.get_realized_pnl,
                    pv,
                    symbol,
                    pos_side,
                )
                if res:
                    res["cid"] = cid
                    pv.pop("_entry_ts", None)

            if res: all_finish_results.append(res)

        return all_finish_results


class PosMonitorFSM:
    """
    """
    def __init__(
        self,
        position_vars: Dict[str, Dict[str, Dict[str, Any]]],
        fetch_positions: Callable
    ):
        self.position_vars = position_vars
        self.fetch_positions = fetch_positions

    # --------------------------------------------------
    @staticmethod
    def unpack(position: dict) -> Optional[dict]:
        """
        MEXC position → normalized dict

        Возвращает None если:
        • мусор
        • не holding
        • объём <= 0
        """

        if not isinstance(position, dict):
            return None

        # state: 1=holding, 2=system-held, 3=closed
        if position.get("state") != 1:
            return None

        symbol = position.get("symbol")
        pos_type_int = position.get("positionType")  # 1=LONG, 2=SHORT

        vol = abs(Utils.safe_float(position.get("holdVol"), 0.0))
        if not symbol or vol <= 0:
            return None

        if pos_type_int == 1:
            pos_side = "LONG"
        elif pos_type_int == 2:
            pos_side = "SHORT"
        else:
            return None

        return {
            "symbol": symbol,
            "pos_side": pos_side,
            "qty": vol,
            "entry_price": Utils.safe_float(position.get("openAvgPrice")),
            "avg_price": Utils.safe_float(position.get("holdAvgPrice")),
            "leverage": Utils.safe_int(position.get("leverage"), 1),
            "margin_mode": Utils.safe_int(position.get("openType"), 1),
        }

    # --------------------------------------------------
    async def refresh(self) -> None:
        """
        Полная синхронизация runtime.position_vars с биржей.
        """

        positions = await self.fetch_positions()

        # -------- API ERROR / NETWORK --------
        if positions is None:
            # не трогаем кеш вообще
            return

        active: Dict[Tuple[str, str], dict] = {}

        for raw in positions:
            info = self.unpack(raw)
            if not info:
                continue
            key = (info["symbol"], info["pos_side"])
            active[key] = info

        now_ts = now()

        # -------- APPLY SNAPSHOT --------
        for symbol, sides in self.position_vars.items():
            for pos_side, pv in sides.items():
                key = (symbol, pos_side)
                was_in_position = bool(pv.get("in_position"))

                # ---------- POSITION EXISTS ----------
                if key in active:
                    info = active[key]
                    is_in_position = info["qty"] > 0

                    # ---- NEW ENTRY ----
                    if is_in_position and not was_in_position:
                        pv.update({
                            "in_position": True,
                            "qty": info["qty"],
                            "entry_price": info["entry_price"],
                            "avg_price": info["avg_price"],
                            "leverage": info["leverage"],
                            "margin_mode": info["margin_mode"],
                            "_entry_ts": now_ts,
                        })
                        continue

                    # ---- CONTINUE POSITION ----
                    if is_in_position and was_in_position:
                        pv.update({
                            "in_position": True,
                            "qty": info["qty"],
                            "avg_price": info["avg_price"],
                            "leverage": info["leverage"],
                            "margin_mode": info["margin_mode"],
                        })
                        continue

                # ---------- POSITION NOT FOUND ----------
                if was_in_position:
                    # 🔥 REAL RESET PV
                    entry_ts = pv.get("_entry_ts")
                    pv.update(PosVarTemplate.base_template())
                    pv["_entry_ts"] = entry_ts
                    pv["_state"]= "CLOSED_PENDING"
