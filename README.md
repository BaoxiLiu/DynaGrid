# DynaGrid — 基于币安现货的动态网格交易系统

A multi-tier dynamic grid trading bot for Binance Spot. Buys the dip across four tiers (0%, -4%, -12%, -30%), adapts re-entry levels with ATR/NATR volatility, and ranks coins by market cap for position sizing.

**特点 / Features**
- **四档动态网格** — 越跌越买，从建仓到 -30% 深跌分四档加仓
- **ATR 自适应回调** — T2-T4 止盈后，动态计算再入场触发价，波动率越高回调越深
- **市值排序择优** — T3/T4 仅对市值排名靠前的币种开放，控制风险
- **启动对账** — 每次启动核对本地状态 vs 交易所余额/订单，任何不一致冻结交易
- **WebSocket 观察 + REST 复核** — 实时行情到价唤醒主循环，但所有下单走 REST 完整风控
- **终端仪表盘** — ANSI 彩色仪表盘，实时显示持仓、净值曲线、盈亏
- **风控冻结** — API 异常/限频自动冻结买入，已有卖单不受影响

---

## 架构速览

| 模块 | 职责 |
|------|------|
| `main.py` | 主入口，主循环，命令行交互 |
| `config.py` | 全局参数配置（币种、资金、网格档位、通知等） |
| `strategy.py` | **策略引擎** — 买入候选排序、卖出对账、ATR再入场 |
| `exchange.py` | CCXT 封装，币安现货 REST API |
| `state.py` | JSON 持久化状态管理（原子写入 + 自动备份） |
| `risk.py` | 统一资金风控（预算/持仓上限/可用余额） |
| `pnl.py` | CSV 盈亏追踪（成交即写，重启不丢失） |
| `dashboard.py` | 终端 ANSI 仪表盘（持仓表 + 净值曲线 + 动作日志） |
| `monitor.py` | 系统健康 + 市场异常告警 + 日报 |
| `market_data.py` | WebSocket 行情观察（只观察，不下单） |
| `volatility.py` | ATR/NATR 波动率计算 |
| `logger.py` | 每日轮转日志 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖：`ccxt>=4.0.0` `requests>=2.28.0` `python-dotenv>=1.0.0` `websocket-client>=1.6.0`

### 2. 配置 API 密钥

```bash
cp .env.example .env
```

编辑 `.env`，填入你的币安 API Key 和 Secret：

```
BINANCE_API_KEY=你的Key
BINANCE_SECRET=你的Secret
```

> ⚠️ 在币安创建 API Key 时，**只需要现货交易权限，不要开启提现权限。**

### 3. 选择交易币种

编辑 `config.py`，修改 `COIN_PRESET`：

```python
COIN_PRESET = "top10"   # 可选: top10 | top15 | top20 | custom
```

也可以切换到 `"custom"` 并在 `COIN_POOLS["custom"]` 里填入你自己的币种列表。

### 4. 先模拟运行

```bash
python main.py --dry-run --once
```

确认没有报错后，再用 `python main.py` 启动实盘。

---

## 配置说明

打开 `config.py`，以下参数需要关注：

### 资金
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TOTAL_CAPITAL` | 400 | 总资金预算 (USDT)，超出后不再买入 |
| `MAX_HOLDINGS` | 10 | 最大同时持有币种数 |
| `MIN_FREE_USDT` | 50 | 可用资金低于此值 → 停止新开仓 |
| `MIN_FREE_USDT_HARD` | 20 | 可用资金低于此值 → 冻结全部买入 |

### 网格档位
| 档位 | 金额 | 触发偏移 | 止盈 | 名额 | 市值限制 |
|------|------|----------|------|------|----------|
| T1 | $15 | 0%（建仓）| +1.0% | 10 | 无 |
| T2 | $20 | -4% | +1.5% | 10 | 无 |
| T3 | $30 | -12% | +3.0% | 3 | 前3 |
| T4 | $40 | -30% | +10.0% | 1 | 第1 |

### 代理
```python
# 中国大陆用户如需使用代理，取消注释：
# PROXY_URL = "http://127.0.0.1:7897"
PROXY_URL = ""
```

### 通知
```python
NOTIFICATION_CHANNELS = {
    "feishu":    "",  # 飞书 webhook
    "telegram":  "",  # Telegram bot webhook
    "wecom":     "",  # 企业微信 webhook
}
```
填入 webhook URL 即可接收每日日报。

### 扫描间隔
```python
SCAN_INTERVAL_SECONDS = 30  # REST 扫描间隔（秒）
```

### 风控
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `NO_STOP_LOSS` | True | 不止损，靠网格摊低成本 |
| `MAX_CONSECUTIVE_API_FAILURES` | 3 | 连续 API 失败次数 → 冻结买入 |

---

## 策略原理

### 四档网格

```
初始建仓 T1  @ $100 (基准价)
        ↓ 价格下跌
