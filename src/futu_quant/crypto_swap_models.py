from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import re


SWAP_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,20}-USDT-SWAP$")


def normalize_swap_symbol(value: str) -> str:
    symbol = str(value).strip().upper().replace("/", "-")
    if not SWAP_SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("请输入欧易 USDT 永续合约代码，例如 BTC-USDT-SWAP")
    return symbol


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class CryptoSwapInstrument:
    inst_id: str
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    contract_value: Decimal
    contract_currency: str
    settle_currency: str
    state: str = "live"

    def __post_init__(self):
        object.__setattr__(self, "inst_id", normalize_swap_symbol(self.inst_id))
        if min(self.tick_size, self.lot_size, self.min_size, self.contract_value) <= 0:
            raise ValueError("合约价格、张数精度和面值必须大于 0")

    def normalize_price(self, value: Decimal) -> Decimal:
        return _floor_step(Decimal(value), self.tick_size)

    def normalize_contracts(self, value: Decimal) -> Decimal:
        return _floor_step(Decimal(value), self.lot_size)


@dataclass(frozen=True)
class CryptoSwapOrderRequest:
    inst_id: str
    side: str
    contracts: Decimal
    price: Decimal | None
    reduce_only: bool
    intent: str
    order_type: str = "limit"
    margin_mode: str = "isolated"
    client_order_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "inst_id", normalize_swap_symbol(self.inst_id))
        if self.side not in {"buy", "sell"}:
            raise ValueError("合约订单方向只允许 buy 或 sell")
        if self.contracts <= 0:
            raise ValueError("合约张数必须大于 0")
        if self.margin_mode != "isolated":
            raise ValueError("安全锁：首版永续合约只允许逐仓 isolated")
        if self.order_type not in {"limit", "market", "post_only", "ioc", "fok"}:
            raise ValueError("不支持的合约订单类型")
        if self.order_type != "market" and (self.price is None or self.price <= 0):
            raise ValueError("限价类合约订单必须提供有效价格")
        closing = self.intent in {"CLOSE_LONG", "CLOSE_SHORT"}
        if closing and not self.reduce_only:
            raise ValueError("合约平仓订单必须设置 reduceOnly=true")
        if not closing and self.reduce_only:
            raise ValueError("合约开仓订单不能设置 reduceOnly=true")
