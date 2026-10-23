"""Render / 任意云平台启动探针：先播种 mock 数据，再起服务。

为什么要这一步：Render 的磁盘是临时的，容器重启后 SQLite 是空的，
首页会变成一张空表。这里在启动前先跑一次 mock 同步，保证任何时候打开都有数据可看。

本地开发不需要它，直接 uvicorn 就行。
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import Config, setup_logging  # noqa: E402
from db import connect, fetch_orders, init_db  # noqa: E402
from sync import run_sync  # noqa: E402

log = setup_logging("bootstrap")


def main() -> int:
    config = Config.from_env()

    conn = connect(config.db_path)
    try:
        init_db(conn)
        existing = len(fetch_orders(conn, limit=1))
    finally:
        conn.close()

    if existing:
        log.info("数据库已有数据，跳过播种")
        return 0

    if not config.is_mock:
        log.warning("非 mock 模式且库为空，不自动拉取真实订单（避免意外请求）")
        return 0

    log.info("数据库为空，执行一次 mock 同步播种")
    result = run_sync(config)
    log.info("播种完成：拉取 %d 条，写入 %d 条", result.pulled, result.written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
