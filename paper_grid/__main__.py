"""统一入口：python -m paper_grid {paper,dashboard,backtest}。"""
import argparse
import json
from pathlib import Path

from .config import DEFAULT_DATA, Settings


def main():
    parser = argparse.ArgumentParser(description='DynaGrid · 纸面运行与同引擎回测')
    commands = parser.add_subparsers(dest='command', required=True)
    paper = commands.add_parser('paper', help='订阅公开行情，无需API密钥')
    paper.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    paper.add_argument('--seconds', type=float, default=0)
    dashboard = commands.add_parser('dashboard', help='打开本地监控服务')
    dashboard.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    dashboard.add_argument('--port', type=int, default=8766)
    backtest = commands.add_parser('backtest', help='回放已录制行情')
    source = backtest.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='离线合成行情演示')
    source.add_argument('--input', type=Path, help='jsonl/jsonl.gz文件或raw目录')
    backtest.add_argument('--filters', type=Path, help='录制时的market_filters.json')
    backtest.add_argument('--config', type=Path, help='Settings字段JSON或experiment.json')
    backtest.add_argument('--output', type=Path, default=Path('data/backtest.json'))
    args = parser.parse_args()
    if args.command == 'paper':
        from .run import run_paper
        run_paper(args.data_dir, args.seconds)
    elif args.command == 'dashboard':
        from .dashboard import serve
        serve(args.data_dir, args.port)
    else:
        from .replay import demo_records, records, replay
        from .storage import write_json
        config = json.loads(args.config.read_text()) if args.config else {}
        settings = Settings(**config.get('settings', config))
        if args.demo:
            from .engine import Engine
            filters = {s: (.01, .001, 5) for s in Engine().signals}
            rows = demo_records()
        else:
            if not args.filters:
                parser.error('真实行情回放需要 --filters market_filters.json')
            filters = json.loads(args.filters.read_text())
            paths = sorted(args.input.glob('*.jsonl*')) if args.input.is_dir() else [args.input]
            rows = records(paths)
        result = replay(rows, filters, settings)
        result['synthetic'] = args.demo
        write_json(args.output, result)
        print(f'回测结果：{args.output}（{result["events"]}条事件）')
        for name, account in result['status']['accounts'].items():
            print(f'{name}: 净损益 {account["realized"]+account["unrealized"]:.4f}，库存成本 {account["inventory_cost"]:.2f}')


if __name__ == '__main__':
    main()
