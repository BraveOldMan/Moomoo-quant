from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from tools import run_us_stock_daily_report as daily


def test_infer_target_date_maps_beijing_0530_to_us_previous_day() -> None:
    now = datetime(2026, 6, 9, 5, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert daily.infer_target_date(now=now).isoformat() == "2026-06-08"


def test_load_us_watchlist_filters_comments_and_non_us(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text(
        "US.AAPL # Apple\nHK.00700\nUS.MSFT, US.AAPL\n\n# comment\n",
        encoding="utf-8",
    )

    assert daily.load_us_watchlist(watchlist) == ("US.AAPL", "US.MSFT")


def test_load_positions_missing_db_does_not_create_file(tmp_path: Path) -> None:
    db_path = tmp_path / "missing_positions.db"

    assert daily.load_positions(str(db_path)) == {}
    assert not db_path.exists()


def test_load_positions_reads_existing_db_with_read_only_uri(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "positions.db"
    db_path.write_text("", encoding="utf-8")
    calls: list[tuple[str, bool]] = []

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def execute(self, sql: str):
            if "sqlite_master" in sql:
                return FakeCursor([(1,)])
            if "PRAGMA table_info" in sql:
                return FakeCursor(
                    [
                        (0, "code"),
                        (1, "cost_price"),
                        (2, "buy_date"),
                        (3, "tranches_bought"),
                        (4, "peak_price"),
                    ],
                )
            return FakeCursor([("US.AAPL", 100.0, "2026-06-05", 1, 110.0, 0, "regular")])

    def fake_connect(database_uri: str, uri: bool = False) -> FakeConnection:
        calls.append((database_uri, uri))
        return FakeConnection()

    monkeypatch.setattr(daily.sqlite3, "connect", fake_connect)

    positions = daily.load_positions(str(db_path))

    assert calls
    assert calls[0][1] is True
    assert "mode=ro" in calls[0][0]
    assert "immutable=1" in calls[0][0]
    assert positions["US.AAPL"].cost_price == 100.0


def test_select_expiries_and_atm_pair() -> None:
    expiries = pd.DataFrame(
        [
            {"strike_time": "2026-07-17", "option_expiry_date_distance": 40},
            {"strike_time": "2026-06-19", "option_expiry_date_distance": 10},
            {"strike_time": "2026-06-26", "option_expiry_date_distance": 17},
        ],
    )
    chain = pd.DataFrame(
        [
            {"code": "US.C105", "option_type": "CALL", "strike_price": 105.0},
            {"code": "US.P105", "option_type": "PUT", "strike_price": 105.0},
            {"code": "US.C100", "option_type": "CALL", "strike_price": 100.0},
            {"code": "US.P100", "option_type": "PUT", "strike_price": 100.0},
        ],
    )

    assert daily.select_expiries(expiries, 2) == ["2026-06-19", "2026-06-26"]
    call, put = daily.select_atm_pair(chain, 103.0)
    assert call["code"] == "US.C105"
    assert put["code"] == "US.P105"


def test_find_first_key_parses_nested_lark_receipt() -> None:
    receipt = {
        "stdout": {
            "data": {
                "file": {"file_token": "boxcn_token", "url": "https://feishu/doc"},
                "message": {"message_id": "om_123"},
            },
        },
    }

    assert daily.find_first_key(receipt, {"file_token"}) == "boxcn_token"
    assert daily.find_first_key(receipt, {"message_id"}) == "om_123"
    assert daily.find_first_key(receipt, {"url"}) == "https://feishu/doc"


def test_resolve_lark_command_resolves_windows_lark(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        if name == "lark-cli.cmd":
            return r"C:\Users\MrLee\AppData\Roaming\npm\lark-cli.cmd"
        return None

    monkeypatch.setattr(daily.shutil, "which", fake_which)

    command = daily.resolve_lark_command(["lark-cli", "im", "+messages-send"])

    assert command[0] in {"powershell", r"C:\Users\MrLee\AppData\Roaming\npm\lark-cli.cmd"}
    assert command[-2:] == ["im", "+messages-send"]


def test_build_lark_message_text_contains_takeaways(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# 美股日报 2026-06-05\n\n"
        "## 30秒结论\n\n"
        "- 主策略低风险候选: US.AAPL\n"
        "- 主策略卖出/高风险: US.NASA\n\n"
        "## 指数行情\n",
        encoding="utf-8",
    )

    text = daily.build_lark_message_text(
        daily.date.fromisoformat("2026-06-05"),
        summary,
        "https://www.feishu.cn/file/demo",
    )

    assert text.startswith("美股日报 2026-06-05")
    assert "US.AAPL" in text
    assert "https://www.feishu.cn/file/demo" in text


def test_text_message_content_preserves_newlines() -> None:
    content = daily.text_message_content("第一行\n第二行")

    assert json.loads(content) == {"text": "第一行\n第二行"}


def test_build_lark_summary_card_contains_doc_link(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# 美股日报 2026-06-05\n\n"
        "## 30秒结论\n\n"
        "- 主策略低风险候选: US.AAPL\n"
        "- 主策略卖出/高风险: US.NASA\n\n"
        "## 指数行情\n",
        encoding="utf-8",
    )

    card = daily.build_lark_summary_card(
        daily.date.fromisoformat("2026-06-05"),
        summary,
        "https://www.feishu.cn/file/demo",
    )

    assert card["header"]["title"]["content"] == "美股日报 2026-06-05"
    assert "US.AAPL" in card["elements"][0]["text"]["content"]
    assert card["elements"][-1]["actions"][0]["url"] == "https://www.feishu.cn/file/demo"


def test_write_lark_card_files_uses_interactive_body(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# 美股日报 2026-06-05\n\n## 30秒结论\n\n- BUY: US.AAPL\n",
        encoding="utf-8",
    )
    paths = daily.build_paths(daily.date.fromisoformat("2026-06-05"), tmp_path)

    daily.write_lark_card_files(
        paths,
        daily.date.fromisoformat("2026-06-05"),
        "oc_demo",
        "https://www.feishu.cn/file/demo",
    )

    body = json.loads(paths.lark_send_body_json.read_text(encoding="utf-8"))
    card = json.loads(body["content"])
    assert body["receive_id"] == "oc_demo"
    assert body["msg_type"] == "interactive"
    assert card["elements"][-1]["actions"][0]["url"] == "https://www.feishu.cn/file/demo"


def test_verify_lark_card_readback_requires_interactive_and_link() -> None:
    receipt = {
        "stdout": {
            "data": {
                "items": [
                    {
                        "msg_type": "interactive",
                        "body": {
                            "content": "美股日报 2026-06-05 https://www.feishu.cn/file/demo",
                        },
                    },
                ],
            },
        },
    }

    daily.verify_lark_card_readback(
        receipt,
        "https://www.feishu.cn/file/demo",
        daily.date.fromisoformat("2026-06-05"),
    )


class FakeQuoteContext:
    """Fake quote context for read-only report tests."""

    def __init__(self) -> None:
        self.trade_methods_called = False

    def get_market_snapshot(self, code_list: list[str]):
        if code_list and code_list[0].startswith("US.OPT"):
            rows = []
            for code in code_list:
                rows.append(
                    {
                        "code": code,
                        "option_implied_volatility": 55.0
                        if code.endswith("C")
                        else 70.0,
                        "option_open_interest": 1000 if code.endswith("C") else 1500,
                    },
                )
            return ft.RET_OK, pd.DataFrame(rows)
        code = code_list[0]
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "code": code,
                    "last_price": 101.0,
                    "turnover": 5_000_000.0,
                    "turnover_rate": 2.0,
                },
            ],
        )

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype=ft.KLType.K_DAY,
        max_count: int = 100,
    ):
        rows = []
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        dates = pd.date_range("2026-06-01", periods=len(closes), freq="B")
        for day, close in zip(dates, closes, strict=True):
            if day.strftime("%Y-%m-%d") > end:
                continue
            rows.append(
                {
                    "code": code,
                    "time_key": day.strftime("%Y-%m-%d"),
                    "open": close - 1,
                    "high": close + 1,
                    "low": close - 2,
                    "close": close,
                    "change_rate": 1.0,
                    "turnover": 10_000_000.0,
                    "turnover_rate": 1.5,
                    "volume": 100_000.0,
                },
            )
        return ft.RET_OK, pd.DataFrame(rows), None

    def get_capital_distribution(self, code: str):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "capital_in_super": 100.0,
                    "capital_in_big": 100.0,
                    "capital_out_super": 50.0,
                    "capital_out_big": 50.0,
                },
            ],
        )

    def get_option_expiration_date(self, code: str):
        return ft.RET_OK, pd.DataFrame(
            [
                {"strike_time": "2026-06-19", "option_expiry_date_distance": 10},
                {"strike_time": "2026-06-26", "option_expiry_date_distance": 17},
            ],
        )

    def get_option_chain(self, code: str, start: str, end: str):
        return ft.RET_OK, pd.DataFrame(
            [
                {"code": "US.OPT100C", "option_type": "CALL", "strike_price": 105.0},
                {"code": "US.OPT100P", "option_type": "PUT", "strike_price": 105.0},
            ],
        )

    def get_daily_short_volume(self, code: str):
        return ft.RET_OK, pd.DataFrame([{"short_volume": 1000, "volume": 10000}])

    def get_short_interest(self, code: str):
        return ft.RET_OK, pd.DataFrame([{"short_interest": 1.0, "days_to_cover": 1.0}])


