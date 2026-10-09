# TripPlanner MCP App

把 TripPlanner 接进 WorkBuddy 对话：一句话生成行程，并在对话下方渲染**可视化行程面板**。

当前阶段：**M0 ✅ → M1-pre ✅ → M1 主体 ✅（2026-10-09，自检 63/63）→ M2 待开工**

面板已渲染**真实行程**：按天时间轴（含交通段/用餐）、主题与理由、日里程、硬约束违规徽标、
被剔除点位及原因、POI 照片缩略图。数据来自本地 31 城 POI 库 + 交通/路线缓存，无需联网。

---

## ✅ M0 验收结果（2026-10-09，全部实测通过）

| 环节 | 证据 |
|---|---|
| server 自检 | `selftest.py` **19/19 PASS**（零依赖、走真 stdio） |
| 宿主加载 | `mcp-tool-list.json` 新增 entry：`['tripplanner_open_plan','tripplanner_ping']` |
| 授权落盘 | `mcp-approvals.json`：`{"<configHash>::tripplanner": <ts>}` |
| App 注册 | `rpc.appsChanged.push apps=[{"mcpAppId":"tripplanner/tripplanner_open_plan", …}]` |
| 工具 `_meta` 解析 | `openHostApp.appResolved toolMeta={"hasUi":true,"uiKeys":["resourceUri","visibility"],"visibility":["model","app"]}` |
| 面板资源拉取 | `resolver.resolve.ok htmlLen=7451 mimeType=text/html;profile=mcp-app` |
| 面板实例创建 | `openHostApp.created appUrl=http://127.0.0.1:54994/mcp-apps/<appInstanceId>` |
| **肉眼确认** | **面板出现，两个探针按钮点击均成功**（UI 渲染 + AppBridge 双向通道） |

**核心结论：自建 stdio（非市场）MCP server 的 UI 资源可被 WorkBuddy 正常渲染为面板，CSP 零拦截。**

### 两条实测约束（M1 必须遵守）

- **资源有缓存**：第二次调用命中 `resolver.resolve.cacheHit … shared=true`，不再拉 HTML
  → **改面板代码后宿主可能仍用旧版本**，需触发刷新或换 `resourceUri`。
- **每次调用新开一扇**：两次调用得到**两个不同 `appInstanceId`**（不复用）
  → 必须做「首次开面板 / 后续回摘要」分流，否则用户面前堆面板。

---

## ✅ M1 前置改造（2026-10-09，已实施 + 自检 29/29 通过）

解决的三个问题：

| # | 问题 | 处置 |
|---|---|---|
| 1 | 面板会堆叠（每次调 `open_plan` 都新开一扇） | **A + C 组合分流**（见下） |
| 2 | 高德直链照片会被 CSP 拦 | `_meta.ui.csp.resourceDomains` 追加高德域名 |
| 3 | `appInstanceId` server 侧拿不到 | 新增 `tripplanner_panel_ready` 工具，由**面板自报** |

### A + C 分流（Kenny 2026-10-09 拍板）

| 方案 | 角色 | 机制 |
|---|---|---|
| **A**（精确） | 主判据 | 面板握手成功后调 `tripplanner_panel_ready`，把宿主注入的 `appInstanceId` 报给 server；server 维护「活跃实例集合」。`open_plan` 带该 id 且已登记 ⇒ `panelOpened=false` |
| **C**（兜底） | 保险丝 | 模块级 `SESSION.opened_any`：**本进程只要开过任意一扇面板，后续 `open_plan` 一律判为「已打开」**。防实例 id 取不到时仍堆面板 |

> ⚠️ **为什么判据不能只用 `appInstanceId`**：它由宿主中间层生成（日志里的 `openHostApp.created`），
> `rpc.openHostApp params` 里**不含**它，`proxyAppToolCall` 转发给 MCP server 的 `tools/call`
> 也只有 `name` + `arguments` ⇒ **server 侧根本看不到**。所以只能由面板自报（A），且必须有 C 兜底。

