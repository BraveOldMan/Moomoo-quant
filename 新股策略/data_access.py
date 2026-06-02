# -*- coding: utf-8 -*-
"""统一行情/交易数据访问层。

职责：
  1. TTL 缓存：snapshot / 资金分布 / K 线 / 持仓 / 账户，避免对同一标的
     在推送 + 轮询双重触发下重复请求。
  2. 令牌桶限流：moomoo OpenD 约 30 次/30 秒，统一在此节流，防止撞频。
  3. 线程安全：缓存与限流器均加锁，可被多线程共享。

所有方法保持与原生 SDK 相同的 (ret_code, data) 返回形态，便于调用方迁移。
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import moomoo as ft

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class _TokenBucket:
    """滑动窗口限流：window 秒内最多 limit 次调用，超出则阻塞等待。"""

    def __init__(self, limit: int, window_s: float):
        self._limit = max(1, limit)
        self._window = window_s
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self._window:
                self._calls.popleft()
            if len(self._calls) >= self._limit:
                sleep_for = self._window - (now - self._calls[0]) + 0.01
                if sleep_for > 0:
                    logger.debug("限流等待 %.2fs", sleep_for)
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class DataAccess:
    """带缓存与限流的行情/交易数据门面。"""

    def __init__(self, quote_ctx, trade_ctx, config):
        self._quote = quote_ctx
        self._trade = trade_ctx
        self._cfg = config
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._bucket = _TokenBucket(config.api_rate_limit, config.api_rate_window_s)
        self._trd_env = (
            ft.TrdEnv.REAL if config.trd_env == "REAL" else ft.TrdEnv.SIMULATE
        )

    # ── 缓存核心 ────────────────────────────────────────────────────────
    def _cached(self, key: str, ttl: float, fetch: Callable[[], tuple]) -> tuple:
        """fetch 返回 (ret, data...)；仅在 ret==RET_OK 时缓存。"""
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value

        self._bucket.acquire()
        result = fetch()
        if result and result[0] == ft.RET_OK:
            with self._cache_lock:
                self._cache[key] = _CacheEntry(result, now + ttl)
        return result

    def invalidate(self, *prefixes: str) -> None:
        """按前缀失效缓存（如下单后失效 position/accinfo）。空参清空全部。"""
        with self._cache_lock:
            if not prefixes:
                self._cache.clear()
                return
            for k in [k for k in self._cache if k.startswith(prefixes)]:
                self._cache.pop(k, None)

    # ── 行情 ────────────────────────────────────────────────────────────
    def get_market_snapshot(self, code: str) -> tuple:
        return self._cached(
            f"snapshot:{code}",
            self._cfg.snapshot_cache_ttl_s,
            lambda: self._quote.get_market_snapshot([code]),
        )

    def get_capital_distribution(self, code: str) -> tuple:
        return self._cached(
            f"capital:{code}",
            self._cfg.capital_cache_ttl_s,
            lambda: self._quote.get_capital_distribution(code),
        )

    def get_broker_queue(self, code: str) -> tuple:
        # 经纪队列实时性要求高，用 snapshot TTL
        return self._cached(
            f"broker:{code}",
            self._cfg.snapshot_cache_ttl_s,
            lambda: self._quote.get_broker_queue(code),
        )

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype=ft.KLType.K_DAY,
        max_count: int = 100,
    ) -> tuple:
        key = f"kline:{code}:{ktype}:{start}:{end}:{max_count}"
        return self._cached(
            key,
            self._cfg.kline_cache_ttl_s,
            lambda: self._quote.request_history_kline(
                code, start=start, end=end, ktype=ktype, max_count=max_count
            ),
        )

    def get_capital_flow(self, code: str, period_type, start: str, end: str) -> tuple:
        key = f"capflow:{code}:{period_type}:{start}:{end}"
        return self._cached(
            key,
            self._cfg.capital_cache_ttl_s,
            lambda: self._quote.get_capital_flow(
                code, period_type=period_type, start=start, end=end
            ),
        )

    # ── 交易 ────────────────────────────────────────────────────────────
    def position_list_query(self) -> tuple:
        return self._cached(
            "position",
            self._cfg.position_cache_ttl_s,
            lambda: self._trade.position_list_query(trd_env=self._trd_env),
        )

    def accinfo_query(self) -> tuple:
        return self._cached(
            "accinfo",
            self._cfg.position_cache_ttl_s,
            lambda: self._trade.accinfo_query(trd_env=self._trd_env),
        )

    def on_order_changed(self) -> None:
        """下单/成交后调用：失效持仓与账户缓存，确保下一次读到最新状态。"""
        self.invalidate("position", "accinfo")
