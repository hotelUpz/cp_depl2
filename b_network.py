from __future__ import annotations

import asyncio
import aiohttp
import ssl
from typing import Callable, Optional, TYPE_CHECKING, Literal

from a_config import PING_URL, PING_INTERVAL, SESSION_TTL

if TYPE_CHECKING:
    from c_log import UnifiedLogger


# ============================================================
# SSL
# ============================================================

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


SessionMode = Literal["simple", "manager"]

# ============================================================
# NETWORK MANAGER
# ============================================================

class NetworkManager:
    """
    Infrastructure-level network/session manager.

    Guarantees:
        • session is NEVER closed while logically active
        • recreate only after confirmed failure
        • consumers may wait for session (soft TTL)
        • ping-based degradation detection with fast retries
        • HTTP proxy applied at SESSION level
        • ping NEVER runs before initial session creation
    """

    PING_FAIL_THRESHOLD = 3
    PING_RETRY_DELAY = 0.15

    def __init__(
        self,
        logger: "UnifiedLogger",
        proxy_url: Optional[str],
        stop_flag: Callable[[], bool],
            *,
        mode: SessionMode = "simple",
    ):
        self.logger = logger
        self.stop_flag = stop_flag
        self.mode = mode

        self.session: Optional[aiohttp.ClientSession] = None
        self._ping_task: Optional[asyncio.Task] = None

        self._recreating = False
        self._recreate_lock = asyncio.Lock()

        self._ping_failures = 0
        self._degraded = False

        if not proxy_url or proxy_url.strip() == "0":
            proxy_url = None
        self.proxy_url = proxy_url

    # --------------------------------------------------
    # SESSION
    # --------------------------------------------------
    async def initialize_session(self) -> None:
        if self.session and not self.session.closed:
            return

        timeout = aiohttp.ClientTimeout(total=30)

        # ==================================================
        # MODE 2 — SIMPLE / STABLE (РЕКОМЕНДУЕМЫЙ)
        # ==================================================
        if self.mode == "simple":
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                proxy=self.proxy_url,      # None = direct
                trust_env=False,
            )

            self.logger.info(
                f"NetworkManager: session created [SIMPLE]"
                f"{' proxy=' + self.proxy_url if self.proxy_url else ' direct'}"
            )

        # ==================================================
        # MODE 3 — MANAGER / SSL_CTX (СПЕЦРЕЖИМ)
        # ==================================================        
        elif self.mode == "manager":
            connector = aiohttp.TCPConnector(
                ssl=SSL_CTX,
                limit=0,
            )

            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                proxy=self.proxy_url,          # None = direct
                trust_env=False,
            )

            self.logger.info(
                f"NetworkManager: session created [MANAGER]"
                f"{' proxy=' + self.proxy_url if self.proxy_url else ' direct'}"
            )

        else:
            raise RuntimeError(f"NetworkManager: unknown session mode {self.mode}")

    # --------------------------------------------------
    # WAIT FOR SESSION
    # --------------------------------------------------
    async def wait_for_session(self, timeout_ms: int = SESSION_TTL) -> bool:
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        while not self.stop_flag():
            if self.session and not self.session.closed:
                return True

            if (loop.time() - t0) * 1000 > timeout_ms:
                return False

            await asyncio.sleep(0.01)

        return False

    # --------------------------------------------------
    # FAILURE HANDLING
    # --------------------------------------------------
    async def notify_session_failure(self, reason: str = "") -> None:
        async with self._recreate_lock:
            if self._recreating:
                return

            self._recreating = True
            try:
                self.logger.warning(
                    f"NetworkManager: recreating session due to failure {reason}"
                )

                if self.session and not self.session.closed:
                    try:
                        await asyncio.wait_for(self.session.close(), timeout=3)
                    except Exception:
                        pass

                self.session = None
                await self.initialize_session()

            finally:
                self._recreating = False

    # --------------------------------------------------
    # PING
    # --------------------------------------------------
    async def _ping_once(self) -> bool:
        if not self.session or self.session.closed:
            return False

        try:
            async with self.session.get(PING_URL) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _handle_ping_failure(self) -> None:
        while not self.stop_flag():
            self._ping_failures += 1

            self.logger.warning(
                f"NetworkManager: ping failed "
                f"({self._ping_failures}/{self.PING_FAIL_THRESHOLD})"
            )

            if self._ping_failures >= self.PING_FAIL_THRESHOLD:
                self._degraded = True
                self._ping_failures = 0

                self.logger.error(
                    "NetworkManager: session degraded, triggering reconnect"
                )
                await self.notify_session_failure("ping_degradation")
                return

            await asyncio.sleep(self.PING_RETRY_DELAY)

            if await self._ping_once():
                self._ping_failures = 0
                self._degraded = False
                return

    # --------------------------------------------------
    # PING LOOP
    # --------------------------------------------------
    async def _ping_loop(self):
        self.logger.info("NetworkManager: ping loop started")

        # 🔒 ЖЁСТКО ждём первую сессию
        if not await self.wait_for_session():
            self.logger.warning(
                "NetworkManager: ping loop aborted (session not initialized)"
            )
            return

        try:
            while not self.stop_flag():
                if await self._ping_once():
                    self._ping_failures = 0
                    self._degraded = False
                else:
                    await self._handle_ping_failure()

                await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            pass
        finally:
            self.logger.info("NetworkManager: ping loop stopped")

    # --------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------
    def start_ping_loop(self):
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def shutdown_session(self):
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        if self.session and not self.session.closed:
            try:
                await asyncio.wait_for(self.session.close(), timeout=3)
                self.logger.info("NetworkManager: session closed")
            except Exception as e:
                self.logger.exception(
                    "NetworkManager: error closing session", e
                )

        self.session = None
