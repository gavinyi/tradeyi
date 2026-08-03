from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
import os
import socket
import time

import pandas as pd
from futu import (
    AuType,
    KLType,
    Market,
    ModifyOrderOp,
    OpenQuoteContext,
    OpenSecTradeContext,
    OrderType,
    RET_OK,
    SecurityFirm,
    SecurityType,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

from .config import Settings
from .option_chain_cache import (
    acquire_api_slot, acquire_option_chain_slot, get_frame, save_frame,
)


class BrokerError(RuntimeError):
    pass


class FutuPaperBroker:
    """Guarded Futu OpenAPI facade.

    Defaults to paper trading. REAL is only reachable when Settings has passed
    the explicit live-trading safety gates.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.trd_env = TrdEnv.REAL if settings.trading_env == "REAL" else TrdEnv.SIMULATE
        self.market = TrdMarket.US
        try:
            self.security_firm = getattr(SecurityFirm, settings.security_firm)
        except AttributeError as exc:
            raise BrokerError(f"Unknown FUTU_SECURITY_FIRM: {settings.security_firm}") from exc
        self._frame_cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def _cached(self, key: str, loader, ttl: float = 2.0) -> pd.DataFrame:
        cached = self._frame_cache.get(key)
        if cached and time.monotonic() - cached[0] <= ttl:
            return cached[1].copy()
        frame = loader()
        self._frame_cache[key] = (time.monotonic(), frame.copy())
        return frame

    @contextmanager
    def trade_context(self) -> Iterator[OpenSecTradeContext]:
        ctx = self.new_trade_context()
        try:
            yield ctx
        finally:
            ctx.close()

    def new_trade_context(self) -> OpenSecTradeContext:
        self._ensure_opend_available()
        ctx = OpenSecTradeContext(
            filter_trdmarket=self.market,
            host=self.settings.host,
            port=self.settings.port,
            security_firm=self.security_firm,
        )
        self._set_connect_timeout(ctx)
        return ctx

    def _ensure_opend_available(self) -> None:
        timeout = max(
            0.1, min(2.0, float(os.getenv("FUTU_PREFLIGHT_TIMEOUT_SECONDS", "0.5")))
        )
        try:
            with socket.create_connection(
                (self.settings.host, self.settings.port), timeout=timeout
            ):
                return
        except OSError as exc:
            raise BrokerError(
                f"富途 OpenD 当前不可连接：{self.settings.host}:{self.settings.port}；"
                "已停止本次查询，不会启动 SDK 后台重连线程"
            ) from exc

    @staticmethod
    def _set_connect_timeout(ctx) -> None:
        setter = getattr(ctx, "set_sync_query_connect_timeout", None)
        if setter is not None:
            setter(max(1.0, float(os.getenv("FUTU_CONNECT_TIMEOUT_SECONDS", "5"))))

    @contextmanager
    def quote_context(self) -> Iterator[OpenQuoteContext]:
        self._ensure_opend_available()
        ctx = OpenQuoteContext(
            host=self.settings.host,
            port=self.settings.port,
            security_firm=self.security_firm,
            is_async_connect=True,
        )
        self._set_connect_timeout(ctx)
        try:
            yield ctx
        finally:
            ctx.close()

    @staticmethod
    def _ok(result: tuple, operation: str):
        ret, data = result[:2]
        if ret != RET_OK:
            raise BrokerError(f"{operation} failed: {data}")
        return data

    def accounts(self) -> pd.DataFrame:
        with self.trade_context() as ctx:
            data = self._ok(ctx.get_acc_list(), "get accounts")
        if "trd_env" not in data.columns:
            raise BrokerError("Safety lock: account response has no trading environment")
        selected = data[data["trd_env"].astype(str).str.upper() == self.settings.trading_env].copy()
        if selected.empty:
            raise BrokerError(f"No US {self.settings.trading_env} account was returned by OpenD")
        if self.settings.trading_env == "REAL" and self.settings.real_account_id:
            account_id = str(self.settings.real_account_id)
            account_column = "acc_id" if "acc_id" in selected.columns else "acc_id_str"
            if account_column in selected.columns:
                selected = selected[selected[account_column].astype(str) == account_id].copy()
            if selected.empty:
                raise BrokerError("OpenD 未返回配置的实盘账户，请检查 REAL_ACCOUNT_ID")
        return selected

    def _account_kwargs(self) -> dict:
        if self.settings.trading_env == "REAL":
            if not self.settings.real_account_id:
                raise BrokerError("实盘环境必须配置 REAL_ACCOUNT_ID")
            return {"acc_id": self.settings.real_account_id}
        return {"acc_id": self.settings.acc_id} if self.settings.acc_id else {}

    def account_info(self, refresh: bool = False) -> pd.DataFrame:
        def load():
            acquire_api_slot("account_info", 9)
            with self.trade_context() as ctx:
                return self._ok(
                ctx.accinfo_query(trd_env=self.trd_env, **self._account_kwargs()), "account info"
            )
        return load() if refresh else self._cached("account_info", load)

    def positions(self, refresh: bool = False) -> pd.DataFrame:
        def load():
            acquire_api_slot("positions", 9)
            with self.trade_context() as ctx:
                return self._ok(
                ctx.position_list_query(trd_env=self.trd_env, **self._account_kwargs()),
                "positions",
            )
        return load() if refresh else self._cached("positions", load)

    def orders(self, historical: bool = False) -> pd.DataFrame:
        def load():
            acquire_api_slot("historical_orders" if historical else "orders", 9)
            with self.trade_context() as ctx:
                method = ctx.history_order_list_query if historical else ctx.order_list_query
                extra = {} if historical else {"refresh_cache": True}
                return self._ok(
                method(trd_env=self.trd_env, **self._account_kwargs(), **extra),
                "historical orders" if historical else "today orders",
            )
        return self._cached(f"orders:{historical}", load)

    def candles(self, symbol: str, count: int = 120) -> pd.DataFrame:
        if not symbol.startswith("US."):
            raise BrokerError("Safety lock: only US.* symbols are allowed")
        with self.quote_context() as ctx:
            ret, data, _ = ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=count
            )
            if ret != RET_OK:
                raise BrokerError(f"history for {symbol} failed: {data}")
            return data

    def candles_range(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Download all daily bars in a date range, following Futu pagination."""
        if not symbol.startswith("US."):
            raise BrokerError("Safety lock: only US.* symbols are allowed")
        frames: list[pd.DataFrame] = []
        page_req_key = None
        with self.quote_context() as ctx:
            while True:
                ret, data, page_req_key = ctx.request_history_kline(
                    symbol,
                    start=start,
                    end=end,
                    ktype=KLType.K_DAY,
                    autype=AuType.QFQ,
                    max_count=1000,
                    page_req_key=page_req_key,
                )
                if ret != RET_OK:
                    raise BrokerError(f"history for {symbol} failed: {data}")
                frames.append(data)
                if page_req_key is None:
                    break
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).drop_duplicates("time_key")

    def security_catalog(self) -> pd.DataFrame:
        """Return Futu's current US stock and ETF directory."""
        frames: list[pd.DataFrame] = []
        with self.quote_context() as ctx:
            for security_type in (SecurityType.STOCK, SecurityType.ETF):
                result = ctx.get_stock_basicinfo(Market.US, stock_type=security_type)
                frames.append(self._ok(result, f"US {security_type} directory"))
        if not frames:
            return pd.DataFrame()
        catalog = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"])
        return catalog.sort_values("code").reset_index(drop=True)

    def option_expirations(self, underlying: str) -> pd.DataFrame:
        if not underlying.startswith("US."):
            raise BrokerError("Options underlying must be a US.* symbol")
        cache_key = f"expirations:{underlying}"
        cached = get_frame(cache_key, 15 * 60)
        if cached is not None:
            return cached
        try:
            with self.quote_context() as ctx:
                frame = self._ok(
                    ctx.get_option_expiration_date(underlying), "option expirations"
                )
            save_frame(cache_key, frame)
            return frame
        except BrokerError:
            stale = get_frame(cache_key, 0, allow_stale=True, max_stale_seconds=24 * 60 * 60)
            if stale is not None:
                return stale
            raise

    def option_chain(self, underlying: str, expiration: str) -> pd.DataFrame:
        if not underlying.startswith("US."):
            raise BrokerError("Options underlying must be a US.* symbol")
        normalized_expiration = str(expiration)[:10]
        cache_key = f"chain:{underlying}:{normalized_expiration}"
        cached = get_frame(cache_key, 60)
        if cached is not None:
            return cached
        acquire_option_chain_slot()
        try:
            with self.quote_context() as ctx:
                frame = self._ok(
                    ctx.get_option_chain(
                        underlying, start=normalized_expiration, end=normalized_expiration
                    ),
                    "option chain",
                )
            save_frame(cache_key, frame)
            return frame
        except BrokerError:
            stale = get_frame(cache_key, 0, allow_stale=True, max_stale_seconds=15 * 60)
            if stale is not None:
                return stale
            raise

    def earnings_calendar(self, begin_date: str, end_date: str) -> pd.DataFrame:
        with self.quote_context() as ctx:
            return self._ok(
                ctx.get_earnings_calendar(Market.US, begin_date=begin_date, end_date=end_date),
                "earnings calendar",
            )

    def snapshots(self, symbols: list[str]) -> pd.DataFrame:
        if not symbols or any(not symbol.startswith("US.") for symbol in symbols):
            raise BrokerError("Snapshots require US.* symbols")
        frames: list[pd.DataFrame] = []
        with self.quote_context() as ctx:
            for start in range(0, len(symbols), 200):
                acquire_api_slot("snapshots", 55)
                frames.append(
                    self._ok(ctx.get_market_snapshot(symbols[start : start + 200]), "snapshots")
                )
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def snapshots_tolerant(self, symbols: list[str]) -> pd.DataFrame:
        """Fetch a broad universe while isolating unsupported OTC/bad symbols."""
        if not symbols or any(not symbol.startswith("US.") for symbol in symbols):
            raise BrokerError("Snapshots require US.* symbols")
        frames: list[pd.DataFrame] = []
        with self.quote_context() as ctx:
            def fetch_batch(batch: list[str]) -> None:
                if not batch:
                    return
                acquire_api_slot("snapshots", 55)
                ret, data = ctx.get_market_snapshot(batch)[:2]
                if ret == RET_OK:
                    frames.append(data)
                    return
                message = str(data).lower()
                symbol_error = any(token in message for token in (
                    "not support", "invalid", "不存在", "不支持", "otc", "未知股票",
                    "unknown stock", "unknown symbol",
                ))
                if not symbol_error:
                    raise BrokerError(f"snapshots failed: {data}")
                if len(batch) == 1:
                    return
                middle = len(batch) // 2
                fetch_batch(batch[:middle])
                fetch_batch(batch[middle:])

            for start in range(0, len(symbols), 200):
                fetch_batch(symbols[start : start + 200])
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def order_book(self, symbol: str, depth: int = 10) -> dict:
        if not symbol.startswith("US."):
            raise BrokerError("Order book requires a US.* symbol")
        with self.quote_context() as ctx:
            return self._ok(ctx.get_order_book(symbol, num=max(1, min(10, depth))), "order book")

    def place_limit_order(self, symbol: str, side: str, quantity: int, price: float) -> pd.DataFrame:
        if not self.settings.enable_paper_orders:
            raise BrokerError("券商下单开关未开启：ENABLE_PAPER_ORDERS=false")
        if not symbol.startswith("US.") or side not in {"BUY", "SELL"}:
            raise BrokerError("Safety lock rejected the order")
        trade_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
        operation = "place real order" if self.settings.trading_env == "REAL" else "place simulated order"
        with self.trade_context() as ctx:
            return self._ok(
                ctx.place_order(
                    price=round(float(price), 2), qty=max(1, int(quantity)), code=symbol,
                    trd_side=trade_side, order_type=OrderType.NORMAL,
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                operation,
            )

    def cancel_order(self, order_id: str) -> pd.DataFrame:
        if not str(order_id).strip():
            raise BrokerError("Order ID is required")
        with self.trade_context() as ctx:
            return self._ok(
                ctx.modify_order(
                    ModifyOrderOp.CANCEL, str(order_id), 0, 0,
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                "cancel real order" if self.settings.trading_env == "REAL" else "cancel simulated order",
            )

    def modify_limit_order(self, order_id: str, quantity: int, price: float) -> pd.DataFrame:
        if not str(order_id).strip() or quantity <= 0 or price <= 0:
            raise BrokerError("Order ID, quantity and price are required")
        with self.trade_context() as ctx:
            return self._ok(
                ctx.modify_order(
                    ModifyOrderOp.NORMAL, str(order_id), int(quantity), round(float(price), 2),
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                "modify real limit order" if self.settings.trading_env == "REAL" else "modify simulated limit order",
            )

    @staticmethod
    def is_cancellable_status(status: object) -> bool:
        normalized = str(status).upper().split(".")[-1].replace("ORDERSTATUS_", "")
        return normalized in {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}

    def cancel_orders(self, orders: pd.DataFrame) -> pd.DataFrame:
        active = orders[orders["order_status"].map(self.is_cancellable_status)]
        results: list[dict] = []
        for _, order in active.iterrows():
            order_id = str(order["order_id"])
            try:
                self.cancel_order(order_id)
                results.append({"order_id": order_id, "code": order["code"], "result": "SUCCESS"})
            except BrokerError as exc:
                results.append({"order_id": order_id, "code": order["code"], "result": str(exc)})
            # Futu allows at most 20 cancel/modify requests per 30 seconds per account.
            time.sleep(1.55)
        return pd.DataFrame(results)

    def cancel_all_orders(self) -> pd.DataFrame:
        return self.cancel_orders(self.orders())
