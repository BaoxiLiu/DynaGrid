"""
动态网格策略引擎 — 核心逻辑
"""
import json
import os
import time
from config import (
    MONITOR_COINS, TIERS, TOTAL_CAPITAL, MAX_HOLDINGS,
    MIN_FREE_USDT_HARD, QUOTE_CURRENCY,
    MARKET_CAP_REFRESH_INTERVAL, PAUSE_FLAG_FILE, REENTRY_DIP,
    REENTRY_MIN_PULLBACKS, REENTRY_POLICY_VERSION,
    FREEZE_BUYS_FLAG_FILE, RECONCILE_REPORT_FILE,
    NON_STRATEGY_BALANCE_ALLOWANCES,
    ATR_TIMEFRAME, ATR_PERIOD, ATR_BASELINE_DAYS,
    ATR_VOL_RATIO_MIN, ATR_VOL_RATIO_MAX, ATR_RETRY_SECONDS,
)
from exchange import Exchange
from state import StateManager, TierReentryState, TierState
from pnl import PnLTracker
from risk import RiskManager
from logger import log
from volatility import VolatilityProfile, calculate_volatility_profile


# ============================================================
# 市值排名
# ============================================================

class MarketCapRanker:
    """从 CoinGecko 获取市值排名"""

    CACHE: list[str] = []          # 按排名排好的币种
    LAST_FETCH: float = 0.0

    @classmethod
    def get_ranked(cls, coins: list[str]) -> list[str]:
        """返回按市值从大到小排序的币种列表"""
        now = time.time()
        if cls.CACHE and (now - cls.LAST_FETCH) < MARKET_CAP_REFRESH_INTERVAL:
            # 用缓存
            return [c for c in cls.CACHE if c in coins]

        # 从 CoinGecko 获取
        try:
            import requests
            url = ("https://api.coingecko.com/api/v3/coins/markets"
                   "?vs_currency=usd&order=market_cap_desc&per_page=50&page=1"
                   "&sparkline=false")
            resp = requests.get(url, timeout=15)
            data = resp.json()
            all_ranked = [
                item["symbol"].upper()
                for item in data
                if item.get("symbol")
            ]
            cls.CACHE = all_ranked
            cls.LAST_FETCH = now
            log.debug(f"市值排名已更新 (Top3: {all_ranked[:3]})")
            return [c for c in all_ranked if c in coins]
        except Exception as e:
            log.warning(f"CoinGecko 获取失败: {e}，使用默认排序")
            # 默认排序 (大市值优先)
            default_order = [
                "BTC", "ETH", "BNB", "SOL", "TRX", "DOGE", "XRP",
                "ADA", "AVAX", "LTC", "LINK", "DOT", "UNI",
            ]
            return [c for c in default_order if c in coins]


# ============================================================
# 策略引擎
# ============================================================

