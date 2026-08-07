"""统一资金检查，所有买入必须经过这里。"""
from dataclasses import dataclass

from config import (
    MIN_FREE_USDT,
    MIN_FREE_USDT_HARD,
    ORDER_FEE_BUFFER_RATE,
    TOTAL_CAPITAL,
)
from state import StateManager


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, state: StateManager):
        self._state = state

    def can_buy(self, coin: str, amount: float, free_usdt: float | None,
                is_new: bool) -> RiskDecision:
        if self._state.is_blocked(coin):
            return RiskDecision(False, f"{coin} 已因对账异常冻结")
        if free_usdt is None or free_usdt < 0:
            return RiskDecision(False, "可用余额未知")
        if amount <= 0:
            return RiskDecision(False, "买入金额必须大于零")

        fee_buffer = amount * ORDER_FEE_BUFFER_RATE
        post_trade_free = free_usdt - amount - fee_buffer
        if post_trade_free < MIN_FREE_USDT_HARD:
            return RiskDecision(
                False,
                f"买入后可用资金 ${post_trade_free:.2f} 将低于硬下限 "
                f"${MIN_FREE_USDT_HARD:.2f}",
            )

        committed_after = self._state.get_total_committed_all() + amount
        if committed_after > TOTAL_CAPITAL:
            return RiskDecision(
                False,
                f"买入后活动投入 ${committed_after:.2f} 将超过预算 ${TOTAL_CAPITAL:.2f}",
            )

        if is_new and post_trade_free < MIN_FREE_USDT:
            return RiskDecision(
                False,
                f"新开仓后可用资金 ${post_trade_free:.2f} 将低于软下限 "
                f"${MIN_FREE_USDT:.2f}",
            )

        return RiskDecision(True)
