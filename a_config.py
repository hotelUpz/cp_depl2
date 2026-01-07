# a_config.py

from typing import *
import os
from dotenv import load_dotenv

load_dotenv()

# --- CORE --- 
COPY_NUMBER:          int = 100     # количество копируемых аккаунтов
BLACK_SYMBOLS:        dict = {}   # черный список монет. {"COIN_USDT",}
IS_REPORT:            bool = True
TG_LOG_TTL_MS:        int = 10 * 1000
SPEC_TTL:             int = 15 * 1000    #  ms. период обновления инструментов. Важно успеть обновить для новых монет
SESSION_TTL:          int = 30 * 1000    # ms таймаут на генерацию сессии
CMD_TTL:              int = 0.25 * 1000     # ms таймаут командной кнопки
REQUESTS_DELAY:       int = 0.2 * 1000   # тайм-аут между действиями на бирже внутри аккаунта
REQUIRE_PROXY:        bool = False       # прокси обязательный | необязательный параметр
FALLBACK_LEVERAGE:    int = 5     # дефолтное плечо
FALLBACK_MARGIN_MODE: int = 2     # 1 -- ISOLATED, 2 -- CROSSED
QUOTA_ASSET:          str = "USDT"


# --- SECRETS CONFIG ---                 
TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN") # бот-токен. Создаем в Bot Father
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

# ============================================================
#     ЛОГИРОВАНИЕ: ВКЛ/ВЫКЛ УРОВНЕЙ. Отчеты в папке ./logs
# ============================================================
LOG_DEBUG:     bool = True   # необязатльеный уровень логирования
LOG_INFO:      bool = False    # необязатльеный уровень логирования
LOG_WARNING:   bool = True    # обязатльеный уровень логирования
LOG_ERROR:     bool = True    # обязатльеный уровень логирования
MAX_LOG_LINES: int = 5000     # приблизительный размер длины лог файлов


# -- UTILS ---
TIME_ZONE:     str = "UTC"
PRECISION:     int = 18 # -- точность округления для малых чисел
PING_URL:      str = "https://contract.mexc.com/api/v1/contract/ping"
# PING_INTERVAL: float = 15 # sec
PING_INTERVAL: float = 50 # sec