class StrategyEngine:
    def __init__(self, exchange: Exchange, state: StateManager, pnl: PnLTracker):
        self._ex = exchange
        self._state = state
        self._pnl = pnl
        self._risk = RiskManager(state)
        self._maker_fee: dict[str, float] = {}  # 缓存费率
        self._volatility_cache: dict[
            str, tuple[float, VolatilityProfile | None]
        ] = {}
        self._startup_reconciled = False

    def reconcile_startup(self) -> dict:
        """启动时核对本地持仓、交易所余额和订单；任何疑点都禁止交易。"""
        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False,
            "issues": [],
            "warnings": [],
            "checked_orders": 0,
        }
        balances = self._ex.fetch_balance()
        open_orders = self._ex.fetch_open_orders()
        tickers = self._ex.fetch_tickers(MONITOR_COINS)
        if balances is None:
            report["issues"].append("无法获取交易所总余额")
        if open_orders is None:
            report["issues"].append("无法获取交易所开放订单")
        if tickers is None:
            report["issues"].append("无法获取启动对账行情")
        if report["issues"]:
            self._write_reconcile_report(report)
            return report

        for coin in MONITOR_COINS:
            self._state.unblock_coin(coin)

        local_sell_ids: set[str] = set()
        required_qty: dict[str, float] = {}
        for coin, cs in list(self._state.coins.items()):
            for tier in cs.tiers:
                if tier.status != "holding":
                    continue
                required_qty[coin] = required_qty.get(coin, 0.0) + tier.qty

                order = None
                if tier.sell_order_id:
                    local_sell_ids.add(str(tier.sell_order_id))
                    order = self._ex.fetch_order_status(tier.sell_order_id, coin)
                elif tier.sell_client_order_id:
                    order = self._ex.fetch_order_by_client_id(
                        coin, tier.sell_client_order_id,
                    )
                    if order and order.get("order_id"):
                        tier.sell_order_id = order["order_id"]
                        local_sell_ids.add(str(tier.sell_order_id))

                report["checked_orders"] += 1
                if order is None:
                    reason = f"{coin} T{tier.tier_index+1} 无法确认止盈单"
                    report["issues"].append(reason)
                    self._state.block_coin(coin, reason)
                    continue

                status = order.get("status", "unknown")
                if status != "open":
                    reason = (
                        f"{coin} T{tier.tier_index+1} 本地为 holding，"
                        f"交易所订单状态为 {status}"
                    )
                    report["issues"].append(reason)
                    self._state.block_coin(coin, reason)

        for coin, qty in required_qty.items():
            actual = float(balances.get(coin, 0) or 0)
            if actual + max(1e-12, qty * 0.001) < qty:
                reason = f"{coin} 本地数量 {qty:.12g} 大于交易所总余额 {actual:.12g}"
                report["issues"].append(reason)
                self._state.block_coin(coin, reason)

        for coin in MONITOR_COINS:
            actual = float(balances.get(coin, 0) or 0)
            expected = required_qty.get(coin, 0.0)
            extra = actual - expected
            allowance = max(
                0.0, float(NON_STRATEGY_BALANCE_ALLOWANCES.get(coin, 0) or 0),
            )
            exempted = min(max(extra, 0.0), allowance)
            if exempted > 0:
                report["warnings"].append(
                    f"{coin} 账户自有余额 {exempted:.12g} 已按配置豁免，"
                    "不归策略管理"
                )
                extra -= exempted
            price = float(tickers.get(coin, {}).get("price", 0) or 0)
            if extra > 0 and price <= 0:
                report["warnings"].append(
                    f"{coin} 存在额外余额 {extra:.12g}，但无法判断是否只是尘埃余额"
                )
                continue
            if extra * price >= self._ex.get_min_notional(coin):
                reason = (
                    f"{coin} 超出账户自有余额额度后仍多出 {extra:.12g}，"
                    f"市值约 ${extra * price:.2f}"
                )
                report["issues"].append(reason)
                self._state.block_coin(coin, reason)

        for order in open_orders:
            order_id = str(order.get("order_id") or "")
            coin = order.get("coin") or ""
            if coin in MONITOR_COINS and order_id and order_id not in local_sell_ids:
                reason = f"发现本地未登记的开放订单: {coin}/{order_id}"
                report["issues"].append(reason)
                self._state.block_coin(coin, reason)

        for pending in self._state.get_pending_orders():
            coin = pending.get("coin", "")
            client_id = pending.get("client_order_id", "")
            reason = f"存在未完成订单意图: {coin}/{client_id}"
            report["issues"].append(reason)
            if coin:
                self._state.block_coin(coin, reason)

        if not report["issues"]:
            self._initialize_legacy_reentries(report)

        report["ok"] = not report["issues"]
        self._startup_reconciled = report["ok"]
        self._state.save()
        self._write_reconcile_report(report)
        if report["ok"]:
            log.info(f"启动对账通过，共核对 {report['checked_orders']} 个活动卖单")
        else:
            log.critical(f"启动对账失败，共 {len(report['issues'])} 个问题，禁止交易")
        return report

    def _initialize_legacy_reentries(self, report: dict):
        """为旧状态中已止盈、当前未持有的深层档位补建动态触发价。"""
        for coin, cs in self._state.coins.items():
            for tier_index in range(1, len(TIERS)):
                if any(
                    t.tier_index == tier_index and t.status == "holding"
                    for t in cs.tiers
                ):
                    continue
                sold = [
                    t for t in cs.tiers
                    if t.tier_index == tier_index and t.status == "sold"
                ]
                if not sold:
                    continue
                reentry = self._state.get_tier_reentry(coin, tier_index)
                if reentry and reentry.status == "ready":
                    continue
                latest = sold[-1]
                updated = self._configure_dynamic_reentry(
                    coin, latest, latest.sell_price,
                )
                if updated.status != "ready":
                    report["warnings"].append(
                        f"{coin} T{tier_index+1} 暂无法计算动态触发价，"
                        "该档位保持暂停"
                    )

    @staticmethod
    def _write_reconcile_report(report: dict):
        temp = f"{RECONCILE_REPORT_FILE}.tmp.{os.getpid()}"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, RECONCILE_REPORT_FILE)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    # ============================================================
    # 主循环入口 — 每2分钟调用一次
    # ============================================================

    def run_scan(self) -> dict:
        """返回本轮扫描摘要"""
        summary = {
            "actions": [],
            "errors": [],
            "free_usdt": 0.0,
            "total_value": 0.0,
        }

        # 1. 获取行情
        all_coins = MONITOR_COINS
        if not self._startup_reconciled:
            summary["errors"].append("启动对账尚未通过，禁止交易")
            return summary

        tickers = self._ex.fetch_tickers(all_coins)
        if not tickers:
            summary["errors"].append("无法获取行情")
            return summary

        # 2. 获取余额 & 挂单
        balance = self._ex.fetch_free_balance()
        if balance is None:
            summary["errors"].append("无法确认账户余额，本轮禁止交易")
            return summary
        free_usdt = balance.get(QUOTE_CURRENCY, 0)
        summary["free_usdt"] = free_usdt

        # 资金预算检查 (告警，不做硬性阻止)
        committed = self._state.get_total_committed_all()
        if committed >= TOTAL_CAPITAL:
            log.warning(f"已投入 ${committed:.0f} >= 预算 ${TOTAL_CAPITAL}, "
                        "统一风控将阻止继续买入")

        open_orders = self._ex.fetch_open_orders()
        if open_orders is None:
            summary["errors"].append("无法确认开放订单，本轮禁止交易")
            return summary

        # 3. 计算总资产
        total_value = free_usdt
        for coin, cs in self._state.coins.items():
            if cs.active:
                for t in cs.tiers:
                    if t.status == "holding":
                        price = tickers.get(coin, {}).get("price", t.buy_price)
                        total_value += t.qty * price
        summary["total_value"] = total_value

        # 4. 检查已成交的卖单 + 卖单续期 (仅在挂单查询成功时)
        self._reconcile_sells(open_orders, summary)
        self._check_and_renew_sell_orders(open_orders, tickers, summary)

        # 6. 检查是否有币种全部清仓 → 重置
        self._check_fully_sold_and_reset()

        # 7. 统一买入候选 (按市值↓、档位↑排序，逐个执行)
        if free_usdt >= MIN_FREE_USDT_HARD:
            self._execute_priority_buys(tickers, free_usdt, summary)

        # 8. 保存状态
        self._state.save()

        return summary

    # ============================================================
    # Reconciliation: 检测哪些卖单已成交
    # ============================================================

    def _reconcile_sells(self, open_orders: list[dict], summary: dict):
        """对比活跃挂单 vs 状态中的卖单，检测成交"""
        open_order_ids = {o["order_id"] for o in open_orders}

        for coin, cs in list(self._state.coins.items()):
            for t in cs.tiers:
                if (t.status == "holding"
                        and t.sell_order_id
                        and t.sell_order_id not in open_order_ids):
                    # 卖单不在活跃列表 → 可能已成交
                    order_info = self._ex.fetch_order_status(t.sell_order_id, coin)
                    if order_info is None:
                        reason = f"{coin} T{t.tier_index+1} 卖单状态未知，冻结该币种"
                        self._state.block_coin(coin, reason)
                        summary["errors"].append(reason)
                        continue

                    status = order_info["status"]
                    if status == "closed":
                        self._apply_sell_fill(coin, t, order_info, summary)
                    elif status in {"canceled", "expired", "rejected"}:
                        if float(order_info.get("filled") or 0) > 0:
                            self._apply_sell_fill(coin, t, order_info, summary)
                        if t.status == "holding":
                            t.sell_order_id = ""
                            t.sell_client_order_id = ""
                            log.warning(
                                f"{coin} T{t.tier_index+1} 卖单 {status}，"
                                "仅对剩余数量重新挂单"
                            )
                    elif status == "open":
                        log.warning(
                            f"{coin} T{t.tier_index+1} 单笔查询仍为 open，"
                            "本轮不重复挂单"
                        )
                    else:
                        reason = f"{coin} T{t.tier_index+1} 未识别订单状态 {status}"
                        self._state.block_coin(coin, reason)
                        summary["errors"].append(reason)

    def _apply_sell_fill(self, coin: str, tier: TierState,
                         order_info: dict, summary: dict):
        original_qty = tier.qty
        filled_qty = min(float(order_info.get("filled") or original_qty), original_qty)
        if filled_qty <= 0:
            return
        sell_price = float(order_info.get("price") or tier.sell_price)
        fee_currency = order_info.get("fee_currency") or "USDT"
        raw_fee = float(order_info.get("fee") or 0)
        sell_fee = (
            raw_fee if fee_currency == QUOTE_CURRENCY
            else sell_price * filled_qty * self._get_fee(coin)
        )
        ratio = min(1.0, filled_qty / original_qty)
        total_buy_fee = tier.buy_fee_usdt or (
            tier.buy_price * original_qty * self._get_fee(coin)
        )
        buy_fee = total_buy_fee * ratio
        pnl = self._pnl.record_sell(
            coin, tier.tier_index, tier.buy_price, sell_price, filled_qty,
            buy_fee, sell_fee, order_id=order_info.get("order_id", ""),
            fee_currency=fee_currency,
        )

        remaining = max(0.0, original_qty - filled_qty)
        tier.sell_filled_qty += filled_qty
        tier.sell_fee += sell_fee
        tier.buy_fee_usdt = max(0.0, total_buy_fee - buy_fee)
        if remaining <= max(1e-12, original_qty * 1e-8):
            tier.status = "sold"
            tier.sell_price = sell_price
        else:
            tier.qty = remaining
            tier.amount_usdt *= remaining / original_qty
        tier.sell_order_id = ""
        tier.sell_client_order_id = ""

        if tier.status == "holding" and self._retire_dust_tier(
                coin, tier, sell_price, summary,
        ):
            log.warning(
                f"{coin} T{tier.tier_index+1} 部分成交后剩余尘埃已移出策略管理"
            )

        if tier.status == "sold" and tier.tier_index > 0:
            reentry = self._configure_dynamic_reentry(coin, tier, sell_price)
            if reentry.status != "ready":
                summary["errors"].append(
                    f"{coin} T{tier.tier_index+1} 已止盈，但动态触发价暂不可用"
                )

        log.info(f"[SOLD] {coin} T{tier.tier_index+1} {filled_qty:.8g} "
                 f"@ ${sell_price:.4f} 利润 ${pnl:+.4f}")
        summary["actions"].append(
            f"{coin} T{tier.tier_index+1} 卖出 {filled_qty:.8g}, PnL ${pnl:+.4f}"
        )

    # ============================================================
    # 卖单续期
    # ============================================================

    def _retire_dust_tier(self, coin: str, tier: TierState,
                           exit_price: float, summary: dict) -> bool:
        """若剩余仓位不足以挂限价单，将其作为非策略余额退役。"""
        min_notional = self._ex.get_min_notional(coin)
        notional = tier.qty * exit_price
        if tier.qty > 0 and notional >= min_notional:
            return False
        coin_retired = self._state.retire_dust_tier(
            coin, tier.tier_index, exit_price,
        )
        scope = "该币种已进入冷却" if coin_retired else "该档位已退役"
        message = (
            f"{coin} T{tier.tier_index+1} 剩余尘埃 {tier.qty:.12g} "
            f"(约 ${notional:.2f}) 低于最小下单金额 ${min_notional:.2f}，"
            f"已移出策略管理，{scope}"
        )
        log.warning(message)
        summary["actions"].append(message)
        return True

    def _check_and_renew_sell_orders(self, open_orders: list[dict],
                                      tickers: dict, summary: dict):
        """以 sell_order_id 匹配，检查卖单是否仍在，不在就重新挂"""
        active_order_ids = {o["order_id"] for o in open_orders if o.get("order_id")}

        for coin, cs in self._state.coins.items():
            for t in cs.tiers:
                if t.status != "holding":
                    continue
                # 兼容进程重启前已经留下的尘埃仓位，先退役再检查冻结状态。
                if self._retire_dust_tier(coin, t, t.sell_price, summary):
                    continue
                if self._state.is_blocked(coin):
                    continue
                # 用记录中的订单 ID 去活跃列表里找
                if t.sell_order_id and t.sell_order_id in active_order_ids:
                    continue  # 卖单还在
                if t.sell_order_id:
                    # reconciliation 已确认该订单仍为 open 或未知，本轮不得重挂。
                    continue

                # 尘埃仓位不能满足交易所最小精度/最小金额时，不建立订单意图。
                # 这条预检避免精度异常发生在 begin_order() 之后而遗留 intent。
                log.info(f"[RENEW] {coin} T{t.tier_index+1} 卖单不存在，重新挂 "
                         f"(目标 ${t.sell_price:.4f})")
                client_id = self._state.new_client_order_id(
                    coin, t.tier_index, "sell",
                )
                self._state.begin_order(
                    client_id, coin=coin, tier_index=t.tier_index,
                    side="sell", order_type="limit", qty=t.qty,
                    price=t.sell_price,
                )
                result = self._ex.limit_sell(
                    coin, t.qty, t.sell_price, client_order_id=client_id,
                )
                if result:
                    t.sell_order_id = result["order_id"]
                    t.sell_client_order_id = client_id
                    self._state.complete_order_intent(client_id)
                    summary["actions"].append(
                        f"{coin} T{t.tier_index+1} 卖单已续期 @ ${t.sell_price:.4f}"
                    )
                else:
                    outcome = self._ex.last_order_outcome
                    self._state.update_order_intent(client_id, status=outcome)
                    if outcome == "unknown":
                        self._state.block_coin(
                            coin, f"卖单 {client_id} 结果未知",
                        )
                    else:
                        self._state.complete_order_intent(client_id)
                    summary["errors"].append(
                        f"{coin} T{t.tier_index+1} 卖单续期失败 ({outcome})"
                    )

    # ============================================================
    # 全清仓 → 重置
    # ============================================================

    def _check_fully_sold_and_reset(self):
        """检查是否有币种全部卖出，有则进入冷却"""
        to_reset = []
        for coin in list(self._state.coins.keys()):
            if self._state.is_coin_fully_sold(coin):
                # 取清仓价 (最后一个 tier 的卖出价)
                cs = self._state.coins[coin]
                exit_price = max(
                    (t.sell_price for t in cs.tiers if t.status == "sold"),
                    default=cs.base_price,
                )
                to_reset.append((coin, exit_price))

        for coin, exit_price in to_reset:
            log.info(f"[RESET] {coin} 全部清仓 @ ${exit_price:.4f}, "
                     f"进入冷却 (需回踩 {REENTRY_DIP*100:.1f}%)")
            self._state.add_cooling(coin, exit_price)

    # ============================================================
    # 统一买入候选 — 按优先级排序
    # ============================================================

    def _execute_priority_buys(self, tickers: dict, free_usdt: float,
                                summary: dict):
        """收集所有买入候选 → 按(市值↓, 档位↑)排序 → 逐个执行"""
        if self._all_buys_frozen():
            summary["actions"].append("所有买入已冻结，已有卖单继续管理")
            return
        candidates = self._collect_buy_candidates(tickers, free_usdt)
        if not candidates:
            return

        # 排序: 第一权重市值排名 (大优先), 第二权重档位 (T1优先)
        ranked = MarketCapRanker.get_ranked(MONITOR_COINS)
        # 构建市值排名映射: coin → 排名序号 (0=最大市值)
        cap_order = {c: i for i, c in enumerate(ranked)}

        def sort_key(c: dict) -> tuple:
            return (cap_order.get(c["coin"], 999), c["tier_index"])

        candidates.sort(key=sort_key)

        # 逐个执行
        for c in candidates:
            # 新开仓需检查持仓上限
            if c["is_new"] and self._state.holding_count >= MAX_HOLDINGS:
                continue

            amount = c["tier_cfg"]["amount"]
            decision = self._risk.can_buy(
                c["coin"], amount, free_usdt, c["is_new"],
            )
            if not decision.allowed:
                log.info(f"{c['coin']} T{c['tier_index']+1} 风控拒绝: {decision.reason}")
                continue

            # 暂停检查 (仅新开仓)
            if c["is_new"] and self._is_paused():
                if "⏸ 新开仓已暂停" not in summary["actions"]:
                    summary["actions"].append("⏸ 新开仓已暂停")
                continue

            bought = self._execute_buy(
                c["coin"], c["tier_index"], c["current_price"],
                c["tier_cfg"], summary,
            )
            if not bought:
                continue
            free_usdt -= amount

            # 每笔买入后刷新余额
            balances = self._ex.fetch_free_balance()
            if balances is None:
                summary["errors"].append("买入后无法刷新余额，停止本轮后续买入")
                break
            free_usdt = balances.get(QUOTE_CURRENCY, 0)

    def _collect_buy_candidates(self, tickers: dict,
                                 free_usdt: float) -> list[dict]:
        """收集所有买入候选: 新开仓(T1) + 加仓(T2/T3/T4)

        规则:
        1. 每档全局数量限制 (max_count)
        2. T3/T4 市值过滤 (cap_rank)
        3. 同币种多档同时触发 → 只取最近档位 (最低 tier_index)
        """
        candidates: list[dict] = []
        holding_coins = set(self._state.get_holding_coins())

        # 统计各档位当前持有中的全局数量 (已卖出不计入)
        tier_counts: dict[int, int] = {i: 0 for i in range(len(TIERS))}
        for coin, cs in self._state.coins.items():
            for t in cs.tiers:
                if t.status == "holding":
                    tier_counts[t.tier_index] += 1

        # 市值排名
        ranked = MarketCapRanker.get_ranked(MONITOR_COINS)
        cap_order: dict[str, int] = {c: i + 1 for i, c in enumerate(ranked)}  # 1=最大市值

        # 1. 加仓候选: 已持有币种的未触发 tier
        for coin, cs in self._state.coins.items():
            if not cs.active:
                continue
            if self._state.is_blocked(coin):
                continue
            ticker = tickers.get(coin, {})
            current_price = ticker.get("price", 0)
            if current_price <= 0:
                continue

            # 只算持有中的档位 (已卖出的可以再次触发)
            filled_indices = {t.tier_index for t in cs.tiers if t.status == "holding"}
            coin_candidates: list[dict] = []  # 该币种本轮触发的所有档位

            for i, tier_cfg in enumerate(TIERS):
                if i in filled_indices:
                    continue

                # 每档全局数量限制
                if tier_counts[i] >= tier_cfg["max_count"]:
                    continue

                # 市值限制
                cap_rank = tier_cfg.get("cap_rank")
                if cap_rank is not None:
                    coin_rank = cap_order.get(coin, 99)
                    if coin_rank > cap_rank:
                        continue

                sold_tiers = [
                    t for t in cs.tiers
                    if t.tier_index == i and t.status == "sold"
                ]
                if i > 0 and sold_tiers:
                    trigger_price = self._dynamic_reentry_trigger(
                        coin, sold_tiers[-1],
                    )
                    if trigger_price is None:
                        continue
                else:
                    trigger_price = cs.base_price * (1 + tier_cfg["trigger"])
                if current_price <= trigger_price:
                    coin_candidates.append({
                        "coin": coin,
                        "tier_index": i,
                        "current_price": current_price,
                        "tier_cfg": tier_cfg,
                        "is_new": False,
                    })
                    tier_counts[i] += 1  # 预占名额, 避免同一轮超额

            # 同币种多档同时触发 → 只取最近档位 (tier_index 最小)
            if len(coin_candidates) > 1:
                coin_candidates.sort(key=lambda c: c["tier_index"])
                # 释放被占的更深档位名额
                for cc in coin_candidates[1:]:
                    tier_counts[cc["tier_index"]] -= 1
                coin_candidates = coin_candidates[:1]

            candidates.extend(coin_candidates)

        # 2. 建仓候选: 未持有的币种 T1
        tier1_cfg = TIERS[0]
        if (self._state.holding_count < MAX_HOLDINGS
                and tier_counts[0] < tier1_cfg["max_count"]):
            # 监控池可大于最大持仓数；新 T1 按市值优先，而非按配置书写顺序。
            # CoinGecko 返回范围不足时，仍将未排名币种置于末尾保留候选资格。
            tier1_order = ranked + [
                coin for coin in MONITOR_COINS if coin not in ranked
            ]
            for coin in tier1_order:
                if coin in holding_coins:
                    continue
                if self._state.is_blocked(coin):
                    continue
                # 冷却检查: 刚清仓的币种必须回踩到位
                if self._state.check_cooling(coin, tickers.get(coin, {}).get("price", 0)):
                    continue
                if tier_counts[0] >= tier1_cfg["max_count"]:
                    break
                ticker = tickers.get(coin, {})
                current_price = ticker.get("price", 0)
                if current_price <= 0:
                    continue
                min_notional = self._ex.get_min_notional(coin)
                if tier1_cfg["amount"] < min_notional:
                    continue
                candidates.append({
                    "coin": coin,
                    "tier_index": 0,
                    "current_price": current_price,
                    "tier_cfg": tier1_cfg,
                    "is_new": True,
                })
                tier_counts[0] += 1  # 预占

        return candidates

    # ============================================================
    # 执行买入 + 挂卖单
    # ============================================================

    def _execute_buy(self, coin: str, tier_index: int, current_price: float,
                      tier_cfg: dict, summary: dict) -> bool:
        """市价买入 → 立即挂限价卖单"""
        amount = tier_cfg["amount"]
        if tier_index == 0 and self._state.is_holding(coin):
            summary["errors"].append(f"{coin} 已有真实持仓，拒绝覆盖为新周期")
            return False
        if tier_index > 0 and coin not in self._state.coins:
            summary["errors"].append(f"{coin} 不存在基础仓位，拒绝执行 T{tier_index+1}")
            return False

        buy_client_id = self._state.new_client_order_id(coin, tier_index, "buy")
        self._state.begin_order(
            buy_client_id, coin=coin, tier_index=tier_index, side="buy",
            order_type="market", amount_usdt=amount,
        )
        result = self._ex.market_buy(
            coin, amount, client_order_id=buy_client_id,
        )
        if not result:
            outcome = self._ex.last_order_outcome
            self._state.update_order_intent(buy_client_id, status=outcome)
            if outcome == "unknown":
                self._state.block_coin(coin, f"买单 {buy_client_id} 结果未知")
            else:
                self._state.complete_order_intent(buy_client_id)
            summary["errors"].append(
                f"{coin} T{tier_index+1} 买入失败或结果未知 ({outcome})"
            )
            return False

        buy_price = result["price"]
        qty = result["qty"]
        if buy_price <= 0 or qty <= 0:
            self._state.update_order_intent(
                buy_client_id, status="unknown_fill",
                exchange_order_id=result.get("order_id", ""),
            )
            self._state.block_coin(coin, f"买单 {buy_client_id} 成交数量或价格未知")
            summary["errors"].append(f"{coin} 买单存在但成交详情未知，已冻结")
            return False
        self._state.update_order_intent(
            buy_client_id, status="confirmed",
            exchange_order_id=result.get("order_id", ""),
            filled_qty=qty, average_price=buy_price,
        )

        fee_currency = result.get("fee_currency") or ""
        raw_fee = float(result.get("fee") or 0)
        if fee_currency == coin:
            qty = max(0.0, qty - raw_fee)
            buy_fee_usdt = raw_fee * buy_price
        elif fee_currency == QUOTE_CURRENCY:
            buy_fee_usdt = raw_fee
        else:
            buy_fee_usdt = buy_price * qty * self._get_fee(coin)

        # 计算止盈价
        tp_rate = tier_cfg["tp"]
        sell_price = buy_price * (1 + tp_rate)

        # 如果这是 Tier1，则建立基准价
        if tier_index == 0:
            self._state.add_coin(coin, buy_price)

        sell_client_id = self._state.new_client_order_id(coin, tier_index, "sell")
        self._state.begin_order(
            sell_client_id, coin=coin, tier_index=tier_index, side="sell",
            order_type="limit", qty=qty, price=sell_price,
        )
        sell_result = self._ex.limit_sell(
            coin, qty, sell_price, client_order_id=sell_client_id,
        )

        sell_order_id = sell_result["order_id"] if sell_result else ""

        if not sell_result:
            log.error(f"[ALERT] {coin} T{tier_index+1} 已买入但卖单挂失败！"
                      f" {qty:.4f} @ 目标 ${sell_price:.4f} 未确认挂单")
            summary["errors"].append(
                f"⚠️ {coin} T{tier_index+1} 卖单挂失败，裸仓中！"
            )

        # 记录状态
        self._state.add_tier(
            coin=coin,
            tier_index=tier_index,
            amount_usdt=amount,
            buy_price=buy_price,
            sell_price=sell_price,
            qty=qty,
            order_id=sell_order_id,
            buy_order_id=result.get("order_id", ""),
            buy_client_order_id=buy_client_id,
            sell_client_order_id=sell_client_id,
            buy_fee_usdt=buy_fee_usdt,
        )
        if tier_index > 0:
            self._state.clear_tier_reentry(coin, tier_index)

        if sell_result:
            self._state.complete_order_intent(sell_client_id)
        else:
            outcome = self._ex.last_order_outcome
            self._state.update_order_intent(sell_client_id, status=outcome)
            if outcome == "unknown":
                self._state.block_coin(coin, f"卖单 {sell_client_id} 结果未知")
            else:
                self._state.complete_order_intent(sell_client_id)
        self._state.complete_order_intent(buy_client_id)

        # 记录盈亏
        self._pnl.record_buy(
            coin, tier_index, buy_price, qty,
            float(result.get("cost") or amount), buy_fee_usdt,
            order_id=result.get("order_id", ""),
            fee_currency=fee_currency or QUOTE_CURRENCY,
        )

        log.info(f"[ENTRY] {coin} T{tier_index+1} 买入 ${amount} → "
                 f"{qty:.4f} @ ${buy_price:.4f}, 止盈 ${sell_price:.4f} (+{tp_rate*100:.1f}%)")
        summary["actions"].append(
            f"{coin} T{tier_index+1} 买入 @ ${buy_price:.4f} → 止盈 ${sell_price:.4f}"
        )
        return True

        # 标记：不再为此币种触发已完成的tiers
        # (已通过 filled_indices 逻辑处理)

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def _is_paused() -> bool:
        """检查暂停标志文件是否存在"""
        import os
        return os.path.exists(PAUSE_FLAG_FILE)

    @staticmethod
    def _all_buys_frozen() -> bool:
        return os.path.exists(FREEZE_BUYS_FLAG_FILE)

    def _get_fee(self, coin: str) -> float:
        if coin not in self._maker_fee:
            self._maker_fee[coin] = self._ex.fetch_trading_fee(coin)
        return self._maker_fee[coin]

    def _get_volatility_profile(self, coin: str) -> VolatilityProfile | None:
        now = time.time()
        cached = self._volatility_cache.get(coin)
        if cached and now - cached[0] < ATR_RETRY_SECONDS:
            return cached[1]

        baseline_bars = ATR_BASELINE_DAYS * 24
        limit = baseline_bars + ATR_PERIOD + 2
        rows = self._ex.fetch_ohlcv(coin, ATR_TIMEFRAME, limit)
        profile = None
        if rows is not None:
            profile = calculate_volatility_profile(
                rows,
                timeframe=ATR_TIMEFRAME,
                period=ATR_PERIOD,
                baseline_bars=baseline_bars,
            )
        self._volatility_cache[coin] = (now, profile)
        return profile

    def _configure_dynamic_reentry(
        self, coin: str, tier: TierState, sell_price: float,
    ) -> TierReentryState:
        profile = self._get_volatility_profile(coin)
        now = time.time()
        if profile is None or sell_price <= 0:
            reentry = TierReentryState(
                tier_index=tier.tier_index,
                last_buy_price=tier.buy_price,
                anchor_sell_price=sell_price,
                policy_version=REENTRY_POLICY_VERSION,
                calculated_at=now,
                status="unavailable",
                error="ATR/NATR 数据不足或获取失败",
            )
            self._state.set_tier_reentry(coin, reentry)
            log.warning(
                f"{coin} T{tier.tier_index+1} 动态触发价计算失败，"
                "该档位暂不重新买入"
            )
            return reentry

        tp_rate = float(TIERS[tier.tier_index]["tp"])
        neutral_gap = 1 - (1 / (1 + tp_rate))
        effective_ratio = min(
            ATR_VOL_RATIO_MAX,
            max(ATR_VOL_RATIO_MIN, profile.raw_ratio),
        )
        min_gap = float(REENTRY_MIN_PULLBACKS[tier.tier_index])
        atr_gap = neutral_gap * effective_ratio
        # 回补价永远不能比该档的最低回调更浅；ATR 只在波动够大时继续拉深。
        applied_gap = max(min_gap, atr_gap)
        next_trigger = sell_price * (1 - applied_gap)
        reentry = TierReentryState(
            tier_index=tier.tier_index,
            next_trigger_price=next_trigger,
            last_buy_price=tier.buy_price,
            anchor_sell_price=sell_price,
            atr=profile.atr,
            current_natr=profile.natr,
            median_natr=profile.median_natr,
            raw_vol_ratio=profile.raw_ratio,
            effective_vol_ratio=effective_ratio,
            neutral_gap_rate=neutral_gap,
            applied_gap_rate=applied_gap,
            policy_version=REENTRY_POLICY_VERSION,
            calculated_at=now,
        )
        self._state.set_tier_reentry(coin, reentry)
        log.info(
            f"[GRID] {coin} T{tier.tier_index+1} 下一触发 "
            f"${next_trigger:.8g} | 卖出 ${sell_price:.8g} | "
            f"NATR {profile.natr*100:.3f}% / 中位 {profile.median_natr*100:.3f}% | "
            f"波动倍率 {effective_ratio:.3f} | 最低回调 {min_gap*100:.3f}% | "
            f"ATR建议 {atr_gap*100:.3f}% | 实际回调 {applied_gap*100:.3f}%"
        )
        return reentry

    def _dynamic_reentry_trigger(self, coin: str,
                                 latest_sold: TierState) -> float | None:
        tier_index = latest_sold.tier_index
        reentry = self._state.get_tier_reentry(coin, tier_index)
        if (reentry and reentry.status == "ready"
                and reentry.next_trigger_price > 0
                and reentry.policy_version == REENTRY_POLICY_VERSION):
            return reentry.next_trigger_price
        if (reentry and reentry.policy_version == REENTRY_POLICY_VERSION
                and time.time() - reentry.calculated_at < ATR_RETRY_SECONDS):
            return None
        refreshed = self._configure_dynamic_reentry(
            coin, latest_sold, latest_sold.sell_price,
        )
        if refreshed.status != "ready":
            return None
        return refreshed.next_trigger_price

    def get_websocket_observation_levels(self) -> list[dict]:
        """返回可由 WebSocket 观察的既有买入触发价，不发网络请求也不改变状态。

        新开 T1 没有固定价格门槛，动态再入场又不能在后台线程计算 ATR，故只暴露
        已经确定的冷却 T1 与 ready 状态的 T2-T4 再入场价。它们仅用于提示，不下单。
        """
        levels: list[dict] = []
        tier_counts = {i: 0 for i in range(len(TIERS))}
        for cs in self._state.coins.values():
            for tier in cs.tiers:
                if tier.status == "holding":
                    tier_counts[tier.tier_index] += 1

        for coin, exit_price in self._state.get_cooling_prices().items():
            if not self._state.is_blocked(coin) and float(exit_price) > 0:
                levels.append({
                    "coin": coin, "tier": "T1", "tier_index": 0,
                    "trigger_price": float(exit_price) * (1 - REENTRY_DIP),
                    "source": "cooling",
                })

        for coin, cs in self._state.coins.items():
            if not cs.active or self._state.is_blocked(coin):
                continue
            held_indices = {t.tier_index for t in cs.tiers if t.status == "holding"}
            for tier_index, tier_cfg in enumerate(TIERS):
                if tier_index in held_indices or tier_counts[tier_index] >= tier_cfg["max_count"]:
                    continue
                sold = [
                    t for t in cs.tiers
                    if t.tier_index == tier_index and t.status == "sold"
                ]
                if tier_index > 0 and sold:
                    reentry = self._state.get_tier_reentry(coin, tier_index)
                    if not (reentry and reentry.status == "ready"
                            and reentry.next_trigger_price > 0
                            and reentry.policy_version == REENTRY_POLICY_VERSION):
                        continue
                    trigger_price = reentry.next_trigger_price
                    source = "dynamic_reentry"
                else:
                    trigger_price = cs.base_price * (1 + tier_cfg["trigger"])
                    source = "static_tier"
                levels.append({
                    "coin": coin, "tier": f"T{tier_index + 1}",
                    "tier_index": tier_index, "trigger_price": trigger_price,
                    "source": source,
                })
        return levels

    # ============================================================
    # 日报
    # ============================================================

    def get_holdings_summary(self, tickers: dict,
                             free_usdt: float | None = None) -> list[dict]:
        """生成持仓摘要 (用于日报)"""
        result = []
        tier_counts: dict[int, int] = {i: 0 for i in range(len(TIERS))}
        for state in self._state.coins.values():
            for tier in state.tiers:
                if tier.status == "holding":
                    tier_counts[tier.tier_index] += 1

        rank_order = {
            coin: index + 1 for index, coin in enumerate(MarketCapRanker.CACHE)
        }
        for coin, cs in self._state.coins.items():
            if not cs.active:
                continue
            price = tickers.get(coin, {}).get("price", cs.base_price)
            tiers_info = []
            for t in cs.tiers:
                unrealized = 0.0
                if t.status == "holding":
                    unrealized = (price - t.buy_price) * t.qty
                tiers_info.append({
                    "tier": f"T{t.tier_index+1}",
                    "buy_price": round(t.buy_price, 4),
                    "sell_price": round(t.sell_price, 4),
                    "qty": round(t.qty, 4),
                    "status": t.status,
                    "unrealized_pnl": round(unrealized, 4),
                })
            link_value = sum(
                t.qty * price for t in cs.tiers if t.status == "holding"
            )
            next_buy = self._get_next_buy_target(
                coin, cs, price, free_usdt, tier_counts, rank_order,
            )
            result.append({
                "coin": coin,
                "base_price": round(cs.base_price, 4),
                "current_price": round(price, 4),
                "link_value": round(link_value, 2),
                "tiers": tiers_info,
                "next_buy": next_buy,
            })
        return result

    def _get_next_buy_target(self, coin: str, cs, current_price: float,
                             free_usdt: float | None,
                             tier_counts: dict[int, int],
                             rank_order: dict[str, int]) -> dict | None:
        """返回最低未持有档位的触发价及当前可执行性，不产生订单或行情请求。"""
        held_indices = {t.tier_index for t in cs.tiers if t.status == "holding"}
        for tier_index, tier_cfg in enumerate(TIERS):
            if tier_index in held_indices:
                continue

            trigger_price = None
            dynamic_pending = False
            sold_tiers = [
                t for t in cs.tiers
                if t.tier_index == tier_index and t.status == "sold"
            ]
            if tier_index > 0 and sold_tiers:
                reentry = self._state.get_tier_reentry(coin, tier_index)
                if reentry and reentry.status == "ready" and reentry.next_trigger_price > 0:
                    trigger_price = reentry.next_trigger_price
                else:
                    dynamic_pending = True
            else:
                trigger_price = cs.base_price * (1 + tier_cfg["trigger"])

            result = {
                "tier": f"T{tier_index + 1}",
                "tier_index": tier_index,
                "amount": tier_cfg["amount"],
                "trigger_price": round(trigger_price, 8) if trigger_price else None,
                "distance_pct": round(
                    (trigger_price / current_price - 1) * 100, 2,
                ) if trigger_price and current_price > 0 else None,
                "status": "",
            }
            reasons = []
            if dynamic_pending:
                reasons.append("动态触发价待更新")
            if self._state.is_blocked(coin):
                reasons.append("币种已冻结")
            if tier_counts[tier_index] >= tier_cfg["max_count"]:
                reasons.append(f"全局 {result['tier']} 名额已满")
            cap_rank = tier_cfg.get("cap_rank")
            if cap_rank is not None:
                rank = rank_order.get(coin)
                if rank is None:
                    reasons.append("市值排名待刷新")
                elif rank > cap_rank:
                    reasons.append(f"市值排名第 {rank}，需前 {cap_rank}")
            decision = self._risk.can_buy(coin, tier_cfg["amount"], free_usdt, False)
            if not decision.allowed:
                reasons.append(decision.reason)

            if reasons:
                # 仪表盘只展示当前真正可执行的下一档，避免把受限档位
                # 误读为待执行的买入计划。
                return None
            elif trigger_price and current_price <= trigger_price:
                result["status"] = "已到价，可执行"
            else:
                result["status"] = "等待触发"
            return result
        return None
