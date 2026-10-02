"""Webhook 接收端（可选组件）。

这一段在干嘛：
  - 起一个本地 HTTP 服务，接 Shopify 推过来的 `orders/create`、`orders/update`、`refunds/create` 等事件；
  - **先验签**：用 `SHOPIFY_WEBHOOK_SECRET` 对原始请求体做 HMAC-SHA256，
    和请求头 `X-Shopify-Hmac-Sha256` 对比（用 `hmac.compare_digest` 防时序攻击）；
  - 验签通过才处理：orders/* 直接 UPSERT 进库；refunds/* 先取出订单号再把整单重新拉一次；
  - 收到什么都会打印主题 + 订单号，便于调试。

本地怎么联调（思路，不写死具体工具）：
  1. 先把本服务跑起来（默认 127.0.0.1:8000）；
  2. 用内网穿透工具把本地 8000 端口映射成一个公网 HTTPS 地址
     （常见做法：ngrok / cloudflared / frp / localtunnel，任选其一）；
  3. 在 Shopify 后台 Settings -> Notifications -> Webhooks 里，
     填 `<你的公网地址>/webhook`，格式选 JSON，事件勾 orders/create、orders/update、refunds/create；
  4. Shopify 会给一个签名密钥，把它填进 .env 的 `SHOPIFY_WEBHOOK_SECRET`，重启服务；
  5. 后台点 “Send test notification”，本地日志应出现 `验签通过`。

实现只用标准库 `http.server`（不额外装 Flask），依赖最少、方便初学者读懂。
生产环境建议换成 gunicorn/uvicorn + 反向代理，并加失败重试队列。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from config import Config, setup_logging
from db import connect, init_db, upsert_order
from shopify_client import ShopifyClient

log = setup_logging("webhook")

#: 单次请求体最大 2MB，防止被塞爆内存
MAX_BODY_BYTES: Final[int] = 2 * 1024 * 1024


def verify_hmac(raw_body: bytes, header_hmac: str, secret: str) -> bool:
    """校验 Shopify 的 HMAC-SHA256 签名。

    注意：必须用**原始字节**计算，不能先用 json 解析再 dump 回去（顺序会变，签名必错）。
    """
    if not secret or not header_hmac:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, header_hmac.strip())


def handle_event(topic: str, payload: dict[str, Any], config: Config) -> None:
    """按主题处理事件：订单事件直接落库，退款事件触发一次单订单重拉。"""
    conn = connect(config.db_path)
    try:
        init_db(conn)

        if topic.startswith("orders/"):
            upsert_order(conn, payload)
            log.info("订单已入库：%s", payload.get("name") or payload.get("id"))

        elif topic.startswith("refunds/"):
            order_id = payload.get("order_id") or (payload.get("order") or {}).get("id")
            log.info("收到退款事件 refund_id=%s order_id=%s", payload.get("id"), order_id)
            if not order_id:
                log.warning("退款事件里没有 order_id，跳过")
                return
            # 退款事件的 payload 里没有完整订单，重新拉一次这单最省事
            with ShopifyClient(config) as client:
                if client.mock:
                    log.info("[mock] 跳过重拉，仅记录退款事件")
                else:
                    fresh = client.fetch_all_orders()
                    for order in fresh:
                        if int(order.get("id", 0)) == int(order_id):
                            upsert_order(conn, order)
                            log.info("已根据退款事件刷新订单 %s", order.get("name"))
                            break
        else:
            log.info("未处理的主题：%s", topic)
    finally:
        conn.close()


def make_handler(config: Config) -> type[BaseHTTPRequestHandler]:
    """生成一个绑定了 config 的 handler 类（http.server 的用法就是这样）。"""

    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "shopify-ops-webhook/1.0"

        # ---- 工具方法 ----

        def _reply(self, code: int, text: str = "ok") -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            """把 http.server 自带的访问日志也接到我们的 logger。"""
            log.debug("HTTP %s - %s", self.address_string(), fmt % args)

        # ---- 路由 ----

        def do_GET(self) -> None:  # noqa: N802 - 标准库约定的方法名
            """健康检查：浏览器直接访问就能看到服务活着。"""
            if self.path in {"/", "/healthz"}:
                self._reply(200, f"shopify-ops webhook is up ({'mock' if config.is_mock else 'live'} mode)\n")
            else:
                self._reply(404, "not found\n")

        def do_POST(self) -> None:  # noqa: N802
            """接收 webhook。"""
            if self.path != "/webhook":
                self._reply(404, "not found\n")
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(400, "bad body size\n")
                return

            raw_body = self.rfile.read(length)
            topic = self.headers.get("X-Shopify-Topic", "")
            header_hmac = self.headers.get("X-Shopify-Hmac-Sha256", "")

            # 1) 验签（mock 模式下允许跳过，方便本地拿 curl 自测）
            if config.webhook_secret:
                if not verify_hmac(raw_body, header_hmac, config.webhook_secret):
                    log.warning("验签失败，拒绝处理（topic=%s）", topic)
                    self._reply(401, "invalid signature\n")
                    return
                log.info("验签通过 topic=%s", topic)
            elif not config.is_mock:
                log.error("缺少 SHOPIFY_WEBHOOK_SECRET，拒绝处理真实店铺事件")
                self._reply(500, "webhook secret not configured\n")
                return
            else:
                log.info("mock 模式：跳过验签（topic=%s）", topic)

            # 2) 解析
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.error("payload 不是合法 JSON：%s", exc)
                self._reply(400, "invalid json\n")
                return

            # 3) 处理
            try:
                handle_event(topic, payload, config)
            except Exception as exc:  # noqa: BLE001 - 处理失败也要回 200 之外的状态，让 Shopify 重试
                log.exception("处理事件失败：%s", exc)
                self._reply(500, "processing failed\n")
                return

            self._reply(200, "ok\n")

    return WebhookHandler


def serve(config: Config, host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动服务（前台阻塞运行，Ctrl+C 停止）。"""
    handler = make_handler(config)
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info("Webhook 服务已启动：http://%s:%d/webhook ｜mode=%s", host, port, "mock" if config.is_mock else "live")
    log.info("本地联调：用内网穿透工具把 %d 端口映射成公网 HTTPS，再填到 Shopify 后台 Webhooks", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在关闭……")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Shopify Webhook 接收端（可选组件）")
    parser.add_argument("--mock", action="store_true", help="强制 mock 模式")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    serve(Config.from_env(force_mock=args.mock), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
