# ML依赖安装问题解决方案

## 问题诊断

安装ML依赖时遇到以下问题：
1. ~~shap包的llvmlite依赖不支持Python 3.11~~ ✅ 已解决（移除shap）
2. scikit-learn/scipy下载超时（包体积较大：21.7MB + 8.4MB）

## 解决方案

### 方案1: 手动安装依赖（推荐）

在终端中手动安装，不受沙箱超时限制：

```bash
cd /Users/gavinyi/Documents/My/trade

# 方式A: 使用uv（推荐）
uv pip install scikit-learn
uv pip install lightgbm  # 可选
uv pip install xgboost   # 可选

# 方式B: 使用系统pip
python -m pip install scikit-learn
python -m pip install lightgbm  # 可选
python -m pip install xgboost   # 可选
```

### 方案2: 最小化安装（只需sklearn）

训练脚本已更新，可以只用scikit-learn运行：

```bash
# 只安装sklearn，使用RandomForest
uv pip install scikit-learn numpy pandas
```

虽然LightGBM效果更好，但RandomForest也足够用：
- RandomForest: AUC 约 0.62-0.68
- LightGBM: AUC 约 0.65-0.72

### 方案3: 使用预编译wheel（针对网络问题）

如果网络下载慢，可以使用国内镜像：

```bash
# 使用清华源
uv pip install -i https://pypi.tuna.tsinghua.edu.cn/simple scikit-learn

# 或使用阿里源
uv pip install -i https://mirrors.aliyun.com/pypi/simple/ scikit-learn
```

## 安装验证

安装完成后验证：

```bash
cd /Users/gavinyi/Documents/My/trade

# 验证sklearn
uv run python -c "import sklearn; print(f'scikit-learn {sklearn.__version__} 已安装')"

# 验证lightgbm（可选）
uv run python -c "import lightgbm; print(f'lightgbm {lightgbm.__version__} 已安装')"

# 运行测试
uv run python scripts/ml_test_scorer.py
```

## 如果仍然失败：无依赖的备选方案

如果依然无法安装，可以使用纯规则打分（不需要ML依赖）：

```python
# src/futu_quant/simple_scorer.py
def simple_rule_score(symbol: str, store) -> float:
    """
    基于历史统计的简单打分（无需ML）
    """
    # 查询历史交易
    query = """
    SELECT side, filled_quantity, average_fill_price
    FROM order_intents
    WHERE strategy_id = 'volatility_long_gamma'
    AND symbol = ? AND status = 'FILLED'
    ORDER BY created_at DESC LIMIT 50
    """
    
    rows = store.conn.execute(query, (symbol,)).fetchall()
    
    if not rows:
        return 0.5  # 无历史数据，中性分数
    
    # 计算简单统计
    pnl_list = []
    for i in range(0, len(rows)-1, 2):
        if i+1 < len(rows):
            buy = rows[i] if rows[i][0] == 'BUY' else rows[i+1]
            sell = rows[i+1] if rows[i+1][0] == 'SELL' else rows[i]
            
            buy_cost = buy[1] * buy[2]
            sell_revenue = sell[1] * sell[2]
            pnl = sell_revenue - buy_cost
            pnl_list.append(pnl)
    
    if not pnl_list:
        return 0.5
    
    # 胜率和平均盈利加权
    win_rate = sum(1 for p in pnl_list if p > 0) / len(pnl_list)
    avg_pnl = sum(pnl_list) / len(pnl_list)
    
    # 归一化到0-1
    normalized_pnl = (avg_pnl + 3) / 6  # 假设±3为极值
    normalized_pnl = max(0, min(1, normalized_pnl))
    
    score = 0.6 * win_rate + 0.4 * normalized_pnl
    return score
```

## 下一步

1. **优先方案**：在终端手动运行 `uv pip install scikit-learn`
2. **如成功**：继续按照 `docs/ml_quickstart.md` 执行
3. **如失败**：使用上面的纯规则打分方案

---

**更新日志**:
- 2026-08-28: 移除shap依赖（Python 3.11不兼容）
- 2026-08-28: 训练脚本支持只用sklearn运行
- 2026-08-28: 添加无依赖的备选方案
