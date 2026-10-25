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

from fastapi import FastAPI, Request
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
    fetch_email,
    fetch_emails,
    fetch_order,
    fetch_orders,
    init_db,
    insert_email,
    reset_email_to_draft,
    stats as db_stats,
)
from email_gen import SCENARIO_LABELS, detect_scenario, generate_email  # noqa: E402
from smtp_client import send_email  # noqa: E402
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

def _auto_seed_once() -> None:
    """空库自动播种：AUTO_SEED=true 且一条订单都没有时，跑一次 mock 同步。

    存在的意义：Render 免费实例重启后数据会丢，面试官打开链接不该看到空页面。
    只在**确实为空**时播种，不会覆盖任何已有数据，也不会反复灌重复订单
    （mock 数据带唯一 order_number，同步本身幂等）。
    """
    cfg = get_config()
    if not cfg.auto_seed_enabled:
        return
    conn = open_db()
    try:
        if int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] or 0) > 0:
            return
        log.info("AUTO_SEED 已开启且库为空，自动灌入 mock 数据")
    finally:
        conn.close()
    try:
        from config import Config as _Cfg

        seed_cfg = _Cfg.from_env(force_mock=True)
        res = run_sync(seed_cfg, full=True)
        log.info("AUTO_SEED 完成：%s", res)
    except Exception as exc:  # 播种失败不能拖垮服务启动
        log.warning("AUTO_SEED 失败（不影响服务启动）：%s", exc)


def _order_redirect(order_id: object) -> RedirectResponse:
    """只允许跳回本站在订单页，绝不接受任意外部 URL。

    为什么不用 RedirectResponse(f"/order/{order_id}") 了事：
    一旦哪天有人把用户传入的字符串直接拼进来，这里就会变成开放重定向
    （钓鱼者可以构造 /... -> evil.com 的链接）。这里强制 int 转换 +
    路由白名单，从根上堵住。
    """
    try:
        oid = int(order_id)  # 非数字直接抛异常，走兜底
    except (TypeError, ValueError):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/order/{oid}", status_code=303)


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
            "sender": cfg.mail_mode,
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
                "SELECT id, scenario, subject, body, provider, status, generated_at, "
                "sent_at, fail_reason "
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
            "sender": get_config().mail_mode,
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
    return _order_redirect(order_id)


@app.post("/email/{email_id}/send")
def action_send_email(email_id: int, order_id: int = 0) -> RedirectResponse:
    """发送一封已生成的邮件（表单提交，发完跳回订单页）。

    email_id 来自路径且是 int，FastAPI 已做类型校验；
    收件地址一律从数据库里按 order_id 关联取，**不接受任何外部传入的地址**，避免被当成转发器滥用。
    """
    conn = open_db()
    try:
        row = fetch_email(conn, email_id)
        if row is None:
            return RedirectResponse("/", status_code=303)
        # 目标订单以数据库里的归属为准，忽略表单传入值，避免越权跳转
        target = order_id or row["order_id"]
        result = send_email(conn, email_id=email_id, config=get_config())
        log.info("发送结果 email_id=%s mode=%s ok=%s", email_id, result.mode, result.ok)
    finally:
        conn.close()
    return _order_redirect(target)


@app.post("/email/{email_id}/retry")
def action_retry_email(email_id: int, order_id: int = 0) -> RedirectResponse:
    """把 failed 的邮件改回 draft，回到待发送状态（不直接重发，让人再确认一次）。"""
    conn = open_db()
    try:
        row = fetch_email(conn, email_id)
        if row is None:
            return RedirectResponse("/", status_code=303)
        target = order_id or row["order_id"]
        reset_email_to_draft(conn, email_id)
    finally:
        conn.close()
    return _order_redirect(target)


@app.post("/sync")
def action_sync() -> RedirectResponse:
    run_sync(get_config())
    return RedirectResponse("/", status_code=303)


# 服务启动时执行一次：只有 AUTO_SEED=true 且库为空才真正播种。
# 放在文件末尾，确保 get_config / open_db / run_sync 都已定义。
_auto_seed_once()  # 服务启动时执行一次（空库才真正播种）
