# c_utils.py

from __future__ import annotations
from typing import *
from datetime import datetime
from a_config import PRECISION, QUOTA_ASSET
from c_log import TZ
from decimal import Decimal, getcontext
import time


getcontext().prec = PRECISION  # точность Decimal


def now() -> int:
    """Return current timestamp in milliseconds."""
    return int(time.time() * 1000)


class Utils:        
    @staticmethod
    def safe_float(value: Any, default: float = 0.0) -> float:
        """Преобразует значение в float, если не удалось — возвращает default"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
        
    @staticmethod
    def safe_int(value: Any, default: int = 0) -> int:
        """Преобразует значение в int, если не удалось — возвращает default"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
        
    @staticmethod
    def safe_round(value: Any, ndigits: int = 2, default: float = 0.0) -> float:
        """Безопасный round для None или нечисловых значений"""
        try:
            return round(float(value), ndigits)
        except (TypeError, ValueError):
            return default        

    @staticmethod
    def parse_precision(symbols_info: list[dict], symbol: str) -> dict:
        """
        Возвращает настройки для qty, price и макс. плеча в виде словаря:
        {
            "contract_precision": int,
            "price_precision": int,
            "contract_size": float,
            "price_unit": float,
            "vol_unit": float,
            "max_leverage": int | None
        }
        Если символ не найден или данные пустые → None.
        """
        symbol_data = next((item for item in symbols_info if item.get("symbol") == symbol or item.get("baseCoinName") + f"_{QUOTA_ASSET}" == symbol), None)
        if not symbol_data:
            return None

        # обработка maxLeverage
        raw_leverage = symbol_data.get("maxLeverage")
        try:
            max_leverage = int(float(raw_leverage)) if raw_leverage is not None else None
        except (ValueError, TypeError):
            max_leverage = None

        return {
            "contract_precision": symbol_data.get("volScale", 3),
            "price_precision": symbol_data.get("priceScale", 2),
            "contract_size": float(symbol_data.get("contractSize", 1)),
            "price_unit": float(symbol_data.get("priceUnit", 0.01)),
            "vol_unit": float(symbol_data.get("volUnit", 1)),
            "max_leverage": max_leverage
        }

    @staticmethod
    def milliseconds_to_datetime(milliseconds):
        if milliseconds is None:
            return "N/A"
        try:
            ms = int(milliseconds)   # <-- приведение к int
            if milliseconds < 0: return "N/A"
        except (ValueError, TypeError):
            return "N/A"

        if ms > 1e10:  # похоже на миллисекунды
            seconds = ms / 1000
        else:
            seconds = ms

        dt = datetime.fromtimestamp(seconds, TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
        
    def format_duration(ms: int) -> str:
        """
        Конвертирует миллисекундную разницу в формат "Xh Ym" или "Xm" или "Xs".
        :param ms: длительность в миллисекундах
        """
        if ms is None:
            return ""
        
        total_seconds = ms // 1000
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0 and seconds > 0:
            return f"{minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m"
        else:
            return f"{seconds}s"
        
    @staticmethod
    def to_human_digit(value):
        if value is None:
            # return "N/A"
            return None
        getcontext().prec = PRECISION
        dec_value = Decimal(str(value)).normalize()
        if dec_value == dec_value.to_integral():
            return format(dec_value, 'f')
        else:
            return format(dec_value, 'f').rstrip('0').rstrip('.')  
        
    @staticmethod
    def clear_runtime_positions(pos_vars_root: dict) -> None:
        """
        Clears runtime position state (LONG / SHORT) while keeping spec intact.
        Safe, idempotent.
        """
        pv = pos_vars_root.get("position_vars", {})
        for sym in pv.values():
            sym.pop("LONG", None)
            sym.pop("SHORT", None)