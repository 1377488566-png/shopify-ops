"""统一命令行入口。

常用命令（Windows PowerShell，项目根目录下）：
    .\\venv\\Scripts\\python.exe cli.py sync --mock      # 同步（mock 假数据）
    .\\venv\\Scripts\\python.exe cli.py emails --limit 5 # 生成 5 封英文售后邮件
    .\\venv\\Scripts\\python.exe cli.py stats            # 查看统计
    .\\venv\\Scripts\\python.exe cli.py orders --limit 5 # 看订单
    .\\venv\\Scripts\\python.exe cli.py show-emails --limit 5  # 打印已生成的邮件全文
    .\\venv\\Scripts\\python.exe cli.py export --out samples   # 把样本导出到 samples/
    .\\venv\\Scripts\\python.exe cli.py webhook --mock   # 起 webhook 接收端
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import Config, setup_logging
from db import (
    connect,
    fetch_emails,
    fetch_orders,
    fetch_orders_without_email,
    init_db,
    insert_email,
    stats as db_stats,
)
from email_gen import SCENARIO_LABELS, generate_email
from sync import run_sync

log = setup_logging("cli")

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace, config: Config) -> int:
    """同步订单。"""
    result = run_sync(config, full=args.full)
    print("\n=== 同步结果 ===")
    print(f"模式        : {result.mode}")
    print(f"拉取 / 写入 : {result.pulled} / {result.written} 条（{result.pages} 页）")
    print(f"水位        : {result.watermark_from or '(无)'}  ->  {result.watermark_to}")
    if result.mode == "mock":
        print("提示        : 本次全部是内置假数据，没有向任何真实店铺发请求。")
    return 0


def cmd_emails(args: argparse.Namespace, config: Config) -> int:
    """为还没生成过邮件的订单生成英文售后邮件（默认幂等，不会重复生成）。"""
    conn = connect(config.db_path)
    try:
        init_db(conn)
        targets = (
            fetch_orders(conn, args.limit) if args.force else fetch_orders_without_email(conn, args.limit)
        )
        if not targets:
            print("没有需要生成邮件的订单（可能都已生成，试试 --force 强制重生成）。")
            return 0

        engine = f"DeepSeek:{config.deepseek_model}" if config.has_llm else "内置英文模板"
        print(f"邮件引擎：{engine}｜待生成 {len(targets)} 封\n")

        for order in targets:
            draft = generate_email(order, config)
            email_id = insert_email(
                conn,
                order_id=order.id,
                scenario=draft.scenario,
                subject=draft.subject,
                body=draft.body,
                provider=draft.provider,
            )
            print(f"[{email_id}] {order.order_number} | {draft.scenario_label} | provider={draft.provider}")
            print(f"     Subject: {draft.subject}")
            print(f"     Preview: {draft.body.splitlines()[0] if draft.body else ''}")
            print()
        return 0
    finally:
        conn.close()


def cmd_show_emails(args: argparse.Namespace, config: Config) -> int:
    """打印已生成邮件的全文。"""
    conn = connect(config.db_path)
    try:
        rows = fetch_emails(conn, args.limit)
        if not rows:
            print("库里还没有邮件，先跑：cli.py emails --limit 5")
            return 0
        for row in rows:
            print("=" * 78)
            print(f"邮件 #{row['id']} | 订单 {row['order_number']} | 场景 {SCENARIO_LABELS.get(row['scenario'], row['scenario'])}")
            print(f"收件人 : {row['customer_name']} <{row['customer_email']}>")
            print(f"生成器 : {row['provider']} | 状态 {row['status']} | {row['generated_at']}")
            print(f"Subject: {row['subject']}")
            print("-" * 78)
            print(row["body"])
        print("=" * 78)
        return 0
    finally:
        conn.close()


def cmd_orders(args: argparse.Namespace, config: Config) -> int:
    """列表方式看订单。"""
    conn = connect(config.db_path)
    try:
        rows = fetch_orders(conn, args.limit)
        if not rows:
            print("库里没有订单，先跑：cli.py sync --mock")
            return 0
        print(f"{'订单号':<8} {'日期':<12} {'客户':<18} {'金额':<12} {'支付':<10} {'发货':<10} {'退款'}")
        for row in rows:
            refund = f"{row.currency} {row.refund_amount:.2f}" if row.refund_count else "-"
            print(
                f"{row.order_number:<8} {row.created_at[:10]:<12} {row.customer_name[:17]:<18} "
                f"{row.currency + ' ' + format(row.total_price, '.2f'):<12} "
                f"{(row.financial_status or '-'):<10} {(row.fulfillment_status or '-'):<10} {refund}"
            )
        return 0
    finally:
        conn.close()


def cmd_stats(args: argparse.Namespace, config: Config) -> int:
    """统计概览。"""
    conn = connect(config.db_path)
    try:
        report = db_stats(conn)
    finally:
        conn.close()

    print("\n=== 数据概览 ===")
    print(f"操作模式      : {'MOCK（内置假数据）' if config.is_mock else 'LIVE（真实店铺 ' + config.shop + '）'}")
    print(f"邮件引擎      : {'DeepSeek ' + config.deepseek_model if config.has_llm else '内置英文模板（未配置 API key）'}")
    print(f"数据库        : {config.db_path}")
    print(f"订单总数      : {report['orders']}")
    print(f"订单明细行数  : {report['line_items']}")
    print(f"含退款订单    : {report['orders_with_refund']}（退款总额 {report['refund_total_amount']}）")
    print(f"已生成邮件    : {report['emails']} 封，覆盖 {report['orders_with_email']} 个订单")
    print(f"邮件按场景    : {report['emails_by_scenario'] or '{}'}")
    print(f"邮件按生成器  : {report['emails_by_provider'] or '{}'}")
    print(f"同步水位      : {report['last_sync'] or '(未同步过)'}")
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """把运行结果导出到 samples/，作为“跑通了”的证据。"""
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(config.db_path)
    try:
        orders = fetch_orders(conn, args.limit)
        emails = fetch_emails(conn, args.limit)
        report = db_stats(conn)
    finally:
        conn.close()

    # 1) 5 条订单（JSON，方便机器读 + 人工核对）
    orders_payload = [
        {
            "order_number": o.order_number,
            "created_at": o.created_at,
            "customer": {"name": o.customer_name, "email": o.customer_email},
            "total": f"{o.currency} {o.total_price:.2f}",
            "financial_status": o.financial_status,
            "fulfillment_status": o.fulfillment_status,
            "refund_amount": o.refund_amount,
            "refund_count": o.refund_count,
            "items": list(o.items),
        }
        for o in orders
    ]
    (out_dir / "orders_sample.json").write_text(
        json.dumps(orders_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 2) 5 封邮件（纯文本，直接能读）
    parts: list[str] = []
    for row in emails:
        parts.append(
            "=" * 78
            + f"\n邮件 #{row['id']} | 订单 {row['order_number']} | 场景 {SCENARIO_LABELS.get(row['scenario'], row['scenario'])}"
            + f"\n收件人 : {row['customer_name']} <{row['customer_email']}>"
            + f"\n生成器 : {row['provider']} | 状态 {row['status']} | {row['generated_at']}"
            + f"\nSubject: {row['subject']}\n"
            + "-" * 78
            + f"\n{row['body']}\n"
        )
    (out_dir / "emails_sample.txt").write_text("\n".join(parts), encoding="utf-8")

    # 3) 统计快照
    (out_dir / "stats.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已导出到 {out_dir}")
    print(f"  - orders_sample.json（{len(orders_payload)} 条订单）")
    print(f"  - emails_sample.txt（{len(emails)} 封邮件）")
    print("  - stats.json")
    return 0


def cmd_webhook(args: argparse.Namespace, config: Config) -> int:
    """起 webhook 服务。"""
    from webhook import serve

    serve(config, host=args.host, port=args.port)
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Shopify 订单同步 + AI 英文售后邮件（默认 mock 模式，不会碰真实店铺）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mock", action="store_true", help="全局强制 mock 模式（不联网）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="增量同步订单")
    p_sync.add_argument("--mock", action="store_true", help="强制 mock")
    p_sync.add_argument("--full", action="store_true", help="忽略水位，全量重拉")
    p_sync.set_defaults(func=cmd_sync)

    p_emails = sub.add_parser("emails", help="生成英文售后邮件")
    p_emails.add_argument("--limit", type=int, default=5, help="最多生成几封（默认 5）")
    p_emails.add_argument("--force", action="store_true", help="已生成过的订单也重新生成")
    p_emails.add_argument("--mock", action="store_true", help="强制 mock")
    p_emails.set_defaults(func=cmd_emails)

    p_show = sub.add_parser("show-emails", help="打印已生成的邮件全文")
    p_show.add_argument("--limit", type=int, default=5)
    p_show.set_defaults(func=cmd_show_emails)

    p_orders = sub.add_parser("orders", help="查看订单列表")
    p_orders.add_argument("--limit", type=int, default=10)
    p_orders.set_defaults(func=cmd_orders)

    p_stats = sub.add_parser("stats", help="查看统计")
    p_stats.set_defaults(func=cmd_stats)

    p_export = sub.add_parser("export", help="导出样本到 samples/")
    p_export.add_argument("--out", default="samples", help="输出目录（默认 samples/）")
    p_export.add_argument("--limit", type=int, default=5)
    p_export.set_defaults(func=cmd_export)

    p_webhook = sub.add_parser("webhook", help="启动 webhook 接收端（可选）")
    p_webhook.add_argument("--host", default="127.0.0.1")
    p_webhook.add_argument("--port", type=int, default=8000)
    p_webhook.add_argument("--mock", action="store_true")
    p_webhook.set_defaults(func=cmd_webhook)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 子命令里的 --mock 或全局 --mock 任意一个生效都行
    force_mock = bool(getattr(args, "mock", False))
    config = Config.from_env(force_mock=force_mock)
    log.debug("配置：%s", config.describe())

    return int(args.func(args, config))


if __name__ == "__main__":
    sys.exit(main())
