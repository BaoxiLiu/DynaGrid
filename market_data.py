"""币安 WebSocket 行情缓存与买入触发观察器。

本模块刻意不依赖 StrategyEngine、StateManager 或 Exchange，也不会创建订单。
WebSocket 线程只接收盘口并判断由主线程提供的不可变触发价快照，避免与状态保存
和交易执行发生并发竞争。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import threading
import time
from typing import Iterable
from collections.abc import Callable
from urllib.parse import urlparse

from logger import log


@dataclass(frozen=True)
class MarketQuote:
    coin: str
    bid: float
    ask: float
    updated_at: float
    sequence: int = 0


class MarketDataCache:
    """线程安全的最新盘口缓存。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._quotes: dict[str, MarketQuote] = {}
        self._connected = False
        self._last_message_at = 0.0
        self._last_error = ""
        self._reconnects = 0

    def update(self, coin: str, bid: float, ask: float,
               updated_at: float | None = None, sequence: int = 0) -> bool:
        bid, ask = float(bid), float(ask)
        if bid <= 0 or ask <= 0:
            return False
        now = time.time() if updated_at is None else float(updated_at)
        with self._lock:
            previous = self._quotes.get(coin)
            # bookTicker 的更新序号倒退时，宁可丢弃旧包，也不能用旧 ask 触发买入。
            if (sequence > 0 and previous and previous.sequence > 0
                    and sequence <= previous.sequence):
                return False
            self._quotes[coin] = MarketQuote(coin, bid, ask, now, int(sequence))
            self._last_message_at = now
        return True

    def get(self, coin: str, max_age: float) -> MarketQuote | None:
        with self._lock:
            quote = self._quotes.get(coin)
        if quote is None or time.time() - quote.updated_at > max_age:
            return None
        return quote

    def set_connected(self, connected: bool):
        with self._lock:
            self._connected = connected
            if connected:
                self._last_error = ""

    def set_error(self, message: str):
        with self._lock:
            self._connected = False
            self._last_error = str(message)[:180]
            self._reconnects += 1

    def status_text(self, expected_coins: int, stale_seconds: float) -> str:
        now = time.time()
        with self._lock:
            fresh = sum(
                1 for quote in self._quotes.values()
                if now - quote.updated_at <= stale_seconds
            )
            connected = self._connected
            error = self._last_error
            last_message_at = self._last_message_at
        if connected:
            message_age = now - last_message_at if last_message_at else float("inf")
            if message_age <= stale_seconds:
                return (
                    f"WS 通道已连接，最近消息 {message_age:.1f}s 前；"
                    f"{fresh}/{expected_coins} 个盘口近 {stale_seconds:.0f}s 有变动（仅观察）"
                )
            return (
                f"WS 通道已连接，但最近消息 {message_age:.0f}s 前；"
                "REST 扫描仍正常兜底"
            )
        if error:
            return f"WS 行情重连中：{error}（REST 扫描不受影响）"
        return "WS 行情正在连接（REST 扫描不受影响）"


