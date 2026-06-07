"""Moomoo API frequency limits used by this repository.

The table records official per-interface limits when the docs publish them.
For repository calls whose current page does not publish a specific limit, the
entry is marked as non-official and uses a conservative pacing fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


AUTHORITIES_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html"
)
QUOTE_QA_URL = "https://openapi.moomoo.com/moomoo-api-doc/en/qa/quote.html"
SNAPSHOT_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html"
)
HISTORY_KLINE_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/request-history-kline.html"
)
CAPITAL_FLOW_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-capital-flow.html"
)
CAPITAL_DISTRIBUTION_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-capital-distribution.html"
)
OPTION_EXPIRATION_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-expiration-date.html"
)
OPTION_CHAIN_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-chain.html"
)
POSITIONS_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-position-list.html"
)
FUNDS_URL = "https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-funds.html"
ORDERS_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-order-list.html"
)
PLACE_ORDER_URL = (
    "https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html"
)


@dataclass(frozen=True)
class MoomooRateLimitRule:
    """One moomoo interface frequency rule.

    limit and window_s describe max requests per window. A None limit means the
    call reads subscribed OpenD cache or the docs do not define request pacing.
    """

    interface: str
    limit: int | None
    window_s: float | None
    min_interval_s: float | None
    scope: str
    source_url: str
    official: bool = True
    request_size_limit: int | None = None
    requires_subscription: bool = False
    refresh_cache_only: bool = False
    note: str = ""

    def is_frequency_limited(self) -> bool:
        """Return whether this rule has a concrete request-window limit."""

        return self.limit is not None and self.window_s is not None


SNAPSHOT_RATE_LIMIT = MoomooRateLimitRule(
    interface="get_market_snapshot",
    limit=60,
    window_s=30.0,
    min_interval_s=0.5,
    scope="per interface",
    source_url=SNAPSHOT_URL,
    request_size_limit=400,
    note="HK BMP quote rights have lower single-request symbol caps.",
)
HISTORY_KLINE_RATE_LIMIT = MoomooRateLimitRule(
    interface="request_history_kline",
    limit=60,
    window_s=30.0,
    min_interval_s=0.5,
    scope="per interface, first page per stock",
    source_url=HISTORY_KLINE_URL,
    note="Limit applies to the first page per stock; page_req_key pages are exempt.",
)
CAPITAL_FLOW_RATE_LIMIT = MoomooRateLimitRule(
    interface="get_capital_flow",
    limit=30,
    window_s=30.0,
    min_interval_s=1.0,
    scope="per interface",
    source_url=CAPITAL_FLOW_URL,
)
CAPITAL_DISTRIBUTION_RATE_LIMIT = MoomooRateLimitRule(
    interface="get_capital_distribution",
    limit=30,
    window_s=30.0,
    min_interval_s=1.0,
    scope="per interface",
    source_url=CAPITAL_DISTRIBUTION_URL,
)
OPTION_EXPIRATION_RATE_LIMIT = MoomooRateLimitRule(
    interface="get_option_expiration_date",
    limit=60,
    window_s=30.0,
    min_interval_s=0.5,
    scope="per interface",
    source_url=OPTION_EXPIRATION_URL,
)
OPTION_CHAIN_RATE_LIMIT = MoomooRateLimitRule(
    interface="get_option_chain",
    limit=10,
    window_s=30.0,
    min_interval_s=3.0,
    scope="per interface",
    source_url=OPTION_CHAIN_URL,
    note="The incoming option-chain time span is limited to 30 days.",
)
TRADE_CACHE_10_PER_30 = MoomooRateLimitRule(
    interface="trade_cache_query",
    limit=10,
    window_s=30.0,
    min_interval_s=3.0,
    scope="per account ID",
    source_url=POSITIONS_URL,
    refresh_cache_only=True,
    note="Only restricted when refresh_cache=True; default strategy calls use OpenD cache.",
)
PLACE_ORDER_RATE_LIMIT = MoomooRateLimitRule(
    interface="place_order",
    limit=15,
    window_s=30.0,
    min_interval_s=0.02,
    scope="per account ID",
    source_url=PLACE_ORDER_URL,
)
OPEND_CACHE_REALTIME_RULE = MoomooRateLimitRule(
    interface="opend_cache_realtime_get",
    limit=None,
    window_s=None,
    min_interval_s=None,
    scope="OpenD subscribed cache",
    source_url=QUOTE_QA_URL,
    requires_subscription=True,
    note="After subscription, these getters read OpenD's pushed cache; subscription quota still applies.",
)
UNPUBLISHED_SERVER_REQUEST_RULE = MoomooRateLimitRule(
    interface="unpublished_server_request",
    limit=30,
    window_s=30.0,
    min_interval_s=1.0,
    scope="repository conservative fallback",
    source_url=AUTHORITIES_URL,
    official=False,
    note="No page-specific Interface Limitations text was found; keep conservative pacing.",
)


MOOMOO_RATE_LIMITS: dict[str, MoomooRateLimitRule] = {
    SNAPSHOT_RATE_LIMIT.interface: SNAPSHOT_RATE_LIMIT,
    HISTORY_KLINE_RATE_LIMIT.interface: HISTORY_KLINE_RATE_LIMIT,
    CAPITAL_FLOW_RATE_LIMIT.interface: CAPITAL_FLOW_RATE_LIMIT,
    CAPITAL_DISTRIBUTION_RATE_LIMIT.interface: CAPITAL_DISTRIBUTION_RATE_LIMIT,
    OPTION_EXPIRATION_RATE_LIMIT.interface: OPTION_EXPIRATION_RATE_LIMIT,
    OPTION_CHAIN_RATE_LIMIT.interface: OPTION_CHAIN_RATE_LIMIT,
    "position_list_query": replace(
        TRADE_CACHE_10_PER_30,
        interface="position_list_query",
        source_url=POSITIONS_URL,
    ),
    "accinfo_query": replace(
        TRADE_CACHE_10_PER_30,
        interface="accinfo_query",
        source_url=FUNDS_URL,
    ),
    "order_list_query": replace(
        TRADE_CACHE_10_PER_30,
        interface="order_list_query",
        source_url=ORDERS_URL,
    ),
    "place_order": PLACE_ORDER_RATE_LIMIT,
}

for _interface in (
    "get_stock_quote",
    "get_order_book",
    "get_cur_kline",
    "get_rt_data",
    "get_rt_ticker",
    "get_broker_queue",
):
    MOOMOO_RATE_LIMITS[_interface] = replace(
        OPEND_CACHE_REALTIME_RULE,
        interface=_interface,
    )

for _interface in (
    "get_daily_short_volume",
    "get_short_interest",
    "get_ipo_list",
    "request_trading_days",
    "get_history_kl_quota",
    "get_stock_basicinfo",
    "get_rehab",
    "get_holding_change_list",
    "get_option_volatility",
    "get_option_exercise_probability",
    "get_financials_earnings_calendar",
    "get_financials_earnings_price_history",
    "get_company_profile",
    "get_company_executives",
    "get_company_operation",
    "get_shareholders_insider",
    "get_shareholders_holding",
    "get_insider_holder_list",
    "get_insider_trade_list",
    "get_financials_statements",
    "get_financials_revenue_breakdown",
    "get_corporate_actions_stock_splits",
    "get_corporate_actions_buybacks",
    "get_corporate_actions_dividends",
    "get_shareholders_overview",
):
    MOOMOO_RATE_LIMITS[_interface] = replace(
        UNPUBLISHED_SERVER_REQUEST_RULE,
        interface=_interface,
    )


DEFAULT_RATE_WINDOW_S = 30.0
DEFAULT_DATA_ACCESS_RATE_LIMIT = 28
DEFAULT_BACKFILL_SLEEP_SECONDS = SNAPSHOT_RATE_LIMIT.min_interval_s or 0.5
DEFAULT_OPTION_CHAIN_SLEEP_SECONDS = OPTION_CHAIN_RATE_LIMIT.min_interval_s or 3.0


def rate_limit_for(interface: str) -> MoomooRateLimitRule:
    """Return the configured rate-limit rule for a moomoo interface."""

    rule = MOOMOO_RATE_LIMITS.get(interface)
    if rule is not None:
        return rule
    return replace(UNPUBLISHED_SERVER_REQUEST_RULE, interface=interface)
