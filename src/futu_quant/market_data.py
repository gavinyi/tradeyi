from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import importlib
import json
import os
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .config import normalize_us_symbol


CANONICAL_BAR_COLUMNS = [
    "symbol", "timestamp", "session_date", "open", "high", "low", "close", "volume",
    "adjustment", "source", "feed", "fetched_at", "available_at", "quality_status",
    "dataset_version",
]


class MarketDataError(RuntimeError):
    """Base exception for research market-data providers."""


class MarketDataNotConfigured(MarketDataError):
    """Raised only when an explicitly requested optional provider is not configured."""


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    configured: bool
    available: bool
    message: str
    research_only: bool = True


class MarketDataProvider(Protocol):
    name: str

    def health(self) -> ProviderStatus: ...

    def get_bars(
        self, symbol: str, start: date | str, end: date | str, interval: str = "1d"
    ) -> pd.DataFrame: ...

    def get_corporate_actions(
        self, symbol: str, start: date | str, end: date | str
    ) -> pd.DataFrame: ...


def _date_text(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


def _dataset_version(frame: pd.DataFrame, symbol: str, source: str, adjustment: str) -> str:
    stable = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    stable["timestamp"] = stable["timestamp"].astype(str)
    payload = stable.to_json(orient="records", date_format="iso", double_precision=10)
    digest = hashlib.sha256(
        f"{symbol}|{source}|{adjustment}|{payload}".encode("utf-8")
    ).hexdigest()
    return digest[:20]


def normalize_bars(
    frame: pd.DataFrame,
    symbol: str,
    source: str,
    feed: str,
    adjustment: str = "RAW",
    *,
    fetched_at: datetime | None = None,
) -> pd.DataFrame:
    """Normalize historical research bars and attach auditable provenance."""
    aliases = {"time_key": "timestamp", "date": "timestamp", "datetime": "timestamp"}
    data = frame.rename(columns={key: value for key, value in aliases.items() if key in frame}).copy()
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"缺少行情字段：{', '.join(missing)}")
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="coerce", utc=True)
    data = data.dropna(subset=["timestamp"]).copy()
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close", "volume"])
    normalized_symbol = normalize_us_symbol(symbol)
    data["symbol"] = normalized_symbol
    data["session_date"] = data["timestamp"].dt.strftime("%Y-%m-%d")
    data["adjustment"] = adjustment.upper()
    data["source"] = source.upper()
    data["feed"] = feed.upper()
    fetch_time = pd.Timestamp(fetched_at or datetime.now(timezone.utc)).tz_convert("UTC")
    data["fetched_at"] = fetch_time
    data["available_at"] = data["timestamp"]
    valid_price = (
        (data["low"] <= data[["open", "close"]].min(axis=1))
        & (data["high"] >= data[["open", "close"]].max(axis=1))
        & (data["low"] >= 0)
        & (data["volume"] >= 0)
    )
    data["quality_status"] = valid_price.map({True: "GOOD", False: "QUARANTINED"})
    data = data.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    version = _dataset_version(data, normalized_symbol, source.upper(), adjustment.upper())
    data["dataset_version"] = version
    return data[CANONICAL_BAR_COLUMNS]


class FutuMarketDataProvider:
    name = "FUTU"

    def __init__(self, broker):
        self.broker = broker

    def health(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, "富途历史行情已配置；实际权限由 OpenD 返回")

    def get_bars(
        self, symbol: str, start: date | str, end: date | str, interval: str = "1d"
    ) -> pd.DataFrame:
        if interval != "1d":
            raise MarketDataError("富途研究 Provider 当前只支持日线")
        code = normalize_us_symbol(symbol)
        raw = self.broker.candles_range(code, _date_text(start), _date_text(end))
        return normalize_bars(raw, code, self.name, "FUTU", "QFQ")

    def get_corporate_actions(self, symbol, start, end) -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "ex_date", "action_type", "value", "source"])


