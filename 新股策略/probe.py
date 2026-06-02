# -*- coding: utf-8 -*-
"""数据可得性探针。

策略有效性高度依赖少数几个字段在 **美股** 上的可得性，尤其：
  - get_market_snapshot 的 turnover_rate / turnover（换手率因子，权重高）
  - get_capital_distribution（机构资金分布，核心因子，权重最高）
  - get_broker_queue（经纪队列，美股通常不可用）

本脚本连 OpenD 实测上述接口对给定美股的返回，输出每个核心因子能否落地，
帮助在上线前确认策略是否成立（而非空跑）。

用法：
    python -m 新股策略.probe US.RDDT US.ARM
    python 新股策略/probe.py            # 默认探测一批近期美股
"""

import sys

import moomoo as ft

from .config import StrategyConfig

_DEFAULT_CODES = ["US.AAPL", "US.NVDA"]


def _status(ok: bool) -> str:
    return "✅ 可用" if ok else "❌ 不可用"


def _check_snapshot(quote_ctx, code: str) -> dict:
    ret, df = quote_ctx.get_market_snapshot([code])
    if ret != ft.RET_OK or df.empty:
        return {"ok": False, "detail": str(df)}
    row = df.iloc[0]
    fields = {}
    for f in ("last_price", "turnover", "turnover_rate", "lot_size", "volume"):
        val = row.get(f) if hasattr(row, "get") else None
        fields[f] = val
    has_turnover = bool(row.get("turnover_rate") or row.get("turnover"))
    return {"ok": True, "fields": fields, "turnover_usable": has_turnover}


def _check_capital_distribution(quote_ctx, code: str) -> dict:
    try:
        ret, df = quote_ctx.get_capital_distribution(code)
    except Exception as exc:  # 美股可能直接抛接口不支持
        return {"ok": False, "detail": f"异常: {exc}"}
    if ret != ft.RET_OK or df.empty:
        return {"ok": False, "detail": str(df)}
    row = df.iloc[0]
    needed = (
        "capital_in_super",
        "capital_in_big",
        "capital_out_super",
        "capital_out_big",
    )
    present = {f: row.get(f) for f in needed}
    usable = all(present[f] is not None for f in needed)
    return {"ok": True, "fields": present, "usable": usable}


def _check_broker_queue(quote_ctx, code: str) -> dict:
    try:
        ret, bid, ask = quote_ctx.get_broker_queue(code)
    except Exception as exc:
        return {"ok": False, "detail": f"异常: {exc}"}
    if ret != ft.RET_OK:
        return {"ok": False, "detail": str(bid)}
    return {"ok": True, "bid_rows": len(bid), "ask_rows": len(ask)}


def _check_kline(quote_ctx, code: str) -> dict:
    from datetime import date, timedelta

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=60)).isoformat()
    ret, df, _ = quote_ctx.request_history_kline(
        code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=30
    )
    if ret != ft.RET_OK or df.empty:
        return {"ok": False, "detail": str(df)}
    cols = set(df.columns)
    needed = {"close", "high", "low", "volume"}
    return {"ok": True, "rows": len(df), "has_ohlcv": needed.issubset(cols)}


def probe(codes: list[str]) -> None:
    cfg = StrategyConfig.from_env()
    quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    try:
        for code in codes:
            print("\n" + "=" * 56)
            print(f"探测 {code}")
            print("=" * 56)

            snap = _check_snapshot(quote_ctx, code)
            print(f"[snapshot]            {_status(snap['ok'])}")
            if snap["ok"]:
                print(f"  字段: {snap['fields']}")
                print(f"  换手率因子(turnover): {_status(snap['turnover_usable'])}")
            else:
                print(f"  {snap['detail']}")

            cap = _check_capital_distribution(quote_ctx, code)
            print(
                f"[capital_distribution] {_status(cap['ok'] and cap.get('usable', False))}"
            )
            if cap["ok"]:
                print(f"  字段: {cap['fields']}")
            else:
                print(f"  {cap['detail']}")
            if not (cap["ok"] and cap.get("usable")):
                print("  ⚠ 核心因子缺失 → 策略将自动降级为 turnover+momentum(+RS)")

            kl = _check_kline(quote_ctx, code)
            print(
                f"[history_kline]        {_status(kl['ok'] and kl.get('has_ohlcv', False))}"
            )
            print(f"  {kl}")

            bq = _check_broker_queue(quote_ctx, code)
            print(
                f"[broker_queue]         {_status(bq['ok'])}  (美股通常不可用，默认关闭)"
            )
            if not bq["ok"]:
                print(f"  {bq['detail']}")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    codes = sys.argv[1:] or _DEFAULT_CODES
    probe(codes)
