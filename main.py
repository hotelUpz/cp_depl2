# main.py
from __future__ import annotations

import asyncio
import contextlib
from typing import *
# from aiogram import Bot, Dispatcher

from a_config import (
    TG_BOT_TOKEN,
    SPEC_TTL,
)
from b_context import MainContext, MASTER_TEMPLATE
from c_log import UnifiedLogger, log_time
from b_network import NetworkManager

# telegram UI
from TG.menu_ import UIMenu
from TG.notifier_ import TelegramNotifier

# master system
from MASTER.signal_fsm_ import SignalFSM

# copy system
from COPY.cmd_ import CmdDestrib
from COPY.state_ import CopyState

from API.MX.public import MXPublic

import traceback

import os
os.environ["PYDANTIC_DISABLE_MODEL_REBUILD"] = "1"


"""
PIPELINE:
WS → CACHE → HL → FSM → COPY → EXECUTOR
"""


class CoreApp:
    def __init__(self):
        self.mc = MainContext()
        self.logger = UnifiedLogger(
            name="core",
            context="CoreApp",
        )

        # 🔥 ЗАГРУЖАЕМ АККАУНТЫ ОДИН РАЗ
        self.mc.load_accounts()

        # self.bot = Bot(TG_BOT_TOKEN, parse_mode=None)
        # self.dp = Dispatcher()

        self._stop_flag: bool = False

        self.copy_state = CopyState(
            mc=self.mc,
            logger=self.logger,
            stop_flag=lambda: self._stop_flag,
        )

        self.signal = SignalFSM(
            mc=self.mc,
            logger=self.logger,
            copy_state=self.copy_state,
            stop_flag=lambda: self._stop_flag,
        )

        self.cmd = CmdDestrib(
            mc=self.mc,
            logger=self.logger,
            stop_flag=lambda: self._stop_flag,
        )

        self.logger.wrap_object_methods(self.copy_state)
        self.logger.wrap_object_methods(self.signal)
        self.logger.wrap_object_methods(self.cmd)

        self.public_connector: Optional[NetworkManager] = None
        self.spec_task: Optional[asyncio.Task] = None

    # ==========================================================================
    # TELEGRAM
    # ==========================================================================
    async def run_telegram(self):
        while not self._stop_flag:
            try:
                await self.dp.start_polling(
                    self.bot,
                    skip_updates=True,
                    polling_timeout=60,
                    handle_as_tasks=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.exception("Telegram polling crashed", e)
                await asyncio.sleep(2)

    async def init_telegram(self):
        from aiogram import Bot, Dispatcher   # 🔥 LAZY IMPORT

        self.bot = Bot(TG_BOT_TOKEN, parse_mode=None)
        self.dp = Dispatcher()

        self.ui_copytrade = UIMenu(
            bot=self.bot,
            dp=self.dp,
            ctx=self.mc,
            logger=self.logger,
            copy_state=self.copy_state,
            admin_id=self.mc.admin_chat_id,
            on_close=self.cmd.on_close,
        )

        self.mc.tg_notifier = TelegramNotifier(
            bot=self.bot,
            logger=self.logger,
            chat_id=self.mc.admin_chat_id,
            stop_bot=lambda: self._stop_flag,
        )

        self.logger.wrap_object_methods(self.ui_copytrade)
        self.logger.wrap_object_methods(self.mc.tg_notifier)

    # ==========================================================================
    # PUBLIC CONNECTOR (ONE INSTANCE)
    # ==========================================================================
    async def init_public_connector(self):
        proxy = MASTER_TEMPLATE.get("exchange", {}).get("proxy")

        self.public_connector = NetworkManager(
            logger=self.logger,
            proxy_url=proxy,
            stop_flag=lambda: self._stop_flag,
            mode="simple",   # прод-режим
        )
        self.logger.wrap_object_methods(self.public_connector)

        # 🔑 КЛЮЧЕВО: ЯВНО создаём сессию
        await self.public_connector.initialize_session()

        # потом уже пинг
        self.public_connector.start_ping_loop()

        ok = await self.public_connector.wait_for_session()
        if not ok:
            raise RuntimeError("Failed to init public connector")

    # ==========================================================================
    # LOAD SPEC DATA (NO CONNECTOR CREATION HERE)
    # ==========================================================================
    async def load_spec_data(self) -> None:
        for _ in range(10):
            if self._stop_flag:
                return

            try:
                data = await MXPublic.get_instruments(
                    session=self.public_connector.session
                )
                if data:
                    self.mc.instruments_data = data
                    return
            except Exception as e:
                self.logger.warning(f"Spec fetch failed: {e}")

            await asyncio.sleep(0.8)

    # ==========================================================================
    # SPEC STREAM (TTL LOOP)
    # ==========================================================================
    async def refrashe_spec_data_loop(self):
        self.logger.info("Spec stream started")

        while not self._stop_flag:
            try:
                await self.load_spec_data()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.exception("Spec refresh failed", exc_info=e)

            await asyncio.sleep(SPEC_TTL)

        self.logger.info("Spec stream stopped")

    # ==========================================================================
    # RUN
    # ==========================================================================
    async def run(self):

        self.logger.info(f"Start time: {log_time()}")

        # 1. Telegram
        await self.init_telegram()
        tg_task = asyncio.create_task(
            self.run_telegram(), name="telegram",
            # self.dp.start_polling(
            #     self.bot,
            #     skip_updates=True,
            #     polling_timeout=60,
            #     handle_as_tasks=True,
            # ),
            # name="telegram",
        )
        self.logger.info("Telegram started")

        # 2. Public infra
        await self.init_public_connector()

        # 3. Spec stream
        self.spec_task = asyncio.create_task(
            self.refrashe_spec_data_loop(),
            name="spec-stream",
        )
        self.logger.info("Spec stream started")

        # дождаться первой загрузки спеки
        while not self.mc.instruments_data and not self._stop_flag:
            await asyncio.sleep(0.1)

        self.logger.info("Instruments loaded")

        # 4. Master supervisor
        master_task = asyncio.create_task(
            self.signal.master_supervisor(),
            name="master-supervisor",
        )
        
        self.logger.info("Master supervisor started")

        try:
            await asyncio.gather(
                tg_task,
                master_task
            )
        except asyncio.CancelledError:
            pass
        finally:
            for t in (tg_task, master_task):
                if t and not t.done():
                    t.cancel()
            await self.shutdown()

    # ======================================================================
    # SHUTDOWN
    # ======================================================================
    async def shutdown(self):
        self.logger.info("CoreApp shutdown started")
        self._stop_flag = True

        # --------------------------------------------------
        # 1. STOP COPY + MASTER LOOPS (больше НИЧЕГО не создаётся)
        # --------------------------------------------------
        try:
            self.signal.copy.stop_signal_loop()
        except Exception:
            pass

        # --------------------------------------------------
        # 2. WAIT BACKGROUND COPY TASKS
        # --------------------------------------------------
        tasks = list(self.mc.background_tasks)
        if tasks:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # --------------------------------------------------
        # 3. STOP SPEC TASK
        # --------------------------------------------------
        if self.spec_task:
            self.spec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.spec_task

        # --------------------------------------------------
        # 4. SHUTDOWN ALL COPY RUNTIMES (NetworkManager внутри)
        # --------------------------------------------------
        for cid in list(self.mc.copy_runtime_states.keys()):
            try:
                await self.copy_state.shutdown_runtime(cid)
            except Exception:
                pass

        # --------------------------------------------------
        # 5. SHUTDOWN PUBLIC CONNECTOR (ГЛОБАЛЬНЫЙ)
        # --------------------------------------------------
        if self.public_connector:
            try:
                await self.public_connector.shutdown_session()
            except Exception:
                pass

        # --------------------------------------------------
        # 6. STOP TELEGRAM POLLING
        # --------------------------------------------------
        try:
            await self.dp.stop_polling()
        except Exception:
            pass

        # if tg_task:
        #     tg_task.cancel()
        #     with contextlib.suppress(asyncio.CancelledError):
        #         await tg_task

        await asyncio.sleep(0)

        # --------------------------------------------------
        # 7. CLOSE BOT SESSION (СТРОГО ПОСЛЕДНИМ)
        # --------------------------------------------------
        try:
            await self.bot.session.close()
        except Exception:
            pass

        self.logger.info("CoreApp stopped cleanly")


# ==========================================================================
# ENTRYPOINT
# ==========================================================================
async def main():
    app = CoreApp()
    try:
        await app.run()
        print("🔥 EXIT:")
    except Exception:
        traceback.print_exc()
    except KeyboardInterrupt:
        print("💥 Exit: Ctrl+C pressed")


if __name__ == "__main__":
    asyncio.run(main())


# taskkill /F /IM python.exe  -- для убийства параллельных процессов на Windows


# # убедились, что права уже установлены (вы это сделали)
# chmod 600 ssh_key

# # запустить агент (если он не запущен) и добавить ключ из текущей директории
# eval "$(ssh-agent -s)" && ssh-add ./ssh_key

# ssh-add -l        # выведет список добавленных ключей или "The agent has no identities"

# ssh -T git@github.com  


# "proxy": "http://Lg7hLbC8:cXxwCBy8@154.219.72.198:64810"