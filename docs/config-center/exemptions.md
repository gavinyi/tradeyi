# 配置中心豁免清单基线 (Phase 0 冻结)

> 文档状态：基线冻结 (Frozen Baseline)  
> 规则：本文件中的豁免项总数在 CI 守卫中严格断言**不得增长**。任何新增豁免必须经 Reviewer 签字并显式修改基线数字，严禁通过随意扩展白名单规避配置登记。

---

## 1. 豁免总数基线
- **当前冻结豁免总数**：`20` 项

---

## 2. 豁免项清单与逐项审查理由

| 序号 | 豁免键名 | 分类 | 逐项合规理由 |
|---:|---|---|---|
| 1 | `PATH` | 系统环境 | 操作系统可执行文件查找路径，非交易业务配置 |
| 2 | `SYSTEMROOT` | 系统环境 | Windows 系统根目录路径，非业务配置 |
| 3 | `WINDIR` | 系统环境 | Windows 核心安装目录，非业务配置 |
| 4 | `APPDATA` | 系统环境 | 用户漫游应用数据路径，非业务配置 |
| 5 | `LOCALAPPDATA` | 系统环境 | 用户本地应用数据路径，非业务配置 |
| 6 | `PROGRAMDATA` | 系统环境 | 机器公共应用数据路径，非业务配置 |
| 7 | `TEMP` | 系统环境 | 临时文件存放目录，非业务配置 |
| 8 | `TMP` | 系统环境 | 临时文件存放目录，非业务配置 |
| 9 | `USERPROFILE` | 系统环境 | 用户个人资料主目录，非业务配置 |
| 10 | `HOMEPATH` | 系统环境 | 用户主文件夹，非业务配置 |
| 11 | `SYSTEMDRIVE` | 系统环境 | Windows 系统盘符，非业务配置 |
| 12 | `PYTHONUNBUFFERED` | 解释器参数 | 控制 Python 标准流无缓冲实时刷新，非业务配置 |
| 13 | `PYTHONUTF8` | 解释器参数 | 强制 Python 运行时使用 UTF-8 代码页，非业务配置 |
| 14 | `PYTHONPATH` | 解释器参数 | 模块搜索路径变量，非业务配置 |
| 15 | `PYTHONIOENCODING` | 解释器参数 | 标准输入输出编码格式，非业务配置 |
| 16 | `MSYSTEM` | 解释器参数 | 终端环境类型标识，非业务配置 |
| 17 | `TERM` | 解释器参数 | 控制台终端类型，非业务配置 |
| 18 | `FUTU_OPEND_EXE` | 外部程序路径 | 宿主机 FutuOpenD.exe 本地安装绝对路径，非策略参数 |
| 19 | `FUTU_OPEND_DIR` | 外部程序路径 | 宿主机 FutuOpenD 安装目录绝对路径，非策略参数 |
| 20 | `PROXIFIER_EXE` | 外部程序路径 | 宿主机出海网络代理工具绝对路径，非策略参数 |

---

## 3. 测试沙箱与工作区环境变量说明
以下环境变量属于测试沙箱与 fail-closed 生产指纹守卫（`conftest.py`）核心机制，由测试套件直接维护：
- `TRADEYI_ENV`
- `TRADEYI_PROD_DB_PATH`
- `TRADEYI_PROD_DB_OVERRIDE`
- `TRADEYI_DEV_ROOT`
- `TRADEYI_PROD_ROOT`
- `TRADEYI_TEST_ROOT`
- `DSH_HADOOP_USER`
- `CUA_DRIVER_PIPE`
