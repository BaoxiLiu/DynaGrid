"""
盈亏追踪 — 每笔成交写入 CSV，定期汇总
"""
import csv
import os
import json
import shutil
from datetime import datetime, timezone, timedelta
from config import TRADE_LOG_FILE, DAILY_SUMMARY_FILE
from logger import log

BEIJING_TZ = timezone(timedelta(hours=8))


class PnLTracker:
    FIELDNAMES = [
        "timestamp", "coin", "tier", "side", "price", "qty",
        "usdt_value", "fee", "pnl", "total_pnl", "order_id",
        "fee_currency",
    ]

    def __init__(self):
        self._trades: list[dict] = []
        self._recorded_order_ids: set[str] = set()
        self._init_csv()
        self._load_from_csv()

    def _init_csv(self):
        if not os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDNAMES).writeheader()
            return

        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            if old_fields == self.FIELDNAMES:
                return
            rows = list(reader)

        backup = f"{TRADE_LOG_FILE}.bak"
        shutil.copy2(TRADE_LOG_FILE, backup)
        temp = f"{TRADE_LOG_FILE}.tmp.{os.getpid()}"
        try:
            with open(temp, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: row.get(name, "") for name in self.FIELDNAMES})
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, TRADE_LOG_FILE)
            log.info(f"成交记录字段已升级，旧文件备份到 {backup}")
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    def _load_from_csv(self):
        """启动时从 CSV 恢复历史交易，避免重启后盈亏归零"""
        if not os.path.exists(TRADE_LOG_FILE):
            return
        try:
            with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trade = {
                        "timestamp": row.get("timestamp", ""),
                        "coin": row.get("coin", ""),
                        "tier": row.get("tier", ""),
                        "side": row.get("side", ""),
                        "price": float(row.get("price", 0)),
                        "qty": float(row.get("qty", 0)),
                        "usdt_value": float(row.get("usdt_value", 0)),
                        "fee": float(row.get("fee", 0)),
                        "pnl": float(row.get("pnl", 0)),
                        "total_pnl": float(row.get("total_pnl", 0)),
                        "order_id": row.get("order_id", ""),
                        "fee_currency": row.get("fee_currency", ""),
                    }
                    self._trades.append(trade)
                    if trade["order_id"]:
                        self._recorded_order_ids.add(trade["order_id"])
            log.info(f"PnL 已恢复: {len(self._trades)} 笔历史交易,"
                     f" 累计盈亏 ${self.get_total_pnl():+.2f}")
        except Exception as e:
            log.warning(f"PnL 恢复失败: {e}")

    def record_buy(self, coin: str, tier: int, price: float, qty: float,
                   usdt_value: float, fee: float, order_id: str = "",
                   fee_currency: str = "USDT"):
        """记录买入"""
        row = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "coin": coin,
            "tier": f"T{tier+1}",
            "side": "BUY",
            "price": round(price, 6),
            "qty": round(qty, 6),
            "usdt_value": round(usdt_value, 4),
            "fee": round(fee, 6),
            "pnl": 0.0,
            "total_pnl": round(self.get_total_pnl(), 4),
            "order_id": order_id,
            "fee_currency": fee_currency,
        }
        self._write_row(row)
        self._trades.append(row)

    def record_sell(self, coin: str, tier: int, buy_price: float,
                    sell_price: float, qty: float, buy_fee: float,
                    sell_fee: float, order_id: str = "",
                    fee_currency: str = "USDT"):
        """记录卖出并计算利润"""
        if order_id and order_id in self._recorded_order_ids:
            existing = next(
                (t for t in self._trades if t.get("order_id") == order_id),
                None,
            )
            return existing["pnl"] if existing else 0.0
        gross = (sell_price - buy_price) * qty
        pnl = gross - buy_fee - sell_fee
        cumulative = self.get_total_pnl() + pnl
        row = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "coin": coin,
            "tier": f"T{tier+1}",
            "side": "SELL",
            "price": round(sell_price, 6),
            "qty": round(qty, 6),
            "usdt_value": round(sell_price * qty, 4),
            "fee": round(sell_fee, 6),
            "pnl": round(pnl, 4),
            "total_pnl": round(cumulative, 4),
            "order_id": order_id,
            "fee_currency": fee_currency,
        }
        self._write_row(row)
        self._trades.append(row)
        if order_id:
            self._recorded_order_ids.add(order_id)
        return row["pnl"]

    def _write_row(self, row: dict):
        with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.FIELDNAMES).writerow(row)

    def get_today_pnl(self) -> float:
        """今日盈亏"""
        today = datetime.now(BEIJING_TZ).date().isoformat()
        return sum(
            t["pnl"] for t in self._trades
            if t["side"] == "SELL" and t["timestamp"][:10] == today
        )

    def get_total_pnl(self) -> float:
        return sum(t["pnl"] for t in self._trades if t["side"] == "SELL")

    def get_today_trades(self) -> list[dict]:
        today = datetime.now(BEIJING_TZ).date().isoformat()
        return [t for t in self._trades if t["timestamp"][:10] == today]

    def generate_daily_summary(self, holdings_info: list[dict],
                                free_usdt: float, total_value: float) -> dict:
        """生成日报"""
        today_trades = self.get_today_trades()
        buys = [t for t in today_trades if t["side"] == "BUY"]
        sells = [t for t in today_trades if t["side"] == "SELL"]
        today_pnl = sum(t["pnl"] for t in sells)

        summary = {
            "date": datetime.now(BEIJING_TZ).date().isoformat(),
            "total_trades": len(today_trades),
            "buys": len(buys),
            "sells": len(sells),
            "today_pnl": round(today_pnl, 4),
            "total_pnl": round(self.get_total_pnl(), 4),
            "free_usdt": round(free_usdt, 2),
            "total_value": round(total_value, 2),
            "holdings": holdings_info,
        }

        # 保存
        with open(DAILY_SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        return summary