日志依据：每次调用 `openHostApp.created` 都带新的 `appInstanceId`（`cf01c3e8…` / `e0becf56…`）。

### 高德图片 CSP 放行

```python
resourceDomains = ["http://127.0.0.1:*",
                   "https://store.is.autonavi.com", "https://store.is.autonavi.com:*",
                   "https://*.amap.com", "https://*.autonavi.com"]
```

面板内新增「CSP 放行验证」卡片，点按钮实际加载一张高德直链图：
- **加载成功** → 直链方案可行，M1 照片直接用 `photo_cache` 里的 URL
- **加载失败** → CSP 对图片另有约束，M1 改走本地代理（`/api/photo` 已有现成实现）

⚠️ 这一条**必须在 WorkBuddy 里肉眼验收**，`selftest.py` 只能验声明写没写对。

---

## ✅ M1 主体（2026-10-09 完成，自检 63/63）

对话 → 城市识别 → 规划内核 → 时间轴面板，全链路接通。

### 工具分层（只有带 `_meta.ui.resourceUri` 的才开面板）

| 工具 | 面板 | 作用 |
|---|---|---|
| `tripplanner_plan_trip` | ★开 | **主入口**。一句话需求 → 真实行程 + 打开面板 |
| `tripplanner_open_plan` | ★开 | 只开面板，复用上次行程 |
| `tripplanner_get_plan` | — | 面板握手后主动拉行程（M1 数据通道） |
| `tripplanner_list_cities` | — | 列出 31 个可规划城市 + POI 数 |
| `tripplanner_panel_ready` | — | 面板自报 `appInstanceId`（A 方案） |
| `tripplanner_ping` | — | 连通性对照组 |

### 关键实现要点

- **城市识别**：显式 `city` 参数优先；否则取「在需求文本中出现且**最长**」的城市名
  —— 最长匹配避免「上海」命中「海上」，也让「六盘水」压过「水」。
  识别不出时**明确报错并列出可用城市**，绝不猜一个默认城市。
- **天数**：`query_days.extract_days(query)` 优先于 `days` 参数，再 clamp 到 1–7。
- **stdout 保护**：规划期间 `sys.stdout = sys.stderr`，内核任何 print 都不会污染
  JSON-RPC 通道（写脏一个字节宿主就断连）。
- **数据双通道**：主通道是面板握手后主动 `tools/call → tripplanner_get_plan`；
  同时兼容宿主下发 `ui/notifications/tool-result` 的 bootstrap 结果。
- **照片**：server 侧从 `photo_cache` 解析高德直链后随行程下发；面板 `onerror` 隐藏，不留占位框。
- **内核懒加载**：`initialize` 阶段不 import `src/`（ortools 等），首次规划才加载，保持启动轻快。
- 🔴 **必须 `config.apply_env()`**：`load_config()` 只**返回** dict、**不写** `os.environ`，
  而 `llm_client` 惰性只读 `os.environ`。宿主拉起本进程时 env 里只有 `TRIPPLANNER_HOME`，
  不注入就会 **静默降级**到离线链路 —— 跑得飞快、结果看着也合理，极难发现。
  现在内核加载时主动注入，并把 `llmEnabled` / `degraded` 回传到结果里，让降级**可见**。
  自检有专门的「贫瘠环境」阶段（抹掉 `DEEPSEEK_API_KEY` 再跑）守住这条。

### ⚠️ 改面板必须 +1 版本号

宿主按 uri 缓存面板 HTML（`resolver.resolve.cacheHit`）。`PANEL_VERSION` 常量在
`tripplanner_mcp_server.py` 顶部 —— **每次改完 `panel/plan_panel.html` 就 +1**
（uri 随之变成 `ui://tripplanner/plan-<v>`），否则用户看到的还是旧面板。

---

## ⚠️ 最重要的一条：注册后必须「重载」才生效

从 `app.asar` 取证（2026-10-09）：

```js
// UserMcpProvider —— 自定义 MCP 的加载器
get filePath(){ return join(this.deps.getConfigDir(), "mcp.json") }   // -> ~/.workbuddy/mcp.json
async bootstrap(){ await this.trust.load(); await this.refreshFromDisk() }   // 只在构造时读一次
```

