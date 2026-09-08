from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import socket
import time
from typing import Any
from urllib.parse import urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen

import pandas as pd

from .crypto_models import CryptoInstrument, CryptoOrderRequest, normalize_crypto_symbol
from .crypto_swap_models import (
    CryptoSwapInstrument,
    CryptoSwapOrderRequest,
    normalize_swap_symbol,
)
from .okx_config import OkxSettings


class OkxApiError(RuntimeError):
    pass


def _network_error_message(exc: Exception, attempts: int, timeout: float) -> str:
    text = str(exc)
    if isinstance(exc, TimeoutError) or "timed out" in text.lower():
        cause = (
            f"网络超时：OKX HTTPS 握手或响应超过 {timeout:g} 秒，"
            "通常是本地网络到 OKX API 不稳定、DNS/代理慢或当前域名不可达"
        )
    elif isinstance(exc, URLError):
        cause = f"网络连接失败：{exc.reason}"
    elif isinstance(exc, socket.gaierror):
        cause = "DNS 解析失败"
    else:
        cause = f"网络请求异常：{exc}"
    return (
        f"欧易请求失败：{cause}；已尝试 {attempts} 次。"
        "可在“币策略配置与回测”切换 OKX API 域名，或调大 "
        "OKX_TIMEOUT_SECONDS / OKX_RETRY_ATTEMPTS。"
    )


class UrlLibTransport:
    def request(self, method: str, url: str, headers: dict, body: str, timeout: float) -> dict:
        request = Request(
            url, data=body.encode() if body else None, headers=headers, method=method,
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode())