def test_analyze_options_retries_transient_chain_failures(monkeypatch) -> None:
    monkeypatch.setattr(daily, "OPTION_API_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(daily, "OPTION_API_SUCCESS_PAUSE_SECONDS", 0.0)

    class FlakyOptionContext:
        """Option quote context that fails once like an OpenD frequency spike."""

        def __init__(self) -> None:
            self.chain_calls = 0

        def get_option_expiration_date(self, code: str):
            return ft.RET_OK, pd.DataFrame(
                [{"strike_time": "2026-06-12", "option_expiry_date_distance": 5}],
            )

        def get_option_chain(self, code: str, start: str, end: str):
            self.chain_calls += 1
            if self.chain_calls == 1:
                return ft.RET_ERROR, "frequency limit"
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "code": "US.AMD260612C100000",
                        "option_type": "CALL",
                        "strike_time": "2026-06-12",
                        "strike_price": 100.0,
                    },
                    {
                        "code": "US.AMD260612P100000",
                        "option_type": "PUT",
                        "strike_time": "2026-06-12",
                        "strike_price": 100.0,
                    },
                ],
            )

        def get_market_snapshot(self, code_list: list[str]):
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "code": code,
                        "option_implied_volatility": 40.0 if "C" in code else 55.0,
                        "option_open_interest": 1000 if "C" in code else 2000,
                    }
                    for code in code_list
                ],
            )

    ctx = FlakyOptionContext()
    rows, errors = daily.analyze_options(
        ctx,
        ("US.AMD",),
        [{"code": "US.AMD", "bar": {"close": 100.0}}],
        daily.date.fromisoformat("2026-06-05"),
    )

    assert rows[0]["gap"] == ""
    assert rows[0]["call_code"] == "US.AMD260612C100000"
    assert rows[0]["put_code"] == "US.AMD260612P100000"
    assert rows[0]["risk_label"] != "N/A"
    assert ctx.chain_calls == 2
    assert errors == []