`bootstrap()` **只在应用启动时跑一次**；运行期间既不 watch 这个文件，也不定时重读。
宿主侧唯一的重读入口是 IPC **`mcpRefresh`**（→ `McpManager.refresh()` → `userProvider.refreshFromDisk()`）；
另外写操作（`importServer` / `saveConfiguration`）会在写文件的同时把新内容 commit 进内存。
（`mcpRefresh` / `mcpImportServer` / `mcpApprove` 等都是 renderer↔main 的通道，**外部进程调不到**。）

### 🚨 别信「我重启过了」—— 必须验证进程真的换了

**实测踩坑两次**：点窗口 `X` 只是**收进托盘**，主进程继续跑；再次启动被 Electron 的
`requestSingleInstanceLock` 挡下、只做「聚焦已有窗口」。**「重开」≠「重启」。**

判据两条（缺一不可）：

1. `Get-Process WorkBuddy.exe | Select-Object Id,StartTime`
   —— **主进程 / daemon / renderer 的 StartTime 若还是老的，就一定没重读**。
2. `~/.workbuddy/logs/AppStartup.log` 最后一条 `[AppStartup]` 的时间戳。

**最强论证**：跑 agent 的 daemon（`daemon-app-server-entry.js --stdio`，写 `mcp-apps-diag.log`）
如果还是老 PID，宿主一定没重启 —— 这个 pid 直接印在日志行里（`[pid=NNNNN]`）。

### 零重启修复路径（推荐，实测有效）

`main/server.js` 的 `McpManager` 暴露给界面三个方法：

```js
async refresh()            { … userProvider.refreshFromDisk() }   // 重读磁盘 → commit → 刷新列表
async configuration()      { … file.readContent() }              // ★ 每次调用都重读磁盘
async saveConfiguration(e) { … replaceConfiguration(e.content) } // 先 commit 进内存、再落盘
```

→ **在「连接器 → 自定义 MCP」页点「刷新」**，或**用页内 JSON 编辑器原样保存一次**，
即走 `mcpRefresh` / `saveConfiguration`，**立即生效、不用重启**。
（2026-10-09 本次就是靠这条路修好的，全程零重启。）

**实证（本次「UI 看不到」的根因）**：

| 事件 | 时间 |
|---|---|
| WorkBuddy 启动（v5.7.6，`[AppStartup]`） | `10:44:03` |
| 宿主落盘工具快照 `~/.workbuddy/mcp-tool-list.json` | `10:44:19` 创建 / `10:44:28` 最后写 |
| `~/.workbuddy/mcp.json` **创建**（ctime） | `11:13:17` ← 比启动晚 29 分钟 |
| `mcp.json` 最后修改（改名 tripplanner） | `11:27:35` |
| **页面刷新/保存 → 生效** | `11:47:26`（`appsChanged.push`，主进程 pid 仍是 30200，**零重启**） |

→ 启动那一刻文件还不存在，`readServers()` 走 `ENOENT` 分支返回 `{}`（**静默！不告警**），
内存快照里**从来没有过**这一条。**所以「UI 里看不到」= 没重载，不是配置错。**

**路径已从源码钉死**：`resolveDefaultConfigDir(){ WORKBUDDY_CONFIG_DIR || CODEBUDDY_CONFIG_DIR || join(homedir(), dataFolderName || '.workbuddy') }`
→ `mcp.json` 在 `%USERPROFILE%\.workbuddy\`。**`.mcp.json` 不是它读的**（反例：里面有 `playwright`，宿主完全不认）。
另：旧「聚合文件」`connectors/<userId>/mcp.json` 会被 `cleanupStaleUserMcpJson()` **主动删除**，别写那里。

---

## 为什么用 Python 而不是重写

TripPlanner 的核心资产是 `src/`（TOPTW 求解 + 31 城 1193 POI + 905KB 交通矩阵 + 114KB 照片缓存）。
MCP server 只是一层 stdio JSON-RPC 壳，直接 import 现有内核即可，重写成 Node/TS 等于把求解器重做一遍。

## 目录

```
mcp/
  tripplanner_mcp_server.py   # stdio MCP server（M0+M1pre：纯标准库、零依赖）
  panel/plan_panel.html     # 面板（自包含，零外部引用）
  selftest.py               # 不开 WorkBuddy 也能验协议与真实规划（63 项）
  register.py               # 注册进用户级 MCP 配置（双写 mcp.json + .mcp.json，合并不覆盖）
