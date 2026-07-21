from __future__ import annotations

import pandas as pd

from futu_quant.account_metrics import account_pnl_metrics


def test_account_pnl_uses_position_detail_and_ignores_na():
    positions = pd.DataFrame({
        "today_pl_val": [-100, "N/A", 25],
        "pl_val": [-200, "N/A", 50],
    })

    result = account_pnl_metrics(positions)

    assert result["today_position_pnl"] == -75
    assert result["total_position_pnl"] == -150
