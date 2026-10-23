"""#4 回归：波动率控制页的 campaign 列表与清理必须按**当前页租户**过滤。

背景（10-23 Reviewer 实测的失效模式）：
- 候选列表遍历**全部** campaign（含两个租户），持仓检查按 **code** 取
  （`shadow_positions.get(code)` / `paper_position_map.get(code)`），
  `current_quantity` 不分租户；
- 后果：实盘租户下一张**有真实持仓**的合约，在候选检查里读到的是**模拟盘的持仓**（=0）
  → 若同时 expired/invalid → 进清理列表 → 用户点一下 → **实盘 campaign 被删、
  持仓失去管理**（META 那次的失效形态）。

本文件守两件事：
1. **准入判据本身**（`futu_quant.tenant.campaign_belongs_to_tenant`）——可单测；
2. **页面真的用了它**，且删除走**完整身份**（AST 断言）——页面不是 importable 的
   Streamlit 脚本，所以这一层用 AST，而不是靠字符串包含。
"""
from __future__ import annotations

import ast
from pathlib import Path

from futu_quant.tenant import campaign_belongs_to_tenant

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "10_volatility_options_strategy.py"
REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


# --------------------------------------------------------------------------
# 1. 准入判据
# --------------------------------------------------------------------------
def test_other_tenant_campaign_is_excluded():
    assert campaign_belongs_to_tenant({"tenant_id": REAL, "code": "US.X"}, PAPER) is False
    assert campaign_belongs_to_tenant({"tenant_id": PAPER, "code": "US.X"}, REAL) is False


def test_same_tenant_and_legacy_records_are_included():
    # 同租户必须可见，否则本页会把自己的策略藏起来
    assert campaign_belongs_to_tenant({"tenant_id": PAPER, "code": "US.X"}, PAPER) is True
    # legacy（无租户）记录：早于租户分区写入，下一次写入会被收敛；藏起来反而无人可见
    assert campaign_belongs_to_tenant({"code": "US.X"}, PAPER) is True
    assert campaign_belongs_to_tenant({}, REAL) is True


def test_blank_tenant_id_is_treated_as_legacy_not_as_match():
    """`tenant_id: ""` 是"没有租户信息"，不是"租户叫空串"。"""
    assert campaign_belongs_to_tenant({"tenant_id": "", "code": "US.X"}, PAPER) is True
    assert campaign_belongs_to_tenant({"tenant_id": "   ", "code": "US.X"}, REAL) is True


# --------------------------------------------------------------------------
# 2. 页面真的按租户过滤，且删除走完整身份
# --------------------------------------------------------------------------
def _page_ast() -> ast.Module:
    return ast.parse(PAGE.read_text(encoding="utf-8"))


def test_page_filters_campaigns_by_current_tenant():
    """两处读取点（常用数据页签、已保存策略页签）都必须先过滤再使用。

    页租户变量名允许 `page_tenant_id` 或 `_page_tenant_id`（#1 之后统一为后者，
    在页面顶部算一次、两个页签共用）。
    """
    tree = _page_ast()
    source = ast.unparse(tree).replace("'", '"')
    filters = sum(
        source.count(f"campaign_belongs_to_tenant(item, {name})")
        for name in ("page_tenant_id", "_page_tenant_id")
    )
    assert filters >= 2, (
        f"页面里按当前页租户过滤 campaign 的调用只有 {filters} 处（需 >= 2）—— "
        "「常用数据」或「已保存策略」至少有一处仍在使用全量列表（跨租户可见/可删）"
    )
    # 过滤所依赖的页租户必须来自 broker，不能是硬编码常量或空串
    assert 'tenant_id_of_broker(broker) or ""' in source, (
        "页租户不是从 broker 解析出来的 —— 过滤会退化成固定值（跨租户隔离失效）"
    )


def test_page_deletes_campaigns_by_full_identity():
    """删除必须传完整身份（`{tenant}|{code}`），不能传裸 code。

    裸 code 在跨租户同 code 时命中多个租户：`delete_campaign` 会**抛异常拒绝**，
    于是"看着有候选、点下去删不掉"；而页面若因此回退成全局删除，就是跨租户误删。
    """
    tree = _page_ast()
    source = ast.unparse(tree).replace("'", '"')

    delete_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "delete_campaign"
    ]
    assert delete_calls, "页面里找不到 delete_campaign 调用 —— 删除路径可能被改写"
    for call in delete_calls:
        args = [ast.unparse(a).replace("'", '"') for a in call.args]
        assert len(args) >= 2, f"delete_campaign 调用缺少身份参数：{args}"
        assert args[1] in {"identity", 'item["identity"]'}, (
            f"delete_campaign 传的是裸 code（{args[1]!r}）而不是完整身份 —— "
            "跨租户同 code 时会被拒绝或误删"
        )

    # 候选列表必须把身份算好带走，而不是在删除时现拼裸 code
    assert '"identity": _campaign_identity(campaign)' in source, (
        "清理候选没有带上完整身份 —— 删除阶段无法做到按租户精确删除"
    )


def test_page_identity_helper_is_reused_not_reimplemented():
    """身份必须复用 config 的**唯一**实现，页面不得另拼一份。"""
    tree = _page_ast()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "futu_quant.config":
            imported.update(alias.name for alias in node.names)
    assert "_campaign_identity" in imported, (
        "页面没有导入 config._campaign_identity —— 自己拼身份会与存储的去重口径漂移"
    )
