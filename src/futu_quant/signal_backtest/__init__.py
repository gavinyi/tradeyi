"""历史信号回测引擎包（只读）：信号重放 → 分层撮合 → 对账与报告。

**命名说明**：包名 `signal_backtest` 避免与既有 `futu_quant/backtest.py`
（K 线级回测）冲突——后者是既有生产模块，本包是信号级回测（PRD
`data/qa/prd_backtest_engine_20261107.md`，架构设计
`data/qa/backtest_engine_design_20261107.md`）。

只读铁律与确定性约束见各模块 docstring。
"""

from . import ledger, matcher, optimizer, replay  # noqa: F401  再导出：页面静态导入守卫要求包顶层可见子模块名

__all__ = ["ledger", "matcher", "optimizer", "replay"]
