"""日志配置：文件轮转 + 控制台输出。

systemd 场景下控制台输出进 journal，文件输出便于离线排查。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 10 * 1024 * 1024  # 单个日志文件 10MB
BACKUP_COUNT = 5


def setup_logger(
    name: str = "reuters-crawler",
    log_dir: str = "./logs",
    level: str = "INFO",
) -> logging.Logger:
    """创建并返回配置好的 logger（重复调用不会重复添加 handler）。"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 文件（按大小轮转）
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "crawler.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("日志文件初始化失败，仅输出控制台: %s", exc)

    return logger
