# COPY.copy_.py

from __future__ import annotations

import asyncio
# import time
from typing import *

from a_config import TG_LOG_TTL_MS, IS_REPORT
from c_log import UnifiedLogger
from c_utils import now

from .pv_fsm_ import PosMonitorFSM, PreparePnlReport
from .state_ import CopyOrderIntentFactory
from .exequter_ import CopyExequter
from TG.notifier_ import FormatUILogs
from MASTER.payload_ import MasterEvent
from .helpers_ import get_cid_pos

if TYPE_CHECKING:
    from .state_ import CopyState
    from b_context import MainContext
    from MASTER.payload_ import MasterPayload


# ==================================================
# SNAPSHOT HASH (ACCOUNT-LEVEL)
# ==================================================
def snapshot_hash(position_vars: Dict[str, Dict[str, dict]]) -> int:
    h = 0
    for symbol, sides in position_vars.items():
        for side, pv in sides.items():
            qty = pv.get("qty", 0)
            if qty:
                h ^= hash((symbol, side, qty))
    return h

# ==================================================
# SAFE REFRESH
# ==================================================
async def safe_refresh(m: PosMonitorFSM, timeout: float = 2.0) -> bool:
    try:
        await asyncio.wait_for(m.refresh(), timeout=timeout)
        return True
    except Exception:
        return False

