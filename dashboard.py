"""
终端仪表盘 — 每轮扫描后清屏重绘，用 ANSI 颜色区分关键信息
"""
import shutil
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional

from config import SCAN_INTERVAL_SECONDS, EQUITY_FILE, TOTAL_CAPITAL

BEIJING_TZ = timezone(timedelta(hours=8))
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible_width(text: str) -> int:
    """Terminal display width, ignoring ANSI color codes."""
    text = ANSI_RE.sub("", str(text))
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad text to a display width without being confused by ANSI codes."""
    text = str(text)
    diff = max(0, width - visible_width(text))
    if align == "right":
        return " " * diff + text
    if align == "center":
        left = diff // 2
        return " " * left + text + " " * (diff - left)
    return text + " " * diff


def fit(text: str, width: int) -> str:
    """Trim plain text to terminal display width and pad it."""
    text = str(text)
    out = ""
    used = 0
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if used + ch_w > width:
            break
        out += ch
        used += ch_w
    if visible_width(out) < visible_width(text) and width >= 1:
        while visible_width(out + "…") > width and out:
            out = out[:-1]
        out += "…"
    return pad(out, width)


def fmt_price(value: float) -> str:
    """Compact price formatting for fixed-width terminal columns."""
    value = float(value)
    if abs(value) >= 1000:
        return f"{value:.2f}"
    if abs(value) >= 100:
        return f"{value:.2f}"
    if abs(value) >= 10:
        return f"{value:.3f}"
    return f"{value:.4f}"


class Color:
    """ANSI 颜色常量"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    WHITE = "\033[37m"

    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"

    @staticmethod
    def g(text: str) -> str:
        return f"{Color.GREEN}{text}{Color.RESET}"

    @staticmethod
    def r(text: str) -> str:
        return f"{Color.RED}{text}{Color.RESET}"

    @staticmethod
    def y(text: str) -> str:
        return f"{Color.YELLOW}{text}{Color.RESET}"

    @staticmethod
    def c(text: str) -> str:
        return f"{Color.CYAN}{text}{Color.RESET}"

    @staticmethod
    def b(text: str) -> str:
        return f"{Color.BOLD}{text}{Color.RESET}"

    @staticmethod
    def dim(text: str) -> str:
        return f"{Color.DIM}{text}{Color.RESET}"

    @staticmethod
    def pnl(value: float) -> str:
        """盈亏着色: 正绿负红"""
        if value > 0:
            return f"{Color.GREEN}+${value:.4f}{Color.RESET}"
        elif value < 0:
            return f"{Color.RED}-${abs(value):.4f}{Color.RESET}"
        else:
            return f"${value:.4f}"

    @staticmethod
    def pct(value: float) -> str:
        """涨跌百分比着色"""
        if value > 0:
            return f"{Color.GREEN}+{value:.2f}%{Color.RESET}"
        elif value < 0:
            return f"{Color.RED}{value:.2f}%{Color.RESET}"
        else:
            return "0.00%"

    @staticmethod
    def _colored(text: str, value: float) -> str:
        """给已格式化的字符串着色 (不改变宽度)"""
        if value > 0:
            return f"{Color.GREEN}{text}{Color.RESET}"
        elif value < 0:
            return f"{Color.RED}{text}{Color.RESET}"
        return text

    @staticmethod
    def dot(value: float) -> str:
        """盈亏圆点"""
        if value > 0:
            return f"{Color.GREEN}●{Color.RESET}"
        elif value < 0:
            return f"{Color.RED}●{Color.RESET}"
        else:
            return "○"

    @staticmethod
    def remaining_to_tp(text: str, value: float) -> str:
        """距离止盈所需涨幅着色: 越接近止盈越好."""
        if value <= 0:
            return f"{Color.GREEN}{text}{Color.RESET}"
        if value <= 1.0:
            return f"{Color.GREEN}{text}{Color.RESET}"
        if value <= 3.0:
            return f"{Color.YELLOW}{text}{Color.RESET}"
        return f"{Color.RED}{text}{Color.RESET}"

    @staticmethod
    def tp_bar(progress: float, width: int = 6) -> str:
        """止盈进度条: 0=刚开始/亏损, 1=达到止盈."""
        progress = max(0.0, min(1.0, progress))
        filled = round(progress * width)
        text = "█" * filled + "░" * (width - filled)
        if progress >= 0.8:
            return f"{Color.GREEN}{text}{Color.RESET}"
        if progress >= 0.4:
            return f"{Color.YELLOW}{text}{Color.RESET}"
        return f"{Color.RED}{text}{Color.RESET}"


# ============================================================
# 仪表盘
# ============================================================

class Dashboard:
    SPARK = "▁▂▃▄▅▆▇█"
    EQUITY_SAMPLE_POINTS = 120

    def __init__(self):
        self._action_log: deque[str] = deque(maxlen=6)
        self._alt_screen = False
        self._equity_raw: list[tuple[str, float]] = []
        self._equity: deque[tuple[str, float]] = deque(maxlen=self.EQUITY_SAMPLE_POINTS)
        self._daily: dict[str, float] = {}  # date → 当日最后净值
        self._load_equity()

    # ----------------------------------------------------------
    # 净值加载
    # ----------------------------------------------------------

    @staticmethod
    def _sample_points(points: list[tuple[str, float]], limit: int) -> list[tuple[str, float]]:
        """Evenly sample a time series while preserving first and last points."""
        if len(points) <= limit:
            return points
        if limit <= 1:
            return points[-limit:]
        sampled = []
        for i in range(limit):
            idx = round(i * (len(points) - 1) / (limit - 1))
            sampled.append(points[idx])
        return sampled

    def _load_equity(self):
        """从 equity.csv 加载历史: 格式 'YYYY-MM-DD HH:MM:SS,value'"""
        raw: list[tuple[str, float]] = []
        try:
            with open(EQUITY_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or "," not in line:
                        continue
                    ts_str, val_str = line.split(",", 1)
                    try:
                        raw.append((ts_str, float(val_str)))
                    except ValueError:
                        continue
        except FileNotFoundError:
            return

        if not raw:
            return

        # 兼容旧版本: equity.csv 早期曾记录总资产(约 400),
        # 现在记录净收益。混在一起会把净收益曲线压扁。
        last_legacy_idx = -1
        for i, (_, v) in enumerate(raw):
            if abs(v) > 100:
                last_legacy_idx = i
        if 0 <= last_legacy_idx < len(raw) - 2:
            raw = raw[last_legacy_idx + 1:]

        # 整体曲线: 从全部历史中均匀采样, 而不是只看最近一段。
        self._equity_raw = raw
        self._equity = deque(
            self._sample_points(raw, self.EQUITY_SAMPLE_POINTS),
            maxlen=self.EQUITY_SAMPLE_POINTS,
        )

        # 日线汇总: 每天取最后一笔
        for ts, v in raw:
            date = ts[:10]
            self._daily[date] = v

    # ----------------------------------------------------------
    # 记录
    # ----------------------------------------------------------

    def record_equity(self, total_value: float):
        """记录净值 (带时间戳) 并持久化"""
        now = datetime.now(BEIJING_TZ)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        self._equity_raw.append((ts, total_value))
        self._equity = deque(
            self._sample_points(self._equity_raw, self.EQUITY_SAMPLE_POINTS),
            maxlen=self.EQUITY_SAMPLE_POINTS,
        )
        self._daily[ts[:10]] = total_value
        try:
            with open(EQUITY_FILE, "a") as f:
                f.write(f"{ts},{total_value:.2f}\n")
        except Exception:
            pass

    # ----------------------------------------------------------
    # 净收益曲线图
    # ----------------------------------------------------------

    @classmethod
    def _line_chart(cls, values, w: int, label: str) -> list[str]:
        """生成固定比例的终端折线图."""
        if len(values) < 2:
            return []

        vmin = min(values)
        vmax = max(values)
        latest = values[-1]
        if vmax <= vmin:
            line = f"{label}: {latest:+.4f}"
            return [f"  {Color.dim(line)}"]

        span = vmax - vmin
        chart_w = min(max(42, w - 20), 78)
        chart_w = min(chart_w, len(values))
        if chart_w < 2:
            return []

        sampled = []
        for i in range(chart_w):
            idx = round(i * (len(values) - 1) / (chart_w - 1))
            sampled.append(values[idx])

        height = 9
        rows = [[" " for _ in range(chart_w)] for _ in range(height)]

        def y_for(v: float) -> int:
            return height - 1 - round((v - vmin) / span * (height - 1))

        ys = [max(0, min(height - 1, y_for(v))) for v in sampled]
        zero_y = None
        if vmin < 0 < vmax:
            zero_y = y_for(0)
            if 0 <= zero_y < height:
                for x in range(chart_w):
                    rows[zero_y][x] = "─"

        for x, y in enumerate(ys):
            if x > 0:
                prev_y = ys[x - 1]
                lo, hi = min(prev_y, y), max(prev_y, y)
                if prev_y == y:
                    if rows[y][x - 1] == " ":
                        rows[y][x - 1] = "─"
                else:
                    for yy in range(lo + 1, hi):
                        rows[yy][x - 1] = "│"
                    rows[prev_y][x - 1] = "╮" if y > prev_y else "╯"
            rows[y][x] = "●" if x == chart_w - 1 else "─"

        lines = [
            f"  {Color.BOLD}{label}{Color.RESET}  "
            f"现 {Color.pnl(latest)}  区间 {Color.r(f'${vmin:+.2f}')}..{Color.g(f'${vmax:+.2f}')}"
        ]
        for y, row in enumerate(rows):
            if y == 0:
                axis = f"{vmax:+7.2f} "
            elif zero_y is not None and y == zero_y:
                axis = f"{0:+7.2f} "
            elif y == height - 1:
                axis = f"{vmin:+7.2f} "
            else:
                axis = " " * 8
            body = "".join(row)
            if zero_y is not None and y == zero_y:
                body = Color.dim(body)
            lines.append(f"  {Color.dim(axis)}│{body}{Color.RESET}")
        lines.append(f"  {Color.dim(' ' * 8 + '└' + '─' * chart_w)}{Color.RESET}")
        lines.append(f"  {Color.dim('整体历史均匀采样 ' + str(len(values)) + ' 点')}")
        return lines

    @staticmethod
    def _connector(prev_y: int, cur_y: int, level: int) -> str:
        """两点之间在给定 level 上的连线字符"""
        lo, hi = min(prev_y, cur_y), max(prev_y, cur_y)
        if level < lo or level > hi:
            return " "
        if prev_y == cur_y:
            return "─" if level == prev_y else " "
        # 斜线
        if prev_y < cur_y:
            if level == prev_y:
                return "╰"
            elif level == cur_y:
                return "╭"
            else:
                return "│"
        else:
            if level == prev_y:
                return "╮"
            elif level == cur_y:
                return "╯"
            else:
                return "│"

    @staticmethod
    def _daily_line(daily: dict[str, float], w: int) -> list[str]:
        """最近 30 天日线折线图"""
        if len(daily) < 5:
            return []
        sorted_dates = sorted(daily.keys())[-30:]
        values = [daily[d] for d in sorted_dates]
        return Dashboard._line_chart(values, w, f"日线净收益 ({sorted_dates[0]} -> {sorted_dates[-1]})")

    def enter_alt_screen(self):
        """进入交替屏幕缓冲 (类似 vim/htop)"""
        if not self._alt_screen:
            print("\033[?1049h\033[2J\033[H", end="", flush=True)
            self._alt_screen = True

    def exit_alt_screen(self):
        """退出交替屏幕, 恢复原始终端"""
        if self._alt_screen:
            print("\033[?1049l", end="", flush=True)
            self._alt_screen = False

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------

    def render(
        self,
        scan_count: int,
        free_usdt: float,
        total_value: float,
        holdings: list[dict],
        pnl_today: float,
        pnl_total: float,
        actions: list[str],
        errors: list[str],
        alerts: list[str],
        committed: float = 0.0,
        unrealized_pnl: float = 0.0,
        net_pnl: float = 0.0,
        market_data_status: str = "",
    ):
        w = self._term_width()
        now = datetime.now(BEIJING_TZ)

        # 追加动作到缓冲区
        for a in actions:
            ts = now.strftime("%H:%M")
            self._action_log.appendleft(f"{ts}  {a}")
        # 错误也进缓冲区
        for e in errors:
            ts = now.strftime("%H:%M")
            self._action_log.appendleft(f"{ts}  WARN {e}")

        lines: list[str] = []

        # ---- 清屏 ----
        lines.append("\033[2J\033[H")

        # ---- 标题栏 ----
        lines.append(self._header(scan_count, now, w))

        # ---- 账户摘要 ----
        lines.append(self._account_bar(free_usdt, total_value, pnl_today, pnl_total,
                                       unrealized_pnl, net_pnl, committed, w))
        if market_data_status:
            lines.append(f"  {Color.DIM}{market_data_status}{Color.RESET}")

        # ---- 净值曲线 ----
        if self._equity:
            intraday_vals = [v for _, v in self._equity]
            start_ts = self._equity[0][0][5:16] if self._equity else ""
            end_ts = self._equity[-1][0][5:16] if self._equity else ""
            chart = Dashboard._line_chart(intraday_vals, w, f"累计盈亏 ({start_ts} -> {end_ts})")
            if chart:
                lines.append("")
                lines.extend(chart)
        if self._daily:
            daily_chart = Dashboard._daily_line(self._daily, w)
            if daily_chart:
                lines.append("")
                lines.extend(daily_chart)

        # ---- 持仓表格 ----
        lines.append("")
        lines.extend(self._holdings_table(holdings, w))

        # ---- 分隔 + 动态 ----
        if self._action_log:
            lines.append("")
            lines.append(self._sep(w, "最近动态"))
            for entry in list(self._action_log)[:6]:
                lines.append(f"  {Color.DIM}{entry}{Color.RESET}")

        # ---- 告警 ----
        if alerts:
            lines.append("")
            for a in alerts:
                lines.append(f"  {Color.RED}{Color.BOLD}ALERT {a}{Color.RESET}")

        # ---- 底栏 ----
        lines.append("")
        lines.append(self._sep(w, None))
        lines.append(f"  {Color.DIM}下次扫描约 {SCAN_INTERVAL_SECONDS}s 后 | "
                     f"Ctrl+C 安全退出{Color.RESET}")

        print("\n".join(lines), flush=True)

    # ----------------------------------------------------------
    # 组件
    # ----------------------------------------------------------

    def _header(self, scan_count: int, now: datetime, w: int) -> str:
        title = f"  动态网格策略系统"
        right = f"第 {scan_count} 次扫描  {now.strftime('%H:%M:%S')} CST  "
        padding = max(2, w - visible_width(title) - visible_width(right) - 2)
        return (
            f"{Color.BOLD}┌{'─' * (w - 2)}┐{Color.RESET}\n"
            f"{Color.BOLD}│{Color.RESET}"
            f"{Color.CYAN}{title}{Color.RESET}"
            f"{' ' * padding}"
            f"{Color.DIM}{right}{Color.RESET}"
            f"{Color.BOLD}│{Color.RESET}"
        )

    def _account_bar(self, free_usdt: float, total_value: float,
                     pnl_today: float, pnl_total: float,
                     unrealized_pnl: float, net_pnl: float,
                     committed: float, w: int) -> str:
        cumulative_return = (
            net_pnl / TOTAL_CAPITAL * 100 if TOTAL_CAPITAL > 0 else 0.0
        )
        line1 = (
            f"  总资产 {Color.b(f'${total_value:.2f}')} │ "
            f"可用 {Color.c(f'${free_usdt:.2f}')} │ "
            f"今日已实现 {Color.pnl(pnl_today)}"
        )
        line2 = (
            f"  已实现 {Color.pnl(pnl_total)} │ "
            f"未实现 {Color.pnl(unrealized_pnl)} │ "
            f"净收益 {Color.pnl(net_pnl)} │ "
            f"累计收益率 {Color.pct(cumulative_return)}"
        )
        return f"{line1}\n{line2}"

    def _holdings_table(self, holdings: list[dict], w: int) -> list[str]:
        if not holdings:
            return [f"  {Color.DIM}暂无持仓，等待建仓机会...{Color.RESET}"]

        lines = [self._sep(w, "当前持仓")]

        # 固定列宽，保留右侧层级列随终端宽度伸缩。
        cols = [
            ("币种", 6, "left"),
            ("档位", 9, "right"),
            ("买入价", 11, "right"),
            ("现价", 11, "right"),
            ("止盈价", 11, "right"),
            ("盈亏率", 9, "right"),
            ("进度", 8, "left"),
            ("浮盈", 11, "right"),
        ]

        def row(cells: list[str], dim: bool = False) -> str:
            parts = []
            for value, (_, width, align) in zip(cells, cols):
                parts.append(pad(value, width, align))
            text = "  " + "  ".join(parts)
            return Color.dim(text) if dim else text

        # 表头
        lines.append(Color.dim(row([c[0] for c in cols])))
        lines.append(f"  {Color.DIM}{'─' * max(20, min(w - 4, sum(c[1] for c in cols) + 2 * (len(cols) - 1)))}{Color.RESET}")

        for h in holdings:
            coin = h["coin"]
            cur = h["current_price"]

            # 所有持有中的 tier
            holding_tiers = [t for t in h["tiers"] if t["status"] == "holding"]
            tier_labels = " ".join(t["tier"] for t in holding_tiers)

            # 总浮盈
            total_upnl = sum(t["unrealized_pnl"] for t in holding_tiers)
            position = sum(t["buy_price"] * t["qty"] for t in holding_tiers)
            qty = sum(t["qty"] for t in holding_tiers)
            avg_buy = position / qty if qty else 0.0
            nearest_tp = min((t["sell_price"] for t in holding_tiers), default=0.0)
            pnl_pct = (total_upnl / position * 100) if position > 0 else 0.0
            tier_progress_values = []
            for t in holding_tiers:
                span = t["sell_price"] - t["buy_price"]
                tier_progress_values.append(((cur - t["buy_price"]) / span) if span > 0 else 1.0)
            tp_progress = max(tier_progress_values, default=0.0)

            # 先格式化再着色, 避免 ANSI 码干扰对齐
            pnl_pct_str = Color._colored(pad(f"{pnl_pct:+.2f}%", 9, "right"), pnl_pct)
            upnl_str = Color._colored(pad(f"{total_upnl:+.4f}", 11, "right"), total_upnl)

            lines.append(row([
                coin,
                fit(tier_labels, cols[1][1]),
                fmt_price(avg_buy),
                fmt_price(cur),
                fmt_price(nearest_tp),
                pnl_pct_str,
                Color.tp_bar(tp_progress),
                upnl_str,
            ]))

            next_buy = h.get("next_buy")
            if next_buy:
                trigger = next_buy.get("trigger_price")
                distance = next_buy.get("distance_pct")
                trigger_text = fmt_price(trigger) if trigger else "动态计算中"
                distance_text = (
                    f"距现价 {distance:+.2f}%" if distance is not None else "距现价 --"
                )
                lines.append(
                    Color.dim(
                        f"    ↳ 下一买入：{next_buy['tier']} @ {trigger_text} "
                        f"（{distance_text}）│ 金额 ${next_buy['amount']:.2f} │ "
                        f"{next_buy['status']}"
                    )
                )

            # 多 tier 时展开显示每层
            if len(holding_tiers) > 1:
                for t in holding_tiers:
                    tier_cost = t["buy_price"] * t["qty"]
                    tier_pnl_pct = (t["unrealized_pnl"] / tier_cost * 100) if tier_cost > 0 else 0.0
                    tier_span = t["sell_price"] - t["buy_price"]
                    tier_progress = ((cur - t["buy_price"]) / tier_span) if tier_span > 0 else 1.0
                    lines.append(row([
                        "",
                        t["tier"],
                        fmt_price(t["buy_price"]),
                        fmt_price(cur),
                        fmt_price(t["sell_price"]),
                        Color._colored(pad(format(tier_pnl_pct, "+.2f") + "%", cols[5][1], "right"), tier_pnl_pct),
                        Color.tp_bar(tier_progress),
                        Color._colored(pad(format(t["unrealized_pnl"], "+.4f"), cols[7][1], "right"), t["unrealized_pnl"]),
                    ], dim=True))

            # 已卖出的 tier
            sold_tiers = [t for t in h["tiers"] if t["status"] == "sold"]
            if sold_tiers:
                sold_labels = " ".join(t["tier"] for t in sold_tiers)
                lines.append(Color.dim(f"  {pad('', cols[0][1])}  {sold_labels} sold"))

        return lines

    def _sep(self, w: int, label: Optional[str] = None) -> str:
        if label:
            left = f"  {label}  "
            remaining = max(0, w - visible_width(left) - 2)
            return f"{Color.DIM}{left}{'─' * remaining}{Color.RESET}"
        else:
            return f"{Color.DIM}{'─' * w}{Color.RESET}"

    @staticmethod
    def _term_width() -> int:
        try:
            return shutil.get_terminal_size().columns
        except Exception:
            return 120
