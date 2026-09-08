from __future__ import annotations

import os
import re


OPTION_PATTERN = re.compile(r"^US\..*\d{6}[CP]\d+$")


def estimate_fees(symbol: str, side: str, quantity: float, price: float) -> float:
    """Conservative configurable US-paper fee estimate; never used for real billing."""
    quantity, price = abs(float(quantity)), max(0.0, float(price))
    if OPTION_PATTERN.match(symbol):
        per_contract = float(os.getenv("FEE_OPTION_PER_CONTRACT", "0.65"))
        minimum = float(os.getenv("FEE_OPTION_MINIMUM", "1.00"))
        return round(max(minimum, quantity * per_contract), 4)
    commission_per_share = float(os.getenv("FEE_STOCK_PER_SHARE", "0.005"))
    minimum = float(os.getenv("FEE_STOCK_MINIMUM", "1.00"))
    regulatory = 0.0
    if side.upper() == "SELL":
        regulatory = quantity * price * float(os.getenv("FEE_SELL_REGULATORY_RATE", "0.0000278"))
    return round(max(minimum, quantity * commission_per_share) + regulatory, 4)
