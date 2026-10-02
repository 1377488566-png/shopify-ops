"""SQLite 存储层。

这一段在干嘛：
  - 定义 5 张表：orders / line_items / refunds / emails / sync_state；
  - `init_db()` 建表（幂等，跑多少次都行）；
  - `upsert_order()` 把一条 Shopify 订单写进库：**同一个订单重复同步只会覆盖，不会重复插入**（幂等）；
  - `get_last_sync()` / `set_last_sync()` 存同步水位（下次增量拉取的起点）；
  - 一些查询：列出订单、统计、取还没生成邮件的订单。

设计要点：
  - 主键直接用 Shopify 的数字 ID，天然幂等；
  - line_items / refunds 每次先按 order_id 删掉再插入，保证订单更新后子表不残留旧数据；
  - 时间统一存 UTC ISO8601 字符串（SQLite 没有原生时间类型，字符串可比较、可排序）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import setup_logging

log = setup_logging("db")

#: 建表语句：全部 IF NOT EXISTS，可以反复执行
SCHEMA: str = """
CREATE TABLE IF NOT EXISTS orders (
    id                 INTEGER PRIMARY KEY,   -- Shopify 订单数字 ID
    order_number       TEXT    NOT NULL,      -- 展示用订单号，如 #1001
    created_at         TEXT,                  -- 下单时间
    updated_at         TEXT,                  -- 店铺侧最后更新时间（增量同步靠它）
    processed_at       TEXT,                  -- 订单处理完成时间
    financial_status   TEXT,                  -- paid / refunded / pending ...
    fulfillment_status TEXT,                  -- fulfilled / null ...
    total_price        REAL,                  -- 订单总额
    currency           TEXT,                  -- 币种，如 USD
    customer_email     TEXT,
    customer_name      TEXT,
    synced_at          TEXT,                  -- 我们本地写入时间
    raw_json           TEXT                   -- 原始 JSON，便于日后扩展字段
);

CREATE TABLE IF NOT EXISTS line_items (
    id       INTEGER PRIMARY KEY,             -- Shopify line item ID
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    title    TEXT,
    sku      TEXT,
    quantity INTEGER,
    price    REAL
);

