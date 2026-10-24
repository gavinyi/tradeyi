"""`registered_fill_*` / `fill_registered_at` 是**审计留痕，不是保护机制**。

P0-4 复核结论（Reviewer 实测）：

- 读侧全仓 **0 处业务消费** —— 真正防止「旧合约被删」的防线是成交账本
  （`store.fills()` / `has_recorded_fill()`）；
- 写侧只在 `owned=True` 的 MARKED 路径写，ADOPT 路径不写；
- 因此它是「写了没人读、且 ADOPT 分支还漏写」的半状态字段。
  当时裁定：**不补写**（补写只会让"假保护"更像真的），并入后续清理项。

将来若有人把它当成交判据，ADOPT 出来的仓位会被误判为"无成交"——这正是
`registered_fill_*` 最危险的地方。本文件用**静态判据**钉住「只在写入侧出现」：

变异：在 `src/` 其它模块里加一行读取 `registered_fill_qty` → 本用例转红。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 允许出现这些字段的文件：写入方（order_push）+ 测试自身。
ALLOWED = {
    "src/futu_quant/order_push.py",
}

TOKENS = ("registered_fill_qty", "registered_fill_price", "fill_registered_at")


def test_fill_registration_fields_have_no_business_readers():
    offenders: list[str] = []
    for base in ("src", "scripts", "pages"):
        for path in (ROOT / base).rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if relative in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in TOKENS:
                if token in text:
                    offenders.append(f"{relative}:{token}")
    assert not offenders, (
        "registered_fill_* 只能作为审计留痕出现在写入侧；发现业务代码引用：\n  "
        + "\n  ".join(offenders)
        + "\n真正的成交判据是 store.fills() / has_recorded_fill()"
    )


def test_order_push_still_writes_registration_fields():
    """写入侧必须保留（历史语义：MARKED 路径登记一次），别顺手删成另一个半状态。"""
    text = (ROOT / "src" / "futu_quant" / "order_push.py").read_text(encoding="utf-8")
    for token in TOKENS:
        assert token in text, f"order_push 不再写 {token} —— 若要清理请整体移除并更新本守卫"
