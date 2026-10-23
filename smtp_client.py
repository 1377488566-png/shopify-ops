"""邮件发送层：把 emails 表里 status='draft' 的信发出去，并把状态回写。

三种模式（自动判定，按优先级）：
  1. **smtp**    —— 配了 SMTP_HOST 且 NOTIFY_DRY_RUN=false，真的用 smtplib 投递；
  2. **console** —— 没配 SMTP_HOST（或 dry-run），把邮件打到日志，状态记 'sent' 但标注 provider=console；
  3. **失败**    —— smtp 真的抛异常，状态记 'failed'，error 记进 fail_reason，可重试。

为什么要 console 模式：本地开发和公网 Demo 都不该绑定真实邮箱密码。
面试演示时能完整看到「发送 → 状态流转」的闭环，而不用泄露任何凭据。
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from config import Config, setup_logging
from db import connect, mark_email_failed, mark_email_sent

log = setup_logging("smtp_client")


@dataclass
class SendResult:
    """一次发送尝试的结果。"""

    email_id: int
    order_number: str
    to_addr: str
    ok: bool
    mode: str          # smtp / console / failed
    detail: str = ""   # 失败原因，或 console 模式的说明


def _build_message(*, to_addr: str, to_name: str, subject: str, body: str, config: Config) -> EmailMessage:
    """把纯文本正文包成一封标准邮件（带 Message-ID / Date，避免被收件方判垃圾）。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.smtp_from_name, config.smtp_from_addr or config.smtp_user))
    msg["To"] = formataddr((to_name, to_addr))
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="shopify-ops.local")
    msg.set_content(body)
    return msg


def _send_via_smtp(msg: EmailMessage, config: Config) -> None:
    """真正投递。465 走 SSL，其它端口走 STARTTLS。"""
    if config.smtp_port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=20, context=ctx) as server:
            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)
        return

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as server:
        server.ehlo()
        if config.smtp_starttls:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg)


def send_email(conn, *, email_id: int, config: Config) -> SendResult:
    """发送一封已生成的信：读出来 → 投递 → 回写状态。

    conn 由调用方提供并负责关闭；这里只做业务，不碰连接生命周期。
    """
    row = conn.execute(
        """
        SELECT e.id, e.subject, e.body, e.status, e.provider,
               o.order_number, o.customer_email, o.customer_name
        FROM emails e JOIN orders o ON o.id = e.order_id
        WHERE e.id = ?
        """,
        (email_id,),
    ).fetchone()

    if row is None:
        raise LookupError(f"email id={email_id} 不存在")

    if row["status"] == "sent":
        return SendResult(
            email_id=email_id,
            order_number=row["order_number"],
            to_addr=row["customer_email"],
            ok=True,
            mode="skipped",
            detail="已经发送过，跳过（幂等）",
        )

    to_addr = row["customer_email"]
    to_name = row["customer_name"] or "there"
    msg = _build_message(
        to_addr=to_addr,
        to_name=to_name,
        subject=row["subject"],
        body=row["body"],
        config=config,
    )

    # ---- console 模式：不真的投递，但把全文打出来，证明内容正确 ----
    if not config.has_smtp:
        log.info(
            "========================================\n"
            "SMTP 未配置 → console 模式（不会真的发出）\n"
            "To:      %s <%s>\n"
            "Subject: %s\n"
            "----------------------------------------\n"
            "%s\n"
            "========================================",
            to_name, to_addr, row["subject"], row["body"],
        )
        mark_email_sent(conn, email_id)
        return SendResult(
            email_id=email_id,
            order_number=row["order_number"],
            to_addr=to_addr,
            ok=True,
            mode="console",
            detail="SMTP 未配置，内容已输出到日志（状态记为 sent）",
        )

    # ---- 真实 smtp ----
    try:
        _send_via_smtp(msg, config)
    except Exception as exc:  # noqa: BLE001 - 网络/SMTP 任何异常都要落成 failed，不能炸整页
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("发送失败 email_id=%s to=%s → %s", email_id, to_addr, reason)
        mark_email_failed(conn, email_id, reason)
        return SendResult(
            email_id=email_id,
            order_number=row["order_number"],
            to_addr=to_addr,
            ok=False,
            mode="failed",
            detail=reason,
        )

    mark_email_sent(conn, email_id)
    log.info("发送成功 email_id=%s to=%s", email_id, to_addr)
    return SendResult(
        email_id=email_id,
        order_number=row["order_number"],
        to_addr=to_addr,
        ok=True,
        mode="smtp",
    )


def send_pending(config: Config, limit: int = 10) -> list[SendResult]:
    """批量发送：把所有 status='draft' 的信按 id 顺序发掉（CLI / 定时任务用）。"""
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM emails WHERE status = 'draft' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()

    results: list[SendResult] = []
    for row in rows:
        conn = connect(config.db_path)
        try:
            results.append(send_email(conn, email_id=row["id"], config=config))
        finally:
            conn.close()

    ok = sum(1 for r in results if r.ok)
    log.info("批量发送结束：成功 %d / 共 %d 封", ok, len(results))
    return results
