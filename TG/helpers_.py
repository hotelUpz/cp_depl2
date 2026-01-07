# TG.helpers_.py

from __future__ import annotations
from typing import *

from a_config import (
    COPY_NUMBER,
    CMD_TTL,
    REQUIRE_PROXY
)
from c_utils import Utils, now

if TYPE_CHECKING:
    from b_context import MainContext


# =====================================================================
#                VALIDATION HELPERS
# =====================================================================

def validate_exchange(
    cfg: dict,
    *,
    require_proxy: bool = False,
) -> Optional[str]:
    ex = cfg.get("exchange", {})

    for key in ("api_key", "api_secret", "uid"):
        if not ex.get(key):
            return f"{key} не заполнен"

    if require_proxy and (not ex.get("proxy") or ex.get("proxy") == "0"):
        return "proxy обязателен"

    return None

def validate_master(cfg: Dict):
    return validate_exchange(cfg, require_proxy=False)

def validate_copy(cfg: Dict):
    if cfg is None:
        return "Аккаунт не инициализирован"
    rnd = cfg.get("random_size_pct")
    if not isinstance(rnd, (list, tuple)) or len(rnd) != 2:
        return "random_size_pct должен быть формата (min, max)"
    delay_ms = cfg.get("delay_ms")
    if not isinstance(delay_ms, (list, tuple)) or len(delay_ms) != 2:
        return "delay_ms должен быть формата (min, max)"
    return validate_exchange(cfg, require_proxy=REQUIRE_PROXY)


# # =====================================================================
# #                  RANGE PARSER FOR DELETE
# # =====================================================================

def parse_id_range(raw: str, allow_zero: bool = False) -> List[int]:
    """
    Поддерживает форматы (разделитель — ПРОБЕЛ):
        1
        1 3 5
        2-6
        1-3 5 8-4

    • лишние пробелы игнорируются
    • диапазоны могут быть обратные (8-4)
    • allow_zero=True → разрешает ID=0 (мастер)
    """

    if not raw or not raw.strip():
        raise ValueError("empty input")

    # ❌ запятые запрещены — явная ошибка формата
    if "," in raw:
        raise ValueError("comma is not allowed")

    tokens = raw.strip().split()
    result: set[int] = set()

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        # поддержка всех видов тире
        token = token.replace("–", "-").replace("—", "-")

        if "-" in token:
            try:
                a_str, b_str = token.split("-", 1)
                a = int(a_str)
                b = int(b_str)
            except Exception:
                raise ValueError(f"invalid range: {token}")

            lo, hi = sorted((a, b))
            for cid in range(lo, hi + 1):
                if cid == 0:
                    if allow_zero:
                        result.add(0)
                elif 1 <= cid <= COPY_NUMBER:
                    result.add(cid)
        else:
            try:
                cid = int(token)
            except Exception:
                raise ValueError(f"invalid id: {token}")

            if cid == 0:
                if allow_zero:
                    result.add(0)
            elif 1 <= cid <= COPY_NUMBER:
                result.add(cid)

    if not result:
        raise ValueError("no valid ids")

    return sorted(result)

# # =====================================================================
# #                  
# # =====================================================================
def _mask_secret(
    val: Optional[str],
    *,
    head: int = 4,
    tail: int = 4,
) -> str:
    if not val or val in ("", "0"):
        return "—"

    if len(val) <= head + tail:
        return "***"

    return f"{val[:head]}***{val[-tail:]}"


