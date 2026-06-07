# -*- coding: utf-8 -*-
"""Alert channel regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from us_strategy import alerts
from us_strategy.alerts import AlertManager
from us_strategy.config import StrategyConfig


def test_feishu_alert_sends_interactive_card(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **_kwargs):
        params_path = _arg_path(command, "--params")
        body_path = _arg_path(command, "--data")
        captured["command"] = command
        captured["params"] = json.loads(params_path.read_text(encoding="utf-8"))
        captured["body"] = json.loads(body_path.read_text(encoding="utf-8"))
        return SimpleNamespace(
            returncode=0,
            stdout='{"data":{"message_id":"om_demo"}}',
            stderr="",
        )

    monkeypatch.setattr(alerts.subprocess, "run", fake_run)
    manager = AlertManager(StrategyConfig(feishu_chat_id="oc_demo"))

    manager.send("买入成功", "US.AAPL 成交均价=100.000 qty=10")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["receive_id"] == "oc_demo"
    assert body["msg_type"] == "interactive"
    card = json.loads(str(body["content"]))
    assert card["header"]["title"]["content"] == "美股策略提醒 - 买入成功"
    assert card["header"]["template"] == "green"
    assert "US.AAPL" in card["elements"][0]["text"]["content"]
    assert captured["params"] == {"receive_id_type": "chat_id"}


def test_no_feishu_chat_id_does_not_call_lark(monkeypatch) -> None:
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(alerts.subprocess, "run", fake_run)

    AlertManager(StrategyConfig()).send("买入成功", "US.AAPL")

    assert calls == 0


def test_config_reads_feishu_chat_id(monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_CHAT_ID", "oc_demo")
    monkeypatch.setenv("LARK_CLI", "D:\\tools\\lark-cli.cmd")

    cfg = StrategyConfig.from_env()

    assert cfg.feishu_chat_id == "oc_demo"
    assert cfg.lark_cli == "D:\\tools\\lark-cli.cmd"


def _arg_path(command: list[str], option: str) -> Path:
    raw = command[command.index(option) + 1]
    assert raw.startswith("@")
    return Path(raw[1:])
