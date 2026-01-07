# COPY.helpers_.py

from __future__ import annotations

from typing import *
from b_context import PosVarTemplate

if TYPE_CHECKING:
    from MASTER.payload_ import MasterEvent


# ======================================================================
# POSITION ACCESS
# ======================================================================
def get_cid_pos(rt: dict) -> dict:
    return rt.setdefault("position_vars", {})

def get_cid_symbol_pos(rt: dict, symbol: str, side: str) -> dict:
    pv_root = get_cid_pos(rt)
    sym = pv_root.setdefault(symbol, {})
    if side not in sym:
        sym[side] = PosVarTemplate.base_template()
    return sym[side]

# ==================================================
# LATENCY (DEBUG PRINT ONLY)
# ==================================================
def record_latency(
    cid: int,
    mev: "MasterEvent",
    res: Optional[dict],
) -> None:
    """
    Debug-only latency print.
    No storage, no side effects.
    """

    if not res or not isinstance(res, dict):
        return

    master_ts = getattr(mev, "ts", None)
    if not master_ts:
        return

    copy_ts = res.get("ts")
    if not copy_ts:
        return

    latency = copy_ts - master_ts

    print(
        f"[LATENCY]"
        f" cid={cid}"
        f" {mev.symbol}"
        f" {mev.pos_side}"
        f" latency={latency}ms"
        # f" master_ts={master_ts}"
        # f" copy_ts={copy_ts}"
    )  
