from __future__ import annotations

import atexit
from contextlib import contextmanager
from functools import wraps
from typing import Iterator
import logging
import logging.handlers
import os
from pathlib import Path
import socket
import threading
import time

if os.name == "nt":
    _local_appdata = Path(__file__).resolve().parents[2] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

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
from futu.common.constant import ContextStatus
from futu.common.ft_logger import FTLog

from .config import Settings
from .option_chain_cache import (
    acquire_api_slot, acquire_option_chain_slot, get_frame, save_frame,
)


class BrokerError(RuntimeError):
    pass


_OPEND_HEALTH_LOCK = threading.Lock()
_OPEND_FAILURES = 0
_OPEND_BACKOFF_UNTIL = 0.0

# Process-global persistent connection pool.  Creating and closing a fresh SDK
# context for every quote/trade operation churns TCP connections, stresses
# OpenD, and turns momentary hiccups into repeated failures.  All
# FutuPaperBroker instances in one process now share one quote context and one
# trade context per OpenD endpoint/account instead.
_POOL_REGISTRY: dict[tuple, "_BrokerConnectionPool"] = {}
_POOL_REGISTRY_LOCK = threading.Lock()

# Substrings that indicate a transport-level failure worth a single automatic
# reconnect-and-retry for idempotent read operations.  Order placement /
# cancellation is never retried automatically.
def configure_futu_process_log(role: str) -> Path:
    """Route this process's Futu SDK file log to an isolated project file.

    Futu's SDK logger is process-global and otherwise writes to one shared
    rotating file under AppData, which is unsafe when UI/service/radar run
    together on Windows.
    """
    safe_role = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in role)
    directory = Path(os.getenv("FUTU_LOG_DIR", "data/futu-logs"))
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parents[2] / directory
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"futu-{safe_role or 'process'}.log"
    sdk_logger = FTLog()
    file_logger = sdk_logger.file_logger
    file_logger.handlers.clear()
    handler = logging.handlers.WatchedFileHandler(target, encoding="utf-8") if os.name != "nt" else logging.FileHandler(target, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(sdk_logger.formatter)
    file_logger.addHandler(handler)
    return target


logger = logging.getLogger("futu_quant.broker")


_RETRYABLE_READ_TOKENS = (    "disconn", "timeout", "timed out", "etimedout", "callclose",
    "connection reset", "reset by peer", "broken pipe", "wait_reconnect",
    "network error", "连接失败", "连接超时", "已断开", "连接被断开", "链路断开",
    "服务端关闭", "socket closed", "远程主机", "unreachable",
)


class _BrokerConnectionPool:
    """Holds the reusable quote/trade contexts for one endpoint/account."""

    __slots__ = ("key", "lock", "quote", "trade")

    def __init__(self, key: tuple):
        self.key = key
        self.lock = threading.RLock()
        self.quote: OpenQuoteContext | None = None
        self.trade: OpenSecTradeContext | None = None

    def drop(self, kind: str) -> None:
        """Close and forget one kind ('quote' or 'trade'); next use reconnects."""
        with self.lock:
            ctx = getattr(self, kind)
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # silent-ok: 连接池丢弃路径，关闭失败无补救动作
                    pass
                setattr(self, kind, None)

    def close_all(self) -> None:
        self.drop("quote")
        self.drop("trade")


def close_all_connections() -> None:
    """Release every pooled connection in this process (used at shutdown)."""
    with _POOL_REGISTRY_LOCK:
        pools = list(_POOL_REGISTRY.values())
        _POOL_REGISTRY.clear()
    for pool in pools:
        pool.close_all()


def _atexit_close() -> None:
    """Close pooled connections at interpreter exit without hanging shutdown.

    Closing a Futu context inside an atexit handler can itself block while the
    interpreter is already tearing down, so the close runs on a daemon thread
    with a short join cap.  Long-running entry points close explicitly.
    """
    closer = threading.Thread(target=close_all_connections, daemon=True)
    closer.start()
    closer.join(5.0)


atexit.register(_atexit_close)


def _retryable_message(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _RETRYABLE_READ_TOKENS)


def _read_with_reconnect(kind: str):
    """Retry an idempotent read once after dropping the pooled connection.

    Applies only to reads (account/position queries, candles, snapshots, ...).
    Order placement and cancellation must never be retried automatically.
    """

    def decorate(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except BrokerError as exc:
                if not _retryable_message(exc):
                    raise
                pool = self._pool()
                pool.drop(kind)
                return fn(self, *args, **kwargs)

        return wrapper

    return decorate


class FutuPaperBroker:
    """Guarded Futu OpenAPI facade.

    Defaults to paper trading. REAL is only reachable when Settings has passed
    the explicit live-trading safety gates.
    """

    def __init__(
        self,
        settings: Settings,
        trading_env_override: str | None = None,
        account_id_override: int | str | None = None,
        security_firm_override: str | None = None,
    ):
        self.settings = settings
        effective_env = (trading_env_override or settings.trading_env).upper()
        self.trading_env = effective_env
        self.account_id = (
            int(account_id_override)
            if account_id_override
            else (int(settings.real_account_id) if effective_env == "REAL" and settings.real_account_id else None)
        )
        self.trd_env = TrdEnv.REAL if effective_env == "REAL" else TrdEnv.SIMULATE
        self.market = TrdMarket.US
        firm_str = security_firm_override or settings.security_firm
        try:
            self.security_firm = getattr(SecurityFirm, firm_str)
        except AttributeError as exc:
            raise BrokerError(f"Unknown FUTU_SECURITY_FIRM: {firm_str}") from exc
        self._frame_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        role = os.getenv("FUTU_LOG_ROLE", "process")
        if role:
            configure_futu_process_log(role)

    # ---- persistent pooled connections ------------------------------------

    def _pool_key(self) -> tuple:
        target_account = (
            self.account_id
            or (self.settings.real_account_id if self.trading_env == "REAL" else getattr(self.settings, "acc_id", ""))
        )
        return (
            self.settings.host, self.settings.port, self.security_firm,
            self.trd_env, target_account,
        )

    def _pool(self) -> _BrokerConnectionPool:
        key = self._pool_key()
        with _POOL_REGISTRY_LOCK:
            pool = _POOL_REGISTRY.get(key)
            if pool is None:
                pool = _POOL_REGISTRY[key] = _BrokerConnectionPool(key)
            return pool

    @staticmethod
    def _ctx_alive(ctx) -> bool:
        status = getattr(ctx, "status", None)
        if status is None:
            return True  # SDK without a status property: assume usable
        return status not in (ContextStatus.CLOSED, ContextStatus.CLOSING)

    @staticmethod
    def _wait_ready(ctx, label: str) -> None:
        """Wait for an async-connect context to finish its handshake.

        Sending a sync query (especially push-backed ones like order books) on
        a context whose async connect is still in flight can block forever, so
        never hand a context to a caller before it is READY/CONNECTED.
        """
        deadline = time.monotonic() + max(
            3.0, float(os.getenv("FUTU_CONNECT_TIMEOUT_SECONDS", "5"))
        )
        ready = (ContextStatus.CONNECTED, ContextStatus.READY)
        status = None
        while True:
            status = getattr(ctx, "status", None)
            if status in ready:
                return
            if status in (ContextStatus.CLOSED, ContextStatus.CLOSING):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        try:
            ctx.close()
        except Exception:  # silent-ok: 连接就绪超时的清理路径，随后即抛 BrokerError
            pass
        raise BrokerError(
            f"富途 OpenD {label} 连接在超时时间内未就绪（status={status}）；"
            "请确认 OpenD 已启动且牛牛已登录解锁"
        )

    def _acquire_quote(self, pool: _BrokerConnectionPool) -> OpenQuoteContext:
        ctx = pool.quote
        if ctx is None or not self._ctx_alive(ctx):
            pool.drop("quote")
            self._ensure_opend_available()
            ctx = OpenQuoteContext(
                host=self.settings.host,
                port=self.settings.port,
                security_firm=self.security_firm,
                is_async_connect=True,
            )
            self._set_connect_timeout(ctx)
            self._wait_ready(ctx, "行情")
            pool.quote = ctx
        return ctx

    def _acquire_trade(self, pool: _BrokerConnectionPool) -> OpenSecTradeContext:
        ctx = pool.trade
        if ctx is None or not self._ctx_alive(ctx):
            pool.drop("trade")
            self._ensure_opend_available()
            ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=self.settings.host,
                port=self.settings.port,
                security_firm=self.security_firm,
            )
            self._set_connect_timeout(ctx)
            self._wait_ready(ctx, "交易")
            pool.trade = ctx
        return ctx

    @property
    def pool(self) -> _BrokerConnectionPool:
        """Expose the shared pool (diagnostics/tests); contexts stay cached."""
        return self._pool()

    def _cached(self, key: str, loader, ttl: float = 2.0) -> pd.DataFrame:
        cached = self._frame_cache.get(key)
        if cached and time.monotonic() - cached[0] <= ttl:
            return cached[1].copy()
        frame = loader()
        self._frame_cache[key] = (time.monotonic(), frame.copy())
        return frame

    @contextmanager
    def trade_context(self) -> Iterator[OpenSecTradeContext]:
        """Yield the process-wide persistent trade context (serialized).

        The context stays connected and is reused by every operation instead of
        rebuilding a TCP connection per call.  A failed context is dropped and
        recreated lazily on the next acquisition.
        """
        pool = self._pool()
        with pool.lock:
            yield self._acquire_trade(pool)

    def new_trade_context(self) -> OpenSecTradeContext:
        """Create a dedicated, caller-owned trade context (e.g. order push)."""
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
        global _OPEND_FAILURES, _OPEND_BACKOFF_UNTIL
        now = time.monotonic()
        with _OPEND_HEALTH_LOCK:
            remaining = _OPEND_BACKOFF_UNTIL - now
        if remaining > 0:
            raise BrokerError(
                f"富途 OpenD 连接处于自动退避，约 {remaining:.1f} 秒后重试；"
                "本轮不会反复创建 SDK 连接"
            )
        timeout = max(
            0.1, min(2.0, float(os.getenv("FUTU_PREFLIGHT_TIMEOUT_SECONDS", "0.5")))
        )
        try:
            with socket.create_connection(
                (self.settings.host, self.settings.port), timeout=timeout
            ):
                with _OPEND_HEALTH_LOCK:
                    _OPEND_FAILURES = 0
                    _OPEND_BACKOFF_UNTIL = 0.0
                return
        except OSError as exc:
            with _OPEND_HEALTH_LOCK:
                _OPEND_FAILURES += 1
                backoff = (
                    min(
                        60.0,
                        float(os.getenv("FUTU_OPEND_MAX_BACKOFF_SECONDS", "60")),
                        float(2 ** min(_OPEND_FAILURES - 2, 6)),
                    )
                    if _OPEND_FAILURES >= 2 else 0.0
                )
                _OPEND_BACKOFF_UNTIL = time.monotonic() + backoff
            raise BrokerError(
                f"富途 OpenD 当前不可连接：{self.settings.host}:{self.settings.port}；"
                f"已停止本次查询并退避 {backoff:.0f} 秒，不会启动 SDK 后台重连线程"
            ) from exc

    @staticmethod
    def _set_connect_timeout(ctx) -> None:
        setter = getattr(ctx, "set_sync_query_connect_timeout", None)
        if setter is not None:
            setter(max(1.0, float(os.getenv("FUTU_CONNECT_TIMEOUT_SECONDS", "5"))))

    @contextmanager
    def quote_context(self) -> Iterator[OpenQuoteContext]:
        """Yield the process-wide persistent quote context (serialized).

        Same persistence contract as :meth:`trade_context`: one long-lived
        quote connection per endpoint instead of connect-per-call churn.
        """
        pool = self._pool()
        with pool.lock:
            yield self._acquire_quote(pool)

    @staticmethod
    def _ok(result: tuple, operation: str):
        ret, data = result[:2]
        if ret != RET_OK:
            raise BrokerError(f"{operation} failed: {data}")
        return data

    @_read_with_reconnect("trade")
    def accounts(self) -> pd.DataFrame:
        with self.trade_context() as ctx:
            data = self._ok(ctx.get_acc_list(), "get accounts")
        if "trd_env" not in data.columns:
            raise BrokerError("Safety lock: account response has no trading environment")
        selected = data[data["trd_env"].astype(str).str.upper() == self.trading_env].copy()
        if selected.empty:
            raise BrokerError(f"No US {self.trading_env} account was returned by OpenD")
        target_account = (
            self.account_id
            or (self.settings.real_account_id if self.trading_env == "REAL" else getattr(self.settings, "acc_id", None))
        )
        if target_account:
            account_id = str(target_account)
            account_column = "acc_id" if "acc_id" in selected.columns else "acc_id_str"
            if account_column in selected.columns:
                selected = selected[selected[account_column].astype(str) == account_id].copy()
            if selected.empty:
                raise BrokerError(f"OpenD 未返回配置的账户 {target_account} ({self.trading_env})")
        return selected

    def _account_kwargs(self) -> dict:
        target_account = (
            self.account_id
            or (self.settings.real_account_id if self.trading_env == "REAL" else getattr(self.settings, "acc_id", None))
        )
        if self.trading_env == "REAL":
            if not target_account:
                raise BrokerError("实盘环境必须配置 REAL_ACCOUNT_ID")
            return {"acc_id": int(target_account)}
        return {"acc_id": int(target_account)} if target_account else {}

    @_read_with_reconnect("trade")
    def account_info(self, refresh: bool = False) -> pd.DataFrame:
        def load():
            acquire_api_slot("account_info", 9)
            with self.trade_context() as ctx:
                return self._ok(
                ctx.accinfo_query(trd_env=self.trd_env, **self._account_kwargs()), "account info"
            )
        return load() if refresh else self._cached("account_info", load)

    @_read_with_reconnect("trade")
    def positions(self, refresh: bool = False) -> pd.DataFrame:
        def load():
            acquire_api_slot("positions", 9)
            with self.trade_context() as ctx:
                return self._ok(
                ctx.position_list_query(trd_env=self.trd_env, **self._account_kwargs()),
                "positions",
            )
        return load() if refresh else self._cached("positions", load)

    @_read_with_reconnect("trade")
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

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
    def earnings_calendar(self, begin_date: str, end_date: str) -> pd.DataFrame:
        with self.quote_context() as ctx:
            return self._ok(
                ctx.get_earnings_calendar(Market.US, begin_date=begin_date, end_date=end_date),
                "earnings calendar",
            )

    @_read_with_reconnect("quote")
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

    @_read_with_reconnect("quote")
    def snapshots_tolerant(
        self, symbols: list[str], *, dropped_out: list[str] | None = None,
    ) -> pd.DataFrame:
        """Fetch a broad universe while isolating unsupported OTC/bad symbols.

        被丢弃的 symbol 不再静默（2026-10-26 静默降级清零）：逐个 `logger.warning`，
        并通过 `dropped_out` 回传给调用方记事件；行为本身（跳过坏 symbol）不变。
        """
        if not symbols or any(not symbol.startswith("US.") for symbol in symbols):
            raise BrokerError("Snapshots require US.* symbols")
        frames: list[pd.DataFrame] = []

        def _drop(symbols_: list[str], detail) -> None:
            for symbol in symbols_:
                logger.warning("snapshots_tolerant 丢弃标的 %s：%s", symbol, detail)
                if dropped_out is not None:
                    dropped_out.append(symbol)

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
                    _drop(batch, data)
                    return
                middle = len(batch) // 2
                fetch_batch(batch[:middle])
                fetch_batch(batch[middle:])

            for start in range(0, len(symbols), 200):
                fetch_batch(symbols[start : start + 200])
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @_read_with_reconnect("quote")
    def order_book(self, symbol: str, depth: int = 10) -> dict:
        if not symbol.startswith("US."):
            raise BrokerError("Order book requires a US.* symbol")
        with self.quote_context() as ctx:
            return self._ok(ctx.get_order_book(symbol, num=max(1, min(10, depth))), "order book")

    def place_limit_order(self, symbol: str, side: str, quantity: int, price: float,
                          *, real_trade_context=None) -> pd.DataFrame:
        if self.trading_env == "REAL":
            if real_trade_context is None:
                raise BrokerError("实盘下单必须经由执行管理器的实盘门禁")
            from .real_trading import check_real_trade
            store, strategy_id, metadata, intent_id = real_trade_context
            metadata = dict(metadata or {})
            if self.account_id:
                metadata.setdefault("account_id", str(self.account_id))
            if self.security_firm:
                firm_str = getattr(self.security_firm, "name", str(self.security_firm)).split(".")[-1].upper()
                metadata.setdefault("security_firm", firm_str)
            decision = check_real_trade(store, self.settings, strategy_id, symbol, side,
                                        quantity, price, metadata,
                                        exclude_intent_id=intent_id)
            if not decision.allowed:
                raise BrokerError(f"实盘下单门禁拒绝：{decision.reason}")
        if not self.settings.enable_paper_orders:
            raise BrokerError("券商下单开关未开启：ENABLE_PAPER_ORDERS=false")
        if not symbol.startswith("US.") or side not in {"BUY", "SELL"}:
            raise BrokerError("Safety lock rejected the order")
        trade_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
        operation = "place real order" if self.trading_env == "REAL" else "place simulated order"
        with self.trade_context() as ctx:
            return self._ok(
                ctx.place_order(
                    price=round(float(price), 2), qty=max(1, int(quantity)), code=symbol,
                    trd_side=trade_side, order_type=OrderType.NORMAL,
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                operation,
            )

    def _check_order_environment(self, expected_env: str | None) -> None:
        if expected_env is not None and expected_env != self.trading_env:
            raise BrokerError(f"订单执行环境 {expected_env} 与券商环境 {self.trading_env} 不一致")

    def cancel_order(self, order_id: str, *, expected_env: str | None = None) -> pd.DataFrame:
        self._check_order_environment(expected_env)
        if not str(order_id).strip():
            raise BrokerError("Order ID is required")
        with self.trade_context() as ctx:
            return self._ok(
                ctx.modify_order(
                    ModifyOrderOp.CANCEL, str(order_id), 0, 0,
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                "cancel real order" if self.trading_env == "REAL" else "cancel simulated order",
            )

    def modify_limit_order(self, order_id: str, quantity: int, price: float,
                           *, expected_env: str | None = None) -> pd.DataFrame:
        self._check_order_environment(expected_env)
        if not str(order_id).strip() or quantity <= 0 or price <= 0:
            raise BrokerError("Order ID, quantity and price are required")
        with self.trade_context() as ctx:
            return self._ok(
                ctx.modify_order(
                    ModifyOrderOp.NORMAL, str(order_id), int(quantity), round(float(price), 2),
                    trd_env=self.trd_env, **self._account_kwargs(),
                ),
                "modify real limit order" if self.trading_env == "REAL" else "modify simulated limit order",
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
