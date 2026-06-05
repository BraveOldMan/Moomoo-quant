# -*- coding: utf-8 -*-
"""观察列表加载单测（WATCHLIST 环境变量优先 + watchlist.txt 回退，无需 OpenD）。"""

from hk_strategy.config import _load_watchlist


def test_env_takes_precedence_over_file(tmp_path, monkeypatch):
    # Arrange：文件与环境变量同时存在
    wl = tmp_path / "watchlist.txt"
    wl.write_text("US.FILE1\nUS.FILE2\n", encoding="utf-8")
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))
    monkeypatch.setenv("WATCHLIST", "US.ENV1, US.ENV2")

    # Act / Assert：环境变量优先，文件被忽略
    assert _load_watchlist() == ("US.ENV1", "US.ENV2")


def test_file_fallback_parses_comments_blanks_and_dedups(tmp_path, monkeypatch):
    # Arrange：含注释、空行、行内注释、逗号分隔、重复项
    wl = tmp_path / "watchlist.txt"
    wl.write_text(
        "# 头部注释\n\nUS.MRVL    # 行内注释\nUS.AMD, US.NVDA\nUS.MRVL\n",  # 重复应去除
        encoding="utf-8",
    )
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))

    # Act
    result = _load_watchlist()

    # Assert：去重保序，注释/空行被剔除
    assert result == ("US.MRVL", "US.AMD", "US.NVDA")


def test_returns_empty_when_no_env_and_no_file(tmp_path, monkeypatch):
    # Arrange：环境变量为空且文件不存在
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.setenv("WATCHLIST_FILE", str(tmp_path / "missing.txt"))

    # Act / Assert：回退到仅 IPO 扫描
    assert _load_watchlist() == ()


def test_blank_env_falls_back_to_file(tmp_path, monkeypatch):
    # Arrange：WATCHLIST 为空白字符串应视为未设置
    wl = tmp_path / "watchlist.txt"
    wl.write_text("US.TSLA\n", encoding="utf-8")
    monkeypatch.setenv("WATCHLIST", "   ")
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))

    # Act / Assert
    assert _load_watchlist() == ("US.TSLA",)
