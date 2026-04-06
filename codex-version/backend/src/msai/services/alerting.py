from __future__ import annotations

from msai.core.logging import get_logger

logger = get_logger("alerting")


class AlertingService:
    def send_alert(self, level: str, title: str, message: str) -> None:
        logger.warning("alert", level=level, title=title, message=message)

    def send_recovery(self, title: str, message: str) -> None:
        logger.info("alert_recovery", title=title, message=message)