class WebSocketTriggerObserver:
    """按 ask 价记录一次性的“价格已到触发价”观察信号。"""

    def __init__(self, cache: MarketDataCache, stale_seconds: float,
                 rearm_rate: float, rest_wakeup_event: threading.Event | None = None,
                 wake_debounce_seconds: float = 2.0):
        self._cache = cache
        self._stale_seconds = stale_seconds
        self._rearm_rate = rearm_rate
        self._lock = threading.RLock()
        self._levels: dict[tuple[str, int, str], dict] = {}
        self._armed: dict[tuple[str, int, str], bool] = {}
        self._events: deque[str] = deque(maxlen=30)
        self._rest_wakeup_event = rest_wakeup_event
        self._wake_debounce_seconds = wake_debounce_seconds
        self._last_wakeup_at = 0.0

    def set_levels(self, levels: Iterable[dict]):
        """由主线程更新只读快照；该操作不读取或修改策略状态。"""
        normalized = {}
        for level in levels:
            try:
                key = (str(level["coin"]), int(level["tier_index"]), str(level["source"]))
                trigger = float(level["trigger_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if trigger > 0:
                normalized[key] = {**level, "trigger_price": trigger}
        with self._lock:
            self._levels = normalized
            self._armed = {key: self._armed.get(key, True) for key in normalized}

    def on_quote(self, quote: MarketQuote):
        """供 WebSocket 线程调用；只操作内部快照与事件队列。"""
        if time.time() - quote.updated_at > self._stale_seconds:
            return
        with self._lock:
            for key, level in self._levels.items():
                if level["coin"] != quote.coin:
                    continue
                trigger = level["trigger_price"]
                if quote.ask > trigger * (1 + self._rearm_rate):
                    self._armed[key] = True
                    continue
                if quote.ask <= trigger and self._armed.get(key, True):
                    self._armed[key] = False
                    gap_pct = (quote.ask / trigger - 1) * 100
                    self._events.append(
                        f"[WS 观察] {quote.coin} {level['tier']} ask ${quote.ask:.8g} "
                        f"已到触发价 ${trigger:.8g}（{gap_pct:+.2f}%；仅观察，不下单）"
                    )
                    # 只唤醒主线程提前做一次完整 REST 复核；这里绝不下单。
                    now = time.time()
                    if (self._rest_wakeup_event is not None
                            and now - self._last_wakeup_at >= self._wake_debounce_seconds):
                        self._last_wakeup_at = now
                        self._rest_wakeup_event.set()

    def drain_events(self) -> list[str]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events


class BinanceBookTickerStream:
    """维护合并 bookTicker 流；断线时后台重连，不影响 REST 策略循环。"""

    BASE_URL = "wss://stream.binance.com:9443/stream?streams="

    def __init__(self, coins: list[str], cache: MarketDataCache,
                 observer: WebSocketTriggerObserver, proxy_url: str = "",
                 reconnect_seconds: float = 5.0,
                 on_reconnect: Callable[[], None] | None = None):
        self._coins = list(coins)
        self._cache = cache
        self._observer = observer
        self._proxy_url = proxy_url
        self._reconnect_seconds = reconnect_seconds
        self._on_reconnect = on_reconnect
        self._opened_once = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app = None
        self._symbol_to_coin = {f"{coin}USDT": coin for coin in self._coins}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="binance-book-ticker", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        try:
            import websocket
        except ImportError as exc:
            self._cache.set_error(f"缺少 websocket-client: {exc}")
            return

        streams = "/".join(f"{coin.lower()}usdt@bookTicker" for coin in self._coins)
        url = f"{self.BASE_URL}{streams}"
        while not self._stop.is_set():
            self._app = websocket.WebSocketApp(
                url,
                on_open=self._handle_open,
                on_message=lambda _app, message: self._on_message(message),
                on_error=lambda _app, error: self._cache.set_error(str(error)),
                on_close=lambda _app, _status, _message: self._cache.set_connected(False),
            )
            kwargs = {"ping_interval": 20, "ping_timeout": 10}
            if self._proxy_url:
                parsed = urlparse(self._proxy_url)
                if parsed.hostname:
                    kwargs.update({
                        "http_proxy_host": parsed.hostname,
                        "http_proxy_port": parsed.port or 80,
                        "proxy_type": parsed.scheme or "http",
                    })
            try:
                self._app.run_forever(**kwargs)
            except Exception as exc:
                self._cache.set_error(str(exc))
                log.warning(f"WebSocket 行情连接异常，将重试: {exc}")
            if not self._stop.wait(self._reconnect_seconds):
                self._cache.set_error("连接已断开，正在重连")

    def _handle_open(self, _app):
        self._cache.set_connected(True)
        if self._opened_once:
            log.info("WebSocket 行情已重连，主循环将立即执行 REST 复核")
            if self._on_reconnect is not None:
                self._on_reconnect()
        self._opened_once = True

    def _on_message(self, message: str):
        try:
            payload = json.loads(message)
            data = payload.get("data", payload)
            symbol = str(data["s"]).upper()
            coin = self._symbol_to_coin.get(symbol)
            if coin is None:
                return
            quote = MarketQuote(
                coin=coin,
                bid=float(data["b"]),
                ask=float(data["a"]),
                updated_at=time.time(),
                sequence=int(data.get("u") or 0),
            )
            if self._cache.update(
                quote.coin, quote.bid, quote.ask, quote.updated_at, quote.sequence,
            ):
                self._observer.on_quote(quote)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            # 单条异常行情不能中断整个连接。
            return
