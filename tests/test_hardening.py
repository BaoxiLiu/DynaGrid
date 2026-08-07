import json
import os
import tempfile
import threading
import time
import unittest
import csv
from unittest.mock import patch

import exchange as exchange_module
import pnl as pnl_module
import state as state_module
from config import MONITOR_COINS
from exchange import Exchange
from pnl import PnLTracker
from market_data import (
    BinanceBookTickerStream, MarketDataCache, MarketQuote, WebSocketTriggerObserver,
)
from risk import RiskManager
from state import StateCorruptionError, StateManager, TierReentryState
from strategy import StrategyEngine
from volatility import VolatilityProfile, calculate_volatility_profile


class RequestTimeout(Exception):
    pass


class FakeApi:
    def __init__(self):
        self.markets = {
            "SOL/USDT": {
                "id": "SOLUSDT",
                "limits": {"cost": {"min": 10}},
                "precision": {"amount": 0.001, "price": 0.01},
            }
        }
        self.create_buy_calls = 0
        self.create_sell_calls = 0
        self.recovered_order = None
        self.lookup_error = None

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.3f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def create_market_buy_order(self, symbol, qty, params):
        self.create_buy_calls += 1
        client_id = params["clientOrderId"]
        self.recovered_order = {
            "id": "buy-1", "clientOrderId": client_id, "side": "buy",
            "status": "closed", "average": 100.0, "filled": float(qty),
            "amount": float(qty), "remaining": 0, "cost": 15.0,
        }
        raise RequestTimeout("response lost")

    def fetch_order_by_client_id(self, client_id, symbol):
        if self.lookup_error:
            raise self.lookup_error
        return self.recovered_order

    def create_market_sell_order(self, symbol, qty, params):
        self.create_sell_calls += 1
        return {
            "id": "sell-1", "clientOrderId": params.get("clientOrderId", ""),
            "side": "sell", "status": "closed", "average": 101.0,
            "filled": float(qty), "amount": float(qty), "remaining": 0,
        }


class FakePnL:
    def record_sell(self, *args, **kwargs):
        return 0.1


class FakeReconcileExchange:
    def __init__(self, extra_order=False, balances=None):
        self.extra_order = extra_order
        self.balances = balances or {"SOL": 1.0, "USDT": 300.0}

    def fetch_balance(self):
        return self.balances

    def fetch_open_orders(self):
        orders = [{
            "coin": "SOL", "order_id": "sell-local", "side": "sell",
            "status": "open", "amount": 1.0, "filled": 0.0,
        }]
        if self.extra_order:
            orders.append({
                "coin": "SOL", "order_id": "orphan", "side": "sell",
                "status": "open", "amount": 0.2, "filled": 0.0,
            })
        return orders

    def fetch_tickers(self, coins):
        return {coin: {"price": 100.0} for coin in coins}

    def get_min_notional(self, coin):
        return 10.0

    def fetch_order_status(self, order_id, coin):
        return {
            "coin": coin, "order_id": order_id, "side": "sell",
            "status": "open", "amount": 1.0, "filled": 0.0,
            "remaining": 1.0, "price": 101.0,
        }

    def fetch_order_by_client_id(self, coin, client_id):
        return None


