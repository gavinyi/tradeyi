# 代理分流实现文档：OpenD 走 mihomo 自动选择 · DSH/Antigravity 固定走自有洛杉矶机器

> 本文记录 2026-10-17 为本量化交易主机搭建的「按应用分流 + 固定出口 IP」方案。
> 目的：让 **OpenD（富途网关）** 通过 mihomo 出海打猎，同时让 **DSH / Antigravity（AI 订阅）** 固定从**自有洛杉矶机器的固定 IP** 出口，避免 IP 漂移触发风控封号。
>
> 环境：Windows · Clash Party（Electron GUI + mihomo 内核）· Proxifier PE · Futu OpenD 10.11.7108

---

## 1. 最终架构

### 1.1 数据流

```
┌─────────────────┐
│ Futu OpenD      │  不读系统代理，必须靠 Proxifier 拦截
│ Futu_OpenD.exe  │──────────┐
└─────────────────┘          │ Proxifier 规则
                             ▼
                   127.0.0.1:7900 (SOCKS5)
                             │
┌─────────────────┐          │
│ DSH Desktop     │          │
│ (Antigravity /  │          │
│  Codex/Claude/  │──────────┤ DSH 自带代理设置
│  Grok/Copilot)  │          │
└─────────────────┘          ▼
                   127.0.0.1:7893 (HTTP)
                             │
                             ▼
                    ┌──────────────────┐
                    │  mihomo (Clash    │
                    │  Party) 内核      │
                    └──────────────────┘
                        │          │
             自动选择组  │          │  直绑节点
                        ▼          ▼
              订阅落地机集群      自有洛杉矶机器
              (IP 会漂移)        38.34.20.101 (IP 恒定)
```

### 1.2 四大通道分工与端口拓扑

| 用途 | 入站端口 | 协议 | 最终出口 | IP 特性与设计原则 |
| :--- | :--- | :--- | :--- | :--- |
| **Windows 系统代理 / 浏览器** | **`7890`** | Mixed (HTTP+SOCKS) | 规则分流 (`Rule`) | **国内网站满速直连**，海外网站走代理，保证日常上网顺畅 |
| **DSH / Antigravity / AI 订阅** | **`7893`** | HTTP | **自有洛杉矶节点 `38.34.20.101`** | **固定单 IP，绝对不漂移** 🔒（防风控封号） |
| **DSH 应急口**（仅手动备用） | `7894` | HTTP | `♻️ 自动选择` | 仅在洛杉矶机器网络故障时临时手动替换 |
| **OpenD（富途实盘网关）** | **`7900`** | SOCKS5 | `♻️ 自动选择` | **物理级强制出海**，绕过 `GEOIP,CN` 误判，杜绝大陆监管封锁 |

---

## 2. 为什么是这个方案

### 2.1 为什么必须用 Proxifier，不能用 TUN

| 方案 | 问题 |
| :--- | :--- |
| 系统代理 | OpenD 是 C++/Qt 原生程序，走裸 TCP socket，**完全不读 Windows 系统代理** |
| TUN 虚拟网卡 | 会接管全系统路由，容易与办公 VPN / 虚拟机冲突；不希望开 |
| **Proxifier**（选用） | 不建虚拟网卡、不改路由表，只接管指定进程；其他软件 100% 走原生宽带 |

### 2.2 为什么 DSH 不能用「进程名」分流

这是本项目最容易踩的坑：

- DSH 在 `设置 → 订阅 → 代理` 里填了 `http://127.0.0.1:7893`；
- 连接 7893 的 socket 归属进程是 **DSH Desktop 及其拉起的 CLI 子进程**（node 等）；
- **不是 `Antigravity.exe`**——本机甚至没有这个进程；
- 因此 `PROCESS-NAME,Antigravity.exe,...` 这条规则**永远不可能命中**。

> **结论**：DSH 这条链路只能按 **入站端口** 分流，不能按进程分流。

### 2.3 为什么出口必须「直绑节点」而不是「策略组」

