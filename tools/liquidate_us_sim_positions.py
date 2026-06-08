# -*- coding: utf-8 -*-
"""清空指定 moomoo 美股模拟账户持仓。

该工具只允许在 `TrdEnv.SIMULATE` 下操作，并要求显式传入确认参数。
默认目标账户为当前美股模拟账户 `1676392`，市场口径固定为 US。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import moomoo as ft


FINAL_ORDER_MARKERS = (
    "FILLED_ALL",
    "CANCELLED",
    "FAILED",
    "DISABLED",
    "DELETED",
)


@dataclass
class OrderResult:
    """单笔清仓订单回执。"""

    code: str
    requested_qty: float
    order_id: str
    submit_ok: bool
    filled_qty: float
    fill_price: float
    status: str
    error: str = ""


def main() -> int:
    """执行 US SIM 账户持仓清理，并写入 JSON 回执。"""

    args = _parse_args()
    if not args.confirm_us_sim_liquidate:
        raise SystemExit("必须传入 --confirm-us-sim-liquidate 才会执行模拟清仓")

    output_dir = _output_dir(args.output_dir)
    started_at = datetime.now().isoformat(timespec="seconds")
    ctx = ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.US,
        host=args.host,
        port=args.port,
    )
    try:
        account = _validate_sim_account(ctx, args.acc_id)
        before_account = _query_account(ctx, args.acc_id)
        before_positions = _query_positions(ctx, args.acc_id)
        cancel_receipts = _cancel_open_orders(ctx, args.acc_id)
        _assert_no_open_orders(ctx, args.acc_id)

        orders = _liquidate_positions(ctx, args, before_positions)
        after_positions = _wait_positions_settle(
            ctx,
            args.acc_id,
            args.final_wait_s,
            args.poll_s,
        )
        after_account = _query_account(ctx, args.acc_id)
        remaining = _positive_us_positions(after_positions)

        cleared_rows = 0
        if not remaining and args.clear_local_store:
            cleared_rows = _clear_local_positions(args.positions_db)

        result = {
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "account": account,
            "before_account": before_account,
            "after_account": after_account,
            "before_positions": before_positions,
            "after_positions": after_positions,
            "cancel_receipts": cancel_receipts,
            "orders": [asdict(order) for order in orders],
            "remaining_positions": remaining,
            "cleared_local_rows": cleared_rows,
            "positions_db": str(args.positions_db),
        }
        receipt_path = output_dir / "liquidation_result.json"
        receipt_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "receipt": str(receipt_path),
                    "orders": len(orders),
                    "filled_orders": sum(1 for order in orders if order.filled_qty > 0),
                    "remaining_positions": len(remaining),
                    "cleared_local_rows": cleared_rows,
                },
                ensure_ascii=False,
            )
        )
        failed = [order for order in orders if not order.submit_ok or order.filled_qty <= 0]
        return 2 if remaining or failed else 0
    finally:
        ctx.close()


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--acc-id", type=int, default=1676392)
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--poll-s", type=float, default=1.0)
    parser.add_argument("--order-sleep-s", type=float, default=2.2)
    parser.add_argument("--final-wait-s", type=float, default=20.0)
    parser.add_argument(
        "--positions-db",
        type=Path,
        default=Path("us_strategy/positions.db"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="回执目录；默认写入 report/outputs/us_sim_liquidation_<timestamp>",
    )
    parser.add_argument("--clear-local-store", action="store_true")
    parser.add_argument("--confirm-us-sim-liquidate", action="store_true")
    return parser.parse_args()


def _output_dir(path: Path | None) -> Path:
    """返回并创建本次回执目录。"""

    if path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path("report") / "outputs" / f"us_sim_liquidation_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_sim_account(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
) -> dict[str, Any]:
    """校验目标账户必须是 US 模拟账户。"""

    ret, df = ctx.get_acc_list()
    if ret != ft.RET_OK:
        raise RuntimeError(f"get_acc_list 失败: {df}")

    rows = _df_records(df)
    matches = [
        row for row in rows if _same_int(row.get("acc_id"), acc_id)
    ]
    if not matches:
        raise RuntimeError(f"未找到账户 acc_id={acc_id}")

    account = matches[0]
    env_text = str(account.get("trd_env", "")).upper()
    account_text = json.dumps(account, ensure_ascii=False, default=str).upper()
    if "REAL" in env_text or "REAL" in account_text and "SIMULATE" not in account_text:
        raise RuntimeError(f"拒绝操作疑似实盘账户: {account}")
    if "SIMULATE" not in account_text and "SIM" not in account_text:
        raise RuntimeError(f"账户未明确标识为模拟盘，拒绝操作: {account}")
    if "US" not in account_text:
        raise RuntimeError(f"账户未明确包含 US 交易权限，拒绝操作: {account}")
    return account


def _query_account(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
) -> list[dict[str, Any]]:
    """查询目标模拟账户资金信息。"""

    ret, df = ctx.accinfo_query(
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
        refresh_cache=True,
        currency="USD",
    )
    if ret != ft.RET_OK:
        raise RuntimeError(f"accinfo_query 失败: {df}")
    return _df_records(df)


def _query_positions(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
) -> list[dict[str, Any]]:
    """查询目标模拟账户 US 持仓。"""

    ret, df = ctx.position_list_query(
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
        refresh_cache=True,
        position_market=ft.TrdMarket.US,
        currency="USD",
    )
    if ret != ft.RET_OK:
        raise RuntimeError(f"position_list_query 失败: {df}")
    return [
        row for row in _df_records(df)
        if str(row.get("code", "")).startswith("US.")
    ]


def _cancel_open_orders(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
) -> list[dict[str, Any]]:
    """撤销账户中仍处于活动状态的 US 订单。"""

    ret, df = ctx.order_list_query(
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
        refresh_cache=True,
        order_market=ft.TrdMarket.US,
    )
    if ret != ft.RET_OK:
        raise RuntimeError(f"order_list_query 失败: {df}")

    receipts: list[dict[str, Any]] = []
    for row in _df_records(df):
        code = str(row.get("code", ""))
        order_id = row.get("order_id", "")
        status = str(row.get("order_status", ""))
        if not code.startswith("US.") or not order_id or _is_final_order(status):
            continue
        cancel_ret, cancel_data = ctx.modify_order(
            ft.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=ft.TrdEnv.SIMULATE,
            acc_id=acc_id,
        )
        receipts.append(
            {
                "code": code,
                "order_id": order_id,
                "previous_status": status,
                "cancel_ok": cancel_ret == ft.RET_OK,
                "cancel_data": _jsonable(cancel_data),
            }
        )
        time.sleep(0.2)
    return receipts


def _assert_no_open_orders(ctx: ft.OpenSecTradeContext, acc_id: int) -> None:
    """确认没有未完结的 US 订单。"""

    ret, df = ctx.order_list_query(
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
        refresh_cache=True,
        order_market=ft.TrdMarket.US,
    )
    if ret != ft.RET_OK:
        raise RuntimeError(f"order_list_query 复核失败: {df}")

    active = []
    for row in _df_records(df):
        code = str(row.get("code", ""))
        status = str(row.get("order_status", ""))
        if code.startswith("US.") and not _is_final_order(status):
            active.append(row)
    if active:
        raise RuntimeError(f"仍存在未完结 US 订单，拒绝继续清仓: {active}")


def _liquidate_positions(
    ctx: ft.OpenSecTradeContext,
    args: argparse.Namespace,
    positions: list[dict[str, Any]],
) -> list[OrderResult]:
    """按持仓市值从大到小卖出所有可卖 US 持仓。"""

    orders: list[OrderResult] = []
    rows = sorted(
        _positive_us_positions(positions),
        key=lambda row: _positive_float(row.get("market_val")),
        reverse=True,
    )
    for row in rows:
        code = str(row.get("code", ""))
        qty = _sellable_qty(row)
        if qty <= 0:
            continue
        ret, data = ctx.place_order(
            price=0,
            qty=qty,
            code=code,
            trd_side=ft.TrdSide.SELL,
            order_type=ft.OrderType.MARKET,
            trd_env=ft.TrdEnv.SIMULATE,
            acc_id=args.acc_id,
        )
        if ret != ft.RET_OK:
            orders.append(
                OrderResult(
                    code=code,
                    requested_qty=qty,
                    order_id="",
                    submit_ok=False,
                    filled_qty=0.0,
                    fill_price=0.0,
                    status="SUBMIT_FAILED",
                    error=str(data),
                )
            )
            time.sleep(args.order_sleep_s)
            continue

        order_id = str(_extract_first(data, "order_id", ""))
        fill_price, filled_qty, status = _poll_order(ctx, args, order_id, qty)
        orders.append(
            OrderResult(
                code=code,
                requested_qty=qty,
                order_id=order_id,
                submit_ok=True,
                filled_qty=filled_qty,
                fill_price=fill_price,
                status=status,
            )
        )
        time.sleep(args.order_sleep_s)
    return orders


def _poll_order(
    ctx: ft.OpenSecTradeContext,
    args: argparse.Namespace,
    order_id: str,
    want_qty: float,
) -> tuple[float, float, str]:
    """轮询单笔订单直到成交、终态或超时。"""

    if not order_id:
        return 0.0, 0.0, "MISSING_ORDER_ID"

    deadline = time.monotonic() + args.timeout_s
    fill_price = 0.0
    filled_qty = 0.0
    status = "SUBMITTED"
    while time.monotonic() < deadline:
        price, qty, latest_status = _query_order_once(ctx, args.acc_id, order_id)
        if latest_status and latest_status != "ORDER_QUERY_UNAVAILABLE":
            status = latest_status
        if price > 0:
            fill_price = price
        if qty > filled_qty:
            filled_qty = qty
        if _is_filled(status) or filled_qty >= want_qty:
            return fill_price, filled_qty, status
        if latest_status != "ORDER_QUERY_UNAVAILABLE" and _is_final_order(status):
            return fill_price, filled_qty, status
        time.sleep(args.poll_s)

    cancel_status = _cancel_order(ctx, args.acc_id, order_id)
    price, qty, latest_status = _query_order_once(ctx, args.acc_id, order_id)
    if price > 0:
        fill_price = price
    if qty > filled_qty:
        filled_qty = qty
    status = latest_status or status
    return fill_price, filled_qty, f"TIMEOUT:{status};{cancel_status}"


def _query_order_once(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
    order_id: str,
) -> tuple[float, float, str]:
    """读取一次订单状态。"""

    ret, df = ctx.order_list_query(
        order_id=order_id,
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
        refresh_cache=True,
        order_market=ft.TrdMarket.US,
    )
    if ret != ft.RET_OK or df.empty:
        return 0.0, 0.0, "ORDER_QUERY_UNAVAILABLE"
    row = _df_records(df)[0]
    return (
        _positive_float(row.get("dealt_avg_price")),
        _positive_float(row.get("dealt_qty")),
        str(row.get("order_status", "")),
    )


def _cancel_order(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
    order_id: str,
) -> str:
    """撤销超时订单，并返回撤单状态。"""

    ret, data = ctx.modify_order(
        ft.ModifyOrderOp.CANCEL,
        order_id=order_id,
        qty=0,
        price=0,
        trd_env=ft.TrdEnv.SIMULATE,
        acc_id=acc_id,
    )
    if ret == ft.RET_OK:
        return "CANCEL_SENT"
    return f"CANCEL_FAILED:{data}"


def _clear_local_positions(db_path: Path) -> int:
    """删除本地策略持仓缓存，不影响 signal_log。"""

    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        count = int(row[0] if row else 0)
        conn.execute("DELETE FROM positions")
    return count


def _wait_positions_settle(
    ctx: ft.OpenSecTradeContext,
    acc_id: int,
    timeout_s: float,
    poll_s: float,
) -> list[dict[str, Any]]:
    """等待券商持仓刷新到正股数量为 0。"""

    deadline = time.monotonic() + max(0.0, timeout_s)
    positions = _query_positions(ctx, acc_id)
    while _positive_us_positions(positions) and time.monotonic() < deadline:
        time.sleep(max(0.5, poll_s))
        positions = _query_positions(ctx, acc_id)
    return positions


def _positive_us_positions(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """过滤出数量大于 0 的 US 持仓。"""

    result = []
    for row in positions:
        code = str(row.get("code", ""))
        if code.startswith("US.") and _positive_float(row.get("qty")) > 0:
            result.append(row)
    return result


def _sellable_qty(row: dict[str, Any]) -> float:
    """读取可卖数量，当前美股模拟账户按整数股卖出。"""

    qty = _positive_float(row.get("can_sell_qty"))
    if qty <= 0:
        qty = _positive_float(row.get("qty"))
    if qty <= 0:
        return 0.0
    rounded = math.floor(qty)
    return float(rounded) if rounded > 0 else 0.0


def _df_records(df: Any) -> list[dict[str, Any]]:
    """将 pandas DataFrame 转成可 JSON 序列化的行列表。"""

    if df is None or getattr(df, "empty", True):
        return []
    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        records.append({
            str(key): _jsonable(value) for key, value in row.to_dict().items()
        })
    return records


def _jsonable(value: Any) -> Any:
    """转换 SDK/pandas 值，方便写入 JSON。"""

    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _extract_first(data: Any, column: str, default: Any) -> Any:
    """从 SDK 返回的 DataFrame/对象中读取首个字段。"""

    if hasattr(data, "empty") and not data.empty:
        try:
            return data[column].iloc[0]
        except (KeyError, TypeError, IndexError):
            return default
    if isinstance(data, dict):
        return data.get(column, default)
    return default


def _positive_float(value: Any) -> float:
    """返回正浮点数，空值和非法值按 0 处理。"""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _same_int(value: Any, expected: int) -> bool:
    """宽松比较 SDK 返回的账户 ID。"""

    try:
        return int(value) == expected
    except (TypeError, ValueError):
        return False


def _is_final_order(status: object) -> bool:
    """判断订单是否已经进入终态。"""

    text = str(status).upper()
    return any(marker in text for marker in FINAL_ORDER_MARKERS)


def _is_filled(status: object) -> bool:
    """判断订单是否全部成交。"""

    return "FILLED_ALL" in str(status).upper()


if __name__ == "__main__":
    raise SystemExit(main())
