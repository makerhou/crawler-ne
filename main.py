"""Reuters 爬虫入口。

用法：
    python main.py          # 常驻运行（每 5 分钟一轮）
    python main.py --once   # 只跑一轮（调试/验证用）
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from src.config import Config, load_env_file
from src.logging_setup import setup_logger
from src.repository import ArticleRepository
from src.scheduler import create_browser, run_forever, run_once

 # sudo apt update && sudo apt install -y xvfb
def main() -> int:
    parser = argparse.ArgumentParser(description="Reuters 爬虫（Google News RSS）")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    parser.add_argument("--env", default=".env", help=".env 配置文件路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只抓取不入库，打印挖掘到的文章内容（诊断用，不需要数据库）",
    )
    args = parser.parse_args()

    load_env_file(args.env)
    config = Config()
    try:
        config.validate(require_db=not args.dry_run)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    logger = setup_logger("reuters-crawler", config.log_dir, config.log_level)
    repo = ArticleRepository(config)

    stop_event = threading.Event()

    def _handle_stop(signum, _frame) -> None:
        logger.info("收到信号 %s，准备退出", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    try:
        if args.once:
            # 单轮模式同样需要浏览器（auto 模式可能回退到浏览器抓取）
            browser = create_browser(config)
            try:
                stats = run_once(
                    config,
                    None if args.dry_run else repo,
                    browser,
                    dry_run=args.dry_run,
                    stop_event=stop_event,
                )
            except Exception as exc:
                # 单轮失败：清晰提示 + 非 0 退出码（便于脚本判断），不打 traceback
                logger.error("单轮执行失败: %s: %s", type(exc).__name__, exc)
                return 1
            finally:
                if browser is not None:
                    browser.close()
            logger.info("单轮模式结束: %s", stats)
            return 0
        run_forever(config, repo, stop_event)
        return 0
    except KeyboardInterrupt:
        logger.info("被用户中断")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    sys.exit(main())