| 组类型 | 行为 | 能否保证 IP 恒定 |
| :--- | :--- | :--- |
| `url-test`（♻️ 自动选择） | 周期测速，**自动切换到最快节点** | ❌ 会换 IP |
| `fallback` | 主节点健康检查失败时**自动切备用** | ❌ 故障时会换 IP |
| `select` | 不自动切，但**会被界面里的手动选择改写** | ⚠️ 有被误改风险 |
| **直绑节点名**（选用） | 入站直接引用节点，**不经过任何组** | ✅ **绝对恒定** |

实测证据：`♻️ 自动选择` 的出口在两次检查中从 `177.2.22.197` 变成 `177.2.22.192`；而直绑的 7893 连续多次恒为 `38.34.20.101`。

---

## 3. 配置文件位置

| 项目 | 路径 |
| :--- | :--- |
| Clash Party 数据目录 | `C:\Users\Administrator\AppData\Roaming\mihomo-party` |
| **覆写内容（本方案核心）** | `…\mihomo-party\override\1a14776b533.yaml` |
| **覆写登记表** | `…\mihomo-party\override.yaml` |
| 规则覆盖文件（另一种机制） | `…\mihomo-party\rules\<激活的profileId>.yaml` |
| mihomo 实际运行配置 | `…\mihomo-party\work\config.yaml` ← **验证时看这个** |
| mihomo 内核日志 | `…\mihomo-party\logs\core-YYYY-MM-DD.log` |
| Clash Party 自身设置 | `…\mihomo-party\config.yaml`（含端口、sysProxy） |
| mihomo 受控设置 | `…\mihomo-party\mihomo.yaml`（含 DNS、sniffer、`find-process-mode`） |
| 本地代理文件 profile | `…\mihomo-party\profiles\1a1359715e3.yaml`（`clash.yaml`，含洛杉矶节点原始定义） |
| Proxifier（便携版） | `E:\Thisyi\trade\data\ProxifierPE\Proxifier PE\Proxifier.exe` |
| OpenD 主程序 | `C:\Users\Administrator\AppData\Roaming\Futu_OpenD\Futu_OpenD.exe` |
| OpenD 配置 | `…\com.futunn.FutuOpenD\UI\OpenD.xml` |

---

## 4. Clash Party 覆写（Override）机制

### 4.1 语法语义（从 app.asar 源码 `deepMerge()` 反推确认）

覆写本质是 `deepMerge(profile, patch, isOverride=true)`，数组键有特殊前缀含义：

| 写法 | 语义 | 结果 |
| :--- | :--- | :--- |
| `+proxies:` | 数组**前插** | `[...本次的, ...原有的]` |
| `proxies+:` | 数组**后插** | `[...原有的, ...本次的]` |
| `proxies!:` | **整体替换** | 直接覆盖该子树 |
| `proxies:` | 普通覆盖 | 覆盖该键 |

嵌套对象会递归，且 `isOverride` 会向下传递，所以可以写 `dns: → +fake-ip-filter:`。

### 4.2 登记表结构

`override.yaml`：

```yaml
items:
  - id: 1a14776b533          # 文件名 = <id>.yaml
    name: agy&opend
    type: local              # local = 读磁盘文件；remote = 从 URL 拉
    ext: yaml                # yaml = 深合并；js = 执行脚本 main(profile)
    global: true             # 对所有 profile 生效，订阅更新也不会丢
    updated: 1792200324403
```

> `global: true` 是关键：订阅更新时会重新下载 profile，但覆写始终会重新应用，不会被覆盖。

---

## 5. 最终覆写文件全文

`…\mihomo-party\override\1a14776b533.yaml`：