# ==================================================
# REFRESH COORDINATOR (BACKGROUND, HASH-BASED)
# ==================================================
class RefreshCoordinator:
    """
    Background refresh with hash-based convergence per cid.
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._prev_hash: Dict[int, int] = {}
        self.on_stable: Optional[Callable] = None

    def snapshot(self, cid: int, rt: dict):
        pv = get_cid_pos(rt)
        self._prev_hash[cid] = snapshot_hash(pv)

    def trigger(self, monitors: Dict[int, PosMonitorFSM]) -> None:
        if not monitors:
            return

        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run(monitors))

    async def _run(self, monitors: Dict[int, PosMonitorFSM], ttl_ms: int = 5000):
        start_ts = now()
        delay = 0.05

        pending: Set[int] = set(monitors.keys())

        while pending and now() - start_ts < ttl_ms:
            await asyncio.gather(
                *[safe_refresh(monitors[cid]) for cid in list(pending)],
                return_exceptions=True,
            )

            on_stable_ids = []

            for cid in list(pending):
                m = monitors.get(cid)
                if not m:
                    pending.discard(cid)
                    continue

                cur = snapshot_hash(m.position_vars)
                prev = self._prev_hash.get(cid)

                if prev is not None and cur != prev:
                    pending.discard(cid)
                    self._prev_hash[cid] = cur   # ← зафиксировали новое состояние
                    on_stable_ids.append(cid)

            # refresh завершён для on_stable_ids → можно считать PnL
            if IS_REPORT:
                if self.on_stable and on_stable_ids:
                    asyncio.create_task(self.on_stable(on_stable_ids))

            await asyncio.sleep(delay)
            delay = min(delay * 1.25, 0.5)


# ==================================================
# COPY DISTRIBUTOR
# ==================================================
class CopyDestrib:
    """
    CopyDestrib — неблокирующий intake + сериализованный executor.
    """

    def __init__(
        self,
        mc: "MainContext",
        logger: UnifiedLogger,
        copy_state: CopyState,
        stop_flag: Callable[[], bool],
    ):
        self.mc = mc
        self.logger = logger
        self.copy_state = copy_state
        self.stop_flag = stop_flag

        self.payload: Optional["MasterPayload"] = None

        self._stop_signal_loop = True
        self._stop_tracker = True
        self._last_log_flush_ts: int = 0
        self._pnl_results: List = []

        self.intent_factory = CopyOrderIntentFactory(self.mc)
        self.reset_pv_state = PreparePnlReport(self.mc, self.logger)
        self._exequter = CopyExequter(self.mc, self.logger)

        # 🔥 NEW: background refresh
        self._refresh = RefreshCoordinator()
        self._refresh.on_stable = self._on_refresh_stable

    async def _on_refresh_stable(self, ids: List[int]):
        print(f"ids: {ids}")

        results = await self.reset_pv_state.assum_positions(ids)
        if results: self._pnl_results.extend(results)

    # ==================================================
    # PAYLOAD
    # ==================================================
    def attach_payload(self, payload: "MasterPayload"):
        self.payload = payload
        self.logger.info("CopyDestrib: payload attached")

    # ==================================================
    # STOP API
    # ==================================================
    def stop_signal_loop(self):
        self.logger.info("CopyDestrib: stop_signal_loop()")
        self._stop_signal_loop = True

    # ==================================================
    # UI LOG FLUSH WITH TTL
    # ==================================================
    async def _flush_notify_with_ttl(self) -> None:
        if not self.mc.log_events:
            return

        now_ts = now()
        if (
            self._last_log_flush_ts == 0
            or now_ts - self._last_log_flush_ts >= TG_LOG_TTL_MS
        ):
            texts = FormatUILogs.flush_log_events(self.mc.log_events)
            if texts or self._pnl_results:
                self._last_log_flush_ts = now_ts

            if texts:
                await self.mc.tg_notifier.send_block(texts)

            if IS_REPORT and self._pnl_results:
                texts = []
                texts.extend(FormatUILogs.format_general_report(self._pnl_results))
                texts.append(FormatUILogs.format_general_summary(self._pnl_results))                
                await self.mc.tg_notifier.send_block(texts)
                self._pnl_results.clear()

    # ==================================================
    # INTERNAL: FAN-OUT
    # ==================================================
    async def _broadcast_to_copies(
        self,
        mev: MasterEvent,
        monitors: Dict[int, PosMonitorFSM],
    ):
        if mev.sig_type != "copy":
            return

        self.mc.log_events.append((0, mev))

        tasks: list[asyncio.Task] = []

        for cid, cfg in self.mc.copy_configs.items():
            if cid == 0 or not cfg or not cfg.get("enabled"):
                continue

            rt = self.copy_state.ensure_copy_state(cid)
            if not rt:
                continue

            self._refresh.snapshot(cid=cid, rt=rt)

            tasks.append(
                asyncio.create_task(
                    self._exequter.handle_copy_event(cid, cfg, rt, mev, monitors)
                )
            )

        if tasks:
            await asyncio.gather(*tasks)

    # ==================================================
    # EXECUTOR
    # ==================================================
    async def _execute_signal(self, mev: MasterEvent):
        local_monitors: Dict[int, PosMonitorFSM] = {}

        try:
            if mev.sig_type == "manual":
                cid = getattr(mev, "_cid", None)
                if cid is None:
                    return

                cfg = self.mc.copy_configs.get(cid)
                rt = self.copy_state.ensure_copy_state(cid)
                if not cfg or not rt or not cfg.get("enabled"):
                    return
                
                self._refresh.snapshot(cid=cid, rt=rt)

                await self._exequter.handle_copy_event(
                    cid, cfg, rt, mev, local_monitors
                )

            else:
                await self._broadcast_to_copies(mev, local_monitors)

            # 🔥 fire-and-forget refresh
            if local_monitors:
                self._refresh.trigger(local_monitors)

        except Exception:
            self.logger.exception("[CopyDestrib] execute_signal failed")

        finally:
            await self._flush_notify_with_ttl()

    async def _execute_and_ack(self, mev: MasterEvent):
        try:
            await self._execute_signal(mev)
        finally:
            self.payload.out_queue.task_done()

    # ==================================================
    # MANUAL CLOSE EXPANDER
    # ==================================================

    async def _expand_manual_close(
        self,
        mev: MasterEvent,
    ) -> List[MasterEvent]:
        """
        Expands manual CLOSE intent into atomic close events.
        """
        events: List[MasterEvent] = []

        for cid in self.mc.cmd_ids:
            rt = self.copy_state.ensure_copy_state(cid)
            if not rt:
                continue

            position_vars = rt.get("position_vars") or {}

            for symbol, sides in position_vars.items():
                for pos_side, pv in sides.items():
                    if not pv.get("in_position"):
                        continue

                    qty = pv.get("qty")
                    if not qty or qty <= 0:
                        continue

                    sub = MasterEvent(
                        event="sell",
                        method="market",
                        symbol=symbol,
                        pos_side=pos_side,
                        closed=True,
                        sig_type="manual",
                        payload={
                            "qty": qty,
                            "reduce_only": True,
                            "leverage": pv.get("leverage"),
                            "open_type": pv.get("margin_mode"),
                        },
                        ts=mev.ts,
                    )

                    # 🔒 жёсткая привязка к конкретному copy-id
                    sub._cid = cid
                    events.append(sub)

        return events

    # ==================================================
    # SIGNAL LOOP
    # ==================================================
    async def signal_loop(self):
        if not self.payload:
            self.logger.error("CopyDestrib: payload not attached")
            return

        self.logger.info("CopyDestrib: signal_loop STARTED")
        self._stop_signal_loop = False
        self._stop_tracker = False

        while not self.stop_flag() and not self._stop_signal_loop:
            master_rt = self.mc.copy_configs.get(0, {}).get("cmd_state", {})
            if master_rt.get("trading_enabled") and not master_rt.get("stop_flag"):
                break
            await asyncio.sleep(0.1)

        if self._stop_signal_loop:
            return

        self.logger.info("CopyDestrib: READY")

        while not self.stop_flag() and not self._stop_signal_loop:
            try:
                mev: MasterEvent = await self.payload.out_queue.get()

                if self._stop_tracker:
                    break

                if mev.sig_type == "manual":
                    expanded = await self._expand_manual_close(mev)
                    for sub_mev in expanded:
                        task = asyncio.create_task(self._execute_signal(sub_mev))
                        self.mc.background_tasks.add(task)
                        task.add_done_callback(self.mc.background_tasks.discard)

                    self.payload.out_queue.task_done()
                    self.mc.cmd_ids.clear()
                else:
                    task = asyncio.create_task(self._execute_and_ack(mev))
                    self.mc.background_tasks.add(task)
                    task.add_done_callback(self.mc.background_tasks.discard)

            except asyncio.CancelledError:
                break

        self.logger.info("CopyDestrib: signal_loop FINISHED")