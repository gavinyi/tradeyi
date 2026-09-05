# Windows 快速开始指南

## 🚀 5分钟快速部署

### 第一步：准备环境
1. 安装 Python 3.11+
2. 复制项目文件到 `C:\trade`

### 第二步：运行部署脚本
```powershell
cd C:\trade
.\deploy-windows.ps1
```

### 第三步：启动系统
双击运行 `start.bat`

---

## 📁 文件清单

| 文件 | 说明 |
|------|------|
| `WINDOWS_MIGRATION.md` | 完整迁移指南 |
| `deploy-windows.ps1` | 自动部署脚本 |
| `start.bat` | 启动服务（双击运行）|
| `stop.bat` | 停止服务 |

---

## 🎯 核心操作

### 启动系统
```batch
双击 start.bat
```

### 停止系统
```batch
双击 stop.bat
```

### 查看日志
```powershell
Get-Content data\service.log -Tail 50 -Wait
```

### 重启服务
```batch
stop.bat
start.bat
```

---

## 🔧 常用命令

### 手动启动交易服务
```powershell
uv run python scripts\run_service.py
```

### 手动启动 UI
```powershell
uv run streamlit run app.py
```

### 测试单次运行
```powershell
uv run python scripts\run_service.py --once
```

### 运行 ML 训练
```powershell
uv run python scripts\ml_retrain_monthly.py
```

---

## 📊 访问界面

- **Streamlit UI**: http://localhost:8501
- **查看日志**: `data\service.log`
- **数据库**: `data\trading.db`

---

## ⚠️ 常见问题速查

| 问题 | 解决方法 |
|------|---------|
| 无法连接富途 OpenD | 1. 确保富途牛牛已登录<br>2. 启动 OpenD 服务 |
| Python 包编译失败 | 安装 Visual Studio Build Tools |
| 路径错误 | 确保在 `C:\trade` 目录运行 |
| 端口被占用 | 修改 `.env` 中的端口配置 |
| 服务启动失败 | 查看 `data\service.log` |

---

## 🆘 获取帮助

详细文档：`WINDOWS_MIGRATION.md`

---

## 📦 重要文件位置

```
C:\trade\
├── .env                    # 配置文件 ⚠️ 必须配置
├── data\
│   ├── trading.db         # 数据库
│   └── service.log        # 服务日志
├── data\ml_models\        # ML 模型
└── .venv\                 # Python 虚拟环境
```

---

## ✅ 迁移检查清单

复制到 Windows 时，请确保这些文件都已复制：

- [ ] `src/` 目录（所有源代码）
- [ ] `scripts/` 目录（所有脚本）
- [ ] `pages/` 目录（Streamlit 页面）
- [ ] `data/trading.db`（数据库文件）
- [ ] `.env`（配置文件）
- [ ] `pyproject.toml`（项目配置）
- [ ] `uv.lock`（依赖锁文件）
- [ ] `app.py`（Streamlit 入口）
- [ ] `data/ml_models/`（ML 模型，可选）

---

## 🔄 日常维护

### 每日
- 查看 `data\service.log` 检查运行状态
- 访问 http://localhost:8501 查看交易情况

### 每周
- 备份 `data\trading.db`

### 每月
- 运行 ML 模型重训练（已配置自动执行）

---

## 🎓 学习资源

1. **完整文档**: `WINDOWS_MIGRATION.md`
2. **项目文档**: `docs/` 目录
3. **代码示例**: `scripts/` 目录

---

*提示：首次运行前，请务必阅读 `WINDOWS_MIGRATION.md` 完整指南*