```yaml
# ===== OpenD 走「自动选择」；DSH/Antigravity 固定走美国洛杉矶节点 =====
# 覆写语法：+key = 数组前插 / key+ = 数组后插 / key! = 整体替换 / 普通 key = 覆盖
# 注意：listeners 的 proxy 名称必须合法，否则 mihomo 会直接启动失败。
#
# 设计原则：入站【直接绑定节点名】，不经过任何策略组。
#   理由：策略组（select）会被界面里的手动选择改写，url-test / fallback 会自动换节点，
#         两者都会导致出口 IP 漂移。直绑节点才能保证出口 IP 恒定。

# (1) DNS 兜底：mihomo 把代理节点域名的解析交给了它自己的 fake-ip DNS
#     （proxy-server-nameserver: udp://127.0.0.1:7874），会解析出假地址 198.18.0.4 导致超时。
dns:
  +fake-ip-filter:
    - "+.banydoll.com"

# (2) 洛杉矶固定节点（自有地址，直连透传，出口 IP == 服务器 IP）
#     server 用真实 IP 绕开上面的解析链路；
#     servername / ws Host 保留域名，TLS SNI 与 WebSocket 路由与原配置一致。
+proxies:
  - name: "通用-洛杉矶直连-ebool.banydoll.com"
    type: vmess
    server: 38.34.20.101
    port: 443
    uuid: "1a4ca2c9-c680-4861-8091-8704f68c4564"
    alterId: 0
    cipher: auto
    udp: true
    network: ws
    ws-opts:
      path: "/appservice_176_01"
      headers:
        Host: ebool.banydoll.com
    tls: true
    servername: ebool.banydoll.com

# (3) 入站分工
#     7890 -> 混合入站（兼容 Windows 系统代理，保证日常浏览器上网正常）
#     7893 -> 固定洛杉矶节点  ← DSH / Antigravity 用这个，出口 IP 恒定（风控友好）
#     7894 -> 自动选择       ← 手动应急口：洛杉矶节点故障时，把 DSH 端口临时改成 7894
#     7900 -> 自动选择       ← OpenD（经 Proxifier 转发，物理级强制出海）
+listeners:
  - name: "mixed-compat"
    type: mixed
    port: 7890
    listen: 127.0.0.1
  - name: "antigravity-la"
    type: http
    port: 7893
    listen: 127.0.0.1
    proxy: "通用-洛杉矶直连-ebool.banydoll.com"   # 直绑节点，不经组
  - name: "antigravity-auto"
    type: http
    port: 7894
    listen: 127.0.0.1
    proxy: "♻️ 自动选择"
  - name: "opend-in"
    type: socks
    port: 7900
    listen: 127.0.0.1
    proxy: "♻️ 自动选择"

# (4) 安全网：若 Proxifier 暂时仍指向 7890，这条规则也能让 OpenD 走自动选择
+rules:
  - PROCESS-NAME,Futu_OpenD.exe,♻️ 自动选择
```

> `listeners` 的 `proxy` 字段支持**出站代理**或**策略组**，但名称必须合法，否则 mihomo 直接启动失败。

---

## 6. Proxifier 规则

| 顺序 | Applications | Target Hosts | Target Ports | Action |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `mihomo.exe`；`Clash Party.exe` | Any | Any | **Direct**（防代理环路） |
| 2 | Any | `localhost; 127.0.0.1` | Any | **Direct**（本机通信不走代理） |
| 3 | `Futu_OpenD.exe` | Any | Any | **Proxy SOCKS5 `127.0.0.1:7900`** |
| 4 | Default | Any | Any | **Direct** |

**两个必须注意的点：**

1. 进程名是 **`Futu_OpenD.exe`**（下划线），不是空格；
2. **`localhost` 直连规则必须排在 `opend` 规则上面**，否则 OpenD 与 Python 之间的 `127.0.0.1:11111` 通信也会被扔进代理，导致量化下单卡死。

---

## 7. DSH 设置

`DSH Desktop → 设置 → 订阅 → 代理`

```
代理地址：http://127.0.0.1:7893
```

- 该设置**只作用于「订阅相关请求 + 模型 API + 工具请求」**；
- 浏览器 OAuth 走浏览器 / 系统代理（即 7890，按 mihomo 规则分流）；
- 改完重启 DSH，让代理设置对 Codex / Claude / Grok / Copilot 等子进程完全生效。

---

## 8. 日常自查命令

### 8.1 看出口 IP（最重要）

