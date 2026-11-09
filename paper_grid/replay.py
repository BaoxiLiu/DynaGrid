"""逐事件离线回放，共用纸面引擎；不依赖网络，不用K线猜测Maker成交。"""
import gzip
import json
import math
from pathlib import Path

from .config import Settings
from .engine import Engine


def records(paths):
    for path in sorted(paths):
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def replay(rows, filters: dict, settings: Settings | None = None) -> dict:
    engine = Engine(settings)
    engine.filters = {s: tuple(v) for s, v in filters.items()}
    if set(engine.filters) != set(engine.signals):
        raise ValueError('过滤规则必须覆盖全部六个交易对')
    last = None
    count = 0
    equity = []
    last_sample = None
    next_tick = None
    fills = {'buy': set(), 'sell': set()}
    for row in rows:
        now = float(row['received'])
        if not math.isfinite(now) or (last is not None and now < last):
            raise ValueError('行情必须按received时间升序排列')
        if last is None:
            engine.started = now
            next_tick = now
        # 与实时驱动相同的250ms定时器；只执行已到期的tick，绝不先处理未来。
        while next_tick <= now:
            engine.tick(next_tick)
            next_tick += .25
        engine.on_packet(row['packet'], now)
        for event in engine.events:
            if event['kind'] == 'fill':
                fills[event['side']].add(event['order_id'])
        engine.events.clear()
        if (last_sample is None or now-last_sample >= 10) and all(
                signal.bid and now-signal.at <= engine.cfg.stale_seconds
                for signal in engine.signals.values()):
            summary = engine.summary(now)
            equity.append({"time": now, "accounts": {
                key: account["equity"] for key, account in summary["accounts"].items()}})
            last_sample = now
        last = now
        count += 1
    if last is None:
        raise ValueError('回放数据为空')
    result = engine.summary(last)
    return {'mode': 'event_replay', 'events': count, 'from': engine.started, 'to': last,
            'filled_orders': {k: len(v) for k, v in fills.items()}, 'status': result,
            'equity': equity, 'drawdown_sampling_seconds': 10,
            'limitations': '从空仓开始，需30根已收盘分钟K线预热；不复现真实排队、网络中断或旧账户持仓。结束库存按末次报价估值，未强制平仓。'}


def demo_records():
    """确定性的合成行情，只验证流程，不作为策略收益证据。"""
    start = 1800000000
    symbols = list(Engine().signals)
    for i in range(3600):
        now = start+i
        mid = 100 + math.sin(i/45)*.6
        for sym in symbols:
            def row(packet):
                return {'received': now, 'packet': packet}
            if i % 60 == 0:
                yield row({'e': 'kline', 's': sym, 'k': {'x': True, 'T': now*1000-1,
                          'o': '100', 'h': '100.1', 'l': '99.9'}})
            yield row({'s': sym, 'b': str(mid-.01), 'a': str(mid+.01), 'u': i+1})
            yield row({'e': 'aggTrade', 's': sym, 'p': str(mid), 'q': '10',
                       'm': math.cos(i/45)<0, 'a': i+1, 'T': now*1000})
