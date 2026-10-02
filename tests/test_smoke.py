"""冒烟测试（只用标准库 unittest，不用额外装 pytest）。

这一段在干嘛：验证“核心逻辑在没有网络、没有 API key 的情况下也是对的”：
    - mock 数据形状是否符合预期（20 条、2 条带退款）
    - 同步是否幂等（跑两遍不产生重复数据）
    - 四类售后场景判定是否正确
    - 英文模板兜底是否真能生成英文邮件
    - Webhook 的 HMAC 验签对不对
    - 缺凭据时是否自动进 mock 模式

运行（项目根目录）：
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from db import connect, fetch_orders, init_db, stats, upsert_orders
from email_gen import (
    REFUND_APOLOGY,
    REVIEW_RECOVERY,
    SHIPPING_NOTICE,
    WELCOME,
    detect_scenario,
    generate_template_email,
)
from shopify_client import build_mock_orders
from sync import run_sync
from webhook import verify_hmac


class TestMockData(unittest.TestCase):
    """假数据本身要稳定，否则测试和样本都没意义。"""

    def test_shape(self) -> None:
        orders = build_mock_orders()
        self.assertEqual(len(orders), 20)
        self.assertEqual(len({o["id"] for o in orders}), 20, "订单 ID 不能重复")

        with_refund = [o for o in orders if o.get("refunds")]
        self.assertEqual(len(with_refund), 2, "应有 2 条带退款的订单")
        self.assertTrue(all(o.get("line_items") for o in orders), "每条订单都要有商品明细")
        self.assertTrue(all(o.get("email") for o in orders), "每条订单都要有邮箱")

    def test_deterministic(self) -> None:
        """固定随机种子，两次生成结果必须完全一致。"""
        self.assertEqual(build_mock_orders(), build_mock_orders())


class TestDbAndSync(unittest.TestCase):
    """幂等：这是这个项目最重要的性质。"""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_is_idempotent(self) -> None:
        orders = build_mock_orders()
        self.assertEqual(upsert_orders(self.conn, orders), 20)
        self.assertEqual(upsert_orders(self.conn, orders), 20)  # 再写一遍

        report = stats(self.conn)
        self.assertEqual(report["orders"], 20, "重复写入不应产生新订单")
        self.assertEqual(report["orders_with_refund"], 2)
        self.assertEqual(len(fetch_orders(self.conn, 100)), 20)

    def test_sync_mock_is_idempotent(self) -> None:
        """在独立的临时 DB 文件上跑两次完整同步，验证水位推进 + 幂等。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_env(force_mock=True)
            db_file = Path(tmp) / "t.db"
            object.__setattr__(cfg, "db_path", db_file)

            first = run_sync(cfg)
            second = run_sync(cfg)
            self.assertEqual(first.written, 20)
            self.assertEqual(second.written, 20)
            self.assertIsNotNone(second.watermark_from, "第二次同步应带上水位（增量）")

            conn = connect(db_file)
            try:
                self.assertEqual(stats(conn)["orders"], 20)
            finally:
                conn.close()


class TestScenarioDetection(unittest.TestCase):
    """四种场景必须分得清。"""

    def _order(self, **kwargs: object):
        from db import OrderRow

        base = dict(
            id=1,
            order_number="#1",
            customer_name="Test User",
            customer_email="t@example.com",
            total_price=10.0,
            currency="USD",
            financial_status="paid",
            fulfillment_status="",
            created_at="2026-09-01T00:00:00+00:00",
            refund_amount=0.0,
            refund_count=0,
            items=("Thing x1",),
        )
        base.update(kwargs)
        return OrderRow(**base)  # type: ignore[arg-type]

    def test_branches(self) -> None:
        self.assertEqual(detect_scenario(self._order(refund_count=1, refund_amount=5.0)), REFUND_APOLOGY)
        self.assertEqual(detect_scenario(self._order(financial_status="cancelled")), REVIEW_RECOVERY)
        self.assertEqual(detect_scenario(self._order(fulfillment_status="fulfilled")), SHIPPING_NOTICE)
        self.assertEqual(detect_scenario(self._order(financial_status="pending")), WELCOME)


class TestTemplateFallback(unittest.TestCase):
    """没有 API key 也要能产出英文邮件。"""

    def test_all_scenarios_are_english_and_complete(self) -> None:
        from db import OrderRow

        order = OrderRow(
            id=1,
            order_number="#1001",
            customer_name="Emily Carter",
            customer_email="emily@example.com",
            total_price=42.0,
            currency="USD",
            financial_status="paid",
            fulfillment_status="fulfilled",
            created_at="2026-09-01T00:00:00+00:00",
            refund_amount=0.0,
            refund_count=0,
            items=("Mug x2",),
        )
        for scenario in (WELCOME, SHIPPING_NOTICE, REFUND_APOLOGY, REVIEW_RECOVERY):
            draft = generate_template_email(order, scenario)
            self.assertEqual(draft.provider, "template")
            self.assertTrue(draft.subject.strip(), f"{scenario} 缺 subject")
            self.assertGreater(len(draft.body), 200, f"{scenario} 正文太短")
            self.assertIn("#1001", draft.subject + draft.body, "邮件里必须带订单号")
            self.assertIn("Emily Carter", draft.body, "邮件里必须带客户名")
            # 中文一定不应该出现在英文邮件里
            self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in draft.subject), "subject 不该有中文")
            self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in draft.body), "正文不该有中文")


class TestWebhookSignature(unittest.TestCase):
    """验签是 Webhook 的命门。"""

    def test_verify(self) -> None:
        secret = "s3cr3t"
        body = b'{"id": 1, "topic": "orders/create"}'
        good = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_hmac(body, good, secret))
        self.assertFalse(verify_hmac(body, good, "wrong-secret"))
        self.assertFalse(verify_hmac(body, "", secret))
        self.assertFalse(verify_hmac(body + b" ", good, secret), "body 被篡改就该失败")


class TestConfigSafety(unittest.TestCase):
    """安全底线：没凭据 -> 必须 mock。"""

    def test_missing_credentials_falls_back_to_mock(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
            self.assertTrue(config.is_mock, "没有 shop/token 时必须进 mock")
            self.assertFalse(config.has_llm, "没有 key 时不应尝试调用 LLM")

    def test_explicit_mock_flag(self) -> None:
        with patch.dict(
            os.environ,
            {"SHOPIFY_SHOP": "demo.myshopify.com", "SHOPIFY_ACCESS_TOKEN": "tok"},
            clear=True,
        ):
            self.assertFalse(Config.from_env().is_mock, "凭据齐全时默认真机")
            self.assertTrue(Config.from_env(force_mock=True).is_mock, "--mock 必须强制 mock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
