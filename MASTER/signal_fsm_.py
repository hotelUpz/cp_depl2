# MASTER.signal_fsm_.py

from __future__ import annotations

import asyncio
import hashlib
from typing import *

from COPY.copy_ import CopyDestrib
from .stream_ import MasterSignalStream
from .payload_ import MasterPayload
from .state_ import SignalCache
from c_utils import now, Utils

if TYPE_CHECKING:
    from COPY.state_ import CopyState
    from b_context import MainContext
    from c_log import UnifiedLogger


# ============================================================
# HELPERS
# ============================================================
def creds_hash(cfg: dict) -> str:
    ex = cfg.get("exchange", {})
    key = ex.get("api_key") or ""
    sec = ex.get("api_secret") or ""
    proxy = ex.get("proxy") or ""
    return hashlib.md5(f"{key}:{sec}:{proxy}".encode()).hexdigest()

async def _stop_task(task: asyncio.Task | None):
    """
    Корректная отмена asyncio-задачи.
    """
    if task and not task.done():
        task.cancel()
        await asyncio.sleep(0)


# ============================================================
# SIGNAL FSM
# ============================================================
class SignalFSM:
    """
    Supervisor мастера.

    Управляет:
    • жизненным циклом WS
    • MasterPayload
    • Copy signal_loop

    Инвариант:
    signal_loop НИКОГДА не блокирует supervisor
    """

    def __init__(
        self,
        mc: "MainContext",
        logger: "UnifiedLogger",
        copy_state: CopyState,
        stop_flag: Callable[[], bool],
    ):
        self.mc = mc
        self.logger = logger
        self.copy_state = copy_state
        self.stop_flag = stop_flag

        # infra
        self.signal_cache: Optional[SignalCache] = None
        self.signal_stream: Optional[MasterSignalStream] | None = None
        self.payload: Optional[MasterPayload] = None

        # copy layer
        self.copy = CopyDestrib(
            mc=self.mc,
            logger=self.logger,
            copy_state=self.copy_state,
            stop_flag=self.stop_flag,
        )
        self.logger.wrap_object_methods(self.copy)

        # tasks
        self.stream_task: Optional[asyncio.Task] = None
        self.payload_task: Optional[asyncio.Task] = None
        self.copy_loop_task: Optional[asyncio.Task] = None

        # FSM safety
        self._fsm_lock = asyncio.Lock()

    # --------------------------------------------------------
    def _reset_master_state(self):
        """
        Полный reset PV мастера.
        Вызывается ТОЛЬКО при HARD STOP или RELOAD.
        """
        Utils.clear_runtime_positions(pos_vars_root=self.mc.pos_vars_root)        

    # ========================================================
    # MAIN SUPERVISOR LOOP
    # ========================================================

    async def master_supervisor(self):
        self.logger.info("[FSM] Master supervisor started")

        last_hash: Optional[str] = None

        while not self.stop_flag():
            await asyncio.sleep(0.05)

            async with self._fsm_lock:

                master_cfg = self.mc.copy_configs.get(0, {})
                ex = master_cfg.get("exchange", {})
                cmd_state = master_cfg.get("cmd_state", {})

                trading_enabled = cmd_state.get("trading_enabled", False)
                flagged_stop = cmd_state.get("stop_flag", False)

                api_key = ex.get("api_key")
                api_secret = ex.get("api_secret")
                proxy = ex.get("proxy")

                # ==================================================
                # HARD STOP (полная смерть)
                # ==================================================
                if flagged_stop:
                    self.logger.info("[FSM] HARD STOP")

                    if self.signal_stream:
                        self.signal_stream.stop()
                    self.signal_stream = None

                    await _stop_task(self.stream_task)
                    await _stop_task(self.payload_task)

                    if self.payload:
                        self.payload.stop()
                    self.mc.master_payload = None
                    self.payload = None

                    if self.copy_loop_task:
                        self.copy.stop_signal_loop()
                        await _stop_task(self.copy_loop_task)
                    self.copy_loop_task = None

                    self._reset_master_state()
                    last_hash = None

                    await asyncio.sleep(0.3)
                    continue

                # ==================================================
                # PAUSE (без ресета)
                # ==================================================
                if not trading_enabled:
                    await asyncio.sleep(0.2)
                    continue

                # ==================================================
                # CREDS CHECK
                # ==================================================
                if not (api_key and api_secret):
                    await asyncio.sleep(0.3)
                    continue

                cur_hash = creds_hash(master_cfg)

                # ==================================================
                # RUNNING, SAME CREDS
                # ==================================================
                if (
                    cur_hash == last_hash
                    and self.stream_task
                    and not self.stream_task.done()
                ):
                    await asyncio.sleep(0.2)
                    continue

                # ==================================================
                # RELOAD (первый старт или смена кредов)
                # ==================================================
                self.logger.info("[FSM] RELOAD MASTER STREAM")

                # ---- stop old ----
                if self.signal_stream:
                    self.signal_stream.stop()
                await _stop_task(self.stream_task)

                if self.payload:
                    self.payload.stop()
                await _stop_task(self.payload_task)

                self._reset_master_state()

                # ---- NEW CACHE ----
                self.signal_cache = SignalCache()

                # ---- NEW STREAM ----
                self.signal_stream = MasterSignalStream(
                    api_key=api_key,
                    api_secret=api_secret,
                    signal_cache=self.signal_cache,
                    logger=self.logger,
                    stop_flag=self.stop_flag,
                    proxy_url=proxy,
                )
                self.logger.wrap_object_methods(self.signal_stream)

                self.stream_task = asyncio.create_task(
                    self.signal_stream.start()
                )

                # ---- WAIT READY (с таймаутом) ----
                t0 = now()
                while not self.stop_flag():
                    if self.signal_stream.ready:
                        break
                    if now() - t0 > 15000:
                        self.logger.error("[FSM] WS start timeout")
                        break
                    await asyncio.sleep(0.05)

                if not self.signal_stream.ready:
                    await asyncio.sleep(0.5)
                    continue

                self.logger.info("[FSM] Master WS ready")

                # ---- NEW PAYLOAD ----
                self.payload = MasterPayload(
                    cache=self.signal_cache,
                    mc=self.mc,
                    logger=self.logger,
                    stop_flag=self.stop_flag,
                )
                self.mc.master_payload = self.payload   # 👈 ВОТ ОНО

                self.payload_task = asyncio.create_task(
                    self.payload.run()
                )

                self.copy.attach_payload(self.payload)

                # ---- COPY LOOP (FORCE RESTART ON CREDS CHANGE) ----
                if self.copy_loop_task:
                    self.copy.stop_signal_loop()
                    await _stop_task(self.copy_loop_task)
                    self.copy_loop_task = None

                self.copy_loop_task = asyncio.create_task(
                    self.copy.signal_loop()
                )

                last_hash = cur_hash
                self.logger.info("[FSM] ENTER RUNNING")

        self.logger.info("[FSM] EXIT")