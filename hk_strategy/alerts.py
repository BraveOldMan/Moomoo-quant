# -*- coding: utf-8 -*-
"""多渠道告警：日志 / 邮件 / Telegram / 飞书。"""

import json
import logging
import shutil
import smtplib
import subprocess
import tempfile
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

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

        if self._cfg.feishu_chat_id:
            self._send_feishu_card(event, message)

    def _send_email(self, subject: str, body: str) -> None:
        cfg = self._cfg
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[hk_strategy] {subject}"
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

    def _send_feishu_card(self, event: str, message: str) -> None:
        cfg = self._cfg
        params = {"receive_id_type": "chat_id"}
        body = {
            "receive_id": cfg.feishu_chat_id,
            "msg_type": "interactive",
            "content": json.dumps(
                _build_feishu_card(event, message),
                ensure_ascii=False,
            ),
        }
        try:
            with tempfile.TemporaryDirectory(prefix="moomoo_lark_alert_") as tmp:
                tmp_path = Path(tmp)
                params_path = tmp_path / "lark_send_params.json"
                body_path = tmp_path / "lark_send_body.json"
                params_path.write_text(
                    json.dumps(params, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                body_path.write_text(
                    json.dumps(body, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                command = _resolve_lark_command(
                    [
                        cfg.lark_cli,
                        "api",
                        "POST",
                        "/open-apis/im/v1/messages",
                        "--params",
                        f"@{params_path}",
                        "--data",
                        f"@{body_path}",
                    ]
                )
                completed = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=15,
                )
            if completed.returncode != 0:
                logger.error(
                    "飞书告警发送失败 returncode=%s stderr=%s",
                    completed.returncode,
                    completed.stderr.strip(),
                )
                return
            receipt = _parse_json_or_text(completed.stdout)
            message_id = _find_first_key(receipt, {"message_id", "messageId"})
            logger.info("飞书告警已发送 message_id=%s", message_id or "unknown")
        except Exception as exc:
            logger.error("飞书告警发送异常: %s", exc)


def _build_feishu_card(event: str, message: str) -> dict[str, Any]:
    """Build a compact Feishu interactive card for intraday HK trading alerts."""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _feishu_header_template(event),
            "title": {
                "tag": "plain_text",
                "content": f"港股策略提醒 - {event}",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": message},
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "来源: hk_strategy / moomoo SIMULATE",
                    }
                ],
            },
        ],
    }


def _feishu_header_template(event: str) -> str:
    """Map alert events to Feishu card colors."""

    if "买入" in event:
        return "green"
    if "卖出" in event:
        return "red"
    if "失败" in event or "熔断" in event:
        return "orange"
    return "blue"


def _resolve_lark_command(command: list[str]) -> list[str]:
    """Resolve lark-cli on Windows, including npm .cmd/.ps1 shims."""

    if not command:
        return command
    executable = command[0]
    if executable != "lark-cli":
        return command

    cmd_executable = shutil.which("lark-cli.cmd")
    if cmd_executable is not None:
        ps1 = Path(cmd_executable).with_suffix(".ps1")
        if ps1.exists():
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
                *command[1:],
            ]
        return [cmd_executable, *command[1:]]

    found = shutil.which("lark-cli") or shutil.which("lark-cli.ps1")
    if found is None:
        return command
    if found.lower().endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            found,
            *command[1:],
        ]
    return [found, *command[1:]]


def _parse_json_or_text(raw: str) -> Any:
    """Parse CLI stdout as JSON when possible."""

    text = raw.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _find_first_key(payload: Any, keys: set[str]) -> str | None:
    """Recursively find the first non-empty string value for any key."""

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and isinstance(value, str) and value:
                return value
            found = _find_first_key(value, keys)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_first_key(item, keys)
            if found:
                return found
    return None
