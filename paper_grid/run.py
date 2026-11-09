"""公开行情驱动纸面交易入口：没有密钥、签名请求或实盘订单路径。"""
import argparse
import fcntl
import gzip
import json
import logging
import queue
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websocket

from .config import DEFAULT_DATA, REST_URL, WS_URL, Settings
from .engine import Engine
from .report import render
from .storage import _atomic_write_text, write_json


class Runner:
    def __init__(self, data_dir: Path, settings: Settings):
        self.root = data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.root / "paper.lock").open("a+")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.engine = Engine(settings, time.time())
        state = self.root / "state.json"
        if state.exists():
            self.engine.restore(json.loads(state.read_text()), time.time())
        else:
            write_json(self.root / "experiment.json", {"settings": settings.to_dict(), "started": self.engine.started,
                "mode": "PAPER_ONLY", "fee_assumptions": {"bnb_discount": 0.00075, "usdc_zero": 0.0}})
        self.raw_queue = queue.Queue(maxsize=100000)
        self.raw_dropped = 0
        self.callback_max_ms = 0.0
        self.ws = None
        self.connected = False
        self.error = ""
        self.persist()

    def get(self, endpoint: str, **params):
        # 白名单只允许两个公开GET端点，不存在账户或下单HTTP方法。
        if endpoint not in ("exchangeInfo", "klines"):
            raise ValueError("仅允许公开行情GET")
        response = requests.get(f"{REST_URL}/{endpoint}", params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def bootstrap(self):
        symbols = list(self.engine.signals)
        info = self.get("exchangeInfo", symbols=json.dumps(symbols, separators=(",", ":")))
        filters = {}
        for market in info["symbols"]:
            if market["status"] != "TRADING":
                raise ValueError(f"{market['symbol']} 当前不能交易")
            f = {x["filterType"]: x for x in market["filters"]}
            filters[market["symbol"]] = (float(f["PRICE_FILTER"]["tickSize"]),
                float(f["LOT_SIZE"]["stepSize"]), float(f.get("NOTIONAL", f.get("MIN_NOTIONAL", {}))["minNotional"]))
        if set(filters) != set(symbols):
            raise ValueError("交易对过滤规则不完整")
        for sym in symbols:
            bars = self.get("klines", symbol=sym, interval="1m", limit=31)
            with self.mutex:
                s = self.engine.signals[sym]
                for bar in bars:
                    if bar[6] / 1000 < time.time():
                        s.candle(bar[6] / 1000, float(bar[1]), float(bar[2]), float(bar[3]))
        with self.mutex:
            self.engine.filters = filters
            write_json(self.root / "market_filters.json", filters)

    def persist(self):
        # 快照为账本事实来源；事件文件仅用于审计，崩溃时允许最后几条审计缺失。
        write_json(self.root / "state.json", self.engine.snapshot())
        if self.engine.events:
            name = datetime.now(timezone.utc).strftime("events-%Y-%m-%d.jsonl")
            with (self.root / name).open("a", encoding="utf-8") as f:
                for event in self.engine.events:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.engine.events.clear()
        self.engine.dirty = False

    def on_message(self, _ws, raw: str):
        started = time.perf_counter()
        now = time.time()
        packet = json.loads(raw)
        data = packet.get("data", packet)
        sym = data.get("s")
        if sym not in self.engine.signals:
            return
        try:
            self.raw_queue.put_nowait((now, packet))
        except queue.Full:
            self.raw_dropped += 1
        with self.mutex:
            self.engine.on_packet(packet, now)
            if self.engine.dirty:
                self.persist()
        self.callback_max_ms = max(self.callback_max_ms, (time.perf_counter() - started) * 1000)

    def on_error(self, _ws, error):
        self.error = str(error)[:500]
        logging.error("行情错误：%s", self.error)
        _ws.close()  # 回调异常同样断开、重新预热，不在不确定状态继续撮合

    def feed(self):
        while not self.stop.is_set():
            try:
                self.bootstrap()
                streams = [sym.lower() + suffix for sym in self.engine.signals for suffix in ("@bookTicker", "@aggTrade", "@kline_1m")]
                self.ws = websocket.WebSocketApp(WS_URL + "/".join(streams),
                    on_message=self.on_message, on_error=self.on_error,
                    on_open=lambda _: setattr(self, "connected", True))
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.error = str(exc)[:500]
                logging.exception("公开行情连接失败")
            finally:
                self.connected = False
                with self.mutex:
                    self.engine.disconnect(time.time())
                    self.persist()
            self.stop.wait(5)

    def raw_writer(self):
        folder = self.root / "raw"
        folder.mkdir(exist_ok=True)
        handle = None
        current = ""
        try:
            while not self.stop.is_set() or not self.raw_queue.empty():
                try:
                    now, data = self.raw_queue.get(timeout=1)
                except queue.Empty:
                    continue
                name = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H.jsonl.gz")
                if name != current:
                    if handle:
                        handle.close()
                    handle = gzip.open(folder / name, "at", encoding="utf-8", compresslevel=1)
                    current = name
                    for path in folder.glob("*.jsonl.gz"):
                        if path.stat().st_mtime < now - self.engine.cfg.raw_retention_days * 86400:
                            path.unlink()
                handle.write(json.dumps({"received": now, "packet": data}, separators=(",", ":")) + "\n")
        finally:
            if handle:
                handle.close()

    def report(self, now: float, status: dict):
        start = self.engine.report_at
        report_dir = self.root / "reports"
        name = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")
        content = render(status, self.engine.report_baseline, start)
        write_json(report_dir / f"{name}.json", {"from": start, "to": now, "current": status, "previous": self.engine.report_baseline})
        _atomic_write_text(report_dir / f"{name}.md", content)
        _atomic_write_text(self.root / "latest_report.md", content)
        self.engine.report_at = now
        self.engine.report_baseline = status
        self.persist()
        logging.info("小时报告已生成：%s", name)

    def run(self, seconds: float = 0):
        feed = threading.Thread(target=self.feed, daemon=True, name="market-feed")
        raw = threading.Thread(target=self.raw_writer, daemon=True, name="raw-writer")
        feed.start()
        raw.start()
        deadline = time.time() + seconds if seconds else float("inf")
        last_snapshot = 0.0
        try:
            while not self.stop.wait(0.25) and time.time() < deadline:
                now = time.time()
                with self.mutex:
                    self.engine.tick(now)
                    if self.engine.dirty:
                        self.persist()
                    if now - last_snapshot >= self.engine.cfg.snapshot_seconds:
                        status = self.engine.summary(now)
                        status["health"] = {"connected": self.connected, "error": self.error,
                            "raw_dropped": self.raw_dropped, "raw_writer_alive": raw.is_alive(),
                            "callback_max_ms": self.callback_max_ms}
                        write_json(self.root / "status.json", status)
                        self.persist()
                        last_snapshot = now
                        if now - self.engine.report_at >= self.engine.cfg.report_seconds:
                            self.report(now, status)
        finally:
            self.stop.set()
            if self.ws:
                self.ws.close()
            feed.join(timeout=20)
            raw.join(timeout=10)
            with self.mutex:
                self.persist()


def main():
    parser = argparse.ArgumentParser(description="仅公开行情的动态网格纸面交易")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--seconds", type=float, default=0, help="限时冒烟运行；0表示常驻")
    parser.add_argument("--status", action="store_true", help="只读已有状态，不连接行情")
    args = parser.parse_args()
    if args.status:
        print((args.data_dir / "status.json").read_text())
        return
    run_paper(args.data_dir, args.seconds)


def run_paper(data_dir: Path, seconds: float = 0):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 已有实验使用明确保存的配置；资金迁移由停机审计流程完成。
    experiment = data_dir / "experiment.json"
    settings = Settings(**json.loads(experiment.read_text())["settings"]) if experiment.exists() else Settings()
    runner = Runner(data_dir, settings)
    signal.signal(signal.SIGTERM, lambda *_: runner.stop.set())
    signal.signal(signal.SIGINT, lambda *_: runner.stop.set())
    runner.run(seconds)


if __name__ == "__main__":
    main()
