#!/usr/bin/env python3
"""
动态网格策略系统 — 主入口
===========================
多币种四档动态网格 + ATR动态再入场 + 市值排序选择
现货交易，不设止损，按配置定时扫描

用法:
    python main.py                 # 正常运行
    python main.py --once          # 只扫描一次
    python main.py --dry-run       # 模拟运行 (不下单)
    python main.py --no-dashboard  # 无仪表盘模式 (适合 Docker/cron)
"""

import sys
import time
import signal
import atexit
import argparse
import logging
import os
import fcntl
import threading
from datetime import datetime, timezone, timedelta

from config import (
    MONITOR_COINS, TOTAL_CAPITAL, SCAN_INTERVAL_SECONDS,
    FREEZE_BUYS_FLAG_FILE, INSTANCE_LOCK_FILE, PAUSE_FLAG_FILE,
    MAX_HOLDINGS,
    MAX_CONSECUTIVE_API_FAILURES,
    PROXY_URL, WEBSOCKET_MARKET_DATA_ENABLED, WEBSOCKET_OBSERVATION_ONLY,
    WEBSOCKET_STALE_SECONDS, WEBSOCKET_RECONNECT_SECONDS,
    WEBSOCKET_OBSERVATION_REARM_RATE, WEBSOCKET_REST_WAKE_DEBOUNCE_SECONDS,
    NOTIFICATION_CHANNELS,
)
from exchange import Exchange
from state import StateCorruptionError, StateManager
from pnl import PnLTracker
from strategy import StrategyEngine
from monitor import Monitor
from dashboard import Dashboard
from logger import log, set_console_level
from market_data import (
    BinanceBookTickerStream, MarketDataCache, WebSocketTriggerObserver,
)

BEIJING_TZ = timezone(timedelta(hours=8))

# 全局标志
_running = True


def _migrate_legacy_files():
    """将旧位置 (根目录) 的运行时文件迁移到 data/ 目录。"""
    from config import (
        DATA_DIR, LOGS_DIR, FLAGS_DIR,
        STATE_FILE, STATE_BACKUP_FILE, STATE_LOCK_FILE,
        TRADE_LOG_FILE, EQUITY_FILE,
        RECONCILE_REPORT_FILE, DAILY_SUMMARY_FILE,
        LOG_FILE, PAUSE_FLAG_FILE, FREEZE_BUYS_FLAG_FILE,
        INSTANCE_LOCK_FILE,
    )
    import shutil

    _migrations = {
        "state.json":              STATE_FILE,
        "state.json.bak":          STATE_BACKUP_FILE,
        "state.json.lock":         STATE_LOCK_FILE,
        "trades.csv":              TRADE_LOG_FILE,
        "trades.csv.bak":          TRADE_LOG_FILE + ".bak",
        "equity.csv":              EQUITY_FILE,
        "reconcile_report.json":   RECONCILE_REPORT_FILE,
        "daily_summary.json":      DAILY_SUMMARY_FILE,
        "system.log":              LOG_FILE,
        "pause.flag":              PAUSE_FLAG_FILE,
        "freeze_buys.flag":        FREEZE_BUYS_FLAG_FILE,
        "strategy.lock":           INSTANCE_LOCK_FILE,
        "websocket_runtime.log":   os.path.join(LOGS_DIR, "websocket_runtime.log"),
    }

    moved = 0
    for old_name, new_path in _migrations.items():
        old_path = os.path.join(os.path.dirname(DATA_DIR), old_name)
        if not os.path.exists(old_path):
            # 也检查带时间戳的日志轮转文件
            if old_name == "system.log":
                _migrate_rotated_logs(old_path, LOGS_DIR)
            # 检查 state.json.bak_* 历史备份
            if old_name == "state.json.bak":
                _migrate_state_backups(DATA_DIR)
            continue
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.move(old_path, new_path)
            moved += 1
        except OSError:
            pass

    if moved:
        log.warning(f"已从旧位置迁移 {moved} 个文件到 data/ 目录")


def _migrate_state_backups(data_dir: str):
    """迁移 state.json.bak_YYYYMMDD_* 历史备份文件。"""
    import shutil
    import glob
    root = os.path.dirname(data_dir)
    for old_bak in sorted(glob.glob(os.path.join(root, "state.json.bak_*"))):
        name = os.path.basename(old_bak)
        new_path = os.path.join(data_dir, name)
        if not os.path.exists(new_path):
            try:
                shutil.move(old_bak, new_path)
            except OSError:
                pass


