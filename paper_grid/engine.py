"""确定性的事件驱动策略与纸面撮合；没有网络或实盘下单能力。"""
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .config import COINS, SCENARIOS, Settings
from .policy import BuyBudget, RollingRange, Signal, inventory_policy, take_profit_spacing


def quantize(value: float, step: float, up: bool = False) -> float:
    d, s = Decimal(str(value)), Decimal(str(step))
    # 仅修正浮点运算造成的整步附近尾差，不把真实不足一步的数量向上取整。
    units = d / s
    nearest = units.to_integral_value()
    if abs(units - nearest) < Decimal("1e-9"):
        d = nearest * s
    return float((d / s).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * s)




class Engine:
    def __init__(self, settings: Settings | None = None, now: float = 0):
        self.cfg = settings or Settings()
        self.signals = {coin + quote: Signal() for quote, _ in SCENARIOS.values() for coin in COINS}
        self.filters = {}
        self.symbol_stats = {sym: {"realized": 0.0, "fees": 0.0, "buys": 0, "sells": 0, "roundtrips": 0} for sym in self.signals}
        self.accounts = {name: {"quote": quote, "fee": fee, "cash": self.cfg.capital,
                               "realized": 0.0, "fees": 0.0, "buys": 0, "sells": 0,
                               "roundtrips": 0, "peak_equity": self.cfg.capital, "max_drawdown": 0.0}
                         for name, (quote, fee) in SCENARIOS.items()}
        self.budget = BuyBudget(self.cfg)
        self.orders = {}
        self.lots = {}
        self.seq = 0
        self.events = []
        self.decision_states = {}
        self.dirty = False
        self.started = now
        self.report_at = now
        self.report_baseline = {}
        self.counters = {"quotes": 0, "trades": 0, "rejections": 0, "cancels": 0,
                         "disconnects": 0, "trade_gaps": 0}

    def on_packet(self, packet: dict, now: float):
        """实时接入和历史回放使用同一事件适配器，时间由调用方传入。"""
        data = packet.get("data", packet)
        sym = data.get("s")
        if sym not in self.signals:
            return
        if "b" in data and "a" in data and "u" in data:
            self.on_quote(sym, float(data["b"]), float(data["a"]), now, int(data["u"]))
        elif data.get("e") == "aggTrade":
            self.on_trade(sym, float(data["p"]), float(data["q"]), data["m"], int(data["a"]), data["T"] / 1000, now)
        elif data.get("e") == "kline" and data["k"]["x"]:
            k = data["k"]
            self.signals[sym].candle(k["T"] / 1000, float(k["o"]), float(k["h"]), float(k["l"]))

    def event(self, kind: str, now: float, **data):
        self.events.append({"time": now, "kind": kind, **data})
        self.dirty = True

    def key(self, symbol: str) -> str:
        return "usdc_zero" if symbol.endswith("USDC") else "bnb_discount"

    def inventory(self, account: str, symbol: str | None = None) -> float:
        return sum(x["cost"] for x in self.lots.values() if x["account"] == account and (symbol is None or x["symbol"] == symbol))

    def reserve(self, account: str, symbol: str | None = None) -> float:
        fee = self.accounts[account]["fee"]
        return sum(o["price"] * o["remaining"] * (1 + fee) for o in self.orders.values()
                   if o["side"] == "buy" and o["account"] == account and (symbol is None or o["symbol"] == symbol))

    def new_order(self, symbol: str, side: str, price: float, qty: float, spacing: float, now: float, lot_id: str = ""):
        tick, step, minimum = self.filters[symbol]
        price, qty = quantize(price, tick, side == "sell"), quantize(qty, step)
        if price <= 0 or qty <= 0 or price * qty < minimum:
            return None
        self.seq += 1
        oid = str(self.seq)
        order = {"id": oid, "symbol": symbol, "account": self.key(symbol), "side": side,
                 "price": price, "remaining": qty, "qty": qty, "spacing": spacing,
                 "created": now, "active_at": now + self.cfg.latency,
                 "active": False, "cancel_at": 0.0, "lot_id": lot_id or oid}
        if side == "buy":
            signal = self.signals[symbol]
            tp, normal_vol = take_profit_spacing(self.cfg, self.accounts[self.key(symbol)]["fee"], signal.candles)
            order["tp_spacing"] = tp
            order["decision"] = {"normal_vol": normal_vol, "metrics": dict(getattr(signal, "metrics", {})),
                                 "inventory_cost": self.inventory(self.key(symbol), symbol),
                                 "account_inventory": self.inventory(self.key(symbol)),
                                 "coin_factor": self.coin_policy(symbol)[0], "buy_spacing": spacing,
                                 "tp_spacing": tp, "recovery": signal.recovery}
        self.orders[oid] = order
        self.event("submit", now, order=order.copy())
        return order

    def cancel(self, order: dict, now: float, reason: str):
        if not order["cancel_at"]:
            order["cancel_at"] = now + self.cfg.cancel_latency
            self.event("cancel_request", now, order_id=order["id"], reason=reason)

    def advance(self, now: float):
        for oid, o in list(self.orders.items()):
            if o["cancel_at"] and now >= o["cancel_at"]:
                self.orders.pop(oid)
                self.counters["cancels"] += 1
                self.event("canceled", now, order_id=oid)
            elif not o["active"] and now >= o["active_at"]:
                s = self.signals[o["symbol"]]
                stale = not s.at or now - s.at > self.cfg.stale_seconds
                crosses = o["price"] >= s.ask if o["side"] == "buy" else o["price"] <= s.bid
                if stale or crosses:
                    self.orders.pop(oid)
                    self.counters["rejections"] += 1
                    self.event("rejected", now, order_id=oid, reason="stale" if stale else "post_only")
                else:
                    o["active"] = True
                    self.event("accepted", now, order_id=oid)

    def tick(self, now: float):
        self.advance(now)
        for o in list(self.orders.values()):
            s = self.signals[o["symbol"]]
            if o["side"] == "buy" and now - s.at > self.cfg.stale_seconds:
                self.cancel(o, now, "stale_data")

    def on_quote(self, symbol: str, bid: float, ask: float, now: float, update_id: int = 0):
        if symbol not in self.signals or not (0 < bid < ask and math.isfinite(ask)):
            return
        s = self.signals[symbol]
        if now < s.at or (update_id and update_id <= s.quote_id):
            return
        # 报价静默不等于传输断流；保留滚动窗口，恢复报价立即重新评估冲击。
        s.quote_id = update_id or s.quote_id
        self.counters["quotes"] += 1
        ready, effective_vol = s.update(bid, ask, now, self.cfg)
        state = (s.paused, s.recovery, ready)
        if self.decision_states.get(symbol) != state:
            self.decision_states[symbol] = state
            self.event("signal_transition", now, symbol=symbol, paused=s.paused,
                       recovery=s.recovery, ready=ready, reason=s.reason, metrics=dict(s.metrics))
        self.advance(now)
        if symbol not in self.filters:
            return
        self.ensure_sells(symbol, now)
        buys = [o for o in self.orders.values() if o["symbol"] == symbol and o["side"] == "buy"]
        if not ready:
            for o in buys:
                self.cancel(o, now, s.reason)
            return
        account = self.key(symbol)
        a = self.accounts[account]
        inv = self.inventory(account)
        # 两边费率都以计价币估值，保证目标价在扣费后仍达到最低利润。
        floor = (1 + a["fee"]) * (1 + self.cfg.min_profit) / (1 - a["fee"]) - 1
        target_s = max(self.cfg.vol_k * effective_vol, floor, self.cfg.min_spacing)
        target_s *= 1 + self.cfg.inventory_widen * inv / self.cfg.max_inventory
        coin_factor, _ = self.coin_policy(symbol)
        target_s *= coin_factor
        target_s = min(target_s, self.cfg.max_spacing)
        dt = max(0, now - s.last_calc)
        s.last_calc = now
        if target_s >= s.spacing:
            s.spacing = target_s
            s.shrink_since = 0.0
        else:
            s.shrink_since = s.shrink_since or now
            if now - s.shrink_since >= self.cfg.shrink_stable:
                s.spacing += (1 - math.exp(-dt / self.cfg.shrink_tau)) * (target_s - s.spacing)
        mid = (bid + ask) / 2
        entry = self.budget.entries.get(symbol)
        own_lots = set(entry["lot_ids"]) if entry else set()
        holdings = [x for lid, x in self.lots.items()
                    if x["symbol"] == symbol and not self.is_dust(x) and lid not in own_lots]
        cap = min(x["buy_price"] for x in holdings) * (1 - s.spacing) if holdings else float("inf")
        price = min(bid, mid * (1 - s.spacing / 2), cap)
        if buys:
            o = buys[0]
            if o["cancel_at"]:
                return
            widen = s.spacing >= o["spacing"] * (1 + self.cfg.widen_reprice)
            above_cap = o["price"] > cap + self.filters[symbol][0] / 2
            upward = (price / o["price"] - 1) >= s.spacing * self.cfg.reprice_fraction
            s.up_since = (s.up_since or now) if upward else 0.0
            if now - o["created"] >= self.cfg.min_order_life and (widen or above_cap or (upward and now - s.up_since >= self.cfg.reprice_confirm)):
                self.cancel(o, now, "spacing_or_anchor")
                s.up_since = 0.0
            return  # 普通回调不追着价格下移，保留成交机会
        self.place_buy(symbol, price, now)

    def coin_policy(self, symbol: str) -> tuple[float, float]:
        account = self.key(symbol)
        return inventory_policy(self.cfg, self.inventory(account, symbol) + self.reserve(account, symbol))

    def place_buy(self, symbol: str, price: float, now: float):
        """买入唯一入口：按档位额度下单，滚动额度或库存现金不足则等待。"""
        s = self.signals[symbol]
        account = self.key(symbol)
        fee = self.accounts[account]["fee"]
        entry = self.budget.entries.get(symbol)
        minimum = self.filters[symbol][2]
        tick, step, _ = self.filters[symbol]
        buy_price = quantize(price, tick)
        remainder_notional = (quantize(entry["remaining"] / buy_price, step) * buy_price
                              if entry and buy_price > 0 else 0.0)
        if entry and remainder_notional < minimum:
            self.event("entry_complete", now, symbol=symbol, unused=entry["remaining"])
            del self.budget.entries[symbol]
            return  # 下一条报价用完整持仓重新计算下一档锚点
        capacity = min(self.cfg.max_inventory - self.inventory(account) - self.reserve(account),
                       self.cfg.max_coin_inventory - self.inventory(account, symbol) - self.reserve(account, symbol),
                       self.accounts[account]["cash"] - self.reserve(account) - self.cfg.min_cash) / (1 + fee)
        rolling = self.budget.allowance(symbol, now, self.reserve(account, symbol) / (1 + fee))
        amount = min(entry["remaining"] if entry else self.cfg.lot,
                     entry["order_cap"] if entry else self.cfg.lot,
                     self.cfg.lot * (self.cfg.recovery_fraction if s.recovery else 1))
        if amount < minimum or capacity + 1e-9 < amount or rolling + 1e-9 < amount:
            s.reason = "buy_rate_limit" if rolling + 1e-9 < amount else "inventory_limit"
            return
        order = self.new_order(symbol, "buy", price, amount / price, s.spacing, now)
        if order:
            self.budget.start(symbol, s.recovery)
            s.recovery = False


    def ensure_sells(self, symbol: str, now: float):
        s = self.signals[symbol]
        if now - s.at > self.cfg.stale_seconds:
            return
        self.merge_dust(symbol, now)
        step = self.filters[symbol][1]
        for lid, lot in list(self.lots.items()):
            if lot["symbol"] != symbol or lot["qty"] <= 1e-12:
                continue
            sells = [o for o in self.orders.values() if o["side"] == "sell" and o["lot_id"] == lid]
            # 撤单确认前仍占用库存，避免延迟成交导致重复卖出。
            covered = sum(o["remaining"] for o in sells)
            missing = max(0.0, lot["qty"] - covered)
            if missing < step / 2 or any(o["cancel_at"] for o in sells):
                continue
            order = self.new_order(symbol, "sell", max(lot["target"], s.ask), missing, lot["spacing"], now, lid)
            if order is None and sells:
                # 追加量不足最小名义值：撤旧单，确认后按当时实际余量重挂。
                for o in sells:
                    self.cancel(o, now, "sell_coverage_resize")

    def is_dust(self, lot: dict) -> bool:
        """统一按实际可挂卖的价格、数量精度和最小名义值识别尘埃。"""
        symbol = lot["symbol"]
        tick, step, minimum = self.filters[symbol]
        price = quantize(max(lot["target"], self.signals[symbol].ask), tick, True)
        qty = quantize(lot["qty"], step)
        return qty <= 0 or price * qty < minimum

    def merge_dust(self, symbol: str, now: float):
        """只合并已无任何订单的同币尘埃，保留成本、最高止盈目标及来源。"""
        busy = {o["lot_id"] for o in self.orders.values()}
        busy.update(lid for entry in self.budget.entries.values() for lid in entry["lot_ids"])
        minimum = self.filters[symbol][2]
        dust = [(lid, lot) for lid, lot in self.lots.items()
                if lot["symbol"] == symbol and lid not in busy and lot["qty"] > 0
                and self.is_dust(lot)]
        if len(dust) < 2:
            return
        target = max(lot["target"] for _, lot in dust)
        qty = sum(lot["qty"] for _, lot in dust)
        if quantize(qty, self.filters[symbol][1]) * max(target, self.signals[symbol].ask) < minimum:
            return
        lid, first = dust[0]
        sources = [source for key, lot in dust for source in lot.get("source_lots", [key])]
        merged = dict(first, qty=qty, cost=sum(lot["cost"] for _, lot in dust),
                      buy_price=min(lot["buy_price"] for _, lot in dust), target=target,
                      spacing=max(lot["spacing"] for _, lot in dust),
                      opened=min(lot["opened"] for _, lot in dust), source_lots=sources)
        for key, _ in dust:
            del self.lots[key]
        self.lots[lid] = merged
        self.event("dust_merge", now, symbol=symbol, lot_id=lid, source_lots=sources,
                   qty=qty, cost=merged["cost"], target=target)

    def on_trade(self, symbol: str, price: float, qty: float, buyer_maker: bool, trade_id: int, exchange_at: float, now: float):
        if symbol not in self.signals or price <= 0 or qty <= 0:
            return
        s = self.signals[symbol]
        if trade_id <= s.last_trade_id:
            return
        if s.last_trade_id and trade_id != s.last_trade_id + 1:
            self.counters["trade_gaps"] += 1
            self.disconnect(now, symbol, "trade_gap")
            s = self.signals[symbol]
        s.last_trade_id, s.last_trade_at = trade_id, now
        self.counters["trades"] += 1
        self.advance(now)
        # 只使用新鲜、发生在订单激活之后的成交；不根据报价触碰推断成交。
        if abs(now - exchange_at) > self.cfg.stale_seconds or now - s.at > self.cfg.stale_seconds:
            return
        available = qty * self.cfg.participation
        candidates = sorted(self.orders.values(), key=lambda o: ((-o["price"] if buyer_maker else o["price"]), o["created"]))
        for o in candidates:
            if o["symbol"] != symbol or not o["active"] or exchange_at < o["active_at"]:
                continue
            tick, step, _ = self.filters[symbol]
            crosses = (buyer_maker and o["side"] == "buy" and price <= o["price"] - tick * 0.5) or (not buyer_maker and o["side"] == "sell" and price >= o["price"] + tick * 0.5)
            if not crosses:
                continue
            fill = quantize(min(o["remaining"], available), step)
            if fill <= 0:
                continue
            available -= fill
            self.fill(o, fill, now)
        self.ensure_sells(symbol, now)

    def fill(self, o: dict, qty: float, now: float):
        a = self.accounts[o["account"]]
        gross, fee = o["price"] * qty, o["price"] * qty * a["fee"]
        a["fees"] += fee
        stats = self.symbol_stats[o["symbol"]]
        stats["fees"] += fee
        lid = o["lot_id"]
        pnl = 0.0
        if o["side"] == "buy":
            self.budget.record(o["symbol"], lid, gross, now)
            entry = self.budget.entries[o["symbol"]]
            if entry["remaining"] < self.filters[o["symbol"]][2] and o["remaining"] - qty < self.filters[o["symbol"]][1] / 2:
                self.event("entry_complete", now, symbol=o["symbol"], unused=entry["remaining"])
                del self.budget.entries[o["symbol"]]
            a["cash"] -= gross + fee
            a["buys"] += 1
            stats["buys"] += 1
            if lid not in self.lots:
                target = max(o["price"] * (1 + o.get("tp_spacing", o["spacing"])), o["price"] * (1 + a["fee"]) * (1 + self.cfg.min_profit) / (1 - a["fee"]))
                self.lots[lid] = {"account": o["account"], "symbol": o["symbol"], "qty": 0.0,
                                  "cost": 0.0, "buy_price": o["price"], "target": target,
                                  "spacing": o["spacing"], "tp_spacing": o.get("tp_spacing", o["spacing"]), "opened": now}
            self.lots[lid]["qty"] += qty
            self.lots[lid]["cost"] += gross + fee
        else:
            lot = self.lots[lid]
            allocated = lot["cost"] * qty / lot["qty"]
            pnl = gross - fee - allocated
            a["cash"] += gross - fee
            a["realized"] += pnl
            a["sells"] += 1
            stats["sells"] += 1
            stats["realized"] += pnl
            lot["qty"] = max(0.0, lot["qty"] - qty)
            lot["cost"] = max(0.0, lot["cost"] - allocated)
        o["remaining"] = max(0.0, o["remaining"] - qty)
        if o["remaining"] < self.filters[o["symbol"]][1] / 2:
            self.orders.pop(o["id"], None)
        if self.lots[lid]["qty"] < self.filters[o["symbol"]][1] / 2 and not any(x["lot_id"] == lid and x["side"] == "buy" for x in self.orders.values()):
            completed = len(self.lots[lid].get("source_lots", [lid]))
            a["roundtrips"] += completed
            stats["roundtrips"] += completed
            self.event("roundtrip", now, account=o["account"], symbol=o["symbol"], holding_seconds=now - self.lots[lid]["opened"])
            del self.lots[lid]
        self.event("fill", now, order_id=o["id"], account=o["account"], symbol=o["symbol"], side=o["side"], price=o["price"], qty=qty, fee=fee, pnl=pnl)

    def disconnect(self, now: float, symbol: str | None = None, reason: str = "disconnect"):
        self.counters["disconnects"] += 1
        symbols = [symbol] if symbol else list(self.signals)
        for sym in symbols:
            old = self.signals[sym]
            fresh = Signal()
            fresh.candles, fresh.candle_end = old.candles, old.candle_end
            fresh.last_trade_id = old.last_trade_id
            fresh.paused, fresh.pause_until = old.paused, old.pause_until
            fresh.pause_low, fresh.recovery = old.pause_low, old.recovery
            self.signals[sym] = fresh
        for oid, o in list(self.orders.items()):
            if o["symbol"] in symbols:
                # 数据缺口期间无法模拟挂单，明确撤除，不补猜离线成交。
                del self.orders[oid]
        self.event("data_gap_cancel", now, symbols=symbols, reason=reason)

    def snapshot(self) -> dict:
        return {"schema": 2, "buy_budget": self.budget.snapshot(),
                "safety": {sym: {k: (None if k == "pause_low" and not math.isfinite(s.pause_low) else getattr(s, k))
                                 for k in ("paused", "pause_until", "pause_low", "recovery")}
                           for sym, s in self.signals.items()}, "settings": self.cfg.to_dict(), "accounts": self.accounts,
                "symbol_stats": self.symbol_stats,
                "orders": self.orders, "lots": self.lots, "seq": self.seq,
                "started": self.started, "report_at": self.report_at,
                "report_baseline": self.report_baseline, "counters": self.counters}

    def restore(self, data: dict, now: float):
        if data.get("schema") != 2 or data.get("settings") != self.cfg.to_dict():
            raise ValueError("状态版本或参数不一致；必须保留旧实验并另开数据目录")
        for name in ("accounts", "orders", "lots", "seq", "started", "report_at", "report_baseline", "counters"):
            setattr(self, name, data[name])
        self.budget.restore(data["buy_budget"])
        for sym, fields in data["safety"].items():
            for key, value in fields.items():
                setattr(self.signals[sym], key, float("inf") if key == "pause_low" and value is None else value)
        self.symbol_stats = data.get("symbol_stats", self.symbol_stats)
        self.disconnect(now, reason="restart_no_offline_fill_inference")

    def summary(self, now: float) -> dict:
        result = {"time": now, "started": self.started, "settings": self.cfg.to_dict(),
                  "counters": self.counters.copy(), "accounts": {}, "symbols": {}}
        for name, a in self.accounts.items():
            market_value = 0.0
            stale = []
            for lot in self.lots.values():
                if lot["account"] != name:
                    continue
                s = self.signals[lot["symbol"]]
                mark = s.bid or lot["buy_price"]
                if now - s.at > self.cfg.stale_seconds:
                    stale.append(lot["symbol"])
                market_value += lot["qty"] * mark
            equity = a["cash"] + market_value
            if not stale:
                a["peak_equity"] = max(a["peak_equity"], equity)
                a["max_drawdown"] = max(a["max_drawdown"], 1 - equity / a["peak_equity"])
            result["accounts"][name] = {**a, "equity": equity, "unrealized": market_value - self.inventory(name),
                "inventory_cost": self.inventory(name), "reserved": self.reserve(name),
                "open_lots": sum(x["account"] == name for x in self.lots.values()),
                "oldest_lot_seconds": max([now - x["opened"] for x in self.lots.values() if x["account"] == name] or [0]),
                "valuation_valid": not stale, "stale_marks": sorted(set(stale))}
        for sym, s in self.signals.items():
            lots = [x for x in self.lots.values() if x["symbol"] == sym]
            result["symbols"][sym] = {**self.symbol_stats[sym], "bid": s.bid, "ask": s.ask, "quote_age": now - s.at if s.at else None,
                "reason": s.reason, "paused": s.paused, "spacing": s.spacing,
                "buy_allowance": self.budget.allowance(sym, now, self.reserve(self.key(sym), sym) / (1 + self.accounts[self.key(sym)]["fee"])),
                "entry_remaining": self.budget.entries.get(sym, {}).get("remaining"),
                "coin_inventory_factor": self.coin_policy(sym)[0],
                "coin_order_cap": self.coin_policy(sym)[1],
                "unrealized": sum(x["qty"] * (s.bid or x["buy_price"]) - x["cost"] for x in lots),
                "dust_lots": sum(self.is_dust(x) for x in lots) if sym in self.filters else 0,
                "metrics": getattr(s, "metrics", {}),
                "inventory_cost": self.inventory(self.key(sym), sym),
                "buy_orders": sum(o["symbol"] == sym and o["side"] == "buy" for o in self.orders.values()),
                "sell_orders": sum(o["symbol"] == sym and o["side"] == "sell" for o in self.orders.values())}
        return result