def test_fetch_option_chains_keeps_exact_nonempty_response(monkeypatch) -> None:
    monkeypatch.setattr(daily, "OPTION_API_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(daily, "OPTION_API_SUCCESS_PAUSE_SECONDS", 0.0)

    class ExactOnlyContext:
        """Option context whose exact query returns rows with non-standard strike_time."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def get_option_chain(self, code: str, start: str, end: str):
            self.calls.append((start, end))
            if len(self.calls) == 1:
                return ft.RET_OK, pd.DataFrame()
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "code": "US.TSLA260608C390000",
                        "option_type": "CALL",
                        "strike_time": "",
                        "strike_price": 390.0,
                    },
                    {
                        "code": "US.TSLA260608P390000",
                        "option_type": "PUT",
                        "strike_time": "",
                        "strike_price": 390.0,
                    },
                ],
            )

    chains, errors = daily.fetch_option_chains(
        ExactOnlyContext(),
        "US.TSLA",
        ["2026-06-08"],
    )

    assert "2026-06-08" in chains
    assert len(chains["2026-06-08"]) == 2
    assert errors


def test_run_report_no_send_uses_read_only_quote_context(tmp_path: Path, monkeypatch) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("US.AAPL\n", encoding="utf-8")
    output = tmp_path / "out"
    monkeypatch.setenv("WATCHLIST_FILE", str(watchlist))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing_positions.db"))
    args = daily.parse_args(
        [
            "--date",
            "2026-06-05",
            "--no-send",
            "--watchlist",
            str(watchlist),
            "--output-dir",
            str(output),
        ],
    )

    payload, lark_result, paths = daily.run_report(args, quote_ctx=FakeQuoteContext())

    assert lark_result is None
    assert payload["summary"]["target_date"] == "2026-06-05"
    assert paths.summary_md.exists()
    assert paths.report_json.exists()
    saved = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert saved["stocks"][0]["code"] == "US.AAPL"
