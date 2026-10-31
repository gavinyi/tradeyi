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

---

## 4. runtime_config 写调用通用助手豁免清单 (8 处)
针对由通用底层助手方法转发的写调用，由于其形参键名在函数内为变量：
- 策略与配置业务调用处（如 `config.py` 中的 `save_campaign`、`save_volatility_campaigns` 等）的传入键名已在具体的业务调用处被静态或动态守卫覆盖；
- 发布引擎写回链路（`store.py:2439/2447/2459`）当前底层直接采用草稿中的 `release['config_key']`，底层无键白名单强校验；
- **更靠前的缺口：UI 入口本身允许自由文本**。当前唯一前端入口 `pages/18_infrastructure_disaster_recovery.py:91` 采用 `st.text_input("配置键", value="ENV_SETTINGS")`，UI 允许输入任意未登记键创建草稿；
- 因此发布全链路列为技术豁免与已知缺口：**直写路径已完全封死，发布/UI 自由文本链路列为已知缺口，Phase 1 由 ConfigService 在创建草稿处对 key 做登记表强校验并改造 UI**。

| 序号 | 代码站点 | 函数角色 | 豁免理由 |
|---:|---|---|---|
| 1 | `src/futu_quant/config.py:116` | `_load_runtime_value` 内部初始化迁移 | 通用读取回填助手，key 来自调用方 |
| 2 | `src/futu_quant/config.py:422` | `save_volatility_campaigns` | 波动率 campaign 统一写回入口，key 来自 `_volatility_campaigns_key` (已登记) |
| 3 | `src/futu_quant/config.py:495` | `save_campaign` 通用保存助手 | 供页面及策略通用更新，形参 config_key 由调用方传入 (已登记模板) |
| 4 | `src/futu_quant/config.py:550` | `delete_campaign` 通用删除助手 | 供页面及策略通用删除，形参 config_key 由调用方传入 (已登记模板) |
| 5 | `src/futu_quant/store.py:2439` | `publish_config_release` 发布应用 | 发布引擎底层写回，当前无键校验（UI允许自由文本），Phase 1 由 ConfigService 在创建草稿处强校验 |
| 6 | `src/futu_quant/store.py:2447` | `publish_config_release` 发布失败回滚 | 发布失败自动回滚写回原值，Phase 1 由 ConfigService 治理 |
| 7 | `src/futu_quant/store.py:2459` | `rollback_config_release` 人工回滚 | 配置回滚引擎写回，键由发布单决定，Phase 1 由 ConfigService 治理 |
| 8 | `scripts/migrate_volatility_campaigns_tenant_scope.py:1341` | 一次性离线数据迁移脚本 | 离线租户分区数据结构迁移脚本，不属于在线服务代码 |
