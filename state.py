"""
持仓状态持久化 — JSON 文件读写
"""
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from dataclasses import fields
from typing import Any

import fcntl

from config import (
    REENTRY_DIP,
    STATE_BACKUP_FILE,
    STATE_FILE,
    STATE_LOCK_FILE,
)
from logger import log


class StateCorruptionError(RuntimeError):
    pass


@dataclass
class TierState:
    tier_index: int           # 0-3
    amount_usdt: float        # 投入金额
    buy_price: float          # 买入价
    sell_price: float         # 止盈价
    qty: float                # 持有数量
    sell_order_id: str        # 限价卖单ID
    status: str = "holding"   # "holding" | "sold" | "dust"
    buy_order_id: str = ""
    buy_client_order_id: str = ""
    sell_client_order_id: str = ""
    sell_filled_qty: float = 0.0
    sell_fee: float = 0.0
    buy_fee_usdt: float = 0.0


@dataclass
class TierReentryState:
    tier_index: int
    next_trigger_price: float = 0.0
    last_buy_price: float = 0.0
    anchor_sell_price: float = 0.0
    atr: float = 0.0
    current_natr: float = 0.0
    median_natr: float = 0.0
    raw_vol_ratio: float = 0.0
    effective_vol_ratio: float = 0.0
    neutral_gap_rate: float = 0.0
    applied_gap_rate: float = 0.0
    policy_version: int = 0
    calculated_at: float = 0.0
    status: str = "ready"  # "ready" | "unavailable"
    error: str = ""


@dataclass
class CoinState:
    coin: str
    base_price: float         # 基准价 (Tier1 买入价)
    tiers: list[TierState] = field(default_factory=list)
    tier_reentries: dict[int, TierReentryState] = field(default_factory=dict)
    active: bool = True       # 是否仍在监控中


@dataclass
class StrategyState:
    coins: dict[str, CoinState] = field(default_factory=dict)
    cooling: dict[str, float] = field(default_factory=dict)  # coin → 清仓价
    total_trades: int = 0
    total_pnl: float = 0.0
    last_scan: float = 0.0
    updated_at: str = ""
    schema_version: int = 3
    next_order_seq: int = 0
    pending_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked_coins: dict[str, str] = field(default_factory=dict)


@contextmanager
def _exclusive_file_lock():
    os.makedirs(os.path.dirname(STATE_LOCK_FILE), exist_ok=True)
    with open(STATE_LOCK_FILE, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: str, content: str):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


