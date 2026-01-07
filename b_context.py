# b_context.py
from __future__ import annotations

import asyncio
import json
import copy
import os
from typing import *

from a_config import (
    COPY_NUMBER,
    ADMIN_CHAT_ID
)
from c_utils import now
from TG.helpers_ import validate_unique_accounts

if TYPE_CHECKING:
    from TG.notifier_ import TelegramNotifier
    from MASTER.payload_ import MasterPayload


COPIES_JSON_PATH = "./copies.json"


MASTER_TEMPLATE: dict = {
    "id": 0,
    "role": "master",

    "exchange": {
        "api_key": None,
        "api_secret": None,
        "uid": None,
        "proxy": None,
    },

    "cmd_state": {
        "trading_enabled": False,
        "stop_flag": False,
        "stop_confirm": False
    },

    "created_at": None,
}


COPY_TEMPLATE: dict = {
    # ====== УНИКАЛЬНЫЕ ПАРАМЕТРЫ ======
    "id": None,     # None | int
    "name": None,   # None | str
    "role": "copy",
    
    # ====== КРЕДЫ И ПРОКСИ ======
    "exchange": {
        "api_key": None,             # обязательный параметр
        "api_secret": None,          # обязательный параметр
        "uid": None,                 # MEXC UID (обязательный параметр)
        "proxy": None,               # строка для NetworkManager. Необязательный параметр
    },

    # ====== ПАРАМЕТРЫ КОПИРОВАНИЯ ======
    "coef": 1.0,                                # коэффициент копирования (0.5x / 1x / 2x)
    "leverage": None,                           # если None, то копировать плечо из сигнала мастера
    "margin_mode": None,                        # если None, то копировать margin_mode из сигнала мастера
    "max_position_size": None,                  # лимит на позицию в долларах. надо будет сделать пересчет из конрактов в 
    # \доллары и назад если сработает ограничение. если нет то берем контракты такие какие из сигнала. None -- игнорируем опцию

    "random_size_pct": [-0.0, 0.0],             # разброс размера позиции в процентах
    "delay_ms": [0.0, 0.0],                              # доп. задержка перед отправкой ордеров. если есть значение то использовать\
    # в качестве инкремента рандомизацию типа random.uniform(0, delay_ms_val)

    # ====== UI СТАТУС ======
    "enabled": False,                           # включен или нет. Обязательный параметр для работы
    "created_at": None,                         # задается при создании в ТГ кнопках
}


COPY_RUNTIME_STATE = {
    "id": None,

    # ===== NETWORK =====
    "connector": None,
    "mc_client": None,
    "network_ready": False,

    # ===== ERRORS =====
    "last_error": None,
    "last_error_ts": None,

    # ===== ORDERS / POSITIONS =====
    "position_vars": {},  # symbol → side → pv
    "orders_vars": {},    # symbol → side → ov --- добавил. при закрытии силой по cmd -- сбрасываем. \
                          # При сигнальной закрытии не трогаем. нужна отдельная станция
    # "orders" = {
    # symbol -- pos_side...
    #     "limit": {
    #         master_order_id: {
    #             copy_order_id: None,
    #             "price": ...,
    #             "qty": ...,
    #             "status": "OPEN|CANCELED|FILLED"
    #         }
    #     },
    #     "trigger": {
    #         master_order_id: {
    #             copy_order_id: None,
    #             "trigger_price": ...,
    #             "qty": ...,
    #             "status": ...
    #         }
    #     }
    # }

    "cmd_closing": False,
    "dedup": {},
}




class PosVarTemplate:
    @staticmethod
    def base_template() -> dict:
        return {
            "in_position": False,
            # "_state": None,
            "qty": 0.0,

            "entry_price": None,
            "avg_price": None,

            "leverage": None,
            "margin_mode": None,

            # "_entry_ts": int | None,      # ms
            # "_exit_ts": int | None,       # ms
        }
    

# ======================================================================
#                        MAIN CONTEXT
# ======================================================================

class MainContext:
    """
    Главный источник правды:
    copy_configs   — persistent (сохраняется в JSON)
    runtime_states — volatile (в памяти, не сохраняется)
    """

    def __init__(self):
        self.admin_chat_id = None
        if ADMIN_CHAT_ID: self.admin_chat_id: int = int(ADMIN_CHAT_ID)

        # persistent configs
        self.copy_configs: Dict[int, Dict[str, Any]] = {}

        # runtime states
        self.pos_vars_root: Dict = {}
        self.copy_runtime_states: Dict[int, Dict[str, Any]] = {}
        self.last_cmd_ts: int = 0
        self.cmd_ids: List[int] = [] 
        self.master_payload: Optional[MasterPayload] = None

        self.active_copy_ids: Set = set()   

        self.tg_notifier: Optional[TelegramNotifier] = None     

        # market data placeholders
        self.instruments_data: List[Dict[str, Any]] = None
        self.prices: Dict[str, float] = {}
        self.debug: bool = True

        self.log_events = []

        self.background_tasks: set[asyncio.Task] = set()

    # ==================================================================
    #                          LOADING
    # ==================================================================

    def load_accounts(self):
        """Загружает copies.json (если отсутствует — создаёт новые шаблоны)."""

        if os.path.exists(COPIES_JSON_PATH) and not self.copy_configs:
            try:
                with open(COPIES_JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.copy_configs = {
                            int(k): v for k, v in data.items()
                        }
                        print("✔ copies.json загружен.")
            except Exception as e:
                print(f"❗ Ошибка чтения copies.json: {e}")

        # гарантируем что аккаунты существуют
        self._init_accounts()

        reason = validate_unique_accounts(self)
        if reason:
            print("❗ CONFIG ERROR:")
            print(reason)

    # ==================================================================
    #                         SAVING
    # ==================================================================

    async def save_users(self):
        """Простая запись JSON без атомарных извращений: монопроцессный бот."""
        try:
            with open(COPIES_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(self.copy_configs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❗ Ошибка записи copies.json: {e}")

    # ==================================================================
    #                    INITIAL ACCOUNTS CREATION
    # ==================================================================

    def _init_accounts(self):
        """
        Создает:
        master = 0
        copies = 1..COPY_NUMBER
        Если аккаунт есть в JSON — НЕ перезаписывает полностью,
        но корректирует структуру (remove junk / ensure required).
        """

        # ==========================================================
        # MASTER (ID = 0)
        # ==========================================================
        if 0 not in self.copy_configs:
            cfg_0 = copy.deepcopy(MASTER_TEMPLATE)
        else:
            cfg_0 = self.copy_configs[0]

        cfg_0["id"] = 0
        rt_0 = cfg_0.get("cmd_state", {})
        rt_0["trading_enabled"] = False
        rt_0["stop_flag"] = False
        rt_0["stop_confirm"] = False
        cfg_0["created_at"] = cfg_0.get("created_at") or now()
        self.copy_configs[0] = cfg_0

        # COPIES — только placeholders
        for cid in range(1, COPY_NUMBER + 1):
            if cid not in self.copy_configs:
                self.copy_configs[cid] = None
            elif self.copy_configs.get(cid):
                self.copy_configs[cid]["enabled"] = False
            # print(f"[INIT] COPY {cid} enabled={self.copy_configs[cid].get('enabled')}")