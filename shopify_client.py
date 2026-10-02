"""Shopify Admin REST API 客户端。

这一段在干嘛：
  - 真机模式：用 Admin REST API 按 `updated_at_min` 做**增量拉取**订单，
    自动跟着响应头 `Link` 翻页，遇到 429（限流）按 `Retry-After` 退避重试，
    所有请求都带超时，网络错误会重试有限次；
  - mock 模式：完全不联网，直接返回 `build_mock_orders()` 造的 20 条假订单（其中 2 条带退款）。

安全：访问令牌只放在请求头 `X-Shopify-Access-Token` 里，绝不打印到日志。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests

from config import Config, setup_logging

log = setup_logging("shopify_client")

#: Shopify 单页最多 250 条；拉满能少翻几页
PAGE_LIMIT = 250
#: 默认单次请求超时（连接, 读取）秒
DEFAULT_TIMEOUT = (10, 30)
#: 限流 / 网络错误最多重试几次
MAX_RETRIES = 5

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class ShopifyError(RuntimeError):
    """Shopify 接口返回了非预期结果时抛这个。"""


class ShopifyClient:
    """Admin REST API 的薄封装。用 with 语句会自动关连接池。"""

    def __init__(self, config: Config, *, timeout: tuple[int, int] = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout
        self.mock = config.is_mock
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-Shopify-Access-Token": config.access_token,
                "Accept": "application/json",
                "User-Agent": "shopify-ops/1.0 (learning project)",
            }
        )

    # ---------- 上下文管理 ----------

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ---------- 底层请求：超时 + 429 退避重试 ----------

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        """发一个 GET，内置退避重试。

        重试规则：
          - 429：优先用响应头的 Retry-After，没有就指数退避（1,2,4,8...秒）
          - 5xx / 网络超时：指数退避后重试
          - 4xx（除 429）：属于请求本身有问题，直接抛错，不浪费时间重试
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # 网络层错误
                last_error = exc
                wait = min(2 ** attempt, 30)
                log.warning("请求异常（第 %d 次）：%s，%.1fs 后重试", attempt, exc, wait)
                time.sleep(wait)
                continue

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else min(2 ** attempt, 30)
                log.warning("被限流 429，等待 %.1fs 后重试（第 %d 次）", wait, attempt)
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                wait = min(2 ** attempt, 30)
                log.warning("服务端 %s，%.1fs 后重试（第 %d 次）", response.status_code, wait, attempt)
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                # 4xx 多半是 token / scope / 参数问题，重试没意义
                raise ShopifyError(
                    f"请求失败 {response.status_code}：{response.text[:300]}（URL: {response.url.split('?')[0]}）"
                )

            return response

        raise ShopifyError(f"重试 {MAX_RETRIES} 次后仍然失败：{last_error}")

    # ---------- 分页拉订单 ----------

    def iter_orders(
        self,
        *,
        updated_at_min: str | None = None,
        status: str = "any",
        limit: int = PAGE_LIMIT,
    ) -> Iterator[list[dict[str, Any]]]:
        """按页产出订单，每页是一个 list[dict]。

        - `updated_at_min`：增量水位，只拉这个时间之后有变动的订单；None 表示全量
        - `status="any"`：包含已取消/已归档的订单，售后场景需要看到它们
        - 翻页：Shopify 用响应头 Link 给下一页地址，跟着走直到没有 rel="next"
        """
        if self.mock:
            log.info("[mock] 返回内置假订单（不联网）")
            yield build_mock_orders()
            return

        url = f"{self.config.api_base_url}/orders.json"
        params: dict[str, Any] = {
            "limit": limit,
            "status": status,
            "fields": "id,name,created_at,updated_at,processed_at,financial_status,fulfillment_status,"
                      "total_price,currency,email,customer,line_items,refunds",
        }
        if updated_at_min:
            params["updated_at_min"] = updated_at_min

        page = 0
        while url:
            page += 1
            response = self._get(url, params=params)
            payload = response.json()
            batch: list[dict[str, Any]] = payload.get("orders", []) or []
            log.info("拉到第 %d 页，共 %d 条订单", page, len(batch))
            if batch:
                yield batch

            # 翻页后参数已经写进 next URL 里了，再带 params 会重复
            url = _next_link(response.headers.get("Link", ""))
            params = {}

    def fetch_all_orders(self, *, updated_at_min: str | None = None, status: str = "any") -> list[dict[str, Any]]:
        """把所有页拼成一个列表（小店铺够用；大店铺建议直接用 iter_orders 流式处理）。"""
        result: list[dict[str, Any]] = []
        for batch in self.iter_orders(updated_at_min=updated_at_min, status=status):
            result.extend(batch)
        return result

    # ---------- 其它常用接口（保持薄封装，需要时再扩） ----------

    def count_orders(self, *, updated_at_min: str | None = None) -> int:
        """订单总数；真机模式下调 /orders/count.json。"""
        if self.mock:
            return len(build_mock_orders())
        params: dict[str, Any] = {"status": "any"}
        if updated_at_min:
            params["updated_at_min"] = updated_at_min
        response = self._get(f"{self.config.api_base_url}/orders/count.json", params=params)
        return int(response.json().get("count", 0))

    def ping(self) -> bool:
        """连通性自检：能读到店铺名就算通（用 /shop.json）。"""
        if self.mock:
            return True
        try:
            response = self._get(f"{self.config.api_base_url}/shop.json")
        except ShopifyError as exc:
            log.error("连通性检查失败：%s", exc)
            return False
        return bool(response.json().get("shop"))


