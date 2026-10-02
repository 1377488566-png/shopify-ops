"""英文售后邮件生成器。

这一段在干嘛：
  - 先根据订单状态 + 有没有退款，判定该写哪一类信（4 个分支）：
        欢迎信 welcome / 发货通知 shipping_notice / 退款安抚 refund_apology / 差评挽回 review_recovery
  - 再调用 LLM（DeepSeek，OpenAI 兼容接口）生成 **英文** 的 subject + body；
  - 只要没配 DEEPSEEK_API_KEY（或调用失败），立刻退回**内置英文模板**，保证永远跑得通。

安全：API key 只从环境变量读，只放进请求头，绝不写日志、绝不落库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import requests

from config import Config, setup_logging
from db import OrderRow

log = setup_logging("email_gen")

# 四类售后场景
WELCOME: Final[str] = "welcome"
SHIPPING_NOTICE: Final[str] = "shipping_notice"
REFUND_APOLOGY: Final[str] = "refund_apology"
REVIEW_RECOVERY: Final[str] = "review_recovery"

SCENARIO_LABELS: Final[dict[str, str]] = {
    WELCOME: "欢迎信（新订单待处理）",
    SHIPPING_NOTICE: "发货通知",
    REFUND_APOLOGY: "退款安抚",
    REVIEW_RECOVERY: "差评挽回（订单取消）",
}

#: LLM 请求超时（连接, 读取）秒
LLM_TIMEOUT: Final[tuple[int, int]] = (10, 60)


@dataclass(frozen=True, slots=True)
class EmailDraft:
    """一封生成好的邮件。`provider` 记录是 LLM 写的还是模板兜底的。"""

    scenario: str
    subject: str
    body: str
    provider: str  # "deepseek" 或 "template"

    @property
    def scenario_label(self) -> str:
        return SCENARIO_LABELS.get(self.scenario, self.scenario)


# --------------------------------------------------------------------------
# 1) 场景判定
# --------------------------------------------------------------------------

def detect_scenario(order: OrderRow) -> str:
    """按订单状态选信的类型。判断顺序是有讲究的，先看最需要安抚的情况。

    1. 有退款记录                -> 退款安抚
    2. 订单已取消 / 已作废        -> 差评挽回（客户体验受损，优先挽回）
    3. 已发货                    -> 发货通知
    4. 其它（待付款/已付款未发）  -> 欢迎信
    """
    financial = (order.financial_status or "").lower()
    fulfillment = (order.fulfillment_status or "").lower()

    if order.refund_count > 0 or financial in {"refunded", "partially_refunded"}:
        return REFUND_APOLOGY
    if financial in {"cancelled", "voided"} or "cancel" in financial:
        return REVIEW_RECOVERY
    if fulfillment == "fulfilled" or financial == "paid" and fulfillment == "fulfilled":
        return SHIPPING_NOTICE
    return WELCOME


def _money(order: OrderRow, amount: float | None = None) -> str:
    """把金额格式化加上币种，例如 USD 42.00。"""
    value = order.total_price if amount is None else amount
    return f"{order.currency} {value:,.2f}"


def _items_line(order: OrderRow) -> str:
    """商品清单，压成一行；没有明细就给个通用描述。"""
    return ", ".join(order.items) if order.items else "your recent order"


# --------------------------------------------------------------------------
# 2) 内置英文模板（兜底，无 LLM 也能跑）
# --------------------------------------------------------------------------

def _template_welcome(order: OrderRow) -> EmailDraft:
    subject = f"Thank you for your order {order.order_number}!"
    body = f"""Hi {order.customer_name},

Thank you so much for shopping with us! We have received your order {order.order_number} and our team is already preparing it for shipment.

Order summary
-------------
Items:    {_items_line(order)}
Total:    {_money(order)}
Placed:   {order.created_at[:10]}

You will receive a shipping confirmation email with tracking details as soon as your parcel leaves our warehouse. If you need to change anything about this order, simply reply to this email within the next 12 hours and we will do our best to help.

Thanks again for choosing us.

