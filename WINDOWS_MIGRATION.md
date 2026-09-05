# 富途量化交易系统 - Windows 迁移指南

## 📋 准备清单

### 1. 系统要求
- Windows 10/11 (64位)
- Python 3.11 或更高版本
- 至少 8GB 内存
- 10GB 可用磁盘空间

### 2. 必需软件
- [ ] Python 3.11+ (https://www.python.org/downloads/)
- [ ] Git for Windows (https://git-scm.com/download/win)
- [ ] 富途牛牛客户端 (https://www.futunn.com/)
- [ ] Visual Studio Build Tools (用于编译某些 Python 包)

---

## 🔧 迁移步骤

### 步骤1：安装基础环境

#### 1.1 安装 Python
```powershell
# 下载并安装 Python 3.11+
# 安装时勾选 "Add Python to PATH"
python --version  # 验证安装
```

#### 1.2 安装 uv (Python 包管理器)
```powershell
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 或使用 pip
pip install uv
```

#### 1.3 安装 Git
```powershell
# 下载并安装 Git for Windows
git --version  # 验证
```

---

### 步骤2：复制项目文件

#### 方案A：通过 Git (推荐)
```powershell
# 如果项目在 GitHub/GitLab
git clone <your-repo-url> C:\trade
cd C:\trade
```

#### 方案B：直接复制 (本次使用)
需要复制以下文件/目录到 Windows：

**必需文件：**
```
trade/
├── src/                    # 源代码
├── scripts/               # 脚本
├── pages/                 # Streamlit 页面
├── data/
│   └── trading.db        # ⚠️ 数据库文件
├── .env                   # ⚠️ 环境变量配置
├── pyproject.toml        # 项目配置
├── uv.lock               # 依赖锁文件
├── app.py                # Streamlit 入口
└── README.md
```

**可选但建议复制：**
```
├── data/ml_models/       # ML 模型
├── docs/                 # 文档
└── tests/                # 测试
```

---

### 步骤3：环境配置

#### 3.1 修改路径分隔符
Windows 使用反斜杠 `\` 而不是 `/`，但 Python 代码通常兼容两者。

**需要修改的配置文件：**

**`.env` 文件路径修正：**
```bash
# macOS 原始
# FUTU_OPEND_HOST=127.0.0.1

# Windows 相同
FUTU_OPEND_HOST=127.0.0.1
```

大部分路径不需要改，因为代码使用了 `Path` 对象。

#### 3.2 修改 .env 文件
```powershell
# 编辑 .env，确保富途 OpenD 配置正确
notepad .env
```

关键配置：
```bash
# 富途 OpenD 地址（本地）
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111

# 交易模式
EXECUTION_MODE=PAPER  # 或 SHADOW

# 启用的策略
SERVICE_STRATEGIES=Core Satellite,Earnings Gamma,Volatility Long Gamma
```

---

### 步骤4：安装依赖

```powershell
cd C:\trade

# 使用 uv 创建虚拟环境并安装依赖
uv sync

# 或使用传统方式
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

---

### 步骤5：启动富途 OpenD

#### 5.1 下载并安装富途 OpenD
1. 打开富途牛牛客户端
2. 工具 → API → OpenD
3. 启动 OpenD 服务

#### 5.2 验证连接
```powershell
# 测试连接
.venv\Scripts\python -c "from futu import OpenQuoteContext; ctx = OpenQuoteContext('127.0.0.1', 11111); print('Connected:', ctx.start()); ctx.close()"
```

---

### 步骤6：数据库迁移

#### 6.1 验证数据库
```powershell
# 检查数据库文件
dir data\trading.db

# 验证数据库完整性
sqlite3 data\trading.db "PRAGMA integrity_check;"
```

#### 6.2 路径修正 (如果需要)
某些脚本可能硬编码了路径：

```python
# 不推荐 (macOS 特定)
DB_PATH = "/Users/gavinyi/Documents/My/trade/data/trading.db"

# 推荐 (跨平台)
from pathlib import Path
DB_PATH = Path(__file__).parent / "data" / "trading.db"
```

---

### 步骤7：启动服务

#### 7.1 测试运行
```powershell
# 测试单次运行
uv run python scripts/run_service.py --once
```

#### 7.2 后台运行（Windows 方式）

**方案A：使用 Windows 任务计划程序**
```powershell
# 创建任务计划
# 1. 打开任务计划程序 (taskschd.msc)
# 2. 创建基本任务
# 3. 触发器：启动时
# 4. 操作：启动程序
#    程序：C:\trade\.venv\Scripts\python.exe
#    参数：scripts\run_service.py
#    起始于：C:\trade
```

**方案B：使用 NSSM (Non-Sucking Service Manager)**
```powershell
# 下载 NSSM: https://nssm.cc/download
nssm install TradingService "C:\trade\.venv\Scripts\python.exe" "scripts\run_service.py"
nssm start TradingService
```

**方案C：简单后台运行**
```powershell
# 使用 Start-Process
Start-Process -NoNewWindow -FilePath ".venv\Scripts\python.exe" -ArgumentList "scripts\run_service.py"
```

#### 7.3 启动 Streamlit UI
```powershell
# 新开一个 PowerShell 窗口
cd C:\trade
uv run streamlit run app.py --server.port 8501
```

---

### 步骤8：验证运行

#### 8.1 检查服务状态
```powershell
# 查看日志
Get-Content data\service.log -Tail 50 -Wait

# 检查 PID 文件
Get-Content data\trading-service.pid
```

#### 8.2 访问 UI
```
浏览器打开：http://localhost:8501
```

#### 8.3 测试交易
```powershell
# 运行一次策略测试
uv run python scripts/run_gamma_strategy.py --dry-run
```

---

## ⚠️ 常见问题

### 问题1：Python 包编译失败

**症状：**
```
error: Microsoft Visual C++ 14.0 or greater is required
```

**解决：**
安装 Visual Studio Build Tools:
https://visualstudio.microsoft.com/visual-cpp-build-tools/

### 问题2：富途 OpenD 连接失败

**检查清单：**
- [ ] 富途牛牛客户端是否登录
- [ ] OpenD 是否启动
- [ ] 端口 11111 是否被占用
- [ ] 防火墙是否阻止

```powershell
# 检查端口
netstat -ano | findstr 11111
```

### 问题3：路径问题

**常见错误：**
```python
FileNotFoundError: [Errno 2] No such file or directory: 'data/trading.db'
```

**解决：**
确保工作目录正确：
```powershell
cd C:\trade  # 必须在项目根目录
```

### 问题4：权限问题

**症状：**
```
PermissionError: [WinError 5] 拒绝访问
```

**解决：**
以管理员身份运行 PowerShell：
```powershell
# 右键点击 PowerShell → 以管理员身份运行
```

### 问题5：编码问题

**症状：**
中文显示乱码

**解决：**
设置环境变量：
```powershell
$env:PYTHONIOENCODING = "utf-8"
# 或永久设置
[System.Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "User")
```

---

## 🔒 安全注意事项

### 1. 保护敏感信息
```powershell
# .env 文件不要提交到 Git
echo .env >> .gitignore
echo data/trading.db >> .gitignore
```

### 2. 备份数据库
```powershell
# 定期备份
Copy-Item data\trading.db data\backups\trading_$(Get-Date -Format 'yyyyMMdd').db
```

### 3. 防火墙设置
```powershell
# 允许 Python 访问网络
# Windows Defender 防火墙 → 允许应用通过防火墙
```

---

## 📝 迁移检查清单

- [ ] Python 3.11+ 已安装
- [ ] uv 已安装
- [ ] Git 已安装
- [ ] 项目文件已复制
- [ ] .env 文件已配置
- [ ] 依赖已安装 (uv sync)
- [ ] 富途 OpenD 已启动
- [ ] 数据库文件已复制
- [ ] 测试运行成功
- [ ] 服务可以后台运行
- [ ] Streamlit UI 可访问
- [ ] 数据库备份策略已设置

---

## 🆘 获取帮助

如果遇到问题：
1. 检查日志：`data\service.log`
2. 查看文档：`docs\`
3. 提 Issue 或联系支持
