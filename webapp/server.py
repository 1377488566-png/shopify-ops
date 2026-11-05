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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
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
# 可观测性：指标采集（纯标准库，不引第三方依赖）
# --------------------------------------------------------------------------

class Metrics:
    """进程内指标容器。

    为什么自己写而不用 prometheus_client：
      1. 本项目零依赖是硬约束（requirements.txt 只有 requests 系）；
      2. 需要的指标很少，一个带锁的计数器足够；
      3. 输出格式照样遵循 Prometheus 文本协议，将来接 Prometheus 不用改。

    线程安全：uvicorn 单进程下多线程处理请求，所有写操作都持锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        # HTTP 层
        self.requests_total = 0
        self.requests_failed = 0
        self.latency_sum = 0.0
        self.latency_max = 0.0
        self.latency_buckets: dict[str, int] = {
            "0.1": 0, "0.25": 0, "0.5": 0, "1": 0, "2.5": 0, "5": 0, "+Inf": 0,
        }
        # 按路径分组的计数与时延（键 = 路由模板或路径前缀）
        self.path_counts: dict[str, int] = {}
        self.path_latency_sum: dict[str, float] = {}
        # 上游依赖层（故障注入实验要看的核心指标）
        self.upstream_calls: dict[str, int] = {}
        self.upstream_failures: dict[str, int] = {}
        self.upstream_latency_sum: dict[str, float] = {}
        # 业务层
        self.emails_generated = 0
        self.emails_sent = 0
        self.syncs = 0

    # ---- 写入 ----
    def observe_request(self, path: str, seconds: float, failed: bool) -> None:
        with self._lock:
            self.requests_total += 1
            if failed:
                self.requests_failed += 1
            self.latency_sum += seconds
            self.latency_max = max(self.latency_max, seconds)
            self.path_counts[path] = self.path_counts.get(path, 0) + 1
            self.path_latency_sum[path] = self.path_latency_sum.get(path, 0.0) + seconds
            for bound in (0.1, 0.25, 0.5, 1, 2.5, 5):
                if seconds <= bound:
                    key = str(int(bound)) if bound >= 1 else str(bound)
                    self.latency_buckets[key] = self.latency_buckets.get(key, 0) + 1
                    break
            else:
                self.latency_buckets["+Inf"] = self.latency_buckets.get("+Inf", 0) + 1

    def observe_upstream(self, name: str, seconds: float, ok: bool) -> None:
        with self._lock:
            self.upstream_calls[name] = self.upstream_calls.get(name, 0) + 1
            if not ok:
                self.upstream_failures[name] = self.upstream_failures.get(name, 0) + 1
            self.upstream_latency_sum[name] = self.upstream_latency_sum.get(name, 0.0) + seconds

    def bump(self, field: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + n)

    # ---- 导出 ----
    def summary(self) -> dict[str, Any]:
        """供前端状态条使用（JSON 友好）。"""
        with self._lock:
            avg = (self.latency_sum / self.requests_total) if self.requests_total else 0.0
            return {
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests_total": self.requests_total,
                "requests_failed": self.requests_failed,
                "error_rate": round(self.requests_failed / self.requests_total, 4) if self.requests_total else 0.0,
                "latency_avg_ms": round(avg * 1000, 1),
                "latency_max_ms": round(self.latency_max * 1000, 1),
                "emails_generated": self.emails_generated,
                "emails_sent": self.emails_sent,
                "syncs": self.syncs,
            }

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []
            uptime = time.time() - self.started_at
            avg = (self.latency_sum / self.requests_total) if self.requests_total else 0.0

            def emit(name: str, mtype: str, help_: str, value: str) -> None:
                lines.append(f"# HELP {name} {help_}")
                lines.append(f"# TYPE {name} {mtype}")
                lines.append(f"{name} {value}")

            emit("shopify_ops_uptime_seconds", "gauge", "进程运行时长（秒）", f"{uptime:.1f}")
            emit("shopify_ops_requests_total", "counter", "HTTP 请求总数", str(self.requests_total))
            emit("shopify_ops_requests_failed_total", "counter", "HTTP 请求失败数（4xx/5xx）", str(self.requests_failed))
            emit("shopify_ops_request_latency_avg_seconds", "gauge", "HTTP 请求平均时延", f"{avg:.4f}")
            emit("shopify_ops_request_latency_max_seconds", "gauge", "HTTP 请求最大时延", f"{self.latency_max:.4f}")
            emit("shopify_ops_emails_generated_total", "counter", "生成的邮件总数", str(self.emails_generated))
            emit("shopify_ops_emails_sent_total", "counter", "发送的邮件总数", str(self.emails_sent))
            emit("shopify_ops_sync_total", "counter", "订单同步次数", str(self.syncs))

            # 按路径
            for path in sorted(self.path_counts):
                n = self.path_counts[path]
                s = self.path_latency_sum.get(path, 0.0)
                # 花括号在 Prometheus 标签值里是合法字符，不做替换，保留 /order/{order_id} 的语义
                emit(f"shopify_ops_path_requests_total{{path=\"{path}\"}}", "counter", "按路径的请求数", str(n))
                emit(f"shopify_ops_path_latency_avg_seconds{{path=\"{path}\"}}", "gauge", "按路径的平均时延", f"{(s / n if n else 0):.4f}")

            # 上游依赖
            for name in sorted(self.upstream_calls):
                calls = self.upstream_calls[name]
                fails = self.upstream_failures.get(name, 0)
                s = self.upstream_latency_sum.get(name, 0.0)
                emit(f"shopify_ops_upstream_calls_total{{upstream=\"{name}\"}}", "counter", "上游依赖调用次数", str(calls))
                emit(f"shopify_ops_upstream_failures_total{{upstream=\"{name}\"}}", "counter", "上游依赖失败次数", str(fails))
                emit(f"shopify_ops_upstream_latency_avg_seconds{{upstream=\"{name}\"}}", "gauge", "上游依赖平均时延", f"{(s / calls if calls else 0):.4f}")

            return "\n".join(lines) + "\n"


METRICS = Metrics()


def _timed_upstream(name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """包一层上游调用，记录次数/耗时/成功失败。

    为什么在 webapp 层做而不是改 email_gen / smtp_client / sync：
      原项目有「不改动任何原有模块」的设计原则，埋点放在调用方更干净，
      也让「观测」这件事本身成为本层职责。
    """
    started = time.perf_counter()
    ok = False
    try:
        result = fn(*args, **kwargs)
        ok = True
        return result
    finally:
        METRICS.observe_upstream(name, time.perf_counter() - started, ok)


def _route_label(request: Request) -> str:
    """把具体路径归一成路由模板，避免 /order/1、/order/2 各占一条指标。"""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path  # 已是 /order/{order_id} 形式    path = request.url.path
    if path.startswith("/order/"):
        return "/order/{order_id}"
    if path.startswith("/api/orders/"):
        return "/api/orders/{order_id}"
    if path.startswith("/email/"):
        return "/email/{email_id}"
    return path


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    """统一采集时延/失败率。排除 /metrics 自身与探活，避免自我污染。"""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    path = _route_label(request)
    if path not in ("/metrics", "/api/health", "/api/ready"):
        METRICS.observe_request(path, elapsed, response.status_code >= 400)
    response.headers["X-Response-Time"] = f"{elapsed * 1000:.1f}ms"
    return response


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
    """存活探针（liveness）：只证明进程活着，不碰数据库、不碰上游。

    刻意保持轻量：k8s / 负载均衡的 liveness 探测失败会触发重启，
    如果这里去查数据库或调模型，一次上游抖动就会把健康实例误杀。
    """
    cfg = get_config()
    return {
        "status": "ok",
        "mode": "mock" if cfg.is_mock else "live",
        "email_engine": f"deepseek:{cfg.deepseek_model}" if cfg.has_llm else "template",
        "db": str(cfg.db_path),
        "uptime_seconds": round(time.time() - METRICS.started_at, 1),
    }


@app.get("/api/ready")
def api_ready() -> JSONResponse:
    """就绪探针（readiness）：数据库可读 + 关键依赖就绪。

    与 /api/health 的区别是网络运维里的核心概念：
      - liveness 失败 → 重启进程
      - readiness 失败 → 摘掉流量，但不重启
    所以这里要做真实检查：开一次库、跑一条查询。
    """
    checks: dict[str, Any] = {}
    ok = True
    try:
        conn = open_db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            checks["database"] = {"ok": True, "orders": int(n)}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)}
        ok = False

    cfg = get_config()
    checks["mode"] = "mock" if cfg.is_mock else "live"
    checks["llm_configured"] = bool(cfg.has_llm)

    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics_endpoint() -> PlainTextResponse:
    """Prometheus 文本协议指标。压测与故障注入实验的数据就来自这里。"""
    return PlainTextResponse(METRICS.render_prometheus())


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    """同一份指标的 JSON 视图，给前端状态条和论文报表用。"""
    return METRICS.summary()


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
    result = _timed_upstream("shopify_api", run_sync, get_config(), full=full)
    METRICS.bump("syncs")
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

        draft = _timed_upstream("llm", generate_email, order, get_config())
        METRICS.bump("emails_generated")
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
            draft = _timed_upstream("llm", generate_email, order, get_config())
            METRICS.bump("emails_generated")
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
        result = _timed_upstream(
            "smtp", send_email, conn, email_id=email_id, config=get_config()
        )
        if result.ok:
            METRICS.bump("emails_sent")
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
    _timed_upstream("shopify_api", run_sync, get_config())
    METRICS.bump("syncs")
    return RedirectResponse("/", status_code=303)


# 服务启动时执行一次：只有 AUTO_SEED=true 且库为空才真正播种。
# 放在文件末尾，确保 get_config / open_db / run_sync 都已定义。
_auto_seed_once()  # 服务启动时执行一次（空库才真正播种）