加仓 T2  @ $96  (-4%)
        ↓ 继续下跌
加仓 T3  @ $88  (-12%)
        ↓ 深度下跌
加仓 T4  @ $70  (-30%)
```

每个档位买入后立即挂限价止盈单。T1 止盈 +1.0%，T4 止盈 +10.0%。

### ATR 动态再入场

T2-T4 止盈卖出后，系统自动计算下一次触发价。计算公式：

```
触发价 = 卖出价 × (1 - 实际回调%)
实际回调% = max(最低回调%, 止盈缺口% × 波动率倍率)
```

- 波动率越高 → 回调越深 → 再入场越晚 → 风险越低
- 波动率越低 → 回调不低于档位最低门槛 → 保障网格密度

### 冷却机制

某个币种全部清仓后，需要回踩 1.2% 才允许重新建仓，防止高位追回。

### 市值排序

每 30 分钟从 CoinGecko 刷新市值排名。T3 仅允许市值前 3 的币种，T4 仅允许市值第 1。

---

## 命令行用法

```bash
python main.py                 # 正常运行（30s 扫描间隔，仪表盘模式）
python main.py --once          # 只扫描一次后退出
python main.py --dry-run       # 模拟运行（不实际下单）
python main.py --no-dashboard  # 无仪表盘模式（适合 Docker/cron/systemd）
python main.py --manual        # 手动控制台
python main.py --reconcile-only # 只执行启动对账并退出
```

### 手动控制台命令

| 命令 | 作用 |
|------|------|
| `status` | 查看当前余额和持仓状态 |
| `holdings` | 查看每笔持仓的详细盈亏 |
| `pause` | 暂停新开 T1（已有持仓的后续档位不受影响） |
| `freeze` | 冻结全部买入，保留已有卖单 |
| `resume` | 恢复全部买入 |

---

## 安全设计

- **启动对账**：启动时核对本地状态 vs 交易所余额/挂单/持仓。任何不一致都会冻结对应币种，禁止交易。
- **订单意图**：下单前先持久化意图（client_order_id + 预期行为）。网络异常时按 client_order_id 反查交易所状态，避免重复下单或漏单。
- **限频熔断**：检测到 429/418/IP 封禁后自动冷却，冷却期间不发起任何 REST 请求。
- **WebSocket 仅观察**：WebSocket 线程不访问策略状态、不调用交易所、不下单。到价只唤醒主循环，由 REST 流程完整复核后下单。
- **原子状态写入**：状态文件通过 `tmp → fsync → os.replace` 原子写入，异常断电不会损坏。

---

## 文件结构

```
.
├── main.py              # 主入口
├── config.py            # 全局配置（修改参数从这里开始）
├── strategy.py          # 策略引擎
├── exchange.py          # 交易所接口（CCXT）
├── state.py             # 状态持久化
├── risk.py              # 风控检查
├── pnl.py               # 盈亏追踪
├── dashboard.py         # 终端仪表盘
├── monitor.py           # 系统监控
├── market_data.py       # WebSocket 行情
├── volatility.py        # ATR/NATR 计算
├── logger.py            # 日志系统
├── requirements.txt     # 依赖
├── .env.example         # API 密钥模板
├── tests/
│   └── test_hardening.py  # 单元测试
└── README.md
```

---

## 风险声明

⚠️ **本项目仅供研究和学习用途。加密货币交易存在重大风险，可能导致全部本金损失。**

- 网格策略在单边持续下跌行情中会大幅亏损
- 不止损意味着不会自动割肉，浮亏可能很大
- 历史回测不代表未来表现
- 使用前请充分理解策略逻辑，并用小资金测试

---

## License

MIT
