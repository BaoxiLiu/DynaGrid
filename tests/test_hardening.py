import json
import tempfile
import threading
import unittest


class PaperGridTests(unittest.TestCase):
    def engine(self, **overrides):
        from dataclasses import replace
        from paper_grid.config import Settings
        from paper_grid.engine import Engine
        cfg = replace(Settings(), warmup_seconds=0, **overrides)
        engine = Engine(cfg, 1000)
        for symbol, s in engine.signals.items():
            engine.filters[symbol] = (0.01, 0.001, 5.0)
            for i in range(30):
                s.candle(900 + i, 100, 100.01, 99.99)
            s.last_trade_at = 1000
        return engine

    def quote(self, engine, symbol="BTCUSDC", now=1000, mid=100):
        engine.on_quote(symbol, mid - .01, mid + .01, now)

    def test_paper_fee_accounts_and_shared_budget(self):
        e = self.engine()
        for sym in e.signals:
            self.quote(e, sym)
        self.assertEqual(len(e.orders), 6)
        self.assertEqual(e.accounts["bnb_discount"]["cash"], 1000)
        self.assertEqual(e.accounts["usdc_zero"]["cash"], 1000)
        self.assertGreater(e.signals["BTCUSDT"].spacing, e.signals["BTCUSDC"].spacing)
        self.assertLessEqual(e.reserve("bnb_discount"), 76)

    def test_paper_quote_touch_never_fills(self):
        e = self.engine()
        self.quote(e)
        self.quote(e, now=1000.3, mid=99.98)
        self.assertEqual(e.accounts["usdc_zero"]["buys"], 0)

    def test_paper_trade_latency_direction_and_duplicate(self):
        e = self.engine()
        self.quote(e)
        order = next(iter(e.orders.values()))
        price = order["price"] - .02
        e.on_trade("BTCUSDC", price, 10, True, 1, 1000.1, 1000.1)
        self.assertFalse(e.lots)
        e.on_trade("BTCUSDC", price, 10, False, 2, 1000.3, 1000.3)
        self.assertFalse(e.lots)
        e.on_trade("BTCUSDC", order["price"], 10, True, 3, 1000.4, 1000.4)
        self.assertFalse(e.lots)
        e.on_trade("BTCUSDC", price, 10, True, 4, 1000.5, 1000.5)
        self.assertEqual(len(e.lots), 1)
        cash = e.accounts["usdc_zero"]["cash"]
        e.on_trade("BTCUSDC", price, 10, True, 4, 1000.5, 1000.6)
        self.assertEqual(e.accounts["usdc_zero"]["cash"], cash)

    def test_paper_partial_fill_during_cancel(self):
        e = self.engine()
        self.quote(e)
        o = next(iter(e.orders.values()))
        e.advance(1000.3)
        e.cancel(o, 1000.3, "test")
        e.on_trade("BTCUSDC", o["price"] - .02, 1, True, 1, 1000.4, 1000.4)
        self.assertAlmostEqual(e.lots[o["id"]]["qty"], .1)
        e.advance(1000.6)
        self.assertNotIn(o["id"], e.orders)
        self.assertEqual(e.accounts["usdc_zero"]["buys"], 1)

    def test_paper_roundtrip_accounting_with_bnb_fee(self):
        e = self.engine()
        self.quote(e, "BTCUSDT")
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDT", buy["price"] - .02, 10, True, 1, 1000.3, 1000.3)
        sell = next(o for o in e.orders.values() if o["side"] == "sell")
        e.on_trade("BTCUSDT", sell["price"] + .02, 10, False, 2, 1000.7, 1000.7)
        a = e.accounts["bnb_discount"]
        self.assertEqual(a["roundtrips"], 1)
        self.assertFalse(e.lots)
        self.assertGreater(a["fees"], 0)
        self.assertGreater(a["realized"], 0)
        self.assertAlmostEqual(a["cash"] - 1000, a["realized"])

    def test_paper_holdings_anchor_and_no_duplicate_buy(self):
        e = self.engine(buy_windows=((60, 100), (300, 200), (900, 300)))
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, 10, True, 1, 1000.3, 1000.3)
        self.quote(e, now=1000.4, mid=100.02)
        buys = [o for o in e.orders.values() if o["side"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertLess(buys[0]["price"], buy["price"])
        for i in range(10):
            self.quote(e, now=1000.5 + i * .01, mid=100.02)
        self.assertEqual(sum(o["side"] == "buy" for o in e.orders.values()), 1)

    def test_paper_event_driven_shock_does_not_wait_for_second(self):
        e = self.engine()
        self.quote(e, now=1000.1)
        self.quote(e, now=1000.15, mid=99.7)
        self.assertTrue(e.signals["BTCUSDC"].paused)
        self.assertTrue(next(iter(e.orders.values()))["cancel_at"])

    def test_paper_rolling_drawdown_obeys_time_order(self):
        from paper_grid.engine import RollingRange
        w = RollingRange(5)
        w.push(1, 100)
        self.assertAlmostEqual(w.push(2, 90)[2], .1)
        self.assertAlmostEqual(w.push(3, 101)[2], .1)
        self.assertEqual(w.push(8, 102)[2], 0)

    def test_paper_budget_blocks_new_buys(self):
        e = self.engine(max_inventory=4)
        self.quote(e)
        self.assertFalse(e.orders)

    def test_paper_restart_preserves_lots_cancels_orders(self):
        e = self.engine()
        self.quote(e)
        o = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", o["price"] - .02, 10, True, 1, 1000.3, 1000.3)
        restored = self.engine()
        restored.restore(json.loads(json.dumps(e.snapshot())), 1001)
        self.assertEqual(e.accounts, restored.accounts)
        self.assertEqual(e.lots, restored.lots)
        self.assertFalse(restored.orders)
        self.assertEqual(restored.signals["BTCUSDC"].first, 0)

    def test_paper_trade_gap_suspends_fill_inference(self):
        e = self.engine()
        self.quote(e)
        e.on_trade("BTCUSDC", 100, 1, True, 1, 1000.3, 1000.3)
        e.on_trade("BTCUSDC", 99, 10, True, 3, 1000.4, 1000.4)
        self.assertFalse(e.lots)
        self.assertFalse(e.orders)
        self.assertEqual(e.counters["trade_gaps"], 1)

    def test_paper_hour_report_includes_unrealized(self):
        from paper_grid.report import render
        e = self.engine()
        text = render(e.summary(1001), {}, 1000)
        self.assertIn("累计浮盈亏", text)
        self.assertIn("尚无完整回合", text)

    def test_paper_rolling_drawdown_matches_brute_force(self):
        import random
        from paper_grid.engine import RollingRange
        rng = random.Random(17)
        w = RollingRange(5)
        prices = []
        for i in range(300):
            now, price = i * .11, rng.uniform(90, 110)
            prices.append((now, price))
            prices = [(t, p) for t, p in prices if t >= now - 5]
            actual = w.push(now, price)
            peak, dd = 0, 0
            for _, p in prices:
                peak = max(peak, p)
                dd = max(dd, 1 - p / peak)
            self.assertAlmostEqual(actual[0], max(p for _, p in prices))
            self.assertAlmostEqual(actual[1], min(p for _, p in prices))
            self.assertAlmostEqual(actual[2], dd)

    def test_paper_configuration_change_cannot_reuse_account(self):
        e = self.engine()
        altered = self.engine(lot=30)
        with self.assertRaises(ValueError):
            altered.restore(e.snapshot(), 1001)

    def test_paper_report_files_and_restart_are_durable(self):
        from pathlib import Path
        from paper_grid.run import Runner
        from paper_grid.config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(Path(tmp), Settings())
            now = runner.engine.started + 3601
            runner.report(now, runner.engine.summary(now))
            self.assertTrue((Path(tmp) / "latest_report.md").exists())
            self.assertEqual(json.loads((Path(tmp) / "state.json").read_text())["report_at"], now)
            runner.lock_file.close()

    def test_paper_source_has_no_private_exchange_access(self):
        import ast
        from pathlib import Path
        for path in (Path(__file__).resolve().parents[1] / "paper_grid").glob("*.py"):
            if path.name.startswith("._"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(x.name in {"exchange", "ccxt", "dotenv"} for x in node.names))
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertNotIn(node.module, {"config", "state", "exchange", "dotenv"})

    def test_paper_dashboard_only_exposes_readonly_routes(self):
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from urllib.request import urlopen
        from urllib.error import HTTPError
        from paper_grid.dashboard import Dashboard, handler_for
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = Dashboard(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(dashboard))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/") as r:
                    self.assertIn("网格观察台", r.read().decode())
                with urlopen(base + "/api/overview") as r:
                    self.assertEqual(json.load(r)["orders"], [])
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/state.json")
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/overview", data=b"{}")
                self.assertEqual(error.exception.code, 501)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_paper_dashboard_events_include_fills_not_every_order(self):
        from pathlib import Path
        from paper_grid.dashboard import tail_events
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events-2026-11-07.jsonl").write_text(
                '\n'.join(json.dumps({"kind": k, "time": n}) for n, k in enumerate(["submit", "fill", "roundtrip"])))
            self.assertEqual([x["kind"] for x in tail_events(root)], ["roundtrip", "fill"])

    def test_paper_late_buy_fills_extend_sell_coverage(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, 1, True, 1, 1000.3, 1000.3)
        e.cancel(buy, 1000.31, "test")
        e.on_trade("BTCUSDC", buy["price"] - .02, 1, True, 2, 1000.4, 1000.4)
        sells = [o for o in e.orders.values() if o["side"] == "sell"]
        self.assertAlmostEqual(sum(o["remaining"] for o in sells), .2)
        self.assertAlmostEqual(e.lots[buy["id"]]["qty"], .2)

    def test_paper_small_topup_waits_for_cancel_and_accounts_inflight_sell(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, 1, True, 1, 1000.3, 1000.3)
        sell = next(o for o in e.orders.values() if o["side"] == "sell")
        e.on_trade("BTCUSDC", buy["price"] - .02, .1, True, 2, 1000.6, 1000.6)
        self.assertTrue(sell["cancel_at"])
        e.on_trade("BTCUSDC", sell["price"] + .02, .2, False, 3, 1000.7, 1000.7)
        e.ensure_sells("BTCUSDC", 1000.71)
        self.assertEqual(sum(o["side"] == "sell" for o in e.orders.values()), 1)
        e.advance(1000.9)
        e.ensure_sells("BTCUSDC", 1000.9)
        self.assertAlmostEqual(sum(o["remaining"] for o in e.orders.values() if o["side"] == "sell"), .09)
        self.assertAlmostEqual(e.lots[buy["id"]]["qty"], .09)

    def test_paper_partial_buy_keeps_its_remaining_budget(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, .1, True, 1, 1000.3, 1000.3)
        self.quote(e, now=1003)
        self.assertFalse(buy["cancel_at"])
        self.assertEqual([o["id"] for o in e.orders.values() if o["side"] == "buy"], [buy["id"]])
        self.quote(e, now=1003.1, mid=99.7)
        self.assertTrue(buy["cancel_at"])  # 部分成交仍必须服从冲击保护

    def test_paper_quote_silence_does_not_restart_warmup(self):
        e = self.engine()
        self.quote(e)
        s = e.signals["BTCUSDC"]
        first = s.first
        e.advance(1000.3)
        e.tick(1006)
        self.assertTrue(next(iter(e.orders.values()))["cancel_at"])
        self.quote(e, now=1007)
        self.assertIs(e.signals["BTCUSDC"], s)
        self.assertEqual(s.first, first)
        self.assertEqual(s.reason, "ready")
        self.assertEqual(e.counters["disconnects"], 0)
        e.disconnect(1008)
        self.assertEqual(e.signals["BTCUSDC"].first, 0)

    def test_paper_quote_resume_still_detects_drop(self):
        e = self.engine()
        self.quote(e)
        self.quote(e, now=1006, mid=99.3)
        self.assertGreater(e.signals["BTCUSDC"].metrics["a60"], .006)

    def test_paper_dust_merge_preserves_cost_target_and_budget(self):
        e = self.engine()
        self.quote(e)
        e.orders.clear()
        for lid, price in [("a", 100), ("b", 101)]:
            e.lots[lid] = dict(account="usdc_zero", symbol="BTCUSDC", qty=.03,
                               cost=.03 * price, buy_price=price, target=price + 1,
                               spacing=.01, opened=999)
        before = e.inventory("usdc_zero")
        e.ensure_sells("BTCUSDC", 1001)
        self.assertEqual(len(e.lots), 1)
        self.assertAlmostEqual(e.inventory("usdc_zero"), before)
        sell = next(iter(e.orders.values()))
        self.assertAlmostEqual(sell["qty"], .06)
        self.assertGreaterEqual(sell["price"], 102)
        self.assertEqual(e.lots["a"]["source_lots"], ["a", "b"])
        e.ensure_sells("BTCUSDC", 1001.1)
        self.assertEqual(len(e.orders), 1)

    def test_paper_quantize_float_tail_does_not_drop_a_step(self):
        from paper_grid.engine import quantize
        self.assertEqual(quantize(.00031999999999999997, .00001), .00032)
        self.assertEqual(quantize(.00031999, .00001), .00031)

    def test_paper_dust_does_not_merge_live_buy_lot(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, .3, True, 1, 1000.3, 1000.3)
        e.lots["other"] = dict(e.lots[buy["id"]])
        e.ensure_sells("BTCUSDC", 1000.4)
        self.assertEqual(len(e.lots), 2)
        self.assertFalse(any(o["side"] == "sell" for o in e.orders.values()))

    def test_paper_multi_sell_roundtrip_reconciles_cash(self):
        e = self.engine()
        self.quote(e, "BTCUSDT")
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDT", buy["price"] - .02, 1, True, 1, 1000.3, 1000.3)
        e.on_trade("BTCUSDT", buy["price"] - .02, 10, True, 2, 1000.4, 1000.4)
        self.assertGreaterEqual(sum(o["side"] == "sell" for o in e.orders.values()), 2)
        sell_price = max(o["price"] for o in e.orders.values())
        e.on_trade("BTCUSDT", sell_price + .02, 10, False, 3, 1001, 1001)
        self.assertFalse(e.lots)
        self.assertFalse(e.orders)
        a = e.accounts["bnb_discount"]
        self.assertEqual(a["roundtrips"], 1)
        self.assertAlmostEqual(a["cash"] - 1000, a["realized"])

    def test_paper_reprice_budget_survives_cancel_fill_and_restart(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, .5, True, 1, 1000.3, 1000.3)
        e.cancel(buy, 1000.31, "shock")
        e.on_trade("BTCUSDC", buy["price"] - .02, .2, True, 2, 1000.4, 1000.4)
        spent = .07 * buy["price"]
        restored = self.engine()
        restored.restore(json.loads(json.dumps(e.snapshot(), allow_nan=False)), 1000.5)
        self.quote(restored, now=1001)
        new = next(o for o in restored.orders.values() if o["side"] == "buy")
        self.assertLessEqual(new["price"] * new["remaining"] + spent, 25 + 1e-9)
        self.assertAlmostEqual(restored.budget.entries["BTCUSDC"]["remaining"], 25 - spent)
        self.assertAlmostEqual(restored.budget.allowance("BTCUSDC", 1001), 25 - spent)

    def test_paper_rolling_windows_reserve_expiry_and_sell_no_refund(self):
        from paper_grid.policy import BuyBudget
        from paper_grid.config import Settings
        b = BuyBudget(Settings())
        for t in [1000, 1061]:
            b.start("SOLUSDC")
            b.record("SOLUSDC", str(t), 25, t)
            b.entries.pop("SOLUSDC")
        self.assertEqual(b.allowance("SOLUSDC", 1122), 0)  # 五分钟上限
        self.assertEqual(b.allowance("SOLUSDC", 1301, 10), 15)
        for t in [1301, 1362]:
            b.start("SOLUSDC")
            b.record("SOLUSDC", str(t), 25, t)
            b.entries.pop("SOLUSDC")
        self.assertEqual(b.allowance("SOLUSDC", 1663), 0)  # 十五分钟上限
        self.assertEqual(b.allowance("SOLUSDC", 1901), 25)
        self.assertEqual(b.allowance("ETHUSDC", 1663), 25)

    def test_paper_actual_sell_does_not_release_buy_limit(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.on_trade("BTCUSDC", buy["price"] - .02, 10, True, 1, 1000.3, 1000.3)
        sell = next(o for o in e.orders.values() if o["side"] == "sell")
        e.on_trade("BTCUSDC", sell["price"] + .02, 10, False, 2, 1000.7, 1000.7)
        self.quote(e, now=1001)
        self.assertFalse(any(o["side"] == "buy" for o in e.orders.values()))
        self.assertEqual(e.signals["BTCUSDC"].reason, "buy_rate_limit")

    def test_paper_shock_replay_limits_inflight_and_recovers_small(self):
        e = self.engine()
        self.quote(e)
        buy = next(iter(e.orders.values()))
        e.advance(1000.3)
        self.quote(e, now=1000.4, mid=99.7)
        self.assertTrue(buy["cancel_at"])
        e.on_trade("BTCUSDC", 99.6, 1, True, 1, 1000.5, 1000.5)
        self.assertLessEqual(e.inventory("usdc_zero"), 25)
        e.advance(1000.7)
        for t in range(1001, 1036):
            self.quote(e, now=t, mid=99.7)
            self.assertFalse(any(o["side"] == "buy" for o in e.orders.values()))
        self.quote(e, now=1036, mid=99.7)
        buy2 = next(o for o in e.orders.values() if o["side"] == "buy")
        self.assertLessEqual(buy2["price"] * buy2["qty"], 12.5)
        self.assertLessEqual(e.inventory("usdc_zero") + e.reserve("usdc_zero"), 25)
        covered = sum(o["remaining"] for o in e.orders.values() if o["side"] == "sell")
        self.assertAlmostEqual(covered, sum(l["qty"] for l in e.lots.values()))

    def test_paper_v_rebound_cannot_resume_immediately(self):
        e = self.engine()
        self.quote(e)
        self.quote(e, now=1000.1, mid=99.7)
        self.quote(e, now=1000.2, mid=100)
        e.advance(1001)
        self.assertTrue(e.signals["BTCUSDC"].paused)
        self.assertFalse(any(o["side"] == "buy" for o in e.orders.values()))

    def test_paper_gap_rebound_and_restart_keep_safety_pause(self):
        e = self.engine()
        self.quote(e)
        self.quote(e, now=1006, mid=99.5)
        self.assertTrue(e.signals["BTCUSDC"].paused)
        e.disconnect(1007)
        self.assertTrue(e.signals["BTCUSDC"].paused)
        restored = self.engine()
        restored.restore(json.loads(json.dumps(e.snapshot(), allow_nan=False)), 1008)
        self.quote(restored, now=1009, mid=100)
        self.assertTrue(restored.signals["BTCUSDC"].paused)
        self.assertFalse(any(o["side"] == "buy" for o in restored.orders.values()))

    def test_paper_staircase_decline_and_stale_trade_replay(self):
        e = self.engine(trend_seconds=10, trend_drop=.003, shock_floor=.1)
        for i in range(12):
            self.quote(e, now=1000+i, mid=100-i*.04)
        self.assertTrue(e.signals["BTCUSDC"].paused)
        self.assertEqual(e.signals["BTCUSDC"].reason, "downtrend")
        e.on_trade("BTCUSDC", 90, 100, True, 1, 990, 1011.1)
        self.assertFalse(e.lots)  # 延迟成交不能用于填单

    def test_paper_budget_history_bounded_and_snapshot_json(self):
        from paper_grid.policy import BuyBudget
        from paper_grid.config import Settings
        b = BuyBudget(Settings())
        b.start("SOLUSDC")
        for t in range(2000):
            b.record("SOLUSDC", "1", .0001, t)
        self.assertLessEqual(len(b.history["SOLUSDC"]), 901)
        clone = BuyBudget(Settings())
        clone.restore(json.loads(json.dumps(b.snapshot())))
        self.assertEqual(clone.allowance("SOLUSDC", 2000), b.allowance("SOLUSDC", 2000))

    def test_paper_budget_config_rejects_invalid_windows(self):
        from paper_grid.config import Settings
        for kwargs in [dict(buy_windows=()), dict(buy_windows=((300, 50), (60, 25))),
                       dict(buy_windows=((60, 0),)), dict(recovery_fraction=0)]:
            with self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_paper_replay_all_windows_and_accounting_invariants(self):
        e = self.engine(shock_floor=1, trend_drop=1)
        symbol = "BTCUSDC"
        trade_id = 0
        for i in range(1000):
            now = 1000 + i * 2
            mid = 100 - i * .001
            e.signals[symbol].candle(now, mid, mid+.001, mid-.001)
            self.quote(e, now=now, mid=mid)
            trade_id += 1
            e.on_trade(symbol, mid-1, .4, True, trade_id, now+.3, now+.3)
            trade_id += 1
            e.on_trade(symbol, mid+2, .2, False, trade_id, now+.6, now+.6)
            reserved = e.reserve("usdc_zero")
            rows = e.budget.prune(symbol, now+.6)
            for seconds, limit in e.cfg.buy_windows:
                used = sum(v for t, v in rows if t+1 > now+.6-seconds)
                self.assertLessEqual(used + reserved, limit + 1e-8)
            self.assertAlmostEqual(e.accounts["usdc_zero"]["cash"] + e.inventory("usdc_zero"),
                                   1000 + e.accounts["usdc_zero"]["realized"], places=7)
            for lid, lot in e.lots.items():
                covered = sum(o["remaining"] for o in e.orders.values() if o["side"] == "sell" and o["lot_id"] == lid)
                self.assertLessEqual(covered, lot["qty"] + 1e-10)
        self.assertGreater(e.accounts["usdc_zero"]["buys"], 10)

    def test_paper_dust_does_not_pin_reentry_but_keeps_cost(self):
        e = self.engine()
        sym = "SOLUSDC"
        e.lots["dust"] = dict(account="usdc_zero", symbol=sym, qty=.011,
                              cost=.97812, buy_price=88.92, target=88.96,
                              spacing=.00034, opened=999)
        e.accounts["usdc_zero"]["cash"] -= .97812
        self.quote(e, sym, mid=89.15)
        buy = next(o for o in e.orders.values() if o["side"] == "buy")
        self.assertGreater(buy["price"], 89.10)
        self.assertAlmostEqual(e.inventory("usdc_zero"), .97812)
        status = e.summary(1000)
        self.assertEqual(status["symbols"][sym]["dust_lots"], 1)
        self.assertAlmostEqual(status["accounts"]["usdc_zero"]["equity"],
                               1000-.97812+.011*89.14)
        # 尘埃仍占用库存上限，不能靠分类绕过资金约束。
        limited = self.engine(max_coin_inventory=1)
        limited.lots["dust"] = dict(e.lots["dust"])
        self.quote(limited, sym, mid=89.15)
        self.assertFalse(limited.orders)

    def test_paper_sellable_lot_still_anchors_reentry(self):
        e = self.engine()
        sym = "SOLUSDC"
        e.lots["holding"] = dict(account="usdc_zero", symbol=sym, qty=.1,
                                 cost=8.892, buy_price=88.92, target=88.96,
                                 spacing=.00034, opened=999)
        self.quote(e, sym, mid=89.15)
        buy = next(o for o in e.orders.values() if o["side"] == "buy")
        self.assertLess(buy["price"], 88.92)
        self.assertFalse(e.is_dust(e.lots["holding"]))

    def test_paper_coin_inventory_scales_spacing_not_size(self):
        from paper_grid.policy import inventory_policy
        from paper_grid.config import Settings
        cfg = Settings()
        self.assertEqual(inventory_policy(cfg, 0), (1, 25))
        self.assertEqual(inventory_policy(cfg, 125), (1.75, 25))
        self.assertEqual(inventory_policy(cfg, 250), (4, 25))
        e = self.engine()
        sym = "ETHUSDC"
        e.lots["held"] = dict(account="usdc_zero", symbol=sym, qty=1.25, cost=125,
                              buy_price=100, target=101, spacing=.01, opened=999)
        self.quote(e, sym)
        order = next(o for o in e.orders.values() if o["side"] == "buy")
        self.assertGreater(order["price"] * order["qty"], 24.8)
        self.assertEqual(e.budget.entries[sym]["remaining"], 25)
        self.assertGreater(e.coin_policy(sym)[0], 1.75)  # 待成交预留也计入
        self.assertEqual(e.coin_policy("SOLUSDC"), (1, 25))
        self.assertEqual(e.lots["held"]["target"], 101)

    def test_paper_full_entry_waits_for_capacity_and_rolling_allowance(self):
        e = self.engine(max_coin_inventory=20)
        self.quote(e)
        self.assertFalse(e.orders)
        e = self.engine()
        e.budget.start("BTCUSDC")
        e.budget.record("BTCUSDC", "past", 10, 999)
        e.budget.entries.clear()
        self.quote(e)
        self.assertFalse(e.orders)
        self.assertEqual(e.signals["BTCUSDC"].reason, "buy_rate_limit")
        e.signals["BTCUSDC"].candle(1060, 100, 100.01, 99.99)
        self.quote(e, now=1061)
        buy = next(o for o in e.orders.values() if o["side"] == "buy")
        self.assertGreater(buy["qty"] * buy["price"], 24.8)

    def test_paper_entry_remainder_below_minimum_after_precision_finishes(self):
        e = self.engine()
        sym = "BTCUSDC"
        e.filters[sym] = (.01, .00001, 5)
        e.budget.start(sym)
        e.budget.record(sym, "prior", 19.7154464, 900)
        e.signals[sym].bid, e.signals[sym].ask, e.signals[sym].at = 75700, 75701, 1000
        before = dict(e.accounts["usdc_zero"])
        e.place_buy(sym, 75700, 1000)
        self.assertNotIn(sym, e.budget.entries)
        self.assertFalse(e.orders)
        self.assertEqual(e.accounts["usdc_zero"], before)
        self.assertEqual(e.events[-1]["kind"], "entry_complete")
        e.place_buy(sym, 75700, 1001)
        self.assertEqual(len(e.orders), 1)
        self.assertEqual(e.budget.entries[sym]["remaining"], 25)

    def test_paper_independent_tp_inventory_and_partial_fill(self):
        import copy
        e = self.engine()
        sym = "BTCUSDC"
        self.quote(e, sym)
        o = next(iter(e.orders.values()))
        tp = o["tp_spacing"]
        e.budget.start(sym)
        e.fill(o, .05, 1001)
        target = e.lots[o["lot_id"]]["target"]
        e.signals[sym].candles.clear()
        e.signals[sym].candles.extend([.1] * 30)
        e.fill(o, .05, 1002)
        self.assertEqual(e.lots[o["lot_id"]]["target"], target)
        clone = self.engine()
        clone.restore(copy.deepcopy(e.snapshot()), 1003)
        self.assertEqual(clone.lots[o["lot_id"]]["target"], target)
        self.assertEqual(e.snapshot()["orders"][o["id"]]["tp_spacing"], tp)
        self.assertEqual(clone.budget.snapshot(), e.budget.snapshot())
        e2 = self.engine()
        e2.lots["held"] = dict(account="usdc_zero",symbol=sym,qty=1,cost=100,buy_price=100,target=101,spacing=.01,opened=900)
        self.quote(e2, sym)
        o2 = next(o for o in e2.orders.values() if o["side"] == "buy")
        self.assertGreater(o2["spacing"], o["spacing"])
        self.assertEqual(o2["tp_spacing"], tp)
        self.assertEqual(e2.lots["held"]["target"], 101)

    def test_paper_tp_fee_floor_and_compensation_cap(self):
        from paper_grid.policy import take_profit_spacing
        from paper_grid.config import Settings
        cfg = Settings()
        for fee in (0, .00075):
            floor, _ = take_profit_spacing(cfg, fee, [])
            tp, _ = take_profit_spacing(cfg, fee, [.2]*30)
            self.assertAlmostEqual(tp-floor, cfg.tp_extra_cap)
            self.assertGreaterEqual((1+tp)*(1-fee)/(1+fee), 1+cfg.min_profit)

    def test_replay_uses_same_engine_and_rejects_backwards_time(self):
        from paper_grid.replay import replay
        from paper_grid.engine import Engine
        e = self.engine()
        filters = e.filters
        rows = []
        for i in range(31):
            now = 1800000000+i*60
            for sym in e.signals:
                rows.append({"received": now, "packet": {"e":"kline","s":sym,"k":{"x":True,"T":now*1000-1,"o":"100","h":"100.01","l":"99.99"}}})
                rows.append({"received": now, "packet":{"s":sym,"b":"99.99","a":"100.01","u":i+1}})
        result = replay(rows, filters)
        direct = Engine()
        direct.filters = filters
        direct.started = rows[0]["received"]
        last = None
        for r in rows:
            now = r["received"]
            if last is not None and now-last>5:
                direct.tick(last+5.001)
                direct.tick(last+5.252)
            direct.tick(now)
            direct.on_packet(r["packet"], now)
            last=now
        self.assertEqual(result["status"]["accounts"], direct.summary(last)["accounts"])
        with self.assertRaises(ValueError):
            replay(list(reversed(rows)), filters)
        with self.assertRaises(ValueError):
            replay([], filters)

    def test_stale_valuation_does_not_update_peak_or_drawdown(self):
        e = self.engine()
        e.lots["old"] = dict(account="usdc_zero", symbol="BTCUSDC", qty=1,
                             cost=100, buy_price=100, target=101, spacing=.01, opened=900)
        a = e.accounts["usdc_zero"]
        a.update(cash=950, peak_equity=1000, max_drawdown=.02)
        summary = e.summary(1000)
        self.assertFalse(summary["accounts"]["usdc_zero"]["valuation_valid"])
        self.assertEqual(a["peak_equity"], 1000)
        self.assertEqual(a["max_drawdown"], .02)
        e.signals["BTCUSDC"].bid = 40
        e.signals["BTCUSDC"].at = 1000
        self.assertTrue(e.summary(1000)["accounts"]["usdc_zero"]["valuation_valid"])
        self.assertEqual(a["peak_equity"], 1000)

    def test_replay_timer_never_runs_ahead_of_market(self):
        from unittest.mock import patch
        from paper_grid.replay import replay
        from paper_grid.engine import Engine
        ticks = []
        original = Engine.tick
        def record(engine, now):
            ticks.append(now)
            return original(engine, now)
        rows = [{"received":t,"packet":{"s":"BTCUSDC","b":"99","a":"101","u":i+1}}
                for i,t in enumerate([1000,1005.1,1005.2])]
        with patch.object(Engine, "tick", record):
            replay(rows, self.engine().filters)
        self.assertEqual(ticks, sorted(ticks))
        self.assertLessEqual(max(ticks),1005.2)

    def test_sharpe_excludes_invalid_valuation(self):
        from pathlib import Path
        from paper_grid.dashboard import performance
        with tempfile.TemporaryDirectory() as tmp:
            rows=[{"time":3600*i,"capital":1000,"accounts":{"a":{"equity":1000+i,"valuation_valid":i!=3}}} for i in range(1,9)]
            result=performance(Path(tmp),rows,{"settings":{"capital":1000},"accounts":{"a":{}}},28801)
            self.assertEqual(result["a"]["sharpe_samples"],5)


if __name__ == "__main__":
    unittest.main()
