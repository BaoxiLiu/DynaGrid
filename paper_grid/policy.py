"""纯决策模块：行情信号与买入额度，不访问网络、磁盘或订单执行。"""
from collections import deque
from .config import Settings


def combine(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return (max(left[0], right[0]), min(left[1], right[1]),
            max(left[2], right[2], 1 - right[1] / left[0]))


class RollingRange:
    """双栈滑窗：精确保留高低点先后顺序，摊销O(1)计算最大回撤。"""
    def __init__(self, seconds):
        self.seconds = seconds
        self.front, self.back = [], []

    def push(self, now, price):
        agg = combine(self.back[-1][2] if self.back else None, (price, price, 0.0))
        self.back.append((now, price, agg))
        while True:
            if not self.front:
                while self.back:
                    at, p, _ = self.back.pop()
                    agg = combine((p, p, 0.0), self.front[-1][2] if self.front else None)
                    self.front.append((at, p, agg))
            if self.front and self.front[-1][0] < now - self.seconds:
                self.front.pop()
            else:
                break
        return combine(self.front[-1][2] if self.front else None,
                       self.back[-1][2] if self.back else None)


class Signal:
    def __init__(self):
        self.range60, self.range5 = RollingRange(60), RollingRange(5)
        self.seconds = deque()
        self.candles = deque(maxlen=30)
        self.candle_end = 0.0
        self.bid = self.ask = self.at = 0.0
        self.first = 0.0
        self.spacing = 0.0
        self.last_calc = 0.0
        self.shrink_since = 0.0
        self.paused = False
        self.pause_until = 0.0
        self.stable_since = 0.0
        self.pause_low = float("inf")
        self.reason = "warmup"
        self.quote_id = 0
        self.last_trade_id = 0
        self.last_trade_at = 0.0
        self.up_since = 0.0
        self.negative_seconds = 0
        self.recovery = False

    def candle(self, end: float, opening: float, high: float, low: float):
        if end <= self.candle_end or opening <= 0:
            return
        self.candles.append((high - low) / opening)
        self.candle_end = end

    def update(self, bid: float, ask: float, now: float, cfg: Settings):
        gap = self.at and now - self.at > cfg.stale_seconds
        gap_drop = max(0.0, 1 - (bid + ask) / (self.bid + self.ask)) if gap else 0.0
        if gap:
            self.stable_since = 0.0
        self.bid, self.ask, self.at = bid, ask, now
        mid = (bid + ask) / 2
        if not self.first:
            self.first = now
        hi, lo, _ = self.range60.push(now, mid)
        peak5, _, mdd5 = self.range5.push(now, mid)
        second = int(now)
        if not self.seconds or self.seconds[-1][0] != second:
            if len(self.seconds) >= 2:
                self.negative_seconds += int(self.seconds[-1][1] < self.seconds[-2][1])
            self.seconds.append((second, mid))
        else:
            self.seconds[-1] = (second, mid)
        while len(self.seconds) > 1 and self.seconds[0][0] < now - cfg.trend_seconds:
            self.negative_seconds -= int(self.seconds[1][1] < self.seconds[0][1]) if len(self.seconds) > 2 else 0
            self.seconds.popleft()
        vals = list(self.candles)
        v = (0.8 * sum((i + 1) * x for i, x in enumerate(vals[-5:])) / 15
             + 0.2 * sum(vals) / len(vals)) if len(vals) == 30 else 0.0
        a60 = hi / lo - 1
        d5 = 1 - mid / peak5 if peak5 else 0.0
        # v1是可审计的起始阈值；记录所有信号供后续按同口径校准。
        threshold = max(cfg.shock_floor, cfg.shock_vol_multiple * v)
        trend_ready = bool(self.seconds and now - self.seconds[0][0] >= cfg.trend_seconds - 2)
        down_fraction = max(0, self.negative_seconds) / max(1, len(self.seconds) - 2)
        trend = trend_ready and 1 - mid / self.seconds[0][1] >= cfg.trend_drop and down_fraction >= cfg.trend_down_fraction
        shock = max(mdd5, gap_drop) >= threshold
        if shock or trend:
            self.paused = True
            self.pause_until = max(self.pause_until, now + cfg.pause_min)
            self.stable_since = 0.0
            self.pause_low = min(self.pause_low, mid)
            self.reason = "shock" if shock else "downtrend"
        elif self.paused:
            if mid < self.pause_low or d5 > threshold / 2:
                self.stable_since = 0.0
                self.pause_low = min(self.pause_low, mid)
            elif not self.stable_since:
                self.stable_since = now
            if self.stable_since and now >= self.pause_until and now - self.stable_since >= cfg.recovery_stable:
                self.paused = False
                self.recovery = True
                self.pause_low = float("inf")
        ready = now - self.first >= cfg.warmup_seconds and len(vals) == 30 and now - self.candle_end < 120
        if not self.paused:
            self.reason = "ready" if ready else "warmup"
        self.metrics = {"v": v, "a60": a60, "d5": d5, "mdd5": mdd5,
                        "shock_threshold": threshold, "down_fraction": down_fraction}
        return ready and not self.paused, max(v, cfg.realtime_c * a60)


class BuyBudget:
    """每币一个逻辑买入档；订单撤换不重置额度。成交按秒聚合，最多保留最长窗口。"""
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.entries: dict = {}
        self.history: dict = {}

    def prune(self, symbol: str, now: float):
        rows = self.history.setdefault(symbol, deque())
        while rows and rows[0][0] < int(now) - self.cfg.buy_windows[-1][0]:
            rows.popleft()
        return rows

    def allowance(self, symbol: str, now: float, reserved: float = 0.0) -> float:
        rows = self.prune(symbol, now)
        # 整秒桶到秒末才过期，最多多限制一秒，不提前释放额度。
        return max(0.0, min(limit - sum(v for t, v in rows if t + 1 > now - seconds)
                            for seconds, limit in self.cfg.buy_windows) - reserved)

    def start(self, symbol: str, reduced: bool = False):
        if symbol not in self.entries:
            self.entries[symbol] = {"remaining": self.cfg.lot * (self.cfg.recovery_fraction if reduced else 1),
                                    "lot_ids": [], "order_cap": self.cfg.lot}
        if reduced:
            self.entries[symbol]["order_cap"] = self.cfg.lot * self.cfg.recovery_fraction
        return self.entries[symbol]

    def record(self, symbol: str, lot_id: str, amount: float, now: float):
        entry = self.entries[symbol]
        entry["remaining"] = max(0.0, entry["remaining"] - amount)
        if lot_id not in entry["lot_ids"]:
            entry["lot_ids"].append(lot_id)
        rows = self.prune(symbol, now)
        second = int(now)
        if rows and rows[-1][0] == second:
            rows[-1] = (second, rows[-1][1] + amount)
        else:
            rows.append((second, amount))

    def snapshot(self) -> dict:
        return {"entries": self.entries, "history": {k: list(v) for k, v in self.history.items()}}

    def restore(self, data: dict):
        self.entries = data["entries"]
        self.history = {k: deque(v) for k, v in data["history"].items()}


def inventory_policy(cfg: Settings, occupied: float) -> tuple[float, float]:
    """返回单币间距倍率和固定每档名义额度；occupied含持仓成本与未终结买单预留。"""
    ratio = min(1.0, max(0.0, occupied / cfg.max_coin_inventory))
    return 1 + cfg.coin_inventory_widen * ratio * ratio, cfg.lot


def take_profit_spacing(cfg: Settings, fee: float, candles) -> tuple[float, float]:
    """30根已收盘分钟振幅均值；缺样本时只用扣费净利润底线。"""
    normal_vol = sum(candles) / len(candles) if len(candles) == 30 else 0.0
    floor = (1 + fee) * (1 + cfg.min_profit) / (1 - fee) - 1
    return floor + min(cfg.tp_extra_cap, cfg.tp_vol_k * normal_vol), normal_vol