```

## 快速开始

```bash
PY="D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"

"$PY" mcp/selftest.py          # 1. 本地自检（63 项，含真实规划 + 贫瘠环境回归）
"$PY" mcp/register.py          # 2. 注册进 WorkBuddy 配置
# 3. ⚠️ 重载（见上文顶部）：在「自定义 MCP」表单里加，或重启 WorkBuddy
# 4. WorkBuddy → 连接器 → 右上角「自定义 MCP」→ 找到 tripplanner → 点「信任」
# 5. 对话里说：「打开行程面板」
```

回滚：`"$PY" mcp/register.py --remove`

### 改完面板/ server 后怎么让它生效

| 改了什么 | 怎么生效 |
|---|---|
| `tripplanner_mcp_server.py` | 面板里重开一次工具调用（宿主会重启 stdio 子进程），或重启 WorkBuddy |
| `panel/plan_panel.html` | ⚠️ **宿主按 uri 缓存 HTML**（`resolver.resolve.cacheHit`）→ 把 `PANEL_VERSION` +1（uri 变 `ui://tripplanner/plan-<v>`），再重载 MCP |

### 在 WorkBuddy 里验收（M1）

1. **确认宿主真的重载了**（不重载一定看不到新工具）：

   ```
   连接器 → 自定义 MCP → 刷新
   ```

   验：工具列表里应出现 **6 个** `tripplanner_*`
   （`plan_trip` / `open_plan` / `get_plan` / `list_cities` / `panel_ready` / `ping`）。
   只有 2 个（`open_plan` / `ping`）＝ 还是 M0 的旧快照。

2. **直接说人话，不要点工具**：

   > 帮我规划杭州两天的行程，带 8 岁小孩，轻松一点

   期望：模型自动调 `tripplanner_plan_trip` → **右侧弹出面板** → 面板里是逐日时间轴
   （点位 / 交通 / 用餐 + 缩略图 + 里程 + 违规徽标）。

3. **逐条对照看**：

   | 看什么 | 期望 |
   |---|---|
   | 面板弹出 | 一次对话只弹一扇；再说「换个苏州三天的」应**复用/换内容而不叠两扇** |
   | 模式 | 面板或摘要里 `mode=llm`（离线会显示 `offline`，看着也合理但少了 LLM 编排） |
   | 缩略图 | 高德直链能加载（被 CSP 拦则整块隐藏，不留占位框） |
   | 城市识别 | 「两天随便玩玩」应**明确报错并列出 31 个可用城市**，而不是瞎猜一个 |
   | 报错 | 「火星玩两天」→ `未知城市：火星。可用：…` |

4. **兜底排查**（面板没弹 / 报连接错）：先让模型调 `tripplanner_ping`，
   能回就是 server 活着、问题在面板侧；回不了就是注册/重载没生效。

### 表单添加时填这些

| 字段 | 值 |
|---|---|
| 名称 | `tripplanner` |
| 类型 | `stdio` |
| 命令 | `D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe` |
| 参数 | `-u` 和 `D:/Users/xgwu/WorkBuddy/Tripagent/mcp/tripplanner_mcp_server.py`（两行） |
| 环境 | `TRIPPLANNER_HOME` = `D:/Users/xgwu/WorkBuddy/Tripagent` |

---

## 协议要点（从 `app.asar` 源码取证，非推测）

宿主侧判定（等价源码）：

```js
const isApp = Uv(tool) !== undefined;   // Uv 只读 tool._meta.ui.resourceUri
// 兼容旧写法 _meta['ui/resourceUri']；visibility ∈ {model, app}
```

