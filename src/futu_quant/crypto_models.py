from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import re


CRYPTO_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}-[A-Z0-9]{2,10}$")


def normalize_crypto_symbol(value: str) -> str:
    symbol = str(value).strip().upper().replace("/", "-")
    if not CRYPTO_SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("请输入欧易现货代码，例如 BTC-USDT 或 ETH-USDT")
    return symbol


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("交易精度必须大于 0")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class CryptoInstrument:
    inst_id: str
    base_currency: str
    quote_currency: str
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    state: str = "live"

    def normalize_price(self, value: Decimal) -> Decimal:
        return _floor_step(Decimal(value), self.tick_size)

    def normalize_size(self, value: Decimal) -> Decimal:
        return _floor_step(Decimal(value), self.lot_size)


@dataclass(frozen=True)
class CryptoOrderRequest:
    inst_id: str
    side: str
    order_type: str
    size: Decimal
    price: Decimal | None = None
    client_order_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "inst_id", normalize_crypto_symbol(self.inst_id))
        if self.side not in {"buy", "sell"}:
            raise ValueError("币订单方向只允许 buy 或 sell")
        if self.order_type not in {"limit", "market", "post_only", "ioc", "fok"}:
            raise ValueError("不支持的欧易现货订单类型")
        if self.size <= 0:
            raise ValueError("下单数量必须大于 0")
        if self.order_type != "market" and (self.price is None or self.price <= 0):
            raise ValueError("限价类订单必须提供有效价格")

