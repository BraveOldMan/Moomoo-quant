# -*- coding: utf-8 -*-
"""数据可得性探针。

策略有效性高度依赖少数几个字段在 **港股** 上的可得性，尤其：
  - get_market_snapshot 的 turnover_rate / turnover（换手率因子，权重高）
  - get_capital_distribution（机构资金分布，核心因子，权重最高；港股通常可用）
  - get_broker_queue（经纪队列，港股可用但须先订阅 Broker 数据）

本脚本连 OpenD 实测上述接口对给定港股的返回，输出每个核心因子能否落地，
帮助在上线前确认策略是否成立（而非空跑）。

用法：
    python -m hk_strategy.probe HK.00700 HK.09988
    python hk_strategy/probe.py            # 默认探测一批港股蓝筹
"""

import sys
from datetime import timedelta

import moomoo as ft

from .clock import market_date
from .config import StrategyConfig

_DEFAULT_CODES = ["HK.00700", "HK.09988"]


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
    except Exception as exc:  # 接口不支持时可能直接抛异常
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
    today = market_date(StrategyConfig.market_timezone)
    end = today.isoformat()
    start = (today - timedelta(days=60)).isoformat()
    ret, df, _ = quote_ctx.request_history_kline(
        code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=30
    )
    if ret != ft.RET_OK or df.empty:
        return {"ok": False, "detail": str(df)}
    cols = set(df.columns)
    needed = {"close", "high", "low", "volume"}
    return {"ok": True, "rows": len(df), "has_ohlcv": needed.issubset(cols)}


def _check_intraday_flow(quote_ctx, code: str) -> dict:
    """日内机构资金流（intraday_flow 因子）。"""
    try:
        ret, df = quote_ctx.get_capital_flow(code, period_type=ft.PeriodType.INTRADAY)
    except Exception as exc:
        return {"ok": False, "detail": f"异常: {exc}"}
    if ret != ft.RET_OK or df.empty:
        return {"ok": False, "detail": str(df)}
    needed = {"super_in_flow", "big_in_flow"}
    return {"ok": True, "rows": len(df), "usable": needed.issubset(set(df.columns))}


def _check_short(quote_ctx, code: str) -> dict:
    """做空面（short 因子）：每日卖空比例 + 结算空头拥挤度。"""
    out: dict = {}
    try:
        r = quote_ctx.get_daily_short_volume(code)
        out["daily_short_volume"] = (
            r[0] == ft.RET_OK and not r[1].empty and "short_percent" in r[1].columns
        )
    except Exception as exc:
        out["daily_short_volume"] = False
        out["dsv_err"] = str(exc)
    try:
        r = quote_ctx.get_short_interest(code)
        out["short_interest"] = (
            r[0] == ft.RET_OK and not r[1].empty and "short_percent" in r[1].columns
        )
    except Exception as exc:
        out["short_interest"] = False
        out["si_err"] = str(exc)
    out["ok"] = bool(out.get("daily_short_volume") or out.get("short_interest"))
    return out


def _check_option_iv(quote_ctx, code: str) -> dict:
    """期权隐含信息（option_iv 因子）：到期日→链→ATM 合约 IV/greeks。"""
    try:
        ret, exp = quote_ctx.get_option_expiration_date(code=code)
    except Exception as exc:
        return {"ok": False, "detail": f"到期日异常: {exc}"}
    if ret != ft.RET_OK or exp.empty:
        return {"ok": False, "detail": "无期权（多数小盘新股上市初期无期权）"}
    expiry = str(exp.iloc[0]["strike_time"])
    try:
        ret2, chain = quote_ctx.get_option_chain(code, start=expiry, end=expiry)
    except Exception as exc:
        return {"ok": False, "detail": f"期权链异常: {exc}"}
    if ret2 != ft.RET_OK or chain.empty:
        return {"ok": False, "detail": "期权链为空"}
    opt_code = str(chain.iloc[0]["code"])
    ret3, snap = quote_ctx.get_market_snapshot([opt_code])
    has_iv = (
        ret3 == ft.RET_OK
        and not snap.empty
        and "option_implied_volatility" in snap.columns
    )
    return {
        "ok": True,
        "nearest_expiry": expiry,
        "strikes": len(chain),
        "has_iv": has_iv,
    }


def _check_microstructure(quote_ctx, code: str) -> dict:
    """盘中微观结构（order_flow / obi 因子）：需订阅 TICKER / ORDER_BOOK。"""
    import time

    try:
        ret, _ = quote_ctx.subscribe(
            [code], [ft.SubType.TICKER, ft.SubType.ORDER_BOOK], subscribe_push=False
        )
        if ret != ft.RET_OK:
            return {"ok": False, "detail": "订阅失败（额度/权限）"}
        time.sleep(1.5)
        out: dict = {}
        rt = quote_ctx.get_rt_ticker(code, 20)
        out["rt_ticker"] = rt[0] == ft.RET_OK and "ticker_direction" in rt[1].columns
        ob = quote_ctx.get_order_book(code, num=5)
        out["order_book"] = ob[0] == ft.RET_OK and isinstance(ob[1], dict)
        out["ok"] = bool(out.get("rt_ticker") and out.get("order_book"))
        return out
    except Exception as exc:
        return {"ok": False, "detail": f"异常: {exc}"}
    finally:
        try:
            quote_ctx.unsubscribe_all()
        except Exception:
            pass


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
                f"[broker_queue]         {_status(bq['ok'])}  (港股可用，但须先订阅 Broker 数据)"
            )
            if not bq["ok"]:
                print(f"  {bq['detail']}")

            # ── 扩展因子（默认关闭，校准后启用）────────────────────────
            print("  ── 扩展因子数据可得性 ──")

            flow = _check_intraday_flow(quote_ctx, code)
            print(
                f"[intraday_flow]        {_status(flow['ok'] and flow.get('usable', False))}"
            )
            if not flow["ok"]:
                print(f"  {flow.get('detail')}")

            sh = _check_short(quote_ctx, code)
            print(f"[short_metrics]        {_status(sh['ok'])}")
            print(
                f"  每日卖空: {_status(sh.get('daily_short_volume', False))}"
                f"  结算空头: {_status(sh.get('short_interest', False))}"
            )

            oiv = _check_option_iv(quote_ctx, code)
            print(
                f"[option_iv]            {_status(oiv['ok'] and oiv.get('has_iv', False))}"
            )
            if oiv["ok"]:
                print(
                    f"  最近到期: {oiv['nearest_expiry']}  行权价数: {oiv['strikes']}"
                    f"  IV字段: {_status(oiv.get('has_iv', False))}"
                )
            else:
                print(f"  {oiv.get('detail')}")

            ms = _check_microstructure(quote_ctx, code)
            print(
                f"[microstructure]       {_status(ms['ok'])}  (CVD/OBI，需实时订阅，仅RTH有效)"
            )
            if ms["ok"]:
                print(
                    f"  逐笔(CVD): {_status(ms.get('rt_ticker', False))}"
                    f"  盘口(OBI): {_status(ms.get('order_book', False))}"
                )
            else:
                print(f"  {ms.get('detail')}")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    codes = sys.argv[1:] or _DEFAULT_CODES
    probe(codes)
