from __future__ import annotations

import re


OPTION_PATTERN = re.compile(r"^US\..*\d{6}[CP]\d+$")


def estimate_fees(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    security_firm: str = "FUTUSECURITIES",
) -> float:
    """Exact Futu fee model matching official tariff schedule.
    
    Supports both Futu Securities HK (FUTUSECURITIES) and Moomoo US (FUTUINC).
    """
    quantity, price = abs(float(quantity)), max(0.0, float(price))
    if quantity <= 0:
        return 0.0

    is_sell = str(side).upper() == "SELL"

    if OPTION_PATTERN.match(symbol):
        # 1. 佣金 (Commission): 权利金 > 0.10 为 $0.65/张，<= 0.10 为 $0.15/张；每笔订单最低 $1.99
        comm_per_contract = 0.65 if price > 0.10 else 0.15
        commission = max(1.99, quantity * comm_per_contract)

        # 2. 平台使用费 (Platform Fee): $0.30/张
        platform_fee = quantity * 0.30

        # 3. 交收费 / 合约费 (Settlement / Contract Fee): 富途香港约 $0.18/张 (Moomoo US 为 $0.65/张)
        settlement_fee = quantity * (0.65 if str(security_firm).upper() == "FUTUINC" else 0.18)

        # 4. 期权清算费 (OCC Clearing Fee): $0.025/张
        occ_fee = quantity * 0.025

        # 5. 期权监管费 (ORF Fee): $0.0122/张
        orf_fee = quantity * 0.0122

        # 6. 综合审计跟踪监管费 (CAT Fee): $0.0003/张
        cat_fee = quantity * 0.0003

        # 7. 证监会规费 (SEC Fee): 0.0000206 * 成交金额 (仅卖出收取，最低 $0.01)
        sec_fee = 0.0
        if is_sell and price > 0:
            notional = quantity * price * 100.0
            sec_fee = max(0.01, notional * 0.0000206)

        total = commission + platform_fee + settlement_fee + occ_fee + orf_fee + cat_fee + sec_fee
        return round(total, 4)

    # 美股正股 / ETF 收费模型
    notional = quantity * price
    # 1. 佣金: 0.0049/股，每笔最低 $0.99，最高 0.5% * 成交金额
    raw_comm = quantity * 0.0049
    commission = min(notional * 0.005, max(0.99, raw_comm)) if notional > 0 else max(0.99, raw_comm)

    # 2. 平台使用费: 0.005/股，每笔最低 $1.00，最高 0.5% * 成交金额
    raw_plat = quantity * 0.005
    platform_fee = min(notional * 0.005, max(1.00, raw_plat)) if notional > 0 else max(1.00, raw_plat)

    # 3. 交收费: 0.003/股
    settlement_fee = quantity * 0.003

    # 4. 证监会规费 (SEC): 0.0000206 * 成交金额 (仅卖出收取，最低 $0.01)
    sec_fee = 0.0
    if is_sell and notional > 0:
        sec_fee = max(0.01, notional * 0.0000206)

    # 5. 综合审计跟踪监管费 (CAT): $0.000003/股
    cat_fee = quantity * 0.000003

    total = commission + platform_fee + settlement_fee + sec_fee + cat_fee
    return round(total, 4)