class StateManager:
    def __init__(self):
        self._state = StrategyState()
        self._lock = threading.RLock()

    @property
    def coins(self) -> dict[str, CoinState]:
        return self._state.coins

    @property
    def holding_count(self) -> int:
        return sum(
            1 for c in self._state.coins.values()
            if any(t.status == "holding" for t in c.tiers)
        )

    def get_holding_coins(self) -> list[str]:
        """返回当前持有中的币种列表"""
        return [
            coin for coin, state in self._state.coins.items()
            if any(t.status == "holding" for t in state.tiers)
        ]

    def get_active_coin_states(self) -> list[CoinState]:
        return [s for s in self._state.coins.values() if s.active]

    def is_holding(self, coin: str) -> bool:
        return (
            coin in self._state.coins
            and any(t.status == "holding" for t in self._state.coins[coin].tiers)
        )

    def has_unfilled_tiers(self, coin: str) -> bool:
        """该币种还有未触发的 tier 吗"""
        if coin not in self._state.coins:
            return True
        cs = self._state.coins[coin]
        filled = len(cs.tiers)
        return filled < 4

    def add_coin(self, coin: str, base_price: float):
        """新建一个币种"""
        existing = self._state.coins.get(coin)
        if existing and any(t.status == "holding" for t in existing.tiers):
            raise RuntimeError(f"{coin} 仍有活动持仓，拒绝覆盖本地状态")
        self._state.coins[coin] = CoinState(
            coin=coin,
            base_price=base_price,
            tiers=[],
            active=True,
        )

    def add_tier(self, coin: str, tier_index: int, amount_usdt: float,
                 buy_price: float, sell_price: float, qty: float, order_id: str,
                 buy_order_id: str = "", buy_client_order_id: str = "",
                 sell_client_order_id: str = "", buy_fee_usdt: float = 0.0):
        """记录一个新 tier 成交"""
        cs = self._state.coins[coin]
        cs.tiers.append(TierState(
            tier_index=tier_index,
            amount_usdt=amount_usdt,
            buy_price=buy_price,
            sell_price=sell_price,
            qty=qty,
            sell_order_id=order_id,
            status="holding",
            buy_order_id=buy_order_id,
            buy_client_order_id=buy_client_order_id,
            sell_client_order_id=sell_client_order_id,
            buy_fee_usdt=buy_fee_usdt,
        ))

    def set_tier_reentry(self, coin: str, reentry: TierReentryState):
        if coin not in self._state.coins:
            raise KeyError(f"币种状态不存在: {coin}")
        self._state.coins[coin].tier_reentries[reentry.tier_index] = reentry

    def get_tier_reentry(self, coin: str,
                         tier_index: int) -> TierReentryState | None:
        cs = self._state.coins.get(coin)
        return cs.tier_reentries.get(tier_index) if cs else None

    def clear_tier_reentry(self, coin: str, tier_index: int):
        cs = self._state.coins.get(coin)
        if cs:
            cs.tier_reentries.pop(tier_index, None)

    def mark_tier_sold(self, coin: str, tier_index: int):
        """标记某 tier 已卖出"""
        cs = self._state.coins[coin]
        for t in cs.tiers:
            if t.tier_index == tier_index and t.status == "holding":
                t.status = "sold"
                break

    def retire_dust_tier(self, coin: str, tier_index: int,
                          exit_price: float) -> bool:
        """将无法交易的剩余数量移出策略管理，必要时进入冷却。

        尘埃币仍保留在交易所余额中，但不再被记作策略持仓或可挂单数量。
        返回该币种是否已无其他活动仓位并被移入冷却。
        """
        cs = self._state.coins.get(coin)
        if cs is None:
            return False
        for t in cs.tiers:
            if t.tier_index == tier_index and t.status == "holding":
                t.status = "dust"
                t.sell_order_id = ""
                t.sell_client_order_id = ""
                break
        else:
            return False

        self._state.blocked_coins.pop(coin, None)
        if any(t.status == "holding" for t in cs.tiers):
            return False
        del self._state.coins[coin]
        self._state.cooling[coin] = exit_price
        return True

    def is_coin_fully_sold(self, coin: str) -> bool:
        """该币种所有 tier 是否都已卖出"""
        cs = self._state.coins[coin]
        if not cs.tiers:
            return False
        return all(t.status == "sold" for t in cs.tiers)

    def remove_coin(self, coin: str):
        """移除币种 (全部清仓后), 但不进冷却 (兼容旧逻辑)"""
        if coin in self._state.coins:
            del self._state.coins[coin]

    # ---- 冷却机制 ----

    def add_cooling(self, coin: str, exit_price: float):
        """币种全清仓 → 加入冷却, 必须回踩才能重新买入"""
        if coin in self._state.coins:
            del self._state.coins[coin]
        self._state.cooling[coin] = exit_price
        log.info(f"[COOLING] {coin} 清仓价 ${exit_price:.4f}, 需回踩 {REENTRY_DIP*100:.1f}% 后才能再买")

    def check_cooling(self, coin: str, current_price: float) -> bool:
        """检查冷却是否解除. True=冷却中(不能买), False=已解除"""
        if coin not in self._state.cooling:
            return False
        if current_price <= 0:
            log.warning(f"[COOLING] {coin} 行情无效，保持冷却")
            return True
        exit_price = self._state.cooling[coin]
        threshold = exit_price * (1 - REENTRY_DIP)
        if current_price <= threshold:
            del self._state.cooling[coin]
            log.info(f"[COOLING] {coin} 回踩到位 ${current_price:.4f} ≤ ${threshold:.4f}, 解除冷却")
            return False
        return True

    def is_cooling(self, coin: str) -> bool:
        return coin in self._state.cooling

    def get_cooling_prices(self) -> dict[str, float]:
        """返回冷却价快照，供只读展示/观察逻辑使用。"""
        with self._lock:
            return dict(self._state.cooling)

    def get_holding_tiers(self, coin: str) -> list[TierState]:
        """获取某币种仍在持有中的 tiers"""
        if coin not in self._state.coins:
            return []
        return [t for t in self._state.coins[coin].tiers if t.status == "holding"]

    def get_total_committed_usdt(self, coin: str) -> float:
        """某币种已投入的总 USDT"""
        return sum(
            t.amount_usdt
            for t in self._state.coins[coin].tiers
            if t.status == "holding"
        )

    def get_total_committed_all(self) -> float:
        """所有币种已投入的总 USDT"""
        return sum(
            sum(t.amount_usdt for t in cs.tiers if t.status == "holding")
            for cs in self._state.coins.values()
        )

    def get_total_recorded_usdt(self) -> float:
        """包括已卖出历史档位在内的累计名义金额，仅用于审计。"""
        return sum(
            sum(t.amount_usdt for t in cs.tiers)
            for cs in self._state.coins.values()
        )

    def new_client_order_id(self, coin: str, tier_index: int, side: str) -> str:
        self._state.next_order_seq += 1
        seq = self._state.next_order_seq
        return f"asg-{int(time.time())}-{seq}-{coin}-{side[0]}{tier_index+1}"[:36]

    def begin_order(self, client_order_id: str, **details):
        self._state.pending_orders[client_order_id] = {
            "client_order_id": client_order_id,
            "status": "intent",
            "created_at": time.time(),
            **details,
        }
        self.save()

    def update_order_intent(self, client_order_id: str, **updates):
        order = self._state.pending_orders.get(client_order_id)
        if order is None:
            return
        order.update(updates)
        order["updated_at"] = time.time()
        self.save()

    def complete_order_intent(self, client_order_id: str):
        if self._state.pending_orders.pop(client_order_id, None) is not None:
            self.save()

    def get_pending_orders(self) -> list[dict[str, Any]]:
        return list(self._state.pending_orders.values())

    def block_coin(self, coin: str, reason: str):
        self._state.blocked_coins[coin] = reason

    def unblock_coin(self, coin: str):
        self._state.blocked_coins.pop(coin, None)

    def is_blocked(self, coin: str) -> bool:
        return coin in self._state.blocked_coins

    @property
    def blocked_coins(self) -> dict[str, str]:
        return dict(self._state.blocked_coins)

    def save(self):
        """持久化到文件"""
        with self._lock, _exclusive_file_lock():
            self._state.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._state.last_scan = time.time()
            payload = json.dumps(asdict(self._state), indent=2, default=str)

            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        previous = f.read()
                    json.loads(previous)
                    _atomic_write_text(STATE_BACKUP_FILE, previous)
                except (OSError, json.JSONDecodeError) as e:
                    log.error(f"当前状态文件无法备份: {e}")

            _atomic_write_text(STATE_FILE, payload)
            log.debug(f"状态已原子保存 ({len(self._state.coins)} 个币种)")

    def load(self) -> bool:
        """从文件恢复状态"""
        if not os.path.exists(STATE_FILE) and not os.path.exists(STATE_BACKUP_FILE):
            log.info("未找到历史状态文件，从零开始")
            return False

        errors = []
        for path in (STATE_FILE, STATE_BACKUP_FILE):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._load_data(data)
                if path == STATE_BACKUP_FILE:
                    log.critical("主状态文件损坏，已从备份恢复；启动后必须先对账")
                log.info(f"状态已恢复: {len(self._state.coins)} 个币种, "
                         f"{sum(len(cs.tiers) for cs in self._state.coins.values())} 个 tiers")
                return True
            except Exception as e:
                errors.append(f"{path}: {e}")

        raise StateCorruptionError("状态和备份均无法读取；拒绝按空状态启动。" + " | ".join(errors))

    def _load_data(self, data: dict):
        state = StrategyState()
        state.total_trades = data.get("total_trades", 0)
        state.total_pnl = data.get("total_pnl", 0.0)
        state.updated_at = data.get("updated_at", "")
        state.last_scan = data.get("last_scan", 0.0)
        state.cooling = data.get("cooling", {})
        state.schema_version = max(3, int(data.get("schema_version", 1)))
        state.next_order_seq = data.get("next_order_seq", 0)
        state.pending_orders = data.get("pending_orders", {})
        state.blocked_coins = data.get("blocked_coins", {})

        tier_fields = {f.name for f in fields(TierState)}
        reentry_fields = {f.name for f in fields(TierReentryState)}
        for coin, cs_data in data.get("coins", {}).items():
            cs = CoinState(
                coin=coin,
                base_price=cs_data["base_price"],
                active=cs_data.get("active", True),
            )
            for t_data in cs_data.get("tiers", []):
                compatible = {k: v for k, v in t_data.items() if k in tier_fields}
                cs.tiers.append(TierState(**compatible))
            for raw_index, reentry_data in cs_data.get("tier_reentries", {}).items():
                tier_index = int(raw_index)
                compatible = {
                    k: v for k, v in reentry_data.items() if k in reentry_fields
                }
                compatible["tier_index"] = tier_index
                cs.tier_reentries[tier_index] = TierReentryState(**compatible)
            state.coins[coin] = cs
        self._state = state
