"""纸面账户原子快照及独立进程锁，不触碰实盘状态。"""
import json
import os
from pathlib import Path


def _atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, data: dict):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