| 结论 | 含义 |
|---|---|
| **不用手写 `isApp`** | 给工具加 `_meta.ui.resourceUri`，宿主自动认作 app |
| **自建 server 也支持** | `listApps()` 遍历所有 controller，`runtimeSource` 区分 `session-stdio` / `remote` |
| **面板可反向调工具** | AppBridge 走 `ui/initialize` → `tools/call`，token 宿主自动带 |
| **server 侧看不到 `appInstanceId`** | 宿主中间层生成，不下发到 `tools/call` ⇒ **必须由面板自报**（`tripplanner_panel_ready`） |

### 存储字段白名单

`UserMcpProvider` 视为「标准字段」的集合（其余字段在表单保存时被保留，但不参与解析）：

```
type / command / args / env / cwd / url / headers / timeout
```

- stdio 由 **`command` 是否存在**判定，`type: "stdio"` 可写可不写
- **必须加 `-u`**：Python 在管道下默认块缓冲，会把 JSON-RPC 响应憋死在缓冲区里
- `description` 不在白名单，索性不写

### 六条硬约束（踩了就静默失败）

1. `resources/read` 返回的 HTML 有 **256KiB 预取阈值**（Agent CLI）→ 面板必须做「薄壳」，别整份塞。
2. 宿主用 **`document.write`** 注入（刻意不用 srcdoc）→ 相对路径 / `import.meta.url` 指向宿主 proxy 页 → 兄弟资源全 404。
3. iframe src **必须 loopback** —— CSP `frame-src` 只放行 `http://127.0.0.1`，**不含** `file:` / `blob:` / `data:`。
4. 宿主有**两层** `frame-src`（HTTP 响应头 + 注入 meta）→ 必须靠 `_meta.ui.csp.frameDomains` 声明，**缺一层静默拦掉**。
5. **每次调带面板的工具 = 新开一扇，不复用** → 必须做「首次打开 / 后续回表格 + 问一句」分流，否则面板堆叠。
6. `photo_cache` 值是**高德直链** `store.is.autonavi.com` → 要么 `resourceDomains` 放行，要么本地代理。

### 面板侧握手（对标 `@modelcontextprotocol/ext-apps`）

```
面板 → 宿主   {jsonrpc:"2.0", id, method:"ui/initialize",
               params:{appInfo, appCapabilities, protocolVersion:"2025-06-18"}}
宿主 → 面板   {protocolVersion, hostInfo, hostCapabilities, hostContext}
面板 → 宿主   {jsonrpc:"2.0", method:"ui/notifications/initialized"}      # 通知，无回复
面板 → 宿主   {jsonrpc:"2.0", id, method:"tools/call", params:{name, arguments}}
宿主 → 面板   {jsonrpc:"2.0", method:"ui/notifications/tool-result", params}
```

---

## M0 验收清单 ✅ 全部通过（2026-10-09）

跑完 `selftest.py` 全绿只证明 **server 侧**协议正确。下面三条在 WorkBuddy 里肉眼确认：

- [x] **V1 面板能开** —— 说「打开行程面板」，对话下方出现面板
- [x] **V2 桥接能握手** —— 面板里点「探测桥接」，握手成功
- [x] **V3 能反向调工具** —— 点「调 tripplanner_ping」，回传成功

宿主机侧同步取证（`mcp-apps-diag.log`）：

```
rpc.openHostApp            appId=tripplanner/tripplanner_open_plan
openHostApp.appResolved    toolMeta={"hasUi":true,"uiKeys":["resourceUri","visibility"],
                                     "visibility":["model","app"]}
resolver.resolve.ok        htmlLen=7451  mimeType=text/html;profile=mcp-app
openHostApp.created        appUrl=http://127.0.0.1:54994/mcp-apps/<appInstanceId>
```

### 三条实测约束（M1 必须遵守）

