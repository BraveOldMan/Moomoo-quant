# -*- coding: utf-8 -*-
import logging
import threading
from typing import Callable

import moomoo as ft

logger = logging.getLogger(__name__)

# 回调类型：(股票代码, 最新价) -> None
OnQuoteCallback = Callable[[str, float], None]


class _QuotePushHandler(ft.StockQuoteHandlerBase):
    def __init__(self, callback: OnQuoteCallback):
        super().__init__()
        self._callback = callback

    def on_recv_rsp(self, rsp_pb):
        ret, df = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            logger.debug("行情推送解析失败: %s", df)
            return ft.RET_ERROR, df

        for _, row in df.iterrows():
            try:
                self._callback(str(row["code"]), float(row["last_price"]))
            except Exception as exc:
                logger.error("行情回调异常 %s: %s", row.get("code"), exc)
        return ft.RET_OK, df


class RealtimeMonitor:
    def __init__(
        self,
        quote_ctx: ft.OpenQuoteContext,
        callback: OnQuoteCallback,
        extra_sub_types: list | None = None,
    ):
        self._ctx = quote_ctx
        self._callback = callback
        # QUOTE 必订；微观结构因子（CVD/OBI）启用时追加 TICKER/ORDER_BOOK
        self._sub_types = list(dict.fromkeys([ft.SubType.QUOTE, *(extra_sub_types or [])]))
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._ctx.set_handler(_QuotePushHandler(self._callback))
        self._ctx.start()
        logger.info("实时行情监控已启动")

    def subscribe(self, codes: list[str]) -> None:
        new_codes = [c for c in codes if c not in self._subscribed]
        if not new_codes:
            return
        ret, msg = self._ctx.subscribe(new_codes, self._sub_types)
        if ret != ft.RET_OK:
            logger.error("订阅失败 %s: %s", new_codes, msg)
            return
        with self._lock:
            self._subscribed.update(new_codes)
        logger.info("已订阅 %d 只新股: %s", len(new_codes), new_codes)

    def unsubscribe(self, codes: list[str]) -> None:
        targets = [c for c in codes if c in self._subscribed]
        if not targets:
            return
        ret, msg = self._ctx.unsubscribe(targets, self._sub_types)
        if ret != ft.RET_OK:
            logger.error("取消订阅失败 %s: %s", targets, msg)
            return
        with self._lock:
            self._subscribed.difference_update(targets)
        logger.info("已取消订阅: %s", targets)

    def current_subscriptions(self) -> list[str]:
        with self._lock:
            return list(self._subscribed)

    def stop(self) -> None:
        self._ctx.stop()
        logger.info("实时行情监控已停止")
