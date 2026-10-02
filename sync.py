"""轮询同步：把 Shopify 订单增量拉到本地 SQLite。

这一段在干嘛：
  1. 从 `sync_state` 读出上次同步水位 `last_sync`；
  2. 用客户端按 `updated_at_min=水位` 增量拉取（加 `--full` 就全量重拉）；
  3. 逐条 UPSERT 进库（幂等，重复跑不会产生重复数据）；
  4. 成功后把水位推进到“本次请求开始的时间”（用开始时间而不是结束时间，防止同步过程中更新的订单被漏掉）。

命令行里也能单独跑：`python sync.py --mock`
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Config, setup_logging
from db import connect, init_db, set_last_sync, stats, upsert_orders
from shopify_client import ShopifyClient

log = setup_logging("sync")

#: 水位回退量：每次同步往前多拉 5 分钟，避免时区/时钟误差导致漏单
WATERMARK_SAFETY_MARGIN = timedelta(minutes=5)


@dataclass(slots=True)
class SyncResult:
    """一次同步的结果摘要。"""

    mode: str
    pulled: int = 0
    written: int = 0
    watermark_from: str | None = None
    watermark_to: str = ""
    pages: int = 0
    finished_at: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_iso(value: str) -> datetime | None:
    """宽松解析 ISO 时间；失败返回 None。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def run_sync(config: Config, *, full: bool = False) -> SyncResult:
    """执行一次同步。返回 SyncResult，供 CLI 打印。"""
    started = _utc_now()
    mode = "mock" if config.is_mock else "live"
    log.info("开始同步｜mode=%s｜%s", mode, config.describe())

    conn = connect(config.db_path)
    try:
        init_db(conn)

        # 1) 读水位
        previous = None if full else _watermark(conn)
        log.info("上次同步水位：%s", previous or "（无，全量拉取）")

        result = SyncResult(mode=mode, watermark_from=previous)

        # 2) 拉数据
        with ShopifyClient(config) as client:
            for page_index, batch in enumerate(
                client.iter_orders(updated_at_min=previous, status="any"), start=1
            ):
                result.pages = page_index
                result.pulled += len(batch)
                # 3) 落库
                result.written += upsert_orders(conn, batch)
                log.info("已写入 %d / %d 条", result.written, result.pulled)

        # 4) 推进水位（用“请求开始时间”，并用安全余量回退）
        new_watermark = (started - WATERMARK_SAFETY_MARGIN).isoformat()
        set_last_sync(conn, new_watermark)
        result.watermark_to = new_watermark
        result.finished_at = _utc_now().isoformat()

        log.info(
            "同步完成｜拉取 %d 条｜写入 %d 条｜页数 %d｜新水位 %s",
            result.pulled,
            result.written,
            result.pages,
            new_watermark,
        )
        return result
    finally:
        conn.close()


def _watermark(conn: Any) -> str | None:
    """读水位并做安全回退；没有水位返回 None。"""
    from db import get_last_sync  # 局部导入，避免循环引用

    raw = get_last_sync(conn)
    if not raw:
        return None
    parsed = _parse_iso(raw)
    if parsed is None:
        log.warning("水位 %r 解析失败，本次按全量处理", raw)
        return None
    return (parsed - WATERMARK_SAFETY_MARGIN).isoformat()


def main(argv: list[str] | None = None) -> int:
    """直接 `python sync.py --mock` 时的入口。"""
    parser = argparse.ArgumentParser(description="Shopify 订单增量同步")
    parser.add_argument("--mock", action="store_true", help="强制 mock：只用内置假数据，不联网")
    parser.add_argument("--full", action="store_true", help="忽略水位，全量重拉")
    args = parser.parse_args(argv)

    config = Config.from_env(force_mock=args.mock)
    result = run_sync(config, full=args.full)

    conn = connect(config.db_path)
    try:
        report = stats(conn)
    finally:
        conn.close()

    print(f"\n同步模式：{result.mode}")
    print(f"拉取订单：{result.pulled} 条（{result.pages} 页）")
    print(f"写入订单：{result.written} 条")
    print(f"同步水位：{result.watermark_from or '(无)'}  ->  {result.watermark_to}")
    print(f"库内订单：{report['orders']} 条｜含退款的订单：{report['orders_with_refund']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