class OkxDemoBroker:
    def __init__(self, settings: OkxSettings, transport=None, clock=None):
        self.settings = settings
        self.transport = transport or UrlLibTransport()
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

    def _signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{path}{body}".encode()
        digest = hmac.new(self.settings.secret_key.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _request(
        self, method: str, path: str, params: dict | None = None,
        payload: dict | None = None, private: bool = False,
    ) -> list[dict]:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        request_path = f"{path}?{query}" if query else path
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload else ""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "futu-paper-quant/0.1",
            "x-simulated-trading": "1",
        }
        if private:
            if not self.settings.credentials_configured:
                raise OkxApiError(
                    "欧易模拟盘凭证未配置，请设置 OKX_API_KEY、OKX_SECRET_KEY、OKX_PASSPHRASE"
                )
            timestamp = self.clock()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.settings.api_key,
                    "OK-ACCESS-PASSPHRASE": self.settings.passphrase,
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-SIGN": self._signature(timestamp, method, request_path, body),
                }
            )
        attempts = self.settings.retry_attempts + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.transport.request(
                    method.upper(), f"{self.settings.base_url}{request_path}",
                    headers, body, self.settings.timeout_seconds,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    raise OkxApiError(
                        _network_error_message(exc, attempts, self.settings.timeout_seconds)
                    ) from exc
                time.sleep(self.settings.retry_backoff_seconds * (2 ** attempt))
        else:
            raise OkxApiError(
                _network_error_message(
                    last_error or RuntimeError("未知网络错误"),
                    attempts,
                    self.settings.timeout_seconds,
                )
            )
        if str(response.get("code", "")) != "0":
            raise OkxApiError(
                f"欧易接口拒绝请求：code={response.get('code')}，"
                f"原因={response.get('msg') or '未知'}"
            )
        return response.get("data", [])

    @staticmethod
    def _instrument_id(value: str) -> str:
        text = str(value).upper()
        return normalize_swap_symbol(text) if text.endswith("-SWAP") else normalize_crypto_symbol(text)

    @staticmethod
    def _candles_frame(rows: list[dict] | list[list]) -> pd.DataFrame:
        columns = [
            "timestamp", "open", "high", "low", "close", "volume",
            "volume_currency", "volume_quote", "confirm",
        ]
        frame = pd.DataFrame(rows, columns=columns)
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce").astype("Int64")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["confirm"] = frame["confirm"].astype(str).eq("1")
        return (
            frame[frame["confirm"]]
            .dropna(subset=["timestamp", "open", "high", "low", "close"])
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 300) -> pd.DataFrame:
        rows = self._request(
            "GET", "/api/v5/market/candles",
            {"instId": self._instrument_id(inst_id), "bar": bar, "limit": min(300, limit)},
        )
        return self._candles_frame(rows)

    def historical_candles(
        self, inst_id: str, bar: str = "1H", limit: int = 1000,
    ) -> pd.DataFrame:
        instrument = self._instrument_id(inst_id)
        frames = [self.candles(instrument, bar, min(300, limit))]
        remaining = max(0, int(limit) - sum(len(frame) for frame in frames))
        cursor = (
            int(frames[0]["timestamp"].min())
            if frames and not frames[0].empty else None
        )
        while remaining > 0 and cursor:
            rows = self._request(
                "GET", "/api/v5/market/history-candles",
                {
                    "instId": instrument,
                    "bar": bar,
                    "limit": min(100, remaining),
                    "after": str(cursor),
                },
            )
            frame = self._candles_frame(rows)
            if frame.empty:
                break
            frames.append(frame)
            next_cursor = int(frame["timestamp"].min())
            if next_cursor >= cursor:
                break
            cursor = next_cursor
            merged_count = len(
                pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset=["timestamp"])
            )
            remaining = max(0, int(limit) - merged_count)
        if not frames:
            return pd.DataFrame()
        return (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["timestamp"])
            .sort_values("timestamp")
            .tail(int(limit))
            .reset_index(drop=True)
        )

    def ticker(self, inst_id: str) -> dict:
        rows = self._request(
            "GET", "/api/v5/market/ticker", {"instId": self._instrument_id(inst_id)}
        )
        if not rows:
            raise OkxApiError("欧易没有返回该标的行情")
        return rows[0]

    def funding_rate(self, inst_id: str) -> dict:
        rows = self._request(
            "GET", "/api/v5/public/funding-rate",
            {"instId": normalize_swap_symbol(inst_id)},
        )
        return rows[0] if rows else {}

    def open_interest(self, inst_id: str) -> dict:
        rows = self._request(
            "GET", "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": normalize_swap_symbol(inst_id)},
        )
        return rows[0] if rows else {}

    def instruments(self) -> list[CryptoInstrument]:
        rows = self._request("GET", "/api/v5/public/instruments", {"instType": "SPOT"})
        instruments = []
        for row in rows:
            try:
                tick_size = Decimal(str(row.get("tickSz") or ""))
                lot_size = Decimal(str(row.get("lotSz") or ""))
                min_size = Decimal(str(row.get("minSz") or ""))
            except (ArithmeticError, ValueError):
                continue
            if tick_size <= 0 or lot_size <= 0 or min_size <= 0:
                continue
            instruments.append(
                CryptoInstrument(
                    inst_id=row["instId"],
                    base_currency=row["baseCcy"],
                    quote_currency=row["quoteCcy"],
                    tick_size=tick_size,
                    lot_size=lot_size,
                    min_size=min_size,
                    state=row.get("state", ""),
                )
            )
        return instruments

    def account_balance(self) -> list[dict]:
        return self._request("GET", "/api/v5/account/balance", private=True)

    def swap_instruments(self) -> list[CryptoSwapInstrument]:
        rows = self._request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})
        instruments = []
        for row in rows:
            if not str(row.get("instId", "")).endswith("-USDT-SWAP"):
                continue
            try:
                values = [
                    Decimal(str(row.get(key) or ""))
                    for key in ("tickSz", "lotSz", "minSz", "ctVal")
                ]
            except (ArithmeticError, ValueError):
                continue
            if min(values) <= 0:
                continue
            instruments.append(CryptoSwapInstrument(
                row["instId"], *values,
                row.get("ctValCcy", ""), row.get("settleCcy", ""),
                row.get("state", ""),
            ))
        return instruments

    def positions(self, inst_type: str = "SWAP") -> list[dict]:
        return self._request(
            "GET", "/api/v5/account/positions",
            {"instType": inst_type}, private=True,
        )

    def pending_orders(self, inst_type: str = "SPOT") -> list[dict]:
        return self._request(
            "GET", "/api/v5/trade/orders-pending", {"instType": inst_type}, private=True
        )

    def order_history(self, inst_type: str = "SPOT") -> list[dict]:
        return self._request(
            "GET", "/api/v5/trade/orders-history", {"instType": inst_type}, private=True
        )

    def fills(self, inst_type: str = "SPOT") -> list[dict]:
        return self._request(
            "GET", "/api/v5/trade/fills", {"instType": inst_type}, private=True
        )

    def set_position_mode(self, position_mode: str = "net_mode") -> dict:
        if position_mode != "net_mode":
            raise OkxApiError("安全锁：首版永续合约只允许净持仓模式 net_mode")
        rows = self._request(
            "POST", "/api/v5/account/set-position-mode",
            payload={"posMode": position_mode}, private=True,
        )
        return rows[0] if rows else {}

    def set_swap_leverage(
        self, inst_id: str, leverage: Decimal, margin_mode: str = "isolated",
    ) -> dict:
        if margin_mode != "isolated" or not Decimal("1") <= leverage <= Decimal("3"):
            raise OkxApiError("安全锁：永续合约只允许逐仓且杠杆为 1 到 3 倍")
        rows = self._request(
            "POST", "/api/v5/account/set-leverage",
            payload={
                "instId": normalize_swap_symbol(inst_id),
                "lever": format(leverage, "f"),
                "mgnMode": margin_mode,
            },
            private=True,
        )
        return rows[0] if rows else {}

    def place_spot_order(self, request: CryptoOrderRequest) -> dict[str, Any]:
        if not self.settings.enable_demo_orders:
            raise OkxApiError("安全锁：欧易模拟下单开关未启用")
        payload = {
            "instId": request.inst_id, "tdMode": "cash", "side": request.side,
            "ordType": request.order_type, "sz": format(request.size, "f"),
        }
        if request.price is not None:
            payload["px"] = format(request.price, "f")
        if request.client_order_id:
            payload["clOrdId"] = request.client_order_id
        rows = self._request("POST", "/api/v5/trade/order", payload=payload, private=True)
        if not rows:
            raise OkxApiError("欧易下单响应为空，请查询待成交订单后再重试")
        row = rows[0]
        if str(row.get("sCode", "0")) != "0":
            raise OkxApiError(f"欧易下单失败：{row.get('sMsg') or row.get('sCode')}")
        return row

    def place_swap_order(self, request: CryptoSwapOrderRequest) -> dict[str, Any]:
        if not self.settings.enable_demo_orders:
            raise OkxApiError("安全锁：欧易模拟下单开关未启用")
        payload = {
            "instId": request.inst_id,
            "tdMode": request.margin_mode,
            "side": request.side,
            "ordType": request.order_type,
            "sz": format(request.contracts, "f"),
            "reduceOnly": request.reduce_only,
        }
        if request.price is not None:
            payload["px"] = format(request.price, "f")
        if request.client_order_id:
            payload["clOrdId"] = request.client_order_id
        rows = self._request(
            "POST", "/api/v5/trade/order", payload=payload, private=True,
        )
        if not rows:
            raise OkxApiError("欧易合约下单响应为空，请先查询合约待成交订单")
        row = rows[0]
        if str(row.get("sCode", "0")) != "0":
            raise OkxApiError(f"欧易合约下单失败：{row.get('sMsg') or row.get('sCode')}")
        return row

    def cancel_order(self, inst_id: str, order_id: str) -> dict:
        rows = self._request(
            "POST", "/api/v5/trade/cancel-order",
            payload={"instId": self._instrument_id(inst_id), "ordId": str(order_id)},
            private=True,
        )
        return rows[0] if rows else {}