Warm regards,
Customer Care Team
"""
    return EmailDraft(WELCOME, subject, body, "template")


def _template_shipping(order: OrderRow) -> EmailDraft:
    subject = f"Your order {order.order_number} is on the way"
    body = f"""Hi {order.customer_name},

Good news - your order {order.order_number} has just been shipped!

What is in the parcel
---------------------
{_items_line(order)}
Order total: {_money(order)}

Your tracking number will be activated within 24 hours, and you can follow the parcel using the link in your shipping confirmation. Most deliveries arrive within 3-7 business days depending on your location.

If anything looks wrong when the parcel arrives, reply to this email and we will sort it out right away - no lengthy forms, no hassle.

Thank you for your patience and for shopping with us.

Best regards,
Customer Care Team
"""
    return EmailDraft(SHIPPING_NOTICE, subject, body, "template")


def _template_refund(order: OrderRow) -> EmailDraft:
    refunded = _money(order, order.refund_amount) if order.refund_amount > 0 else "the requested amount"
    subject = f"About the refund for order {order.order_number}"
    body = f"""Hi {order.customer_name},

Thank you for reaching out, and I am sorry that order {order.order_number} did not work out as expected. I completely understand how disappointing that is.

We have processed a refund of {refunded} back to your original payment method. Depending on your bank or card issuer, the amount usually appears within 5-10 business days. If it has not shown up after that window, just reply to this email and I will chase it for you personally.

Order reference
---------------
Order:  {order.order_number}
Items:  {_items_line(order)}
Total:  {_money(order)}

Your feedback matters to us, and I have shared it with the product team so we can do better next time. If you would like to give us another chance, I would be glad to send you a personal discount code for your next order.

Sincerely,
Customer Care Team
"""
    return EmailDraft(REFUND_APOLOGY, subject, body, "template")


def _template_recovery(order: OrderRow) -> EmailDraft:
    subject = f"We are sorry about order {order.order_number}"
    body = f"""Hi {order.customer_name},

I am truly sorry that we had to cancel order {order.order_number}. I know how frustrating that is when you were looking forward to receiving it.

What happened
-------------
Order:  {order.order_number}
Items:  {_items_line(order)}
Total:  {_money(order)}
Status: cancelled - any payment taken has been released back to you.

I would like to make this right. Reply to this email and I will personally help you place a replacement order, reserve stock from the next batch, or find the closest alternative we have available.

As a small apology, please accept 15% off your next purchase - just mention this email and we will apply it for you. We would genuinely love the chance to win back your trust.

