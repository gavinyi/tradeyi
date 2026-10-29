"""回归：单笔止损限额必须「UI 可见、可改、留痕可审计」（P1-2/P1-3 的界面硬要求）。

背景：
- `max_position_loss_usd` / `max_position_loss_ratio` 已在下单前强制校验，缺省即
  **拒绝开仓并记 ERROR**（`VOLATILITY_STOP_LIMIT_MISSING`）。
- 但限额此前**在界面上没有任何输入项** —— 用户看不到、改不了、也无法确认自己配的是多少，
  而系统却以「缺省即拒绝」的强语义依赖它。这是「配置错误不可见」。

修复（本文件锁定）：
1. 页面必须暴露这两个限额的输入项；
2. 保存策略时，两个限额必须**显式写入 campaign**（否则新策略立刻进入「拒绝开仓」）；
3. 改动必须留痕：审计事件里带**改动前的旧值**，可追溯到人、时间、新旧值。
"""
from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "10_volatility_options_strategy.py"
RATIO_KEY = "max_position_loss_ratio"
USD_KEY = "max_position_loss_usd"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_page_exposes_both_limit_inputs():
    """两个限额都必须有 number_input，用户可见可改。"""
    src = _page()
    assert 'number_input(\n            "相对止损（权利金亏损比例）"' in src, "缺少相对止损输入项"
    # 2026-10-29：绝对项语义从「固定止损额」改为「上限」（实际值 = min(上限, 8%×权利金)），
    # label 必须跟着改，否则用户会以为填 300 就是亏 300 才止损。
    assert "绝对止损上限（美元）" in src, "缺少绝对止损上限输入项"
    # 默认值必须与生效口径一致（0.25 / 300），否则界面显示值会误导用户
    assert 'setting_float("volatility_default_max_position_loss_ratio", 0.25)' in src
    assert 'setting_float("volatility_default_max_position_loss_usd", 300.0)' in src


def test_page_exposes_premium_scaled_stop_ratio():
    """8% 缩放比例必须可见可改 —— 它决定绝对止损额的量级。"""
    src = _page()
    assert '"绝对止损占建仓权利金比例"' in src, "缺少权利金比例输入项"
    assert 'setting_float("volatility_default_max_loss_premium_ratio", 0.08)' in src
    assert '"max_loss_premium_ratio": float(max_loss_premium_ratio)' in src, "未写入 campaign"


def test_limits_are_persisted_into_campaigns():
    """保存策略时必须把两个限额显式写进 campaign，否则新策略会被自己的门拒开仓。"""
    src = _page()
    assert f'"{RATIO_KEY}": float(max_position_loss_ratio)' in src, "相对止损未写入 campaign"
    assert f'"{USD_KEY}": float(max_position_loss_usd)' in src, "绝对止损未写入 campaign"
    # 两个键必须进入批量下发参数集（否则「应用到所有已保存策略」不会更新它们）
    src = _page()
    assert src.count(f'"{RATIO_KEY}": float(max_position_loss_ratio)') >= 2, "相对止损未同时进保存路径与批量下发参数集"
    assert src.count(f'"{USD_KEY}": float(max_position_loss_usd)') >= 2, "绝对止损未同时进保存路径与批量下发参数集"


def test_limit_changes_are_audited_with_previous_values():
    """改动必须留痕，且带改动前的旧值（可审计 = 能回答「谁、何时、从多少改成多少」）。"""
    src = _page()
    assert '"previous_stop_limits": previous_limits' in src, "批量下发未记录旧值"
    assert '"previous": previous_defaults' in src, "保存默认值未记录旧值"
    assert '"changed_by": "streamlit"' in src, "审计事件未记录改动来源"


def test_ui_warns_about_missing_limit_enforcement():
    """界面必须说明「缺省即拒绝开仓」，避免用户以为不填也能开仓。"""
    assert "拒绝开仓" in _page() and "缺省" in _page()


def test_ui_documents_or_semantics():
    """界面必须写明两层是 OR（取较紧者），否则下一个人会以为是顺序兜底。"""
    src = _page()
    assert "取较紧者" in src, "界面未说明两层止损的 OR 语义"
    assert "不是「先相对、不够再绝对」" in src, "界面未否定顺序兜底的误解"