def format_status(cfg: dict) -> str:
    """
    Текстовый статус для MASTER / COPY.
    Без HTML. Без fallback-логики. Все секреты замаскированы.
    """

    lines: list[str] = []

    # ========================
    # ROLE / ID / NAME
    # ========================
    role = (cfg.get("role") or "copy").upper()
    acc_id = cfg.get("id")
    name = cfg.get("name")

    enabled = bool(cfg.get("enabled", False))
    icon = "🟢" if enabled else "⚪"

    lines.append(f"{icon} Role: {role} (ID={acc_id})")
    if name is not None:
        lines.append(f"Name: {name}")

    # ========================
    # EXCHANGE (ВСЕ МАСКИРОВАНО)
    # ========================
    ex = cfg.get("exchange", {}) or {}

    lines.append("")
    lines.append("Exchange:")
    lines.append(f"  • api_key: {_mask_secret(ex.get('api_key'))}")
    lines.append(f"  • api_secret: {_mask_secret(ex.get('api_secret'))}")
    lines.append(f"  • uid: {_mask_secret(ex.get('uid'))}")
    lines.append(f"  • proxy: {_mask_secret(ex.get('proxy'), head=6, tail=9)}")

    # ========================
    # RUNTIME (MASTER)
    # ========================
    rt = cfg.get("cmd_state")
    if isinstance(rt, dict):
        lines.append("")
        lines.append("Runtime:")
        for k in sorted(rt):
            lines.append(f"  • {k}: {rt[k]}")

    # ========================
    # COPY SETTINGS (ВСЕ ПОЛЯ, None → строка)
    # ========================
    if role == "COPY":
        lines.append("")
        lines.append("Copy Settings:")
        lines.append(f"  • coef: {cfg.get('coef')}")
        lines.append(f"  • leverage: {cfg.get('leverage')}")
        lines.append(f"  • margin_mode: {cfg.get('margin_mode')}")
        lines.append(f"  • max_position_size: {cfg.get('max_position_size')}")
        lines.append(f"  • random_size_pct: {cfg.get('random_size_pct')}")
        lines.append(f"  • delay_ms: {cfg.get('delay_ms')}")
        lines.append(f"  • enabled: {enabled}")

    # ========================
    # CREATED AT
    # ========================
    ts = cfg.get("created_at")
    lines.append("")
    lines.append(
        f"Created at: {Utils.milliseconds_to_datetime(ts)}"
        if ts else
        "Created at: —"
    )

    return "\n".join(lines)

def can_push_cmd(mc: "MainContext") -> bool:
    now_ts = now()
    last = mc.last_cmd_ts or 0

    if now_ts - last < CMD_TTL:
        return False

    mc.last_cmd_ts = now_ts
    return True


# =====================================================================
#           UNIQUE EXCHANGE ACCOUNT VALIDATION
# =====================================================================

def _account_fingerprint(cfg: dict) -> Optional[tuple]:
    """
    Возвращает уникальный fingerprint аккаунта:
    (api_key, uid)
    """
    ex = cfg.get("exchange") or {}
    api_key = ex.get("api_key")
    uid = ex.get("uid")

    if not api_key or not uid:
        return None

    return (api_key, uid)


def find_duplicate_accounts(
    mc: "MainContext",
) -> Dict[tuple, List[int]]:
    """
    Ищет дубли аккаунтов между master и copies.

    Возвращает:
    {
        (api_key, uid): [0, 2, 5]
    }
    """
    seen: Dict[tuple, List[int]] = {}

    for cid, cfg in mc.copy_configs.items():
        if not cfg:
            continue
        fp = _account_fingerprint(cfg)
        if not fp:
            continue
        seen.setdefault(fp, []).append(cid)

    return {fp: ids for fp, ids in seen.items() if len(ids) > 1}


def validate_unique_accounts(mc: "MainContext") -> Optional[str]:
    """
    Проверяет, что все аккаунты уникальны.
    Возвращает текст ошибки или None.
    """
    dups = find_duplicate_accounts(mc)
    if not dups:
        return None

    lines = ["❌ Обнаружены одинаковые аккаунты:"]
    for (api_key, uid), ids in dups.items():
        ids_str = ", ".join(map(str, ids))
        uid_masked = _mask_secret(str(uid), 4, 4)
        lines.append(f"• UID={uid_masked} используется в ID: {ids_str}")

    lines.append("")
    lines.append("Один аккаунт может быть использован только один раз.")
    return "\n".join(lines)


# =====================================================================
#                   MX CREDENTIAL PARSER
# =====================================================================

def parse_mx_credentials(raw: str) -> tuple[dict, Optional[str]]:
    """
    api_key
    api_secret
    uid
    proxy (optional)
    """
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    if len(lines) < 3:
        return {}, "Нужно минимум 3 строки: api_key, api_secret, uid"

    api_key, api_secret, uid = lines[:3]
    proxy = None

    if len(lines) >= 4:
        p = lines[3]
        if "://" not in p:
            try:
                ip, port, user, pwd = p.split(":", 3)
                proxy = f"http://{user}:{pwd}@{ip}:{port}"
            except Exception:
                return {}, "Неверный формат proxy"
        else:
            proxy = p

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "uid": uid,
        "proxy": proxy,
    }, None
