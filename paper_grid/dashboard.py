"""只读监控页，绑定回环地址，通过SSH隧道访问，不提供交易或文件浏览API。"""
import argparse
import gzip
from urllib.parse import urlsplit, parse_qs
import json
import math
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import DEFAULT_DATA
from .storage import write_json

PAGE = Path(__file__).with_name("dashboard.html")


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def tail_events(root: Path, limit: int = 80) -> list:
    files = sorted(root.glob("events-????-??-??.jsonl"))[-2:]
    rows = deque(maxlen=limit)
    for path in files:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 150000))
            if size > 150000:
                f.readline()
            for line in f:
                try:
                    row = json.loads(line)
                    if row.get("kind") in {"fill", "roundtrip", "data_gap_cancel", "rejected"}:
                        rows.append(row)
                except ValueError:
                    continue
    return list(reversed(rows))


def performance(root: Path, history: list, status: dict, now: float) -> dict:
    """只读指标：双边成交额；北京时间自然日；小时净权益收益率，零无风险利率。"""
    day_start = (int((now + 28800) // 86400) * 86400) - 28800
    result = {k: {"total": 0.0, "today": 0.0, "hour": 0.0, "buy": 0.0, "sell": 0.0}
              for k in status.get("accounts", {})}
    for path in sorted(root.glob("events-????-??-??.jsonl")):
        with path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("kind") != "fill" or e.get("account") not in result or e["time"] > now:
                        continue
                    m = result[e["account"]]
                    value = e["price"] * e["qty"]
                    m["total"] += value
                    m[e["side"]] += value
                    if e["time"] >= day_start: m["today"] += value
                    if e["time"] >= now - 3600: m["hour"] += value
                except (ValueError, KeyError):
                    continue
    for key, m in result.items():
        capital = status["settings"]["capital"]
        m["turnover"] = m["total"] / capital
        m["today_turnover"] = m["today"] / capital
        # 取每小时边界后30秒内的首个采样，缺失小时不跨越计算。
        points = {}
        for row in history:
            t = row["time"]
            if (now-86400 <= t <= now and t % 3600 <= 30 and key in row.get("accounts", {})
                    and row["accounts"][key].get("valuation_valid", True)):
                points.setdefault(int(t//3600), (row["accounts"][key]["equity"], row.get("capital", 1000)))
        returns = [points[t][0]/points[t-1][0]-1 for t in sorted(points)
                   if t-1 in points and points[t-1][0]>0 and points[t][1] == points[t-1][1]]
        m["sharpe_samples"] = len(returns)
        sd = statistics.stdev(returns) if len(returns)>=6 else 0
        m["sharpe"] = statistics.mean(returns)/sd*math.sqrt(365*24) if sd>1e-12 else None
    return result


class Dashboard:
    def __init__(self, root: Path):
        self.root = root
        self.metric_cache = {}
        self.metric_at = 0
        self.mutex = threading.Lock()
        self.history = deque(read_json(root / "dashboard_history.json", []), maxlen=8640)

    def sample(self):
        while True:
            status = read_json(self.root / "status.json", {})
            if (status.get("accounts") and time.time() - status.get("time", 0) < 30
                    and all(not a.get("stale_marks") for a in status["accounts"].values())):
                with self.mutex:
                    if not self.history or self.history[-1]["time"] != status["time"]:
                        self.history.append({"time": status["time"], "capital": status["settings"]["capital"], "accounts": {
                            key: {k: a.get(k, True) if k == "valuation_valid" else a[k] for k in ("equity", "realized", "unrealized", "inventory_cost", "fees", "roundtrips", "valuation_valid")}
                            for key, a in status["accounts"].items()}, "symbols": {
                            sym: {"net_pnl": s["realized"] + s["unrealized"], "realized": s["realized"],
                                  "unrealized": s["unrealized"], "inventory_cost": s["inventory_cost"]}
                            for sym, s in status["symbols"].items()}})
                        write_json(self.root / "dashboard_history.json", list(self.history))
            time.sleep(10)

    def overview(self):
        status = read_json(self.root / "status.json", {})
        state = read_json(self.root / "state.json", {})
        now = time.time()
        with self.mutex:
            if now - self.metric_at >= 10:
                self.metric_cache = performance(self.root, list(self.history), status, now)
                self.metric_at = now
            metrics = self.metric_cache
        return {"performance": metrics, "status": status, "orders": list(state.get("orders", {}).values()),
                "lots": list(state.get("lots", {}).values()), "events": tail_events(self.root),
                "iterations": read_json(self.root / "iterations.json", []), "server_time": time.time(),
                "next_report": state.get("report_at", 0) + 3600}


def handler_for(dashboard: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            code, content_type = 200, "application/json; charset=utf-8"
            if path == "/":
                body = PAGE.read_bytes()
                content_type = "text/html; charset=utf-8"
            elif path == "/api/overview":
                if parse_qs(urlsplit(self.path).query).get("view") == ["orders"]:
                    status = read_json(dashboard.root / "status.json", {})
                    state = read_json(dashboard.root / "state.json", {})
                    result = {"time": status.get("time", 0), "symbols": status.get("symbols", {}),
                              "orders": list(state.get("orders", {}).values())}
                else:
                    result = dashboard.overview()
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            elif path == "/api/history":
                with dashboard.mutex:
                    since = float(parse_qs(urlsplit(self.path).query).get("since", ["0"])[0])
                    body = json.dumps([r for r in dashboard.history if r["time"] > since], allow_nan=False).encode()
            elif path == "/api/report":
                try:
                    body = (dashboard.root / "latest_report.md").read_bytes()
                except FileNotFoundError:
                    body = "第一期小时报告尚未生成，页面正在显示实时账户状态。".encode()
                content_type = "text/plain; charset=utf-8"
            elif path == "/favicon.ico":
                code, body = 204, b""
            else:
                code, body = 404, b'{"error":"not_found"}'
            compressed = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024
            if compressed:
                body = gzip.compress(body, compresslevel=1)
            self.send_response(code)
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    serve(args.data_dir, args.port)


def serve(data_dir: Path, port: int = 8766):
    dashboard = Dashboard(data_dir)
    threading.Thread(target=dashboard.sample, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(dashboard))
    server.serve_forever()


if __name__ == "__main__":
    main()
