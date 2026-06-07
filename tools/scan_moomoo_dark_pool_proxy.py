from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import moomoo as ft

from dark_pool_proxy import DarkPoolProxyConfig, DarkPoolProxyTracker


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the moomoo large-print proxy scanner."""

    parser = argparse.ArgumentParser(
        description=(
            "Scan moomoo realtime ticker rows for large-print proxy events. "
            "This is not a TRF dark-pool classifier."
        )
    )
    parser.add_argument("--codes", default="", help="Comma-separated moomoo codes.")
    parser.add_argument("--markets", default="US,HK", help="Comma-separated markets.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--rt-ticker-num", type=int, default=500)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--us-min-notional", type=float, default=100_000.0)
    parser.add_argument("--hk-min-notional", type=float, default=800_000.0)
    parser.add_argument("--alert-cooldown-s", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    """Run a read-only OpenD scanner and print JSON lines for fresh events."""

    args = parse_args()
    codes = _load_codes(args.codes, args.markets)
    if not codes:
        raise SystemExit("No codes provided and watchlists are empty.")

    tracker = DarkPoolProxyTracker(
        DarkPoolProxyConfig(
            us_min_notional=args.us_min_notional,
            hk_min_notional=args.hk_min_notional,
            alert_cooldown_s=args.alert_cooldown_s,
        )
    )
    quote_ctx = ft.OpenQuoteContext(host=args.host, port=args.port)
    try:
        ret, msg = quote_ctx.subscribe(
            codes,
            [ft.SubType.TICKER],
            subscribe_push=False,
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"subscribe failed: {msg}")
        deadline = (
            float("inf")
            if args.duration_seconds <= 0
            else time.monotonic() + args.duration_seconds
        )
        while time.monotonic() < deadline:
            for code in codes:
                ret, frame = quote_ctx.get_rt_ticker(code, args.rt_ticker_num)
                if ret != ft.RET_OK:
                    print(
                        json.dumps(
                            {"code": code, "error": str(frame)},
                            ensure_ascii=False,
                        )
                    )
                    continue
                for metrics in tracker.update(
                    frame,
                    market_date=_market_date_for_code(code),
                ):
                    if metrics.print_count > 0:
                        print(json.dumps(metrics.as_dict(), ensure_ascii=False))
            time.sleep(max(0.1, args.poll_interval))
    finally:
        try:
            quote_ctx.unsubscribe(codes, [ft.SubType.TICKER])
        except Exception:
            pass
        quote_ctx.close()


def _load_codes(raw_codes: str, raw_markets: str) -> list[str]:
    if raw_codes.strip():
        return _dedupe(code.strip() for code in raw_codes.split(",") if code.strip())
    markets = {item.strip().upper() for item in raw_markets.split(",") if item.strip()}
    codes: list[str] = []
    if "US" in markets:
        codes.extend(_read_watchlist(Path("us_strategy/watchlist.txt"), "US."))
    if "HK" in markets:
        codes.extend(_read_watchlist(Path("hk_strategy/watchlist.txt"), "HK."))
    return _dedupe(codes)


def _read_watchlist(path: Path, prefix: str) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean:
            continue
        out.extend(
            code.strip()
            for code in clean.split(",")
            if code.strip().startswith(prefix)
        )
    return out


def _dedupe(codes: object) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for code in codes:
        text = str(code)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _market_date_for_code(code: str) -> str:
    zone = "Asia/Hong_Kong" if code.startswith("HK.") else "America/New_York"
    return datetime.now(ZoneInfo(zone)).date().isoformat()


if __name__ == "__main__":
    main()
