"""
shopify-ops 的 Web 层（FastAPI）。

设计原则：
  1. **不改动任何原有模块**——db / email_gen / sync / webhook 原样复用，本文件只是把
     CLI 里那几段"取数 → 生成 → 落库"的流程搬成 HTTP 路由；
  2. 每个请求独立开一个 SQLite 连接（sqlite3 连接不能跨线程共享），用完即关；
  3. 默认走 mock（没有 Shopify 凭据时 Config.is_mock 自动为真），保证零凭据也能跑通；
  4. 页面全部服务端渲染（Jinja2 模板 + 内联 CSS），不依赖前端构建，任何机器打开即用。

启动：
    python -m uvicorn webapp.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# 允许从项目根目录 import 原有模块（server.py 在 webapp/ 里，上一级才是根）
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import Config, setup_logging  # noqa: E402
from db import (  # noqa: E402
    OrderRow,
    connect,
    fetch_emails,
    fetch_order,
    fetch_orders,
    init_db,
    insert_email,
    stats as db_stats,
)
from email_gen import SCENARIO_LABELS, detect_scenario, generate_email  # noqa: E402
from sync import run_sync  # noqa: E402

log = setup_logging("webapp")

app = FastAPI(
    title="Shopify Ops Console",
    description="订单同步 · 英文售后邮件生成 · 运营指标看板",
    version="1.0.0",
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def get_config() -> Config:
    """每次调用都重新读环境变量，方便改 .env 后不重启也能生效。"""
    return Config.from_env()


def open_db() -> Any:
    """开连接 + 建表（init_db 是幂等的）。调用方负责 close。"""
    conn = connect(get_config().db_path)
    init_db(conn)
    return conn


def order_to_dict(o: OrderRow) -> dict[str, Any]:
    """把 OrderRow 转成可 JSON 序列化的 dict，模板和 API 共用。"""
    return {
        "id": o.id,
        "order_number": o.order_number,
        "customer_name": o.customer_name,
        "customer_email": o.customer_email,
        "total_price": o.total_price,
        "currency": o.currency,
        "financial_status": o.financial_status,
        "fulfillment_status": o.fulfillment_status,
        "created_at": o.created_at,
        "refund_amount": o.refund_amount,
        "refund_count": o.refund_count,
        "items": list(o.items),
        "scenario": detect_scenario(o),
    }


# --------------------------------------------------------------------------
# JSON API（面试时打开 /docs 就能看到这份自动文档）
# --------------------------------------------------------------------------

@app.get("/api/health")
def api_health() -> dict[str, Any]:
    cfg = get_config()
    return {
        "status": "ok",
        "mode": "mock" if cfg.is_mock else "live",
        "email_engine": f"deepseek:{cfg.deepseek_model}" if cfg.has_llm else "template",
        "db": str(cfg.db_path),
    }


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    conn = open_db()
    try:
        return db_stats(conn)
    finally:
        conn.close()


@app.get("/api/orders")
def api_orders(limit: int = 20) -> list[dict[str, Any]]:
    conn = open_db()
    try:
        return [order_to_dict(o) for o in fetch_orders(conn, limit)]
    finally:
        conn.close()


@app.get("/api/orders/{order_id}")
def api_order(order_id: int) -> JSONResponse:
    conn = open_db()
    try:
        order = fetch_order(conn, order_id)
        if order is None:
            return JSONResponse({"error": "order not found"}, status_code=404)
        return JSONResponse(order_to_dict(order))
    finally:
        conn.close()


@app.post("/api/sync")
def api_sync(full: bool = False) -> dict[str, Any]:
    result = run_sync(get_config(), full=full)
    return {
        "mode": result.mode,
        "pulled": result.pulled,
        "written": result.written,
        "pages": result.pages,
        "watermark_from": result.watermark_from,
        "watermark_to": result.watermark_to,
    }


@app.post("/api/orders/{order_id}/email")
def api_generate_email(order_id: int, force: bool = False) -> JSONResponse:
    """生成（或重新生成）一封英文售后邮件并落库。"""
    conn = open_db()
    try:
        order = fetch_order(conn, order_id)
        if order is None:
            return JSONResponse({"error": "order not found"}, status_code=404)

        draft = generate_email(order, get_config())
        email_id = insert_email(
            conn,
            order_id=order.id,
            scenario=draft.scenario,
            subject=draft.subject,
            body=draft.body,
            provider=draft.provider,
        )
        return JSONResponse(
            {
                "email_id": email_id,
                "order_id": order.id,
                "order_number": order.order_number,
                "scenario": draft.scenario,
                "scenario_label": SCENARIO_LABELS.get(draft.scenario, draft.scenario),
                "subject": draft.subject,
                "body": draft.body,
                "provider": draft.provider,
            }
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def page_index(request: Request, limit: int = 20) -> HTMLResponse:
    conn = open_db()
    try:
        orders = [order_to_dict(o) for o in fetch_orders(conn, limit)]
        summary = db_stats(conn)
        recent_emails = [dict(r) for r in fetch_emails(conn, 8)]
    finally:
        conn.close()

    cfg = get_config()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "orders": orders,
            "stats": summary,
            "recent_emails": recent_emails,
            "scenario_labels": SCENARIO_LABELS,
            "mode": "mock" if cfg.is_mock else f"live:{cfg.shop}",
            "engine": f"deepseek:{cfg.deepseek_model}" if cfg.has_llm else "内置英文模板",
        },
    )


@app.get("/order/{order_id}", response_class=HTMLResponse)
def page_order(request: Request, order_id: int) -> HTMLResponse:
    conn = open_db()
    try:
        order = fetch_order(conn, order_id)
        if order is None:
            return HTMLResponse("<h1>404 · 订单不存在</h1>", status_code=404)
        data = order_to_dict(order)
        emails = [
            dict(r)
            for r in conn.execute(
                "SELECT id, scenario, subject, body, provider, status, generated_at "
                "FROM emails WHERE order_id = ? ORDER BY id DESC",
                (order_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    return templates.TemplateResponse(
        request,
        "order.html",
        {
            "order": data,
            "emails": emails,
            "scenario_labels": SCENARIO_LABELS,
        },
    )


@app.post("/order/{order_id}/email")
def action_generate_email(order_id: int) -> RedirectResponse:
    """表单提交：生成邮件后跳回订单页。"""
    conn = open_db()
    try:
        order = fetch_order(conn, order_id)
        if order is not None:
            draft = generate_email(order, get_config())
            insert_email(
                conn,
                order_id=order.id,
                scenario=draft.scenario,
                subject=draft.subject,
                body=draft.body,
                provider=draft.provider,
            )
    finally:
        conn.close()
    return RedirectResponse(f"/order/{order_id}", status_code=303)


@app.post("/sync")
def action_sync() -> RedirectResponse:
    run_sync(get_config())
    return RedirectResponse("/", status_code=303)