class AlpacaMarketDataProvider:
    name = "ALPACA"

    def __init__(
        self, api_key: str = "", api_secret: str = "", *, timeout: float = 15.0,
        opener: Callable = urlopen,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self._opener = opener

    @classmethod
    def from_env(cls) -> "AlpacaMarketDataProvider":
        return cls(os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", ""))

    def health(self) -> ProviderStatus:
        configured = bool(self.api_key and self.api_secret)
        return ProviderStatus(
            self.name, configured, configured,
            "已配置；免费股票行情为 IEX，仅用于研究补全" if configured
            else "未配置 ALPACA_API_KEY / ALPACA_API_SECRET",
        )

    def get_bars(
        self, symbol: str, start: date | str, end: date | str, interval: str = "1d"
    ) -> pd.DataFrame:
        if not self.health().configured:
            raise MarketDataNotConfigured("未配置 ALPACA_API_KEY / ALPACA_API_SECRET")
        if interval != "1d":
            raise MarketDataError("Alpaca 研究 Provider 当前只支持日线")
        ticker = normalize_us_symbol(symbol).removeprefix("US.")
        params = urlencode(
            {"timeframe": "1Day", "start": _date_text(start), "end": _date_text(end),
             "adjustment": "all", "feed": "iex", "limit": 10000}
        )
        request = Request(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars?{params}",
            headers={"APCA-API-KEY-ID": self.api_key, "APCA-API-SECRET-KEY": self.api_secret},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise MarketDataError(f"Alpaca 历史行情请求失败：HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MarketDataError(f"Alpaca 历史行情暂不可用：{type(exc).__name__}") from exc
        rows = payload.get("bars") or []
        raw = pd.DataFrame(rows).rename(
            columns={"t": "timestamp", "o": "open", "h": "high", "l": "low",
                     "c": "close", "v": "volume"}
        )
        if raw.empty:
            return pd.DataFrame(columns=CANONICAL_BAR_COLUMNS)
        return normalize_bars(raw, ticker, self.name, "IEX", "ALL")

    def get_corporate_actions(self, symbol, start, end) -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "ex_date", "action_type", "value", "source"])


class YahooMarketDataProvider:
    name = "YAHOO"

    def __init__(self, importer: Callable[[str], object] = importlib.import_module):
        self._importer = importer

    def _module(self):
        try:
            return self._importer("yfinance")
        except ImportError as exc:
            raise MarketDataNotConfigured(
                "未安装可选依赖 yfinance；运行 uv sync --extra marketdata 后启用"
            ) from exc

    def health(self) -> ProviderStatus:
        try:
            self._module()
        except MarketDataNotConfigured as exc:
            return ProviderStatus(self.name, False, False, str(exc))
        return ProviderStatus(self.name, True, True, "已安装；仅用于个人研究和三级备用")

    def get_bars(
        self, symbol: str, start: date | str, end: date | str, interval: str = "1d"
    ) -> pd.DataFrame:
        yf = self._module()
        ticker = normalize_us_symbol(symbol).removeprefix("US.")
        raw = yf.download(
            ticker, start=_date_text(start), end=_date_text(end), interval=interval,
            auto_adjust=False, actions=False, progress=False,
        )
        if raw.empty:
            return pd.DataFrame(columns=CANONICAL_BAR_COLUMNS)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.reset_index().rename(columns={column: str(column).lower() for column in raw.columns})
        return normalize_bars(raw, ticker, self.name, "YAHOO", "RAW")

    def get_corporate_actions(self, symbol, start, end) -> pd.DataFrame:
        yf = self._module()
        ticker = normalize_us_symbol(symbol).removeprefix("US.")
        actions = yf.Ticker(ticker).actions
        if actions.empty:
            return pd.DataFrame(columns=["symbol", "ex_date", "action_type", "value", "source"])
        rows = []
        for when, row in actions.iterrows():
            if float(row.get("Dividends", 0)):
                rows.append({"symbol": normalize_us_symbol(symbol), "ex_date": str(when.date()),
                             "action_type": "DIVIDEND", "value": float(row["Dividends"]),
                             "source": self.name})
            if float(row.get("Stock Splits", 0)):
                rows.append({"symbol": normalize_us_symbol(symbol), "ex_date": str(when.date()),
                             "action_type": "SPLIT", "value": float(row["Stock Splits"]),
                             "source": self.name})
        return pd.DataFrame(rows)
