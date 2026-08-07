"""
交易所接口 — CCXT 封装 (Binance Spot)
"""
import re
import time
from copy import deepcopy
from typing import Any

try:
    import ccxt
except ImportError:  # 允许离线测试通过注入 Fake API 运行
    ccxt = None

from config import (
    EXCHANGE_CONFIG,
    EXCHANGE_NAME,
    PROXY_URL,
    QUOTE_CURRENCY,
    RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS,
    validate_exchange_config,
)
from logger import log


class ExchangeError(Exception):
    pass


class Exchange:
    def __init__(self, api: Any | None = None):
        self._markets_loaded = False
        self._symbol_map: dict[str, str] = {}
        self.last_error: str = ""
        self.last_order_outcome: str = ""
        self._rate_limit_until: float = 0.0
        self._rate_limit_reason: str = ""

        if api is not None:
            self._api = api
            return
        if ccxt is None:
            raise RuntimeError("缺少 ccxt，请运行: pip install -r requirements.txt")

        validate_exchange_config()
        cfg = deepcopy(EXCHANGE_CONFIG)
        cfg.setdefault("enableRateLimit", True)
        # 币安: 不指定 symbol 查所有挂单的警告静默
        opts = cfg.setdefault("options", {})
        opts.setdefault("warnOnFetchOpenOrdersWithoutSymbol", False)
        # 只加载现货市场, 避免 VPN 节点不通合约 API 导致初始化失败
        opts.setdefault("fetchMarkets", ["spot"])
        # 代理
        if PROXY_URL:
            cfg["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
        self._api = getattr(ccxt, EXCHANGE_NAME)(cfg)

    def _success(self):
        self.last_error = ""

    def _failure(self, message: str):
        self.last_error = message
        self._record_rate_limit(message)

    def _record_rate_limit(self, message: str):
        """记录币安 -1003/429/418 的冷却截止时间，供主循环熔断 REST 请求。"""
        lowered = message.lower()
        if not (
            "too many requests" in lowered
            or "ip banned" in lowered
            or '"code":-1003' in lowered
            or " 429 " in f" {lowered} "
            or " 418 " in f" {lowered} "
        ):
            return

        now = time.time()
        until = now + RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS
        match = re.search(r"banned until\s+(\d{10,16})", message, re.IGNORECASE)
        if match:
            timestamp = float(match.group(1))
            # 币安的 banned-until 为毫秒时间戳；同时兼容秒级时间戳。
            until = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp

        self._rate_limit_until = max(self._rate_limit_until, until)
        self._rate_limit_reason = message

    def rate_limit_cooldown_seconds(self, now: float | None = None) -> int:
        """剩余限频冷却秒数。0 表示可继续访问交易所。"""
        current = time.time() if now is None else now
        return max(0, int(self._rate_limit_until - current + 0.999))

    @property
    def rate_limit_reason(self) -> str:
        return self._rate_limit_reason

    @staticmethod
    def _is_uncertain_order_error(error: Exception) -> bool:
        """仅网络类异常代表订单可能已到达交易所。"""
        uncertain_names = {
            "RequestTimeout", "NetworkError", "ExchangeNotAvailable",
            "DDoSProtection", "OperationFailed",
        }
        if error.__class__.__name__ in uncertain_names:
            return True
        if ccxt is None:
            return False
        return isinstance(error, (
            ccxt.RequestTimeout,
            ccxt.NetworkError,
            ccxt.ExchangeNotAvailable,
        ))

    # ---- 初始化 ----
    def load_markets(self):
        """载入交易对信息"""
        if self._markets_loaded:
            return
        self._api.load_markets()
        self._markets_loaded = True
        self._success()
        log.info(f"已载入 {len(self._api.markets)} 个交易对")

    def init_symbols(self, coins: list[str]):
        """建立币种→交易对映射"""
        self._symbol_map = {}
        for coin in coins:
            symbol = f"{coin}/{QUOTE_CURRENCY}"
            if symbol in self._api.markets:
                self._symbol_map[coin] = symbol
            else:
                log.warning(f"交易对不存在: {symbol}")
        log.info(f"交易对映射: {self._symbol_map}")

    def symbol_of(self, coin: str) -> str:
        return self._symbol_map[coin]

    # ---- 行情 ----
    def fetch_tickers(self, coins: list[str]) -> dict[str, dict] | None:
        """获取多个币种的 ticker"""
        symbols = [self._symbol_map[c] for c in coins if c in self._symbol_map]
        if not symbols:
            return {}
        try:
            raw = self._api.fetch_tickers(symbols)
            result = {}
            for coin, sym in self._symbol_map.items():
                if sym in raw:
                    result[coin] = {
                        "price": raw[sym].get("last") or raw[sym].get("close") or 0,
                        "bid": raw[sym].get("bid") or 0,
                        "ask": raw[sym].get("ask") or 0,
                        "change_24h": raw[sym].get("percentage") or 0,
                        "high_24h": raw[sym].get("high") or 0,
                        "low_24h": raw[sym].get("low") or 0,
                    }
            self._success()
            return result
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取 tickers 失败: {e}")
            return None

    def fetch_ticker(self, coin: str) -> dict | None:
        sym = self._symbol_map.get(coin)
        if not sym:
            return None
        try:
            t = self._api.fetch_ticker(sym)
            self._success()
            return {
                "price": t.get("last") or t.get("close") or 0,
                "bid": t.get("bid") or 0,
                "ask": t.get("ask") or 0,
            }
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取 {coin} ticker 失败: {e}")
            return None

    def fetch_ohlcv(self, coin: str, timeframe: str,
                    limit: int) -> list[list[float]] | None:
        """获取标准化 OHLCV；调用方负责剔除尚未收盘的最后一根K线。"""
        sym = self._symbol_map.get(coin)
        if not sym:
            return None
        try:
            rows = self._api.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
            result = []
            for row in rows:
                if len(row) < 5:
                    continue
                result.append([
                    float(row[0]), float(row[1]), float(row[2]),
                    float(row[3]), float(row[4]),
                    float(row[5]) if len(row) > 5 else 0.0,
                ])
            self._success()
            return result
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取 {coin} {timeframe} K线失败: {e}")
            return None

    # ---- 账户 ----
    def fetch_balance(self) -> dict | None:
        """返回 {'USDT': 123.45, 'BTC': 0.001, ...}"""
        try:
            bal = self._api.fetch_balance()
            result = {}
            for asset, info in bal.get("free", {}).items():
                if info and info > 0:
                    result[asset] = info
            # 也计入 locked (挂单锁定的)
            for asset, info in bal.get("used", {}).items():
                if info and info > 0:
                    result[asset] = result.get(asset, 0) + info
            self._success()
            return result
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取余额失败: {e}")
            return None

    def fetch_free_balance(self) -> dict | None:
        """只返回可用余额"""
        try:
            bal = self._api.fetch_balance()
            result = {
                asset: info
                for asset, info in bal.get("free", {}).items()
                if info and info > 0
            }
            self._success()
            return result
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取可用余额失败: {e}")
            return None

    def check_connection(self) -> bool:
        return self.fetch_free_balance() is not None

    # ---- 下单 ----
    @staticmethod
    def _order_fee(order: dict) -> tuple[float, str]:
        fee = order.get("fee") or {}
        if fee:
            return float(fee.get("cost") or 0), fee.get("currency") or ""
        fees = order.get("fees") or []
        return (
            sum(float(item.get("cost") or 0) for item in fees),
            ",".join(sorted({item.get("currency") or "" for item in fees})),
        )

    def _normalize_order(self, order: dict, coin: str,
                         fallback_price: float = 0,
                         fallback_qty: float = 0) -> dict:
        fee, fee_currency = self._order_fee(order)
        filled = float(order.get("filled") or 0)
        amount = float(order.get("amount") or fallback_qty or 0)
        price = float(order.get("average") or order.get("price") or fallback_price or 0)
        cost = float(order.get("cost") or (price * filled))
        return {
            "coin": coin,
            "side": order.get("side", ""),
            "status": order.get("status", "unknown"),
            "price": price,
            "qty": filled or amount,
            "amount": amount,
            "filled": filled,
            "remaining": float(order.get("remaining") or max(0, amount - filled)),
            "cost": cost,
            "fee": fee,
            "fee_currency": fee_currency,
            "order_id": str(order.get("id") or ""),
            "client_order_id": str(order.get("clientOrderId") or ""),
        }

    def _lookup_order_by_client_id(self, coin: str,
                                   client_order_id: str) -> tuple[str, dict | None]:
        """返回 found/not_found/unknown，unknown 时绝不能盲目重下。"""
        sym = self._symbol_map.get(coin)
        if not sym or not client_order_id:
            return "unknown", None
        market = self._api.market(sym)
        try:
            if hasattr(self._api, "fetch_order_by_client_id"):
                order = self._api.fetch_order_by_client_id(client_order_id, sym)
            elif hasattr(self._api, "private_get_order"):
                raw = self._api.private_get_order({
                    "symbol": market.get("id") or sym.replace("/", ""),
                    "origClientOrderId": client_order_id,
                })
                order = self._api.parse_order(raw, market)
            else:
                return "unknown", None
            return "found", self._normalize_order(order, coin)
        except Exception as e:
            message = str(e)
            if e.__class__.__name__ == "OrderNotFound" or "-2013" in message:
                return "not_found", None
            self._failure(message)
            log.error(f"按客户端订单号查询失败 {client_order_id}: {e}")
            return "unknown", None

    def fetch_order_by_client_id(self, coin: str,
                                 client_order_id: str) -> dict | None:
        status, order = self._lookup_order_by_client_id(coin, client_order_id)
        return order if status == "found" else None

    def _recover_uncertain_create(self, coin: str, client_order_id: str,
                                  error: Exception) -> tuple[str, dict | None]:
        if not self._is_uncertain_order_error(error):
            return "rejected", None
        if not client_order_id:
            log.critical(f"{coin} 下单结果未知且没有客户端订单号，停止重试")
            return "unknown", None
        time.sleep(1)
        status, order = self._lookup_order_by_client_id(coin, client_order_id)
        if status == "found":
            log.warning(f"{coin} 下单响应丢失，但已按客户端订单号找回订单")
        elif status == "unknown":
            log.critical(f"{coin} 无法确认订单是否已提交，停止重试并等待对账")
        return status, order

    def market_buy(self, coin: str, usdt_amount: float,
                   client_order_id: str = "") -> dict | None:
        self.last_order_outcome = "pending"
        sym = self._symbol_map.get(coin)
        if not sym:
            self.last_order_outcome = "rejected"
            return None
        market = self._api.market(sym)
        min_notional = market.get("limits", {}).get("cost", {}).get("min", 10)
        if usdt_amount < min_notional:
            self.last_order_outcome = "rejected"
            log.warning(f"{coin} 买入金额 ${usdt_amount:.2f} 低于最小限额 ${min_notional}")
            return None

        params = {"clientOrderId": client_order_id} if client_order_id else {}
        for attempt in range(3):
            try:
                ticker = self._api.fetch_ticker(sym)
                price = float(ticker.get("last") or ticker.get("close") or 0)
                if price <= 0:
                    raise ExchangeError(f"价格异常: {price}")
                qty = self._api.amount_to_precision(sym, usdt_amount / price)
                order = self._api.create_market_buy_order(sym, qty, params)
                result = self._normalize_order(order, coin, price, float(qty))
                result["client_order_id"] = result["client_order_id"] or client_order_id
                self._success()
                self.last_order_outcome = "confirmed"
                log.info(f"[BUY] {coin} ${usdt_amount:.1f} → {result['qty']:.4f} "
                         f"@ ${result['price']:.4f} = ${result['cost']:.2f}")
                return result
            except Exception as e:
                log.warning(f"{coin} 买入尝试 {attempt+1}/3 异常: {e}")
                recovery, recovered = self._recover_uncertain_create(
                    coin, client_order_id, e,
                )
                if recovery == "found":
                    self.last_order_outcome = "confirmed"
                    return recovered
                if recovery != "not_found":
                    self.last_order_outcome = recovery
                    log.error(f"{coin} 买入停止: {e}")
                    return None
                if attempt < 2:
                    time.sleep(2 ** attempt)
        self.last_order_outcome = "rejected"
        return None

    def limit_sell(self, coin: str, qty: float, price: float,
                   client_order_id: str = "") -> dict | None:
        self.last_order_outcome = "pending"
        sym = self._symbol_map.get(coin)
        if not sym:
            self.last_order_outcome = "rejected"
            return None
        market = self._api.market(sym)
        min_notional = market.get("limits", {}).get("cost", {}).get("min", 10)
        original_price = price
        try:
            qty_text = self._api.amount_to_precision(sym, qty)
            price_text = self._api.price_to_precision(sym, price)
        except Exception as e:
            # 精度校验在请求交易所前执行；必须转换成确定性拒绝，避免调用方
            # 已持久化的订单意图因异常路径无法清理。
            self.last_order_outcome = "rejected"
            log.warning(f"{coin} 卖单数量或价格不符合交易所精度要求: {e}")
            return None
        qty_value = float(qty_text)
        price_value = float(price_text)

        deviation = abs(price_value - original_price) / original_price
        if deviation > 0.01:
            self.last_order_outcome = "rejected"
            log.error(f"[SAFETY] {coin} 卖单价格偏差 {deviation*100:.2f}%，拒绝下单")
            return None
        notional = price_value * qty_value
        if notional < min_notional:
            self.last_order_outcome = "rejected"
            log.warning(f"{coin} 卖单金额 ${notional:.2f} 低于最小限额")
            return None

        params = {"clientOrderId": client_order_id} if client_order_id else {}
        for attempt in range(3):
            try:
                order = self._api.create_limit_sell_order(
                    sym, qty_text, price_text, params,
                )
                result = self._normalize_order(order, coin, price_value, qty_value)
                result["client_order_id"] = result["client_order_id"] or client_order_id
                self._success()
                self.last_order_outcome = "confirmed"
                log.info(f"[SELL LIMIT] {coin} {qty_value} @ ${price_value} "
                         f"(订单ID: {result['order_id'] or '?'})")
                return result
            except Exception as e:
                log.warning(f"{coin} 限价卖出尝试 {attempt+1}/3 异常: {e}")
                recovery, recovered = self._recover_uncertain_create(
                    coin, client_order_id, e,
                )
                if recovery == "found":
                    self.last_order_outcome = "confirmed"
                    return recovered
                if recovery != "not_found":
                    self.last_order_outcome = recovery
                    log.error(f"{coin} 限价卖出停止: {e}")
                    return None
                if attempt < 2:
                    time.sleep(2 ** attempt)
        self.last_order_outcome = "rejected"
        return None

    def market_sell(self, coin: str, qty: float,
                    client_order_id: str = "") -> dict | None:
        """市价卖出；调用方必须先撤销锁定该数量的限价单。"""
        self.last_order_outcome = "pending"
        sym = self._symbol_map.get(coin)
        if not sym:
            self.last_order_outcome = "rejected"
            return None
        qty_text = self._api.amount_to_precision(sym, qty)
        qty_value = float(qty_text)
        params = {"clientOrderId": client_order_id} if client_order_id else {}
        try:
            order = self._api.create_market_sell_order(sym, qty_text, params)
            result = self._normalize_order(order, coin, fallback_qty=qty_value)
            result["client_order_id"] = result["client_order_id"] or client_order_id
            self._success()
            self.last_order_outcome = "confirmed"
            log.info(f"[SELL MARKET] {coin} {result['qty']:.4f} @ ${result['price']:.4f}")
            return result
        except Exception as e:
            recovery, recovered = self._recover_uncertain_create(
                coin, client_order_id, e,
            )
            if recovery == "found":
                self.last_order_outcome = "confirmed"
                return recovered
            self.last_order_outcome = recovery
            log.error(f"{coin} 市价卖出失败或结果未知: {e}")
            return None

    # ---- 订单管理 ----
    def fetch_open_orders(self, coin: str | None = None) -> list[dict] | None:
        try:
            if coin:
                sym = self._symbol_map.get(coin)
                if not sym:
                    return []
                orders = self._api.fetch_open_orders(sym)
                pairs = [(coin, o) for o in orders]
            else:
                orders = self._api.fetch_open_orders()
                reverse = {symbol: c for c, symbol in self._symbol_map.items()}
                pairs = [(reverse.get(o.get("symbol"), ""), o) for o in orders]
            self._success()
            return [self._normalize_order(order, order_coin) for order_coin, order in pairs]
        except Exception as e:
            self._failure(str(e))
            log.error(f"获取挂单失败: {e}")
            return None

    def cancel_order(self, order_id: str, coin: str) -> bool:
        sym = self._symbol_map.get(coin)
        if not sym:
            return False
        try:
            self._api.cancel_order(order_id, sym)
            self._success()
            log.info(f"[CANCEL] {coin} 订单 {order_id}")
            return True
        except Exception as e:
            self._failure(str(e))
            log.error(f"撤单失败 {order_id}: {e}")
            return False

    def fetch_order_status(self, order_id: str, coin: str) -> dict | None:
        sym = self._symbol_map.get(coin)
        if not sym:
            return None
        try:
            order = self._api.fetch_order(order_id, sym)
            self._success()
            return self._normalize_order(order, coin)
        except Exception as e:
            self._failure(str(e))
            log.error(f"查询订单失败 {coin}/{order_id}: {e}")
            return None

    # ---- 精度辅助 ----
    def get_precision(self, coin: str) -> tuple[float, float]:
        """返回 (amount_precision, price_precision) — 注意是 min increment，不是小数位数"""
        sym = self._symbol_map.get(coin)
        if not sym:
            return 1e-8, 1e-8
        m = self._api.market(sym)
        return (
            m.get("precision", {}).get("amount", 1e-8),
            m.get("precision", {}).get("price", 1e-8),
        )

    def get_min_notional(self, coin: str) -> float:
        sym = self._symbol_map.get(coin)
        if not sym:
            return 10.0
        return self._api.market(sym).get("limits", {}).get("cost", {}).get("min", 10.0)

    # ---- 费率 ----
    def fetch_trading_fee(self, coin: str) -> float:
        """返回 maker 费率"""
        sym = self._symbol_map.get(coin)
        if not sym:
            return 0.001
        try:
            fees = self._api.fetch_trading_fee(sym)
            return fees.get("maker", 0.001)
        except Exception:
            return 0.001
