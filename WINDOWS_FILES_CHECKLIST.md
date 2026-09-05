# Windows 迁移文件清单

## 📝 已创建的 Windows 支持文件

### 1. 文档
- ✅ `WINDOWS_MIGRATION.md` - 完整迁移指南（包含所有步骤和故障排除）
- ✅ `WINDOWS_QUICKSTART.md` - 5分钟快速开始指南
- ✅ `WINDOWS_FILES_CHECKLIST.md` - 本文件（迁移清单）

### 2. 脚本
- ✅ `deploy-windows.ps1` - PowerShell 自动部署脚本
- ✅ `start.bat` - 一键启动脚本（双击运行）
- ✅ `stop.bat` - 一键停止脚本

---

## 📦 需要从 macOS 复制到 Windows 的文件

### ✅ 必需文件（必须复制）

```
trade/
├── src/                          # ⭐ 核心源代码
│   └── futu_quant/              
├── scripts/                      # ⭐ 所有脚本
│   ├── run_service.py           # 主服务
│   ├── ml_retrain_monthly.py    # ML 训练
│   └── ...                      
├── pages/                        # ⭐ Streamlit UI 页面
├── data/
│   └── trading.db               # ⭐⭐⭐ 数据库（重要！）
├── .env                          # ⭐⭐⭐ 配置文件（重要！）
├── pyproject.toml               # ⭐ 项目配置
├── uv.lock                      # ⭐ 依赖锁文件
├── app.py                       # ⭐ Streamlit 入口
└── README.md                    # 说明文档
```

### 🎯 可选但建议复制

```
├── data/ml_models/              # 💡 ML 模型文件
│   └── volatility_scorer_v3.pkl
├── docs/                        # 💡 项目文档
├── tests/                       # 💡 测试文件
├── changelog.md                 # 💡 更新日志
└── deploy/                      # 💡 部署配置
```

### ⚠️ 不需要复制（Windows 会重新生成）

```
❌ .venv/                        # Python 虚拟环境
❌ .git/                         # Git 仓库（如果用 Git 克隆则不需要）
❌ __pycache__/                  # Python 缓存
❌ *.pyc                         # 编译文件
❌ .DS_Store                     # macOS 文件
❌ data/*.log                    # 旧日志
❌ data/*.pid                    # PID 文件
```

---

## 🔧 复制后需要修改的文件

### 1. `.env` 文件
**需要检查的配置：**

```bash
# 富途 OpenD 配置（通常不需要改）
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111

# 交易模式
EXECUTION_MODE=PAPER  # 或 SHADOW

# 启用的策略
SERVICE_STRATEGIES=Core Satellite,Earnings Gamma,Volatility Long Gamma

# 数据库路径（自动处理，无需改）
# DATABASE_PATH=data/trading.db
```

### 2. 无需修改的文件
✅ 大部分代码使用 `Path` 对象，自动兼容 Windows 路径
✅ 数据库文件直接复制即可
✅ ML 模型文件直接复制即可

---

## 📋 迁移步骤（推荐方式）

### 方式1：U盘/移动硬盘直接复制

#### 在 macOS 上：
```bash
# 1. 打包必需文件
cd /Users/gavinyi/Documents/My/trade
mkdir -p ~/Desktop/trade-windows

# 2. 复制核心文件
cp -r src pages scripts data ~/Desktop/trade-windows/
cp .env pyproject.toml uv.lock app.py README.md ~/Desktop/trade-windows/

# 3. 复制 Windows 支持文件
cp WINDOWS_*.md deploy-windows.ps1 start.bat stop.bat ~/Desktop/trade-windows/

# 4. 可选：复制 ML 模型
cp -r data/ml_models ~/Desktop/trade-windows/data/ 2>/dev/null || true

# 5. 复制到 U盘
# 将 ~/Desktop/trade-windows 文件夹复制到 U盘
```

#### 在 Windows 上：
```powershell
# 1. 从 U盘复制到 C:\trade
Copy-Item E:\trade-windows C:\trade -Recurse

# 2. 运行部署脚本
cd C:\trade
.\deploy-windows.ps1

# 3. 启动服务
.\start.bat
```

---

### 方式2：Git 仓库（推荐用于协作）

#### 在 macOS 上：
```bash
cd /Users/gavinyi/Documents/My/trade

# 添加 Windows 支持文件到 Git
git add WINDOWS_*.md deploy-windows.ps1 start.bat stop.bat
git commit -m "添加 Windows 支持"
git push
```

#### 在 Windows 上：
```powershell
# 克隆仓库
git clone <your-repo-url> C:\trade
cd C:\trade

# 运行部署
.\deploy-windows.ps1

# 启动
.\start.bat
```

---

### 方式3：网络共享/云盘

通过网络共享或云盘（OneDrive/Dropbox）同步文件夹。

---

## 🔍 验证清单

复制完成后，在 Windows 上检查：

### 文件完整性
- [ ] `src/futu_quant/` 目录存在且包含 Python 文件
- [ ] `scripts/` 目录包含所有脚本
- [ ] `pages/` 目录包含 Streamlit 页面
- [ ] `data/trading.db` 文件存在（检查大小）
- [ ] `.env` 文件存在且配置正确
- [ ] `pyproject.toml` 和 `uv.lock` 存在
- [ ] Windows 脚本文件存在（.ps1 和 .bat）

### 文件大小参考
```
data/trading.db         ~10-100 MB（取决于历史数据）
src/                    ~2-5 MB
scripts/                ~500 KB
pages/                  ~1-2 MB
```

---

## 🎯 部署后验证

### 1. 基础验证
```powershell
# 检查 Python
python --version

# 检查虚拟环境
.venv\Scripts\python.exe --version

# 检查数据库
sqlite3 data\trading.db "SELECT COUNT(*) FROM order_intents;"
```

### 2. 服务验证
```powershell
# 测试单次运行
uv run python scripts\run_service.py --once

# 查看日志
Get-Content data\service.log -Tail 20
```

### 3. UI 验证
```powershell
# 启动 UI
uv run streamlit run app.py

# 访问 http://localhost:8501
```

---

## 📞 获取帮助

如果遇到问题：

1. **查看完整文档**: `WINDOWS_MIGRATION.md`
2. **查看日志**: `data\service.log`
3. **检查配置**: `.env` 文件
4. **验证数据库**: 使用 sqlite3 工具

---

## 💡 提示

1. **首次运行建议**：
   - 先用 `--once` 参数测试运行
   - 确认没有错误后再后台运行

2. **数据库备份**：
   - 复制前备份 macOS 上的数据库
   - Windows 首次运行前也备份

3. **配置检查**：
   - 确保 `.env` 中的路径使用相对路径
   - 检查富途 OpenD 的 IP 和端口

4. **性能优化**：
   - Windows Defender 可能影响性能
   - 建议将 `C:\trade` 加入排除列表

---

## ✅ 完成标志

当你看到以下内容时，说明迁移成功：

1. ✅ `start.bat` 运行无错误
2. ✅ `data\service.log` 显示服务正常运行
3. ✅ http://localhost:8501 可以访问
4. ✅ Streamlit UI 显示正确的数据和持仓
5. ✅ 策略决策日志有新记录

---

*最后更新: 2026-09-02*