With apologies,
Customer Care Team
"""
    return EmailDraft(REVIEW_RECOVERY, subject, body, "template")


_TEMPLATES = {
    WELCOME: _template_welcome,
    SHIPPING_NOTICE: _template_shipping,
    REFUND_APOLOGY: _template_refund,
    REVIEW_RECOVERY: _template_recovery,
}


def generate_template_email(order: OrderRow, scenario: str | None = None) -> EmailDraft:
    """用内置英文模板生成（永远可用）。"""
    chosen = scenario or detect_scenario(order)
    builder = _TEMPLATES.get(chosen, _template_welcome)
    return builder(order)


# --------------------------------------------------------------------------
# 3) LLM 生成（DeepSeek，OpenAI 兼容）
# --------------------------------------------------------------------------

#: 系统提示词：把模型固定成“专业英文客服”，并规定输出格式
_SYSTEM_PROMPT: Final[str] = (
    "You are a senior customer support specialist for a small e-commerce brand. "
    "You write warm, clear, professional AFTER-SALES emails in NATURAL ENGLISH. "
    "Rules: never invent discounts that were not mentioned, never promise refunds that were not given, "
    "keep the tone human and specific, and keep the body between 120 and 200 words. "
    "Always answer in exactly this format:\n"
    "Subject: <one line subject>\n\n<email body>\n"
    "Do not add any commentary, markdown fences, or extra headers."
)

#: 每个场景给模型的额外指令
_SCENARIO_BRIEF: Final[dict[str, str]] = {
    WELCOME: "Goal: thank the customer for the new order, confirm what they bought and the total, "
             "and tell them they will get tracking once it ships.",
    SHIPPING_NOTICE: "Goal: tell the customer the parcel has shipped, recap the contents, and set a realistic "
                     "delivery expectation of 3-7 business days.",
    REFUND_APOLOGY: "Goal: sincerely apologise for the order not working out, confirm the refund amount that was "
                    "already issued and the 5-10 business day bank timeline, and invite them to reply if it "
                    "has not arrived.",
    REVIEW_RECOVERY: "Goal: apologise for the cancellation, explain the payment is released back, and offer "
                     "personal help to place a replacement order.",
}


def _build_user_prompt(order: OrderRow, scenario: str) -> str:
    """把订单事实拼成给模型的用户消息（事实必须来自数据库，别让模型编）。"""
    return (
        f"Write the email for this scenario: {scenario}\n"
        f"Scenario brief: {_SCENARIO_BRIEF.get(scenario, '')}\n\n"
        "Order facts:\n"
        f"- Order number: {order.order_number}\n"
        f"- Customer first name: {order.customer_name}\n"
        f"- Items: {_items_line(order)}\n"
        f"- Order total: {_money(order)}\n"
        f"- Order placed: {order.created_at[:10]}\n"
        f"- Payment status: {order.financial_status or 'unknown'}\n"
        f"- Fulfillment status: {order.fulfillment_status or 'not fulfilled yet'}\n"
        f"- Refund already issued: {_money(order, order.refund_amount) if order.refund_amount > 0 else 'none'}\n\n"
        "Write the email now, in the required Subject + body format."
    )


def _parse_llm_output(text: str, order: OrderRow) -> tuple[str, str]:
    """把模型输出拆成 (subject, body)；格式不对就退化成合理的默认值。"""
    cleaned = text.strip().removeprefix("```").removeprefix("text").removesuffix("```").strip()
    lines = cleaned.splitlines()
    subject = ""
    body_start = 0
    for index, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = index + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    if not subject:
        subject = f"Regarding your order {order.order_number}"
    if not body:
        raise ValueError("LLM 返回的正文为空")
    return subject, body


def generate_llm_email(order: OrderRow, scenario: str, config: Config) -> EmailDraft:
    """调用 DeepSeek（OpenAI 兼容的 /chat/completions）生成英文邮件。

    异常一律往上抛，由 `generate_email()` 统一兜底到模板。
    """
    url = f"{config.deepseek_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.deepseek_api_key}",  # key 只在请求头里出现
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": config.deepseek_model,          # deepseek-chat
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(order, scenario)},
        ],
        "temperature": 0.7,
        "max_tokens": 700,
        "stream": False,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=LLM_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"LLM 调用失败 {response.status_code}: {response.text[:200]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM 返回里没有 choices")
    content = (choices[0].get("message") or {}).get("content") or ""

    subject, body = _parse_llm_output(content, order)
    usage = data.get("usage") or {}
    log.info(
        "LLM 生成成功 order=%s scenario=%s tokens=%s",
        order.order_number,
        scenario,
        usage.get("total_tokens", "?"),
    )
    return EmailDraft(scenario, subject, body, "deepseek")


# --------------------------------------------------------------------------
# 4) 统一入口
# --------------------------------------------------------------------------

def generate_email(order: OrderRow, config: Config, scenario: str | None = None) -> EmailDraft:
    """生成一封邮件：有 key 走 LLM，没 key / 出错就走模板。

    这是全项目唯一需要调用的方法，保证“无论如何都有一封信”。
    """
    chosen = scenario or detect_scenario(order)

    if not config.has_llm:
        log.info("未配置 DEEPSEEK_API_KEY，使用内置模板 order=%s scenario=%s", order.order_number, chosen)
        return generate_template_email(order, chosen)

    try:
        return generate_llm_email(order, chosen, config)
    except Exception as exc:  # noqa: BLE001 - 任何 LLM 问题都必须降级，不能中断流程
        log.warning("LLM 生成失败，降级到内置模板：%s", exc)
        return generate_template_email(order, chosen)