def _migrate_rotated_logs(old_base: str, logs_dir: str):
    """迁移日志轮转文件 system.log.YYYY-MM-DD。"""
    import shutil
    import glob
    for old_log in sorted(glob.glob(old_base + ".*")):
        name = os.path.basename(old_log)
        new_path = os.path.join(logs_dir, name)
        if not os.path.exists(new_path):
            try:
                shutil.move(old_log, new_path)
            except OSError:
                pass


def acquire_instance_lock():
    """防止两个交易主进程同时运行。"""
    lock_file = open(INSTANCE_LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError("已有策略主进程持有运行锁，拒绝启动第二个实例")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def freeze_all_buys(reason: str):
    if os.path.exists(FREEZE_BUYS_FLAG_FILE):
        return
    with open(FREEZE_BUYS_FLAG_FILE, "w", encoding="utf-8") as f:
        f.write(f"{datetime.now(BEIJING_TZ).isoformat()} {reason}\n")
    log.critical(f"已自动冻结全部买入: {reason}")


def wait_for_rate_limit_cooldown(ex: Exchange) -> bool:
    """限频熔断：冷却期间不再向币安发 REST 请求，返回 True 表示本轮跳过。"""
    remaining = ex.rate_limit_cooldown_seconds()
    if remaining <= 0:
        return False

    freeze_all_buys(f"币安接口限频，冷却剩余约 {remaining} 秒")
    log.critical(
        f"[RATE LIMIT] 币安接口限频熔断，暂停全部 REST 请求约 {remaining} 秒；"
        "已有交易所卖单保持不变，恢复后仍需人工 resume 才会买入"
    )
    sleep_time = min(remaining, SCAN_INTERVAL_SECONDS)
    while sleep_time > 0 and _running:
        time.sleep(min(1, sleep_time))
        sleep_time -= 1
    return True


def signal_handler(sig, frame):
    global _running
    log.info("收到停止信号，正在安全退出...")
    _running = False


# ============================================================
# 通知
# ============================================================

def send_notifications(message: str):
    """向所有已配置的渠道发送日报通知。"""
    import requests
    channels = NOTIFICATION_CHANNELS

    for channel, url in channels.items():
        if not url:
            continue
        try:
            if channel == "feishu":
                requests.post(url, json={
                    "msg_type": "text",
                    "content": {"text": message},
                }, timeout=5)
            elif channel == "telegram":
                requests.post(url, json={
                    "chat_id": "",  # 由 URL 中的 chat_id 参数携带
                    "text": message,
                    "parse_mode": "HTML",
                }, timeout=5)
            elif channel == "wecom":
                requests.post(url, json={
                    "msgtype": "text",
                    "text": {"content": message},
                }, timeout=5)
        except Exception:
            pass


# ============================================================
# 运行
# ============================================================

def run_loop(dry_run: bool = False, no_dashboard: bool = False):
    global _running

    try:
        instance_lock = acquire_instance_lock()
    except RuntimeError as e:
        log.critical(str(e))
        return

    # ---- 初始化 ----
    _migrate_legacy_files()
    log.info("=" * 50)
    log.info("  动态网格策略系统 启动")
    log.info(f"  总资金: ${TOTAL_CAPITAL} | 币种: {MONITOR_COINS}")
    log.info(f"  最大持仓: {MAX_HOLDINGS} | 扫描间隔: {SCAN_INTERVAL_SECONDS}s")
    log.info(f"  模拟模式: {'开' if dry_run else '关'}")
    log.info("=" * 50)

    # 交易所
    ex = Exchange()
    try:
        ex.load_markets()
        ex.init_symbols(MONITOR_COINS)
    except Exception as e:
        log.error(f"交易所初始化失败: {e}")
        return

    # 状态
    state = StateManager()
    try:
        state.load()
    except StateCorruptionError as e:
        log.critical(str(e))
        instance_lock.close()
        return

    # 盈亏
    pnl = PnLTracker()

    # 策略引擎
    engine = StrategyEngine(ex, state, pnl)

    report = engine.reconcile_startup()
    if not report["ok"]:
        for issue in report["issues"]:
            log.critical(f"对账问题: {issue}")
        log.critical("启动对账未通过，策略不会进入交易循环")
        instance_lock.close()
        return

    # 监控
    monitor = Monitor()

    # 恢复
    log.info("从历史状态恢复中...")
    if state.coins:
        log.info(f"已恢复 {len(state.coins)} 个币种持仓")
    else:
        log.info("无历史持仓，从零开始")

    # 仪表盘
    dashboard = Dashboard()
    if not no_dashboard:
        dashboard.enter_alt_screen()
        atexit.register(dashboard.exit_alt_screen)
        set_console_level(logging.WARNING)  # 控制台仅显示 WARNING+，仪表盘用 print
    else:
        log.info("无仪表盘模式，日志直接输出到控制台")

    # WebSocket 只做行情观察：线程中不访问策略状态、不调用交易所、更不会下单。
    market_cache = None
    trigger_observer = None
    market_stream = None
    rest_wakeup_event = None
    if WEBSOCKET_MARKET_DATA_ENABLED and not WEBSOCKET_OBSERVATION_ONLY:
        # 这是实盘保护开关：当前版本没有 WebSocket 直连下单路径。
        log.critical("WebSocket 非观察模式未实现，已禁用 WebSocket 行情线程")
    elif WEBSOCKET_MARKET_DATA_ENABLED:
        market_cache = MarketDataCache()
        rest_wakeup_event = threading.Event()
        trigger_observer = WebSocketTriggerObserver(
            market_cache, WEBSOCKET_STALE_SECONDS,
            WEBSOCKET_OBSERVATION_REARM_RATE,
            rest_wakeup_event=rest_wakeup_event,
            wake_debounce_seconds=WEBSOCKET_REST_WAKE_DEBOUNCE_SECONDS,
        )
        trigger_observer.set_levels(engine.get_websocket_observation_levels())
        market_stream = BinanceBookTickerStream(
            MONITOR_COINS, market_cache, trigger_observer,
            proxy_url=PROXY_URL, reconnect_seconds=WEBSOCKET_RECONNECT_SECONDS,
            on_reconnect=rest_wakeup_event.set,
        )
        market_stream.start()
        log.info("WebSocket 行情观察已启动：到价仅唤醒 REST 复核，不直接下单")

    # ---- 主循环 ----
    scan_count = 0
    last_scan_ok = True
    exchange_ok = True
    consecutive_api_failures = 0

    while _running:
        scan_count += 1
        scan_start = time.time()
        # 本轮动作和错误
        scan_actions: list[str] = []
        scan_errors: list[str] = []

        if trigger_observer:
            for event in trigger_observer.drain_events():
                log.info(event)
                scan_actions.append(event)

        try:
            if wait_for_rate_limit_cooldown(ex):
                continue

            # 检查交易所连接
            exchange_ok = ex.check_connection()
            if not exchange_ok:
                if wait_for_rate_limit_cooldown(ex):
                    continue
                consecutive_api_failures += 1
                log.error("交易所连接异常")
                scan_errors.append("交易所连接异常")
                if consecutive_api_failures >= MAX_CONSECUTIVE_API_FAILURES:
                    freeze_all_buys(
                        f"连续 {consecutive_api_failures} 次账户连接失败",
                    )
                time.sleep(30)
                continue
            consecutive_api_failures = 0

            # 执行扫描
            if dry_run:
                log.info("[DRY-RUN] 跳过实际操作")
                last_scan_ok = True
            else:
                summary = engine.run_scan()
                last_scan_ok = len(summary.get("errors", [])) == 0
                scan_actions.extend(summary.get("actions", []))
                scan_errors = summary.get("errors", [])
                if trigger_observer:
                    trigger_observer.set_levels(engine.get_websocket_observation_levels())
                    for event in trigger_observer.drain_events():
                        log.info(event)
                        scan_actions.append(event)

                # 重要事件保留日志
                for action in scan_actions:
                    log.info(f"  → {action}")
                for err in scan_errors:
                    log.warning(f"  ✗ {err}")
                if wait_for_rate_limit_cooldown(ex):
                    continue

            # 获取行情
            tickers = ex.fetch_tickers(MONITOR_COINS) or {}
            if wait_for_rate_limit_cooldown(ex):
                continue

            # 市场异常检测
            alerts = monitor.check_market_anomaly(tickers)

            # 系统健康
            health = monitor.check_system_health(exchange_ok, last_scan_ok)
            if health:
                alerts.extend(health)

            # 构建持仓摘要
            free_balance = ex.fetch_free_balance()
            if wait_for_rate_limit_cooldown(ex):
                continue
            free = free_balance.get("USDT", 0) if free_balance is not None else 0
            holdings = engine.get_holdings_summary(tickers, free)
            total_value = free + sum(h["link_value"] for h in holdings)
            committed = state.get_total_committed_all()

            # 未实现盈亏: 所有持有中 tier 的 (现价-买入价)*数量 之和
            unrealized_pnl = sum(
                t["unrealized_pnl"]
                for h in holdings
                for t in h["tiers"]
                if t["status"] == "holding"
            )
            net_pnl = pnl.get_total_pnl() + unrealized_pnl

            # 记录累计净收益曲线 (已实现 + 未实现)
            dashboard.record_equity(net_pnl)

            # ---- 仪表盘 (每次扫描刷新) ----
            if not no_dashboard:
                dashboard.render(
                    scan_count=scan_count,
                    free_usdt=free,
                    total_value=total_value,
                    holdings=holdings,
                    pnl_today=pnl.get_today_pnl(),
                    pnl_total=pnl.get_total_pnl(),
                    unrealized_pnl=unrealized_pnl,
                    net_pnl=net_pnl,
                    actions=scan_actions,
                    errors=scan_errors,
                    alerts=alerts,
                    committed=committed,
                    market_data_status=(
                        market_cache.status_text(len(MONITOR_COINS), WEBSOCKET_STALE_SECONDS)
                        if market_cache else ""
                    ),
                )
            else:
                log.info(
                    f"#{scan_count} | 总 ${total_value:.2f} | "
                    f"可用 ${free:.2f} | "
                    f"已实现 ${pnl.get_total_pnl():+.4f} | "
                    f"净收益 ${net_pnl:+.4f} | "
                    f"持仓 {len(holdings)} 个"
                )
                for action in scan_actions:
                    log.info(f"  → {action}")
                for err in scan_errors:
                    log.warning(f"  ✗ {err}")
                for alert in alerts:
                    log.warning(f"  ⚠ {alert}")

            # 飞书日报 (仅在每日触发时间发送)
            if monitor.should_send_daily_summary():
                daily = {
                    "today_pnl": pnl.get_today_pnl(),
                    "total_pnl": pnl.get_total_pnl(),
                    "total_trades": len(pnl.get_today_trades()),
                    "buys": len([t for t in pnl.get_today_trades() if t["side"] == "BUY"]),
                    "sells": len([t for t in pnl.get_today_trades() if t["side"] == "SELL"]),
                    "free_usdt": free,
                    "total_value": total_value,
                }
                msg = monitor.format_daily_summary(daily, holdings, alerts)
                log.info("\n" + msg)
                send_notifications(msg)

        except Exception as e:
            log.error(f"扫描异常: {e}", exc_info=True)
            last_scan_ok = False

        # 等待下一轮
        elapsed = time.time() - scan_start
        sleep_time = max(1, SCAN_INTERVAL_SECONDS - elapsed)
        log.debug(f"本轮耗时 {elapsed:.1f}s, 等待 {sleep_time:.0f}s")

        # 分段等待：WebSocket 到价只会唤醒主线程提前执行一次完整 REST 复核。
        # 真实下单仍在下一轮的 engine.run_scan() 内，由原有风控统一裁决。
        woke_by_websocket = False
        while sleep_time > 0 and _running:
            timeout = min(1, sleep_time)
            if rest_wakeup_event and rest_wakeup_event.wait(timeout):
                rest_wakeup_event.clear()
                woke_by_websocket = True
                log.info("[WS WAKE] 到价信号已收到，提前执行完整 REST 复核")
                break
            sleep_time -= timeout

        if woke_by_websocket:
            continue

    # ---- 退出 ----
    log.info("策略已停止")
    if market_stream:
        market_stream.stop()
    state.save()
    log.info("状态已保存")
    if not no_dashboard:
        dashboard.exit_alt_screen()
    instance_lock.close()


# ============================================================
# 手动干预
# ============================================================

def manual_commands():
    """解析手动命令"""
    print("\n可用命令:")
    print("  status        查看当前状态")
    print("  holdings      查看持仓")
    print("  pause         只暂停新 T1")
    print("  freeze        冻结所有买入，保留卖单")
    print("  resume        恢复所有买入")
    print("  exit          退出")
    print()

    ex = Exchange()
    ex.load_markets()
    ex.init_symbols(MONITOR_COINS)
    state = StateManager()
    state.load()

    while True:
        cmd = input("> ").strip().lower()
        if not cmd:
            continue
        if cmd == "exit":
            break

        parts = cmd.split()
        action = parts[0]

        if action == "status":
            bal = ex.fetch_free_balance() or {}
            print(f"可用 USDT: ${bal.get('USDT', 0):.2f}")
            print(f"持有币种: {state.holding_count} 个")
            print(f"总投入: ${state.get_total_committed_all():.2f}")

        elif action == "holdings":
            tickers = ex.fetch_tickers(MONITOR_COINS) or {}
            for coin, cs in state.coins.items():
                if cs.active:
                    price = tickers.get(coin, {}).get("price", cs.base_price)
                    print(f"\n{coin}: 基准 ${cs.base_price:.4f} | 现价 ${price:.4f}")
                    for t in cs.tiers:
                        pnl = (price - t.buy_price) * t.qty if t.status == "holding" else 0
                        status = "持有中" if t.status == "holding" else "已卖出"
                        print(f"  T{t.tier_index+1}: 买${t.buy_price:.4f} "
                              f"→ 止盈${t.sell_price:.4f} | {status} | 浮盈${pnl:+.4f}")

        elif action == "pause":
            with open(PAUSE_FLAG_FILE, "w") as f:
                f.write(datetime.now(BEIJING_TZ).isoformat())
            print("已暂停新 T1；已有持仓的后续档位仍可执行")

        elif action == "freeze":
            with open(FREEZE_BUYS_FLAG_FILE, "w") as f:
                f.write(datetime.now(BEIJING_TZ).isoformat())
            print("已冻结全部买入；现有止盈卖单继续管理")

        elif action == "resume":
            for flag in (PAUSE_FLAG_FILE, FREEZE_BUYS_FLAG_FILE):
                if os.path.exists(flag):
                    os.remove(flag)
            print("已恢复全部买入")

        elif action in {"stop", "clear"}:
            print("该命令已停用：旧实现可能造成交易所与本地状态不一致")

        else:
            print(f"未知命令: {action}")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="动态网格策略系统")
    parser.add_argument("--once", action="store_true", help="只扫描一次")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行 (不下单)")
    parser.add_argument("--manual", action="store_true", help="手动控制台")
    parser.add_argument("--no-dashboard", action="store_true", help="无仪表盘模式，日志直接输出（适合 Docker/cron 等）")
    parser.add_argument("--reconcile-only", action="store_true", help="只执行启动对账")
    args = parser.parse_args()

    if args.manual:
        manual_commands()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    _migrate_legacy_files()

    if args.once or args.reconcile_only:
        try:
            instance_lock = acquire_instance_lock()
        except RuntimeError as e:
            log.critical(str(e))
            sys.exit(1)
        ex = Exchange()
        ex.load_markets()
        ex.init_symbols(MONITOR_COINS)
        state = StateManager()
        try:
            state.load()
        except StateCorruptionError as e:
            log.critical(str(e))
            instance_lock.close()
            sys.exit(1)
        pnl = PnLTracker()
        engine = StrategyEngine(ex, state, pnl)
        report = engine.reconcile_startup()
        if not report["ok"]:
            for issue in report["issues"]:
                log.critical(f"对账问题: {issue}")
            instance_lock.close()
            sys.exit(2)

        if args.reconcile_only:
            log.info("启动对账通过")
        elif args.dry_run:
            log.info("[DRY-RUN] 单次扫描 (不执行交易)")
        else:
            summary = engine.run_scan()
            log.info(f"扫描完成: {len(summary['actions'])} 个动作")
            for a in summary["actions"]:
                log.info(f"  {a}")
        instance_lock.close()
    else:
        run_loop(dry_run=args.dry_run, no_dashboard=args.no_dashboard)