- **面板 HTML 有宿主侧缓存**：第二次调用命中 `resolver.resolve.cacheHit … shared=true`。
  → **改了面板代码，宿主可能仍用旧版本**；调试时需触发刷新，或临时改 `resourceUri` 破缓存。
- **`appInstanceId` 每次不同**（`cf01c3e8…` vs `e0becf56…`）→ 现场验证了「不复用」约束。
- **`appInstanceId` server 侧不可见**（2026-10-09 取证）⇒ 见上方 M1 前置 A+C 方案。

### 对照组 ✅ 已验证

`tripplanner_ping` **故意不带** `_meta.ui.resourceUri`，单独调用**不会**开面板
（日志仅 `notifyToolCallUpdate` / `pushToolResult.buffered`，无任何 `openHostApp`）
⇒ 「面板由 `resourceUri` 决定」成立。

### 排查用日志

- `~/.workbuddy/logs/mcp-apps-diag.log` —— **面板生命周期的权威日志**。
  若里面搜不到 `tripplanner`，说明 server 根本没被加载（配置未生效/未重载），而不是面板渲染问题。
- `~/.workbuddy/logs/daemon.log` —— 搜 `wb:mcp:list` / `custom-mcp:` 看 IPC 与连接过程。

---

## 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M0** | 协议 spike：验证面板可开、桥接可握手、可反向调工具 | ✅ **完成（2026-10-09）** |
| **M1-pre** | CSP 放行高德图片 + A+C 面板分流（防堆叠） | ✅ **完成（2026-10-09，自检 29/29）** |
| **M1** | 最小闭环：`plan_trip` + `list_cities` + `get_plan` + 只读时间轴面板 | ✅ **完成（2026-10-09，自检 63/63）** |
| **M2** | 交互：面板内换点 / 挪时段 / 导出（AppBridge 回工具）+ 地图（路线 polyline） | ⏳ 下一步 |
| **M3** | 远端化：换 `streamable-http` transport，复用线上后端 | ⏳ |

M1 复用入口：`src/m1_planner.plan(city, query, days=2, use_llm=True, date0=None, hotel_text=None)`

### M1 设计定案（2026-10-09 Kenny 拍板 + AI 取证修正）

| # | 议题 | 定案 | 落地要点 |
|---|---|---|---|
| 1 | **「不复用」分流判据** | ~~用 `appInstanceId`~~ → **A + C 组合** | ⚠️ 原定案「server 侧维护 appInstanceId 集合」**前提有误**：取证发现 `appInstanceId` 只由宿主生成、server 侧拿不到。修正为：**A 精确**（面板经 `tripplanner_panel_ready` 自报实例 id）+ **C 兜底**（`SESSION.opened_any` 进程级标记）。已实施，`selftest.py` 4 项覆盖。 |
| 2 | **照片来源** | **高德直链 + `resourceDomains` 放行** | 已实施：`resourceDomains` 追加 `https://store.is.autonavi.com` 等 4 条。面板内加「CSP 放行验证」按钮实测。 |

其余待定项（薄壳策略 / 面板缓存 / 对照组）已按默认方案执行：

- **薄壳**：HTML < 256KiB 硬约束，真实行程数据**不塞进 HTML** —— 优先走 `open_plan` 返回值的
  `structuredContent`（宿主 `awaitBootstrapToolResult` 机制会把它喂给面板），兜底才用面板内 `tools/call`。
- **面板缓存**：调试期破缓存固定手法 = 给 `resourceUri` 加 `?v=<mtime>` 或临时改名。
- **对照组**：✅ 已补验 —— `tripplanner_ping` 单独调用**不触发** `openHostApp`，结论成立。


---

## 环境备注

- 解释器必须是 tripagent venv（M1 起要 `ortools`）：`D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe`
- 启动参数加 `-u`（无缓冲），否则 stdio 响应会被 Python 的块缓冲憋住
- 配置双写：`~/.workbuddy/mcp.json`（宿主实际读取）+ `~/.workbuddy/.mcp.json`（兼容兜底）
- **stdout 被 JSON-RPC 独占** —— 任何 `print` 都会让宿主断连，日志一律走 stderr