```powershell
# DSH 出口（应恒为 38.34.20.101 / US / Los Angeles）
curl.exe -x http://127.0.0.1:7893 http://ip-api.com/json

# 应急口出口（会漂移，属正常）
curl.exe -x http://127.0.0.1:7894 http://ip-api.com/json

# 直接看纯 IP
curl.exe -x http://127.0.0.1:7893 https://api.ipify.org
```

### 8.2 看业务端点是否真通

```powershell
# Gemini / Code Assist 主端点（返回 404 属正常，能返回状态码即通）
curl.exe -x http://127.0.0.1:7893 -s -o NUL -w "%{http_code}`n" https://cloudcode-pa.googleapis.com/
curl.exe -x http://127.0.0.1:7893 -s -o NUL -w "%{http_code}`n" https://daily-cloudcode-pa.googleapis.com/
```

### 8.3 看 mihomo 实际生效的配置

```powershell
# 三个入站是否都在
Select-String -Path "$env:APPDATA\mihomo-party\work\config.yaml" -Pattern "antigravity-la|antigravity-auto|opend-in"

# 节点是否注入
Select-String -Path "$env:APPDATA\mihomo-party\work\config.yaml" -Pattern "38.34.20.101"
```

### 8.4 看内核日志里的真实走向

```powershell
Get-Content "$env:APPDATA\mihomo-party\logs\core-$(Get-Date -Format yyyy-MM-dd).log" -Tail 50
```

日志里 `using <策略组或节点>[<实际节点>]` 是判断"到底走了谁"的最权威依据。
**注意**：出口 IP 的地理位置可能"骗人"——中转节点的入口城市和出口城市可以不同。

---

## 9. 应急与运维

### 9.1 洛杉矶机器挂了怎么办

**不要改配置**，按以下顺序：

1. DSH 里把端口 `7893` 临时改成 **`7894`** → 立刻恢复（出口会变成订阅节点，IP 会漂移）；
2. 等洛杉矶恢复后，把端口改回 `7893`；
3. 如果长期不可用，再考虑更新节点 `server` 的 IP。

### 9.2 洛杉矶机器换了 IP 怎么办

只改覆写里的 `server` 一行，然后重启 Clash Party：

```yaml
    server: 38.34.20.101      # ← 改成新 IP
```

`servername` 与 `ws-opts.headers.Host` 保持域名不变。

### 9.3 修改覆写后必须重启 Clash Party

覆写只在 **Clash Party 启动时**读取并重新生成 `work/config.yaml`。改文件后不重启不生效。

> 建议先退出 Clash Party 再改文件，避免它用内存里的旧副本把文件覆盖回去。

### 9.4 回滚

在 Clash Party「覆写」页面把 `agy&opend` 停用即可；或删除：

```
…\mihomo-party\override\1a14776b533.yaml
…\mihomo-party\override.yaml        （把对应 item 删掉）
```

---

## 10. 已踩过的坑（踩坑记录，避免重演）

| # | 现象 | 真实原因 | 解法 |
| :--- | :--- | :--- | :--- |
| 1 | 自定义规则完全不生效 | `rules/<id>.yaml` 的 `<id>` 必须等于**当前激活**的 profile id。当时写的是未激活的 `1a1359715e3` | 改用 `global: true` 的覆写；或把规则文件改成激活 profile 的 id |
| 2 | 规则引用的节点不存在 | 洛杉矶节点只存在于未激活的本地 profile 里，订阅里没有 | 覆写 `+proxies` 注入节点 |
| 3 | `PROCESS-NAME,Antigravity.exe` 永不命中 | 流量来自 **DSH**，不是 Antigravity.exe（本机无此进程） | 改为按**入站端口**分流 |
| 4 | 连 7893 报 `502` / `SSL UNEXPECTED_EOF` / `i/o timeout` | **循环 DNS**：`proxy-server-nameserver: udp://127.0.0.1:7874` 指向自己的 fake-ip DNS，把节点域名解析成假地址 `198.18.0.4` → 拨不可路由地址 | 节点 `server` 用真实 IP；并把域名加入 `dns.fake-ip-filter` |
| 5 | 出口 IP 不是自有机器，且会变 | 入站指向了 `url-test` 的「自动选择」组 | 入站**直绑节点名**，绕开策略组 |
| 6 | 曾同时存在两份覆写 | 两份覆写都注入同名节点 → mihomo 因重名启动失败 | 只保留一份覆写 |
| 7 | OpenD 下单被拒 `没有解锁交易` | 交易密码解锁状态绑定 OpenD 会话，**OpenD 重启即失效** | 重启 OpenD 后需在界面重新输入 6 位交易密码解锁 |
| 8 | **自定义 listeners 后 7890 端口消失，国内网页全瘫痪** | **核心踩坑**：Mihomo 内核只要一旦声明了自定义 `listeners:`，就会**判定你要完全自定义入站，从而静默停用默认的 mixed-port 7890**！而 Windows 系统代理默认指向 7890，导致浏览器直接被目标计算机拒绝连接（假断网） | **铁律**：凡是自定义 `+listeners:`，必须在列表显式补上 `7890` 混合端口（`mixed-compat`），恢复系统代理监听 |
| 9 | **OpenD 能否和浏览器共用 7890 端口？** | **绝不能共用！** 如果 OpenD 走 7890，当它连接广州/上海富途机房 IP 时，会命中内核的 `GEOIP,CN,DIRECT` 规则被直接当作国内流量直连出境！导致富途服务端再次看到大陆真实 IP，重新触发“中国内地不可交易”封锁 | **解法**：OpenD 必须独占 `7900` 端口并在入站直接绑定 `proxy: ♻️ 自动选择`，在物理端口层实现 100% 强制出海，永不走漏 |

