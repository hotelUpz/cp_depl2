# API.MX.public.py
from __future__ import annotations

import aiohttp
from typing import Optional, Dict, List


class MXPublic:
    # BASE_URL = "https://futures.testnet.mexc.com/api/v1"
    BASE_URL = "https://contract.mexc.com/api/v1"

    @staticmethod
    async def _get(
        path: str,
        session: aiohttp.ClientSession,
        proxy_url: Optional[str] = None,
        params: Optional[Dict] = None
    ) -> Optional[Dict]:

        url = MXPublic.BASE_URL + path

        try:
            async with session.get(url, params=params, proxy=proxy_url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    # ----------------------------
    # ПУБЛИЧНЫЕ ЭНДПОИНТЫ
    # ----------------------------

    @staticmethod
    async def get_instruments(
        session: aiohttp.ClientSession,
        proxy_url: Optional[str] = None
    ) -> Optional[List[Dict]]:

        """
        GET /contract/detail
        Возвращает список инструментов.
        """

        data = await MXPublic._get("/contract/detail", session, proxy_url)

        # Mexc возвращает: {"success": True, "code": 0, "data": [...]}
        if data and data.get("success") and isinstance(data.get("data"), list):
            return data["data"]

        return None

    @staticmethod
    async def get_fair_price(
        symbol: str,
        session: aiohttp.ClientSession,
        proxy_url: Optional[str] = None
    ) -> Optional[float]:

        """
        GET /contract/fair_price/{symbol}
        Возвращает справедливую (марковскую) цену контракта.
        """

        data = await MXPublic._get(f"/contract/fair_price/{symbol}", session, proxy_url)

        if (
            data
            and data.get("success")
            and isinstance(data.get("data"), dict)
            and "fairPrice" in data["data"]
        ):
            try:
                return float(data["data"]["fairPrice"])
            except Exception:
                return None

        return None
