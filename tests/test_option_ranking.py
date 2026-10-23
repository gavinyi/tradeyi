from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import futu_quant.option_ranking as ork
from futu_quant.option_ranking import expiry_profit_probability, rank_long_option_candidates


def test_expiry_profit_probability_uses_premium_in_breakeven() -> None:
    probability, breakeven = expiry_profit_probability(
        100, 100, 5, 0.30, 30 / 365, "CALL"
    )
    itm_probability, _ = expiry_profit_probability(
        100, 100, 0, 0.30, 30 / 365, "CALL"
    )
    assert breakeven == 105
    assert 0 < probability < itm_probability < 0.5


def test_rank_candidates_is_highest_probability_first() -> None:
    candidates = pd.DataFrame({
        "code": ["EXPENSIVE", "CHEAPER"],
        "option_type": ["CALL", "CALL"],
        "strike_price": [100.0, 100.0],
        "strike_time": ["2026-08-15", "2026-08-15"],
        "ask_price": [8.0, 3.0],
        "option_implied_volatility": [30.0, 30.0],
        "spread_pct": [3.0, 3.0],
        "option_open_interest": [1000, 1000],
    })
    ranked = rank_long_option_candidates(candidates, 100, date(2026, 7, 16))
    assert ranked["code"].tolist() == ["CHEAPER", "EXPENSIVE"]
    assert ranked["probability_confidence"].tolist() == ["高", "高"]
    assert ranked.iloc[0]["model_expected_return"] > ranked.iloc[1]["model_expected_return"]


# --------------------------------------------------------------------------
# 京/纽跨日边界回归（Reviewer 要求）：`as_of` 兜底与显式传参必须同为 NY 口径
# --------------------------------------------------------------------------

_NY = ZoneInfo("America/New_York")
# 北京 2026-10-24 00:30 == UTC 2026-10-23 16:30 == 纽约 2026-10-23 12:30
_INSTANT = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc)
_NY_TODAY = date(2026, 10, 23)
_BEIJING_TODAY = date(2026, 10, 24)


class _FrozenDatetime(datetime):
    """冻结 `datetime.now(tz)`，让「北京跨日 / 纽约当日」可复现。"""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return _INSTANT.astimezone(tz) if tz is not None else _INSTANT.replace(tzinfo=None)


def _boundary_candidates() -> pd.DataFrame:
    return pd.DataFrame({
        "code": ["NEAR", "FAR"],
        "option_type": ["CALL", "CALL"],
        "strike_price": [100.0, 110.0],
        "strike_time": ["2026-11-15", "2026-11-15"],
        "ask_price": [3.0, 4.0],
        "option_implied_volatility": [30.0, 30.0],
        "spread_pct": [3.0, 3.0],
        "option_open_interest": [1000, 1000],
    })


def test_cross_date_boundary_fallback_uses_new_york_date(monkeypatch) -> None:
    """京/纽跨日时：`as_of=None` 的兜底必须等于 `as_of=NY_TODAY`，且不等于北京日期口径。

    这条同时覆盖本次两处改动：`option_ranking` 的兜底默认值、`volatility_roll` 的显式传参。
    `dte` 直接喂给到期盈利概率 → 决定 probability / breakeven / confidence / 排序；
    若兜底回落到本机（北京）日期，DTE 会差 1 天，概率与排序随之失真。
    """
    monkeypatch.setattr(ork, "datetime", _FrozenDatetime)

    fallback = rank_long_option_candidates(_boundary_candidates(), 100)
    explicit = rank_long_option_candidates(_boundary_candidates(), 100, _NY_TODAY)

    for column in (
        "estimated_profit_probability",
        "model_expected_return",
        "probability_confidence",
    ):
        assert fallback[column].tolist() == explicit[column].tolist(), (
            f"{column} 在京/纽跨日时与显式 NY 日期不一致 —— 兜底没有用 NY 日期"
        )
    assert fallback["code"].tolist() == explicit["code"].tolist()

    # 反向：若兜底仍是北京日期，结果会与北京口径相同 —— 说明守卫测不出回归。
    beijing = rank_long_option_candidates(_boundary_candidates(), 100, _BEIJING_TODAY)
    assert fallback["estimated_profit_probability"].tolist() != beijing[
        "estimated_profit_probability"
    ].tolist(), "兜底与北京日期口径结果相同 —— 这条测试无法抓住「兜底回落本机日期」的回归"
