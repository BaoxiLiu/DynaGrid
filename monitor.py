"""
系统监控 — 健康检查 + 市场异常告警
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from config import (
    ALERT_BTC_DAILY_CHANGE, ALERT_MULTI_TRIGGER_RATIO,
    MONITOR_COINS, TIERS, DAILY_SUMMARY_TIME,
)
from logger import log

BEIJING_TZ = timezone(timedelta(hours=8))
_last_daily_summary_date: str = ""
_last_summary_hour_checked: str = ""


class Monitor:
    def __init__(self):
        self._btc_price_24h_ago: Optional[float] = None
        self._last_btc_check: float = 0
        self._warnings: list[str] = []
        self._last_alert_log: dict[str, float] = {}

    def check_market_anomaly(self, tickers: dict) -> list[str]:
        """检查市场是否异常，返回告警列表"""
        alerts = []

        # 1. BTC 日内波动
        btc = tickers.get("BTC", {})
        if btc:
            btc_change = abs(btc.get("change_24h", 0) or 0)
            if btc_change > ALERT_BTC_DAILY_CHANGE * 100:
                alerts.append(
                    f"⚠️ BTC 24h 波动 {btc_change:.1f}% (阈值 {ALERT_BTC_DAILY_CHANGE*100:.0f}%)"
                )

        # 2. 多币种同时触发 Tier2 (跌 3%+)
        trigger_count = 0
        for coin in MONITOR_COINS:
            t = tickers.get(coin, {})
            change = t.get("change_24h", 0) or 0
            if change < TIERS[1]["trigger"] * 100:  # 跌超 3%
                trigger_count += 1

        threshold = len(MONITOR_COINS) * ALERT_MULTI_TRIGGER_RATIO
        if trigger_count >= threshold:
            alerts.append(
                f"⚠️ {trigger_count}/{len(MONITOR_COINS)} 币种同步下跌超3%，建议关注"
            )

        if alerts:
            for a in alerts:
                key = a.split("，", 1)[0]
                now = time.time()
                if now - self._last_alert_log.get(key, 0) >= 900:
                    log.warning(a)
                    self._last_alert_log[key] = now

        return alerts

    def should_send_daily_summary(self) -> bool:
        """检查是否到了发日报的时间"""
        global _last_daily_summary_date
        now = datetime.now(BEIJING_TZ)
        today = now.date().isoformat()
        current_hour_min = now.strftime("%H:%M")

        # 每天早上8点发
        if today != _last_daily_summary_date and current_hour_min >= DAILY_SUMMARY_TIME:
            _last_daily_summary_date = today
            return True
        return False

    def check_system_health(self, exchange_ok: bool, last_scan_ok: bool) -> list[str]:
        """检查系统是否健康"""
        issues = []
        if not exchange_ok:
            issues.append("❌ 交易所连接异常")
        if not last_scan_ok:
            issues.append("❌ 上次扫描失败")
        return issues

    def format_daily_summary(self, summary: dict, holdings: list[dict],
                              alerts: list[str]) -> str:
        """格式化日报文本"""
        lines = [
            "=" * 40,
            f"📊 动态网格策略日报 — {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}",
            "=" * 40,
            "",
            f"💰 今日盈亏:    ${summary.get('today_pnl', 0):+.4f}",
            f"💰 累计盈亏:    ${summary.get('total_pnl', 0):+.4f}",
            f"📈 今日成交:    {summary.get('total_trades', 0)} 笔 "
            f"(买{summary.get('buys', 0)}/卖{summary.get('sells', 0)})",
            f"💵 可用资金:    ${summary.get('free_usdt', 0):.2f}",
            f"📦 总资产:      ${summary.get('total_value', 0):.2f}",
            "",
        ]

        if holdings:
            lines.append("📋 当前持仓:")
            for h in holdings:
                lines.append(f"  {h['coin']}: 基准 ${h['base_price']:.4f} "
                           f"| 现价 ${h['current_price']:.4f} "
                           f"| 持仓 ${h['link_value']:.2f}")
                for t in h["tiers"]:
                    if t["status"] == "holding":
                        lines.append(
                            f"    {t['tier']}: 买${t['buy_price']:.4f} "
                            f"→ 止盈${t['sell_price']:.4f} "
                            f"| 浮盈${t['unrealized_pnl']:+.4f}"
                        )
                    else:
                        lines.append(f"    {t['tier']}: 已卖出 ✅")
            lines.append("")

        if alerts:
            lines.append("⚠️ 告警:")
            for a in alerts:
                lines.append(f"  {a}")
            lines.append("")

        return "\n".join(lines)