def _next_link(link_header: str) -> str:
    """从 Link 头里抠出 rel="next" 的地址；没有就返回空串。"""
    if not link_header:
        return ""
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# mock 数据：20 条订单，其中 2 条带退款
# --------------------------------------------------------------------------

_MOCK_ITEMS: tuple[tuple[str, str, float], ...] = (
    ("Ceramic Coffee Mug 350ml", "MUG-350-WHT", 18.0),
    ("Linen Throw Pillow Cover", "PIL-LIN-45", 29.5),
    ("Bamboo Cutting Board Set", "BAM-CUT-3", 42.0),
    ("Wireless Charging Pad", "CHG-PAD-10W", 35.9),
    ("Cotton Bath Towel 2-Pack", "TWL-CTN-2", 24.0),
    ("Stainless Steel Water Bottle", "BTL-SS-750", 27.0),
)

_MOCK_CUSTOMERS: tuple[tuple[str, str, str], ...] = (
    ("Emily", "Carter", "emily.carter@example.com"),
    ("Michael", "Nguyen", "michael.nguyen@example.com"),
    ("Sofia", "Rossi", "sofia.rossi@example.com"),
    ("Daniel", "Okafor", "daniel.okafor@example.com"),
    ("Hannah", "Schmidt", "hannah.schmidt@example.com"),
    ("Liam", "O'Connell", "liam.oconnell@example.com"),
    ("Priya", "Sharma", "priya.sharma@example.com"),
    ("Noah", "Bennett", "noah.bennett@example.com"),
    ("Yuki", "Tanaka", "yuki.tanaka@example.com"),
    ("Chloe", "Dubois", "chloe.dubois@example.com"),
)

#: 固定随机种子 —— 每次跑 mock 得到完全相同的数据，方便对比、也方便写测试
_MOCK_SEED = 20260923


def build_mock_orders(now: datetime | None = None) -> list[dict[str, Any]]:
    """造 20 条假订单。

    规则（刻意覆盖各种售后分支）：
      - 第 4 条（索引 3）：已发货 + 部分退款 —— 退款安抚
      - 第 12 条（索引 11）：已取消 + 全额退款 —— 差评挽回
      - 其余在 pending/paid/fulfilled/cancelled 之间分布
    """
    rng = random.Random(_MOCK_SEED)
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    orders: list[dict[str, Any]] = []

    refund_at_index = {3: "partial", 11: "full"}

    for index in range(20):
        order_id = 5001 + index
        created = now - timedelta(days=index, hours=rng.randint(0, 20))
        first, last, email = _MOCK_CUSTOMERS[index % len(_MOCK_CUSTOMERS)]

        # 挑 1~3 个商品
        line_items: list[dict[str, Any]] = []
        total = 0.0
        for slot, (title, sku, price) in enumerate(rng.sample(_MOCK_ITEMS, k=rng.randint(1, 3)), start=1):
            qty = rng.randint(1, 2)
            line_items.append(
                {
                    "id": order_id * 100 + slot,
                    "title": title,
                    "sku": sku,
                    "quantity": qty,
                    "price": f"{price:.2f}",
                }
            )
            total += price * qty
        total = round(total, 2)

        # 订单状态分布
        if index in refund_at_index:
            financial_status, fulfillment_status = "refunded", ("fulfilled" if index == 3 else None)
        elif index % 7 == 5:
            financial_status, fulfillment_status = "cancelled", None
        elif index % 3 == 0:
            financial_status, fulfillment_status = "paid", "fulfilled"
        elif index % 3 == 1:
            financial_status, fulfillment_status = "paid", None
        else:
            financial_status, fulfillment_status = "pending", None

        refunds: list[dict[str, Any]] = []
        if index in refund_at_index:
            amount = round(total * 0.4, 2) if refund_at_index[index] == "partial" else total
            reason = "customer changed mind" if refund_at_index[index] == "partial" else "out of stock"
            refunds.append(
                {
                    "id": order_id * 10 + 1,
                    "created_at": (created + timedelta(days=1, hours=3)).isoformat(),
                    "reason": reason,
                    "note": "Processed by mock data generator",
                    "transactions": [
                        {"kind": "refund", "status": "success", "amount": f"{amount:.2f}", "currency": "USD"}
                    ],
                }
            )

        orders.append(
            {
                "id": order_id,
                "name": f"#{1001 + index}",
                "created_at": created.isoformat(),
                "updated_at": (created + timedelta(hours=rng.randint(1, 40))).isoformat(),
                "processed_at": (created + timedelta(minutes=25)).isoformat(),
                "financial_status": financial_status,
                "fulfillment_status": fulfillment_status,
                "total_price": f"{total:.2f}",
                "currency": "USD",
                "email": email,
                "customer": {"id": 9000 + index, "first_name": first, "last_name": last, "email": email},
                "line_items": line_items,
                "refunds": refunds,
            }
        )

    return orders