class HardeningTests(unittest.TestCase):
    def state_paths(self, directory):
        return patch.multiple(
            state_module,
            STATE_FILE=os.path.join(directory, "state.json"),
            STATE_BACKUP_FILE=os.path.join(directory, "state.json.bak"),
            STATE_LOCK_FILE=os.path.join(directory, "state.lock"),
        )

    def test_exchange_rate_limit_circuit_parses_ban_expiry_and_429_fallback(self):
        exchange = Exchange(api=FakeApi())
        now = 1_700_000_000.0
        banned_until_ms = int((now + 95) * 1000)
        exchange._failure(
            "binance 418 I'm a teapot "
            f'{{"code":-1003,"msg":"IP banned until {banned_until_ms}"}}'
        )
        self.assertEqual(exchange.rate_limit_cooldown_seconds(now), 95)

        fallback = Exchange(api=FakeApi())
        with patch("exchange.time.time", return_value=now):
            fallback._failure('binance 429 Too Many Requests {"code":-1003}')
        self.assertGreaterEqual(fallback.rate_limit_cooldown_seconds(now), 60)

    def test_new_t1_candidates_follow_market_cap_order_not_config_order(self):
        manager = StateManager()
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())
        test_coins = ["BTC", "ETH", "DOGE", "TRX", "SOL"]
        ranked = ["DOGE", "TRX", "SOL", "ETH", "BTC"]
        with patch("strategy.MONITOR_COINS", test_coins):
            tickers = {coin: {"price": 100.0} for coin in test_coins}
            with patch("strategy.MarketCapRanker.get_ranked", return_value=ranked):
                candidates = engine._collect_buy_candidates(tickers, 400)

        tier1_coins = [
            candidate["coin"] for candidate in candidates
            if candidate["tier_index"] == 0 and candidate["is_new"]
        ]
        # 按市值顺序: DOGE 第一，TRX 第二，SOL 第三
        self.assertEqual(tier1_coins[:3], ["DOGE", "TRX", "SOL"])

    def test_state_is_atomic_backward_compatible_and_counts_only_holding(self):
        with tempfile.TemporaryDirectory() as directory, self.state_paths(directory):
            state_path = os.path.join(directory, "state.json")
            old_state = {
                "coins": {
                    "SOL": {
                        "base_price": 100,
                        "active": True,
                        "tiers": [
                            {"tier_index": 0, "amount_usdt": 15,
                             "buy_price": 100, "sell_price": 101, "qty": 0.15,
                             "sell_order_id": "a", "status": "holding"},
                            {"tier_index": 1, "amount_usdt": 15,
                             "buy_price": 96, "sell_price": 97, "qty": 0.15,
                             "sell_order_id": "b", "status": "sold"},
                        ],
                    }
                },
                "cooling": {"ADA": 1.0},
            }
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(old_state, f)

            manager = StateManager()
            self.assertTrue(manager.load())
            self.assertEqual(manager.get_total_committed_all(), 15)
            self.assertTrue(manager.check_cooling("ADA", 0))
            manager.save()

            with open(state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["schema_version"], 3)
            self.assertTrue(os.path.exists(os.path.join(directory, "state.json.bak")))

    def test_retire_dust_tier_removes_strategy_holding_and_starts_cooling(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 0, 15, 100, 101, 0.0001, "")
        manager.block_coin("SOL", "below minimum order size")

        self.assertTrue(manager.retire_dust_tier("SOL", 0, 101))
        self.assertNotIn("SOL", manager.coins)
        self.assertTrue(manager.is_cooling("SOL"))
        self.assertFalse(manager.is_blocked("SOL"))

    def test_holdings_summary_shows_next_unfilled_tier_trigger(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 0, 15, 100, 101, 0.15, "sell-local")
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())

        summary = engine.get_holdings_summary({"SOL": {"price": 98}}, 100)
        target = summary[0]["next_buy"]
        self.assertEqual(target["tier"], "T2")
        self.assertEqual(target["amount"], 20)
        self.assertEqual(target["trigger_price"], 96.0)
        self.assertEqual(target["status"], "等待触发")

    def test_holdings_summary_hides_unexecutable_next_tier(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 0, 15, 100, 101, 0.15, "sell-local")
        manager.block_coin("SOL", "manual review")
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())

        summary = engine.get_holdings_summary({"SOL": {"price": 98}}, 100)
        self.assertIsNone(summary[0]["next_buy"])

    def test_websocket_observer_emits_once_then_rearms(self):
        cache = MarketDataCache()
        observer = WebSocketTriggerObserver(cache, stale_seconds=5, rearm_rate=0.001)
        observer.set_levels([{
            "coin": "SOL", "tier": "T2", "tier_index": 1,
            "trigger_price": 96.0, "source": "static_tier",
        }])

        now = 1_700_000_000.0
        with patch("market_data.time.time", return_value=now):
            observer.on_quote(MarketQuote("SOL", 95.9, 95.95, now))
            first = observer.drain_events()
            observer.on_quote(MarketQuote("SOL", 95.8, 95.9, now))
            second = observer.drain_events()
            observer.on_quote(MarketQuote("SOL", 96.2, 96.2, now))
            observer.on_quote(MarketQuote("SOL", 95.8, 95.9, now))
            third = observer.drain_events()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(third), 1)
        self.assertIn("仅观察，不下单", first[0])

    def test_websocket_trigger_only_wakes_rest_with_debounce(self):
        cache = MarketDataCache()
        wake_event = threading.Event()
        observer = WebSocketTriggerObserver(
            cache, stale_seconds=5, rearm_rate=0.001,
            rest_wakeup_event=wake_event, wake_debounce_seconds=2,
        )
        observer.set_levels([{
            "coin": "SOL", "tier": "T2", "tier_index": 1,
            "trigger_price": 96.0, "source": "static_tier",
        }])

        with patch("market_data.time.time", return_value=100.0):
            observer.on_quote(MarketQuote("SOL", 95.9, 95.9, 100.0))
            self.assertTrue(wake_event.is_set())
            wake_event.clear()
            observer.on_quote(MarketQuote("SOL", 96.2, 96.2, 100.0))
            observer.on_quote(MarketQuote("SOL", 95.9, 95.9, 100.0))
            self.assertFalse(wake_event.is_set())

        with patch("market_data.time.time", return_value=103.0):
            observer.on_quote(MarketQuote("SOL", 96.2, 96.2, 103.0))
            observer.on_quote(MarketQuote("SOL", 95.9, 95.9, 103.0))
            self.assertTrue(wake_event.is_set())

    def test_websocket_drops_out_of_order_quote_and_reconnect_wakes_rest(self):
        cache = MarketDataCache()
        now = time.time()
        self.assertTrue(cache.update("SOL", 100, 101, updated_at=now, sequence=20))
        self.assertFalse(cache.update("SOL", 90, 91, updated_at=now + 1, sequence=19))
        self.assertEqual(cache.get("SOL", max_age=10).ask, 101)

        wake_event = threading.Event()
        observer = WebSocketTriggerObserver(cache, stale_seconds=5, rearm_rate=0.001)
        stream = BinanceBookTickerStream(
            ["SOL"], cache, observer, on_reconnect=wake_event.set,
        )
        stream._handle_open(None)  # 首次连接不需要额外 REST 扫描
        self.assertFalse(wake_event.is_set())
        stream._handle_open(None)  # 重连必须尽快恢复 REST 复核
        self.assertTrue(wake_event.is_set())

    def test_websocket_levels_use_saved_dynamic_price_without_state_mutation(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 0, 15, 100, 101, 0.15, "sell-t1")
        manager.add_tier("SOL", 1, 20, 96, 97.44, 0.2, "sell-t2")
        manager.mark_tier_sold("SOL", 1)
        manager.set_tier_reentry("SOL", TierReentryState(
            tier_index=1, next_trigger_price=94.0, policy_version=2,
        ))
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())

        before = [(t.tier_index, t.status) for t in manager.coins["SOL"].tiers]
        levels = engine.get_websocket_observation_levels()
        after = [(t.tier_index, t.status) for t in manager.coins["SOL"].tiers]

        dynamic = [level for level in levels if level["tier"] == "T2"]
        self.assertEqual(dynamic[0]["trigger_price"], 94.0)
        self.assertEqual(dynamic[0]["source"], "dynamic_reentry")
        self.assertEqual(before, after)

    def test_corrupt_primary_recovers_backup_but_both_corrupt_raise(self):
        with tempfile.TemporaryDirectory() as directory, self.state_paths(directory):
            primary = os.path.join(directory, "state.json")
            backup = os.path.join(directory, "state.json.bak")
            with open(primary, "w", encoding="utf-8") as f:
                f.write("{")
            with open(backup, "w", encoding="utf-8") as f:
                json.dump({"coins": {}, "cooling": {}}, f)
            self.assertTrue(StateManager().load())

            with open(backup, "w", encoding="utf-8") as f:
                f.write("{")
            with self.assertRaises(StateCorruptionError):
                StateManager().load()

    def test_timeout_recovers_by_client_id_without_duplicate_buy(self):
        api = FakeApi()
        ex = Exchange(api=api)
        ex._symbol_map = {"SOL": "SOL/USDT"}
        with patch.object(exchange_module.time, "sleep", return_value=None):
            result = ex.market_buy("SOL", 15, client_order_id="asg-test-buy")
        self.assertEqual(api.create_buy_calls, 1)
        self.assertEqual(result["order_id"], "buy-1")
        self.assertEqual(ex.last_order_outcome, "confirmed")

    def test_unknown_timeout_never_blindly_retries(self):
        api = FakeApi()
        api.lookup_error = RuntimeError("query unavailable")
        ex = Exchange(api=api)
        ex._symbol_map = {"SOL": "SOL/USDT"}
        with patch.object(exchange_module.time, "sleep", return_value=None):
            result = ex.market_buy("SOL", 15, client_order_id="asg-test-buy")
        self.assertIsNone(result)
        self.assertEqual(api.create_buy_calls, 1)
        self.assertEqual(ex.last_order_outcome, "unknown")

    def test_market_sell_handles_precision_string(self):
        api = FakeApi()
        ex = Exchange(api=api)
        ex._symbol_map = {"SOL": "SOL/USDT"}
        result = ex.market_sell("SOL", 0.203, client_order_id="asg-test-sell")
        self.assertEqual(result["qty"], 0.203)
        self.assertEqual(api.create_sell_calls, 1)

    def test_limit_sell_precision_rejection_is_not_an_exception(self):
        api = FakeApi()
        api.amount_to_precision = lambda symbol, amount: (_ for _ in ()).throw(
            RuntimeError("amount below exchange precision")
        )
        ex = Exchange(api=api)
        ex._symbol_map = {"SOL": "SOL/USDT"}
        self.assertIsNone(ex.limit_sell("SOL", 0.0001, 100))
        self.assertEqual(ex.last_order_outcome, "rejected")

    def test_risk_checks_post_trade_cash_and_active_commitment(self):
        manager = StateManager()
        risk = RiskManager(manager)
        self.assertFalse(risk.can_buy("SOL", 15, 34, False).allowed)
        self.assertTrue(risk.can_buy("SOL", 15, 100, False).allowed)

    def test_pnl_schema_migrates_and_sell_order_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "trades.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "coin", "tier", "side", "price", "qty",
                    "usdt_value", "fee", "pnl", "total_pnl",
                ])
                writer.writerow([
                    "2026-01-01T00:00:00+08:00", "SOL", "T1", "BUY",
                    100, 0.15, 15, 0.015, 0, 0,
                ])
            with patch.object(pnl_module, "TRADE_LOG_FILE", path):
                tracker = PnLTracker()
                first = tracker.record_sell(
                    "SOL", 0, 100, 101, 0.15, 0.015, 0.01515,
                    order_id="sell-1",
                )
                second = tracker.record_sell(
                    "SOL", 0, 100, 101, 0.15, 0.015, 0.01515,
                    order_id="sell-1",
                )
            self.assertEqual(first, second)
            with open(path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertIn("order_id", rows[0])

    def test_startup_reconcile_rejects_orphan_open_order(self):
        with tempfile.TemporaryDirectory() as directory, self.state_paths(directory), patch(
            "strategy.RECONCILE_REPORT_FILE", os.path.join(directory, "report.json")
        ):
            manager = StateManager()
            manager.add_coin("SOL", 100)
            manager.add_tier("SOL", 0, 15, 100, 101, 1.0, "sell-local")

            ok_engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())
            self.assertTrue(ok_engine.reconcile_startup()["ok"])

            bad_engine = StrategyEngine(
                FakeReconcileExchange(extra_order=True), manager, FakePnL(),
            )
            report = bad_engine.reconcile_startup()
            self.assertFalse(report["ok"])
            self.assertTrue(manager.is_blocked("SOL"))

    def test_startup_reconcile_allows_configured_bnb_fee_balance(self):
        with tempfile.TemporaryDirectory() as directory, self.state_paths(directory), patch(
            "strategy.RECONCILE_REPORT_FILE", os.path.join(directory, "report.json")
        ), patch("strategy.NON_STRATEGY_BALANCE_ALLOWANCES", {"BNB": 0.25}):
            manager = StateManager()
            manager.add_coin("SOL", 100)
            manager.add_tier("SOL", 0, 15, 100, 101, 1.0, "sell-local")
            exchange = FakeReconcileExchange(
                balances={"SOL": 1.0, "BNB": 0.18, "USDT": 300.0},
            )

            report = StrategyEngine(exchange, manager, FakePnL()).reconcile_startup()

            self.assertTrue(report["ok"])
            self.assertTrue(any("BNB" in warning for warning in report["warnings"]))

    def test_atr_profile_uses_closed_candles_and_historical_median(self):
        hour = 3_600_000
        rows = []
        for index in range(7):
            rows.append([
                index * hour, 100.0, 101.0, 99.0, 100.0, 1.0,
            ])
        # This open candle has an extreme range and must not affect the result.
        rows.append([7 * hour, 100.0, 150.0, 50.0, 100.0, 1.0])

        profile = calculate_volatility_profile(
            rows, timeframe="1h", period=2, baseline_bars=4,
            now_ms=7 * hour + hour // 2,
        )

        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile.atr, 2.0)
        self.assertAlmostEqual(profile.natr, 0.02)
        self.assertAlmostEqual(profile.median_natr, 0.02)
        self.assertAlmostEqual(profile.raw_ratio, 1.0)

    def test_dynamic_tier_reentry_enforces_minimum_pullback_and_atr_can_widen_it(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 1, 15, 100, 101.5, 0.15, "sell-t2")
        tier = manager.coins["SOL"].tiers[-1]
        tier.status = "sold"
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())

        def profile(ratio):
            return VolatilityProfile(
                atr=1.0, natr=0.01 * ratio, median_natr=0.01,
                raw_ratio=ratio, latest_candle_timestamp=1,
                baseline_samples=720,
            )

        with patch.object(engine, "_get_volatility_profile", return_value=profile(1.0)):
            normal = engine._configure_dynamic_reentry("SOL", tier, 101.5)
        with patch.object(engine, "_get_volatility_profile", return_value=profile(0.5)):
            low = engine._configure_dynamic_reentry("SOL", tier, 101.5)
        with patch.object(engine, "_get_volatility_profile", return_value=profile(2.0)):
            high = engine._configure_dynamic_reentry("SOL", tier, 101.5)

        # T2 的最低回调为 2%，所以 ATR 即使较低或在上限也不能让回补变浅。
        self.assertAlmostEqual(normal.next_trigger_price, 101.5 * 0.98)
        self.assertAlmostEqual(low.next_trigger_price, 101.5 * 0.98)
        self.assertAlmostEqual(high.next_trigger_price, 101.5 * 0.98)
        self.assertAlmostEqual(normal.applied_gap_rate, 0.02)
        self.assertAlmostEqual(low.effective_vol_ratio, 1.0)
        self.assertAlmostEqual(high.effective_vol_ratio, 1.3)

        manager.add_tier("SOL", 3, 40, 70, 77, 0.5, "sell-t4")
        tier4 = manager.coins["SOL"].tiers[-1]
        tier4.status = "sold"
        with patch.object(engine, "_get_volatility_profile", return_value=profile(1.0)):
            normal_t4 = engine._configure_dynamic_reentry("SOL", tier4, 77)
        with patch.object(engine, "_get_volatility_profile", return_value=profile(2.0)):
            high_t4 = engine._configure_dynamic_reentry("SOL", tier4, 77)
        # T4 常态下执行最低 10% 回调；高波动时 ATR 建议 11.818%，进一步拉深。
        self.assertAlmostEqual(normal_t4.next_trigger_price, 77 * 0.90)
        self.assertAlmostEqual(normal_t4.applied_gap_rate, 0.10)
        self.assertAlmostEqual(high_t4.next_trigger_price, 67.9)
        self.assertAlmostEqual(high_t4.applied_gap_rate, (1 - 1 / 1.10) * 1.3)

    def test_missing_atr_disables_reentry_instead_of_using_static_level(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 0, 15, 100, 101, 0.15, "sell-t1")
        manager.add_tier("SOL", 1, 15, 96, 97.44, 0.15, "sell-t2")
        manager.coins["SOL"].tiers[-1].status = "sold"
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())

        with patch.object(engine, "_get_volatility_profile", return_value=None), patch(
            "strategy.MarketCapRanker.get_ranked", return_value=["SOL"]
        ):
            candidates = engine._collect_buy_candidates(
                {"SOL": {"price": 95}}, 300,
            )

        self.assertFalse(any(c["tier_index"] == 1 for c in candidates))
        reentry = manager.get_tier_reentry("SOL", 1)
        self.assertEqual(reentry.status, "unavailable")

    def test_old_reentry_policy_is_recalculated(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 1, 15, 100, 101.5, 0.15, "sell-t2")
        tier = manager.coins["SOL"].tiers[-1]
        tier.status = "sold"
        manager.set_tier_reentry("SOL", TierReentryState(
            tier_index=1, next_trigger_price=100.0, status="ready",
        ))
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())
        profile = VolatilityProfile(
            atr=1.0, natr=0.01, median_natr=0.01, raw_ratio=1.0,
            latest_candle_timestamp=1, baseline_samples=720,
        )

        with patch.object(engine, "_get_volatility_profile", return_value=profile):
            trigger = engine._dynamic_reentry_trigger("SOL", tier)

        self.assertAlmostEqual(trigger, 101.5 * 0.98)
        self.assertGreater(manager.get_tier_reentry("SOL", 1).policy_version, 0)

    def test_reentry_is_created_only_after_tier_is_fully_sold(self):
        manager = StateManager()
        manager.add_coin("SOL", 100)
        manager.add_tier("SOL", 1, 15, 100, 101.5, 1.0, "sell-t2")
        tier = manager.coins["SOL"].tiers[-1]
        tier.buy_fee_usdt = 0.01
        engine = StrategyEngine(FakeReconcileExchange(), manager, FakePnL())
        profile = VolatilityProfile(
            atr=1.0, natr=0.01, median_natr=0.01, raw_ratio=1.0,
            latest_candle_timestamp=1, baseline_samples=720,
        )
        summary = {"actions": [], "errors": []}

        engine._apply_sell_fill("SOL", tier, {
            "filled": 0.4, "price": 101.5, "fee": 0.001,
            "fee_currency": "USDT", "order_id": "partial",
        }, summary)
        self.assertEqual(tier.status, "holding")
        self.assertIsNone(manager.get_tier_reentry("SOL", 1))

        with patch.object(engine, "_get_volatility_profile", return_value=profile):
            engine._apply_sell_fill("SOL", tier, {
                "filled": 0.6, "price": 101.5, "fee": 0.001,
                "fee_currency": "USDT", "order_id": "final",
            }, summary)
        self.assertEqual(tier.status, "sold")
        self.assertAlmostEqual(
            manager.get_tier_reentry("SOL", 1).next_trigger_price,
            101.5 * 0.98,
        )

    def test_t4_neutral_gap_and_reentry_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory, self.state_paths(directory):
            manager = StateManager()
            manager.add_coin("BTC", 100)
            manager.set_tier_reentry("BTC", TierReentryState(
                tier_index=3,
                next_trigger_price=100,
                last_buy_price=100,
                anchor_sell_price=110,
                neutral_gap_rate=1 - (1 / 1.10),
                applied_gap_rate=1 - (1 / 1.10),
            ))
            manager.save()

            restored = StateManager()
            self.assertTrue(restored.load())
            reentry = restored.get_tier_reentry("BTC", 3)
            self.assertIsNotNone(reentry)
            self.assertAlmostEqual(reentry.next_trigger_price, 100)
            self.assertAlmostEqual(reentry.neutral_gap_rate, 1 - (1 / 1.10))


if __name__ == "__main__":
    unittest.main()