### 附：关于富途的登录与 IP

- **IP 变化 / 连接中断 → 不要求重新登录**。实测日志：代理重启导致全部连接断开，登录状态瞬间变 `false`，**11 秒内自动恢复为 `trdLogined: true`**，全程无需输入账号密码（因为 `autoLogin: true` + `isRememberPassword: true`）。
- 日志中从未出现「异地登录 / 设备锁 / 验证码 / 强制登出」记录。
- **但交易密码解锁不同**：OpenD 进程重启后必须重新解锁。
- **监管合规校验按连接 IP 判定**：若出口落到中国大陆 IP，买入开仓会被拒（`根据中国内地监管要求，当前位于中国内地，不可交易`）。当前出口为 HK/SG/US，不会触发。

---

## 11. 关键事实备忘

| 事实 | 数值 / 说明 |
| :--- | :--- |
| 洛杉矶节点 | `38.34.20.101:443`，vmess + ws + TLS，出口 IP == 服务器 IP（直连透传） |
| 洛杉矶出口归属 | United States / California / Los Angeles / Cogent Communications |
| 订阅自动选择出口 | `177.2.22.x` / US LA / Ctcsci Tech Limited（会漂移） |
| Clash Party 端口 | mixed `7890`、socks `7891`、http `7892`、DNS `7874` |
| 端口拓扑 | mixed `7890` (系统代理/日常上网)、socks `7891`、http `7892`、`7893` (DSH 固定)、`7894` (应急)、`7900` (OpenD) |
| 激活中的订阅 profile | `1a081cc93be`（`(∠・ω<)⌒Paoluz`，70 个节点，`autoUpdate: false`） |
| 本地代理文件 profile | `1a1359715e3`（`clash.yaml`，仅 1 个节点，未激活） |

---

## 12. 一页速查

```
浏览器 / 系统上网    →  127.0.0.1:7890 (混合端口)  →  国内直连满速 / 正常规则分流
DSH / Antigravity  →  http://127.0.0.1:7893      →  38.34.20.101 (固定洛杉矶单IP，防封)
DSH 应急口         →  http://127.0.0.1:7894      →  自动选择 (仅在洛杉矶故障时手动切)
OpenD (Proxifier)  →  socks5 127.0.0.1:7900     →  自动选择 (物理级强制出海，绝不漏回大陆)

改完覆写 → 必须重启 Clash Party
OpenD 重启 → 必须在 OpenD 界面重新输入交易密码解锁
验出口 IP → curl.exe -x http://127.0.0.1:7893 http://ip-api.com/json
```
