"""Email alerting service for MSAI v2 operational alerts."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from msai.core.logging import get_logger

log = get_logger(__name__)


class AlertService:
    """Send operational email alerts via SMTP.

    When ``smtp_host`` is empty the service degrades gracefully: alerts are
    logged as warnings but never sent.  This allows the rest of the
    application to call alerting methods unconditionally without crashing
    in environments where SMTP is not configured.
    """

    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        sender: str = "",
        password: str = "",
        default_recipients: list[str] | None = None,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.password = password
        self.default_recipients = default_recipients or []

    async def send_alert(
        self, subject: str, body: str, recipients: list[str] | None = None
    ) -> bool:
        """Send an email alert. Returns ``True`` on success.

        If *recipients* is ``None`` or empty the ``default_recipients``
        configured at construction time are used.
        """
        to = recipients or self.default_recipients
        if not self.smtp_host:
            log.warning("alert_not_sent_no_smtp", subject=subject)
            return False

        if not to:
            log.warning("alert_not_sent_no_recipients", subject=subject)
            return False

        try:
            msg = EmailMessage()
            msg["Subject"] = f"[MSAI Alert] {subject}"
            msg["From"] = self.sender
            msg["To"] = ", ".join(to)
            msg.set_content(body)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._send_smtp, msg)

            log.info("alert_sent", subject=subject, recipients=to)
            return True
        except Exception:
            log.exception("alert_send_failed", subject=subject)
            return False

    def _send_smtp(self, msg: EmailMessage) -> None:
        """Send an email message via SMTP synchronously.

        Intended to be called from :meth:`send_alert` inside
        ``loop.run_in_executor`` so the event loop is not blocked.
        """
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.sender, self.password)
            server.send_message(msg)

    async def alert_strategy_error(self, strategy_name: str, error: str) -> None:
        """Alert when a strategy raises an unexpected error."""
        await self.send_alert(
            f"Strategy Error: {strategy_name}",
            f"Strategy '{strategy_name}' encountered an error:\n\n{error}",
        )

    async def alert_daily_loss(self, current_pnl: float, threshold: float) -> None:
        """Alert when the daily P&L breaches the configured loss threshold."""
        await self.send_alert(
            "Daily Loss Threshold Breached",
            f"Current P&L: ${current_pnl:,.2f}\nThreshold: ${threshold:,.2f}",
        )

    async def alert_system_down(self, service: str) -> None:
        """Alert when a critical service stops responding."""
        await self.send_alert(
            f"Service Down: {service}",
            f"{service} is not responding.",
        )

    async def alert_ib_disconnect(self) -> None:
        """Alert when the IB Gateway connection is lost."""
        await self.send_alert(
            "IB Gateway Disconnected",
            "Interactive Brokers Gateway lost connection. "
            "Check the ib-gateway-troubleshooting runbook for resolution steps.",
        )
