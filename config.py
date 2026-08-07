"""
全局配置 — 动态网格策略系统
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 代理设置（中国大陆用户如需使用代理，取消注释并填写代理地址）
# PROXY_URL = "http://127.0.0.1:7897"
PROXY_URL = ""

# 尝试从 .env 文件加载 API Key (需要 python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

# ============================================================
# 交易所
# ============================================================
EXCHANGE_NAME = "binance"
EXCHANGE_CONFIG = {
    "apiKey": os.environ.get("BINANCE_API_KEY", ""),
    "secret": os.environ.get("BINANCE_SECRET", ""),
    "options": {
        "defaultType": "spot",
    },
}

def validate_exchange_config():
    """在真正创建交易所连接时校验密钥，避免导入配置就产生副作用。"""
    if not EXCHANGE_CONFIG["apiKey"] or not EXCHANGE_CONFIG["secret"]:
        env_path = os.path.join(BASE_DIR, ".env")
        env_example = os.path.join(BASE_DIR, ".env.example")
        hint_lines = [
            "币安 API Key 未配置！",
        ]
        if not os.path.exists(env_path) and os.path.exists(env_example):
            hint_lines.extend([
                "",
                f"  检测到你是第一次使用本项目。请按以下步骤配置 API 密钥：",
                f"",
                f"  1. 复制模板文件：",
                f"     cp .env.example .env",
                f"",
                f"  2. 编辑 .env 文件，填入你的币安 API Key 和 Secret：",
                f"     BINANCE_API_KEY=你的Key",
                f"     BINANCE_SECRET=你的Secret",
                f"",
                f"  3. 在币安创建 API Key（只需现货权限，不要开提现）：",
                f"     https://www.binance.com/zh-CN/my/settings/api-management",
            ])
        else:
            hint_lines.extend([
                "  方式1: 在 .env 文件中设置 BINANCE_API_KEY 和 BINANCE_SECRET",
                "  方式2: export BINANCE_API_KEY=xxx && export BINANCE_SECRET=yyy",
                "  (如果 .env 不生效, 请运行: pip install python-dotenv)",
            ])
        raise RuntimeError("\n".join(hint_lines))

# ============================================================
# 策略参数
# ============================================================
TOTAL_CAPITAL = 400               # 总资金 (USDT)
MAX_HOLDINGS = 10                 # 最大同时持有币种数 (T1放开到10)
SCAN_INTERVAL_SECONDS = 30        # 扫描间隔 (秒)，降低 REST 轮询频率与限频风险
# 收到 429 但响应中没有给出解封时间时，至少静默等待 60 秒再探测。
RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS = 60
QUOTE_CURRENCY = "USDT"           # 计价货币

# WebSocket 仅用于实时行情观察。真实下单、余额/订单核验仍只由 REST 扫描执行，
# 因此它不会绕过现有的风控、对账和冻结逻辑。
WEBSOCKET_MARKET_DATA_ENABLED = True
WEBSOCKET_OBSERVATION_ONLY = True
WEBSOCKET_STALE_SECONDS = 5
WEBSOCKET_RECONNECT_SECONDS = 5
WEBSOCKET_OBSERVATION_REARM_RATE = 0.001  # 价格回到触发价上方 0.1% 后才允许再次提示
# 到价后最多每 2 秒请求一次提前 REST 扫描，避免触发价附近抖动放大接口请求。
WEBSOCKET_REST_WAKE_DEBOUNCE_SECONDS = 2

# ============================================================
# 币种选择
# ============================================================
# COIN_PRESET 可选: "top10" | "top15" | "top20" | "custom"
#   切换到 "custom" 后，下方 COIN_POOLS["custom"] 里的列表生效
COIN_PRESET = "custom"

COIN_POOLS = {
    "top10": [
        "BTC", "ETH", "BNB", "SOL", "XRP",
        "DOGE", "ADA", "AVAX", "DOT", "LINK",
    ],
    "top15": [
        "BTC", "ETH", "BNB", "SOL", "XRP",
        "DOGE", "ADA", "AVAX", "DOT", "LINK",
        "TRX", "LTC", "UNI", "TON", "NEAR",
    ],
    "top20": [
        "BTC", "ETH", "BNB", "SOL", "XRP",
        "DOGE", "ADA", "AVAX", "DOT", "LINK",
        "TRX", "LTC", "UNI", "TON", "NEAR",
        "SUI", "APT", "ARB", "OP", "ATOM",
    ],
    "custom": [
        "BTC", "ETH", "BNB", "SOL", "XRP",
        "ADA", "AVAX", "DOT", "LINK", "UNI",
        "DOGE", "TRX", "LTC",
    ],
}

# 监控列表：根据 COIN_PRESET 自动推导，也可直接覆盖为自定义列表
MONITOR_COINS = COIN_POOLS.get(COIN_PRESET, COIN_POOLS["custom"])

# 不归策略管理、仅用于账户用途的现货余额上限（单位为币本身）。
# 例如 BNB 用于抵扣手续费；超过额度的余额仍会触发启动对账保护。
NON_STRATEGY_BALANCE_ALLOWANCES = {
    "BNB": 0.25,
}

# 四档动态网格: (金额, 买入触发 vs 基准价偏移, 止盈比例, 市值限制)
# 市值限制: None=无限制, 3=排名前3, 1=排名第1
TIERS = [
    {"amount": 15,  "trigger": 0.00, "tp": 0.010, "max_count": 10, "cap_rank": None},   # T1: 建仓, +1.0%
    {"amount": 20,  "trigger": -0.04, "tp": 0.015, "max_count": 10, "cap_rank": None},   # T2: -4%, +1.5%, 不限名额
    {"amount": 30,  "trigger": -0.12, "tp": 0.030, "max_count": 3,  "cap_rank": 3},       # T3: -12%, +3.0%, 仅前3
    {"amount": 40,  "trigger": -0.30, "tp": 0.100, "max_count": 1,  "cap_rank": 1},       # T4: -30%, +10%, 仅第1
]

# ============================================================
# 风控
# ============================================================
REENTRY_DIP = 0.012      # T1 清仓后至少回踩 1.2% 才允许重新建仓
# T2-T4 卖出后再次买入的最低回调幅度。ATR 仅可在此基础上加深回调，不能缩短。
REENTRY_MIN_PULLBACKS = {
    1: 0.020,  # T2: 至少回调 2.0%
    2: 0.035,  # T3: 至少回调 3.5%
    3: 0.100,  # T4: 至少回调 10.0%
}
# 修改回补计算口径时递增；旧版已保存的触发价会在下一次扫描自动重算。
REENTRY_POLICY_VERSION = 2
MIN_FREE_USDT = 50       # 自由资金低于此值 → 停止新开仓
MIN_FREE_USDT_HARD = 20  # 自由资金低于此值 → 全部冻结
NO_STOP_LOSS = True      # 不止损
ORDER_FEE_BUFFER_RATE = 0.002  # 下单预算预留 0.2% 手续费/滑点缓冲
MAX_CONSECUTIVE_API_FAILURES = 3

# ============================================================
# 市值排名刷新
# ============================================================
MARKET_CAP_REFRESH_INTERVAL = 1800  # 每30分钟刷新一次市值排名

# ============================================================
# 动态网格波动率
# ============================================================
ATR_TIMEFRAME = "1h"
ATR_PERIOD = 14
ATR_BASELINE_DAYS = 30
ATR_VOL_RATIO_MIN = 1.0  # 低波动不缩小基础回调；ATR 只允许加深回调
ATR_VOL_RATIO_MAX = 1.3
ATR_RETRY_SECONDS = 300

# ============================================================
# 文件路径 — 所有运行时产物统一放在 data/ 目录下
# ============================================================
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
FLAGS_DIR = os.path.join(DATA_DIR, "flags")

# 确保目录存在 (导入时自动创建)
for _dir in (DATA_DIR, LOGS_DIR, FLAGS_DIR):
    os.makedirs(_dir, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "state.json")
STATE_BACKUP_FILE = os.path.join(DATA_DIR, "state.json.bak")
STATE_LOCK_FILE = os.path.join(DATA_DIR, "state.json.lock")
RECONCILE_REPORT_FILE = os.path.join(DATA_DIR, "reconcile_report.json")
EQUITY_FILE = os.path.join(DATA_DIR, "equity.csv")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "trades.csv")
DAILY_SUMMARY_FILE = os.path.join(DATA_DIR, "daily_summary.json")
LOG_FILE = os.path.join(LOGS_DIR, "system.log")
LOG_MAX_DAYS = 7                    # 日志保留天数

# ============================================================
# 通知
# ============================================================
DAILY_SUMMARY_TIME = "08:00"        # 每天8点发日报 (北京时间)

# 通知渠道（webhook URL，留空则不发送）
NOTIFICATION_CHANNELS = {
    "feishu":    "",  # 飞书: https://open.feishu.cn/open-apis/bot/v2/hook/xxx
    "telegram":  "",  # Telegram: https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>
    "wecom":     "",  # 企业微信: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
}

# ============================================================
# 异常告警阈值
# ============================================================
ALERT_BTC_DAILY_CHANGE = 0.15       # BTC 单日波动超 15% → 告警
ALERT_MULTI_TRIGGER_RATIO = 0.7     # 70% 监控币种同时触发 Tier2 → 告警

# 运行状态 (通过文件跨进程通信)
PAUSE_FLAG_FILE = os.path.join(FLAGS_DIR, "pause.flag")
FREEZE_BUYS_FLAG_FILE = os.path.join(FLAGS_DIR, "freeze_buys.flag")
INSTANCE_LOCK_FILE = os.path.join(DATA_DIR, "strategy.lock")