CREATE TABLE IF NOT EXISTS refunds (
    id         INTEGER PRIMARY KEY,           -- Shopify refund ID
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    created_at TEXT,
    amount     REAL,                          -- 退款金额
    currency   TEXT,
    reason     TEXT,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    scenario     TEXT NOT NULL,               -- welcome / shipping_notice / refund_apology / review_recovery
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    provider     TEXT NOT NULL,               -- deepseek / template
    status       TEXT NOT NULL DEFAULT 'draft',  -- draft / sent / failed
    generated_at TEXT,
    sent_at      TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_updated   ON orders(updated_at);
CREATE INDEX IF NOT EXISTS idx_emails_order     ON emails(order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order    ON refunds(order_id);
"""


def _utc_now() -> str:
    """当前 UTC 时间的 ISO8601 字符串。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开数据库连接（外键约束默认是关的，这里手动打开）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 让查询结果能按列名取值：row["order_number"]
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
    conn.commit()
    log.debug("数据库表已就绪")


# --------------------------------------------------------------------------
# 写入：把 Shopify 订单 JSON 落库
# --------------------------------------------------------------------------

def _customer_info(order: dict[str, Any]) -> tuple[str, str]:
    """从订单 JSON 里安全地取出 (邮箱, 客户名)。字段可能缺失，所以一路用 or {}。"""
    customer = order.get("customer") or {}
    email = order.get("email") or customer.get("email") or ""
    first = customer.get("first_name") or ""
    last = customer.get("last_name") or ""
    name = " ".join(part for part in (first, last) if part).strip()
    if not name:
        # 兜底：用邮箱前缀当名字，保证邮件里不会出现空称呼
        name = email.split("@")[0] if email else "there"
    return email, name


def _refund_amount(refund: dict[str, Any]) -> float:
    """把一条 refund 里所有成功的退款交易金额加起来。"""
    total = 0.0
    for txn in refund.get("transactions") or []:
        if str(txn.get("kind", "")).lower() == "refund" and str(txn.get("status", "")).lower() == "success":
            try:
                total += float(txn.get("amount") or 0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def upsert_order(conn: sqlite3.Connection, order: dict[str, Any]) -> None:
    """写入/更新一条订单，连同它的 line_items 和 refunds。

    幂等策略：
      - orders 用 `ON CONFLICT(id) DO UPDATE`，重复同步只覆盖；
      - 子表先 `DELETE FROM ... WHERE order_id=?` 再插，避免订单修改后残留旧行。
    整段放在一个事务里，要么全成功要么全回滚。
    """
    order_id = int(order["id"])
    email, name = _customer_info(order)

    conn.execute(
        """
        INSERT INTO orders (id, order_number, created_at, updated_at, processed_at,
                            financial_status, fulfillment_status, total_price, currency,
                            customer_email, customer_name, synced_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            order_number       = excluded.order_number,
            created_at         = excluded.created_at,
            updated_at         = excluded.updated_at,
            processed_at       = excluded.processed_at,
            financial_status   = excluded.financial_status,
            fulfillment_status = excluded.fulfillment_status,
            total_price        = excluded.total_price,
            currency           = excluded.currency,
            customer_email     = excluded.customer_email,
            customer_name      = excluded.customer_name,
            synced_at          = excluded.synced_at,
            raw_json           = excluded.raw_json
        """,
        (
            order_id,
            order.get("name") or f"#{order_id}",
            order.get("created_at"),
            order.get("updated_at"),
            order.get("processed_at"),
            order.get("financial_status"),
            order.get("fulfillment_status"),
            float(order.get("total_price") or 0),
            order.get("currency") or "USD",
            email,
            name,
            _utc_now(),
            json.dumps(order, ensure_ascii=False),
        ),
    )

    # ---- 子表：先删后插 ----
    conn.execute("DELETE FROM line_items WHERE order_id = ?", (order_id,))
    for item in order.get("line_items") or []:
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue  # 没有 id 的脏数据直接跳过
        try:
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        conn.execute(
            "INSERT OR REPLACE INTO line_items (id, order_id, title, sku, quantity, price) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, order_id, item.get("title"), item.get("sku"), int(item.get("quantity") or 0), price),
        )

    conn.execute("DELETE FROM refunds WHERE order_id = ?", (order_id,))
    for refund in order.get("refunds") or []:
        try:
            refund_id = int(refund.get("id"))
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO refunds (id, order_id, created_at, amount, currency, reason, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                refund_id,
                order_id,
                refund.get("created_at"),
                _refund_amount(refund),
                order.get("currency") or "USD",
                refund.get("reason"),
                refund.get("note"),
            ),
        )

    conn.commit()


def upsert_orders(conn: sqlite3.Connection, orders: Iterable[dict[str, Any]]) -> int:
    """批量写入，返回写成功的条数。单条失败只记日志，不打断整批同步。"""
    count = 0
    for order in orders:
        try:
            upsert_order(conn, order)
            count += 1
        except Exception as exc:  # noqa: BLE001 - 单条脏数据不该拖垮整批
            log.error("订单 %s 写入失败：%s", order.get("id"), exc)
    return count


# --------------------------------------------------------------------------
# 同步水位
# --------------------------------------------------------------------------

def get_last_sync(conn: sqlite3.Connection) -> str | None:
    """读出上次同步到的时间点（没有就返回 None，表示第一次全量拉）。"""
    row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_sync'").fetchone()
    return row["value"] if row else None


def set_last_sync(conn: sqlite3.Connection, value: str) -> None:
    """更新同步水位。"""
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES ('last_sync', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (value, _utc_now()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderRow:
    """给邮件生成 / 展示用的一行订单（含退款信息）。"""

    id: int
    order_number: str
    customer_name: str
    customer_email: str
    total_price: float
    currency: str
    financial_status: str
    fulfillment_status: str
    created_at: str
    refund_amount: float
    refund_count: int
    items: tuple[str, ...]


_SELECT_ORDER = """
SELECT o.id, o.order_number, o.customer_name, o.customer_email, o.total_price, o.currency,
       o.financial_status, o.fulfillment_status, o.created_at,
       COALESCE((SELECT SUM(amount) FROM refunds r WHERE r.order_id = o.id), 0) AS refund_amount,
       COALESCE((SELECT COUNT(*)     FROM refunds r WHERE r.order_id = o.id), 0) AS refund_count
FROM orders o
"""


def _to_order_row(row: sqlite3.Row, items: Sequence[str]) -> OrderRow:
    return OrderRow(
        id=row["id"],
        order_number=row["order_number"],
        customer_name=row["customer_name"] or "there",
        customer_email=row["customer_email"] or "",
        total_price=float(row["total_price"] or 0),
        currency=row["currency"] or "USD",
        financial_status=row["financial_status"] or "",
        fulfillment_status=row["fulfillment_status"] or "",
        created_at=row["created_at"] or "",
        refund_amount=float(row["refund_amount"] or 0),
        refund_count=int(row["refund_count"] or 0),
        items=tuple(items),
    )


def _items_for(conn: sqlite3.Connection, order_id: int) -> list[str]:
    rows = conn.execute("SELECT title, quantity FROM line_items WHERE order_id = ?", (order_id,)).fetchall()
    return [f"{r['title']} x{r['quantity']}" for r in rows]


def fetch_orders(conn: sqlite3.Connection, limit: int = 10) -> list[OrderRow]:
    """按创建时间倒序取订单。"""
    rows = conn.execute(_SELECT_ORDER + " ORDER BY o.created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_to_order_row(r, _items_for(conn, r["id"])) for r in rows]


def fetch_orders_without_email(conn: sqlite3.Connection, limit: int = 5) -> list[OrderRow]:
    """取还没生成过邮件的订单（邮件生成默认只处理这些，天然幂等）。"""
    rows = conn.execute(
        _SELECT_ORDER
        + " WHERE NOT EXISTS (SELECT 1 FROM emails e WHERE e.order_id = o.id)"
        + " ORDER BY o.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_to_order_row(r, _items_for(conn, r["id"])) for r in rows]


def fetch_order(conn: sqlite3.Connection, order_id: int) -> OrderRow | None:
    """按 ID 取单条订单。"""
    row = conn.execute(_SELECT_ORDER + " WHERE o.id = ?", (order_id,)).fetchone()
    return _to_order_row(row, _items_for(conn, order_id)) if row else None


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------

def insert_email(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    scenario: str,
    subject: str,
    body: str,
    provider: str,
    status: str = "draft",
) -> int:
    """存一封生成的邮件，返回自增 ID。"""
    cur = conn.execute(
        "INSERT INTO emails (order_id, scenario, subject, body, provider, status, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, scenario, subject, body, provider, status, _utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_emails(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """取最近生成的邮件（带订单号/收件人，方便直接打印）。"""
    return conn.execute(
        """
        SELECT e.id, e.order_id, e.scenario, e.subject, e.body, e.provider, e.status, e.generated_at,
               o.order_number, o.customer_email, o.customer_name
        FROM emails e JOIN orders o ON o.id = e.order_id
        ORDER BY e.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_email_sent(conn: sqlite3.Connection, email_id: int) -> None:
    """把邮件标记成已发送（接 SMTP 之后会用上）。"""
    conn.execute("UPDATE emails SET status = 'sent', sent_at = ? WHERE id = ?", (_utc_now(), email_id))
    conn.commit()


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """汇总一张“体检报告”。"""
    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(conn.execute(sql, params).fetchone()[0] or 0)

    by_scenario = {
        row["scenario"]: row["n"]
        for row in conn.execute("SELECT scenario, COUNT(*) AS n FROM emails GROUP BY scenario ORDER BY n DESC")
    }
    return {
        "orders": scalar("SELECT COUNT(*) FROM orders"),
        "line_items": scalar("SELECT COUNT(*) FROM line_items"),
        "orders_with_refund": scalar("SELECT COUNT(DISTINCT order_id) FROM refunds"),
        "refund_total_amount": round(
            float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM refunds").fetchone()[0] or 0), 2
        ),
        "orders_with_email": scalar("SELECT COUNT(DISTINCT order_id) FROM emails"),
        "emails": scalar("SELECT COUNT(*) FROM emails"),
        "emails_by_scenario": by_scenario,
        "emails_by_provider": {
            row["provider"]: row["n"]
            for row in conn.execute("SELECT provider, COUNT(*) AS n FROM emails GROUP BY provider")
        },
        "last_sync": get_last_sync(conn),
    }
