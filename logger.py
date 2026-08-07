"""
日志系统 — INFO/WARN/ERROR 三级，自动轮转
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from config import LOG_FILE, LOG_MAX_DAYS

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("dynamic_grid")
    _logger.setLevel(logging.DEBUG)

    # 控制台 (默认 INFO, 仪表盘模式调为 WARNING)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%m-%d %H:%M:%S",
    ))
    _logger.addHandler(ch)

    # 文件 (每日轮转, 保留7天)
    fh = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1,
        backupCount=LOG_MAX_DAYS, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(fh)

    return _logger


log = get_logger()


def set_console_level(level: int):
    """调整控制台日志级别 (仪表盘模式下设为 WARNING 避免干扰显示)"""
    for h in log.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(level)
