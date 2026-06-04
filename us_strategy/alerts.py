# -*- coding: utf-8 -*-
"""多渠道告警：日志 / 邮件 / Telegram。"""

import logging
import smtplib
import urllib.request
import urllib.parse
from email.mime.text import MIMEText

from .config import StrategyConfig

logger = logging.getLogger(__name__)


class AlertManager:
    def __init__(self, config: StrategyConfig):
        self._cfg = config

    def send(self, event: str, message: str) -> None:
        """向所有已配置的渠道发送告警。"""
        full = f"[{event}] {message}"
        logger.warning("ALERT %s", full)

        if self._cfg.alert_email:
            self._send_email(event, message)

        if self._cfg.telegram_token and self._cfg.telegram_chat_id:
            self._send_telegram(full)

    def _send_email(self, subject: str, body: str) -> None:
        cfg = self._cfg
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[us_strategy] {subject}"
        msg["From"] = cfg.alert_smtp_user
        msg["To"] = cfg.alert_email
        try:
            with smtplib.SMTP(
                cfg.alert_smtp_host, cfg.alert_smtp_port, timeout=10
            ) as smtp:
                smtp.starttls()
                smtp.login(cfg.alert_smtp_user, cfg.alert_smtp_password)
                smtp.send_message(msg)
        except Exception as exc:
            logger.error("邮件发送失败: %s", exc)

    def _send_telegram(self, text: str) -> None:
        cfg = self._cfg
        url = (
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage?"
            + urllib.parse.urlencode({"chat_id": cfg.telegram_chat_id, "text": text})
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.error("Telegram 发送失败 status=%d", resp.status)
        except Exception as exc:
            logger.error("Telegram 发送异常: %s", exc)
