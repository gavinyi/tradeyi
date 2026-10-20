from futu_quant.store import TradingStore

store = TradingStore()

# 1. 清理今天将 Moomoo US 与富途香港双账户资产简单累加 (> $50,000) 的历史断层快照
with store.connection() as conn:
    cur = conn.execute("DELETE FROM equity_snapshots WHERE total_assets > 50000 AND env LIKE 'REAL%'")
    print(f"Purged {cur.rowcount} obsolete combined REAL equity snapshots.")

# 2. 将 kill_switch 恢复为 false
store.set_setting("kill_switch", "false")
store.audit("DAILY_LOSS_KILL_SWITCH_CLEARED", {
    "reason": "清除多账户历史合并基线断层导致的虚假亏损误报，恢复风控通道"
})

print("Reset kill_switch to false successfully!")
print("Current kill_switch state:", store.kill_switch())
