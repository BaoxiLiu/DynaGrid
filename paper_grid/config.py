"""纸面实验全部参数；修改需保留配置快照并新建实验版本。"""
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "paper_grid_v2"
REST_URL = "https://data-api.binance.vision/api/v3"
WS_URL = "wss://data-stream.binance.vision/stream?streams="
COINS = ("BTC", "ETH", "SOL")
SCENARIOS = {"bnb_discount": ("USDT", 0.00075), "usdc_zero": ("USDC", 0.0)}


@dataclass(frozen=True)
class Settings:
    version: str = "paper-v2"
    capital: float = 1000.0  # 每个情景独立预算，三个币共享
    lot: float = 25.0
    # 初始验收参数，非收益最优值；每币买入名义额，不因卖出返还。
    buy_windows: tuple = ((60, 25.0), (300, 50.0), (900, 100.0))
    recovery_fraction: float = 0.5
    max_inventory: float = 750.0
    max_coin_inventory: float = 250.0
    min_cash: float = 250.0
    tp_vol_k: float = 0.5  # 常态波动补偿倍率，不含库存与实时冲击
    tp_extra_cap: float = 0.005  # 波动补偿上限，手续费底线不受截断
    min_profit: float = 0.0003
    min_spacing: float = 0.0003
    max_spacing: float = 0.03
    vol_k: float = 0.5
    realtime_c: float = 1.0
    inventory_widen: float = 1.0
    coin_inventory_widen: float = 3.0  # 单币占用率平方放宽，初始实验参数
    warmup_seconds: float = 60.0
    stale_seconds: float = 5.0
    shock_floor: float = 0.002
    shock_vol_multiple: float = 2.0  # v1固定+波动代理；尚无历史5秒分位标定
    trend_seconds: float = 300.0
    trend_drop: float = 0.005
    trend_down_fraction: float = 0.65
    pause_min: float = 10.0
    recovery_stable: float = 30.0
    shrink_stable: float = 30.0
    shrink_tau: float = 60.0
    reprice_fraction: float = 1.0
    reprice_confirm: float = 5.0
    widen_reprice: float = 0.30
    min_order_life: float = 2.0
    latency: float = 0.25
    cancel_latency: float = 0.25
    participation: float = 0.10  # 穿价成交量最多10%分配给模拟订单
    report_seconds: float = 3600.0
    snapshot_seconds: float = 10.0
    raw_retention_days: int = 3

    def __post_init__(self):
        if self.tp_vol_k < 0 or self.tp_extra_cap < 0:
            raise ValueError("止盈补偿参数不能为负")
        if not self.buy_windows or any(t <= 0 or limit <= 0 for t, limit in self.buy_windows):
            raise ValueError("接货窗口及额度必须为正")
        if list(self.buy_windows) != sorted(self.buy_windows) or len({t for t, _ in self.buy_windows}) != len(self.buy_windows):
            raise ValueError("接货窗口必须按时间严格递增")
        if not 0 < self.recovery_fraction <= 1:
            raise ValueError("恢复买入比例必须在(0,1]内")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["buy_windows"] = [list(x) for x in self.buy_windows]
        return data
