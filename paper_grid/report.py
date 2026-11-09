"""小时报告：累计、区间损益与数据质量一起展示。"""
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def render(current: dict, previous: dict, start: float) -> str:
    lines = ["# 动态配对网格纸面交易小时报告", "",
             f"北京时间：{stamp(start)} — {stamp(current['time'])}", "",
             f"两组各 {current['settings']['capital']:,.0f} 计价币；USDT/USDC 分别按 1 美元估值。全部为模拟成交。", "",
             "撮合：真实主动成交穿过挂价、250ms激活/撤单延迟、最多10%成交量参与；不是实际队列复现。", "",
             "| 情景 | 本期权益变化 | 本期已实现 | 累计浮盈亏 | 权益 | 手续费增量 | 完整回合增量 | 库存成本 | 最大回撤 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, a in current["accounts"].items():
        old = previous.get("accounts", {}).get(name, {})
        lines.append(f"| {name} | {a['realized']+a['unrealized']-old.get('realized',0)-old.get('unrealized',0):.4f} | "
                     f"{a['realized']-old.get('realized',0):.4f} | {a['unrealized']:.4f} | {a['equity']:.4f} | "
                     f"{a['fees']-old.get('fees',0):.4f} | {a['roundtrips']-old.get('roundtrips',0)} | "
                     f"{a['inventory_cost']:.2f} | {a['max_drawdown']:.3%} |")
    lines += ["", "| 交易对 | 状态 | 间距 | 本期已实现 | 浮盈亏 | 本期回合 | 库存成本 | 买单/卖单 | 报价年龄 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for sym, s in current["symbols"].items():
        age = f"{s['quote_age']:.1f}s" if s['quote_age'] is not None else "无报价"
        old = previous.get("symbols", {}).get(sym, {})
        lines.append(f"| {sym} | {s['reason']} | {s['spacing']:.3%} | {s['realized']-old.get('realized',0):.4f} | {s['unrealized']:.4f} | {s['roundtrips']-old.get('roundtrips',0)} | {s['inventory_cost']:.2f} | {s['buy_orders']}/{s['sell_orders']} | {age} |")
    lines += ["", "## 本期诊断", ""]
    issues = []
    for name, a in current["accounts"].items():
        old = previous.get("accounts", {}).get(name, {})
        if a["roundtrips"] == old.get("roundtrips", 0):
            issues.append(f"{name}：本期尚无完整回合；先检查行情覆盖、挂单距离和撮合穿价记录，不据此直接降低利润下限。")
        if a["inventory_cost"] >= current["settings"]["max_inventory"] * .8:
            issues.append(f"{name}：库存接近上限，检查持续下行保护与资金周转。")
        if a["stale_marks"]:
            issues.append(f"{name}：{a['stale_marks']} 估值行情不新鲜，本期收益不可直接用于比较。")
        if a["oldest_lot_seconds"] > 3600:
            issues.append(f"{name}：最老仓位已持有 {a['oldest_lot_seconds']/3600:.1f} 小时，应检查未实现损益和止盈等待时间。")
    for key in ("disconnects", "trade_gaps"):
        delta = current["counters"][key] - previous.get("counters", {}).get(key, 0)
        if delta:
            issues.append(f"数据质量：{key} 本期 {delta} 次；缺口期间不推断成交，恢复后重新预热。")
    for sym, s in current["symbols"].items():
        if s.get("dust_lots"):
            issues.append(f"{sym}：{s['dust_lots']} 笔数量不足最小卖单额的库存，仍纳入成本与浮亏。")
    health = current.get("health", {})
    if health.get("raw_dropped") or health.get("raw_writer_alive") is False:
        issues.append(f"原始行情记录异常：丢弃{health.get('raw_dropped', 0)}条，写入线程存活={health.get('raw_writer_alive')}。")
    lines += [f"- {x}" for x in issues] or ["- 暂无规则触发的异常；仍需累积足够完整回合后评价策略。"]
    lines += ["", "参数与代码版本保持固定。复盘先修复数据/撮合问题；策略改动应新建实验版本，保留基线及历史账户。", ""]
    return "\n".join(lines)
