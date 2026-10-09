# TripPlanner MCP App

把 TripPlanner 接进 WorkBuddy 对话：一句话生成行程，并在对话下方渲染**可视化行程面板**。

当前阶段：**M0 ✅ → M1-pre ✅ → M1 主体 ✅ → M1 修正 ✅ → M1 验收 ✅（Kenny 肉眼确认）→ M2 代码完成 ✅（自检 108/108）→ 待 WorkBuddy 实测**

**M1 已在 WorkBuddy 内实测通过**：一句话需求 → 真实行程 → 面板渲染完整时间线
（含每个点的**停留时长**）、每日主题/里程/违规徽标、**POI 缩略图**、被剔除点及原因。
数据来自本地 31 城 POI 库 + 交通/路线缓存，无需联网。

**M2 新增（面板内交互）**：🔁 换点、⠿ 拖拽调整当天顺序、⬇ 导出 Markdown、
🗺 高德 JS API 交互地图（路线 polyline + 序号标记）。
三个交互工具都带**守门**：换点不得改变当天景点数，重排不得丢点 —— 宁可操作失败，也不悄悄改坏行程。

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
| `tripplanner_replace_poi` | — | **M2** 换点（面板「🔁」按钮） |
| `tripplanner_move_poi` | — | **M2** 挪时段（面板拖拽排序） |
| `tripplanner_export` | — | **M2** 导出 Markdown 文本 |

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
自检已改为**从 `tools/list` 动态取 uri**，不再硬编码版本号，所以 +1 不会弄红测试。

---

## 🔴 M1 修正：`last_plan` 必须落盘（2026-10-09 线上踩坑）

### 现象

面板**能开**（`openHostApp.created` 正常）、握手正常、照片 CSP 正常，
但**行程区渲染不出每天的时间线** —— 只有标题与总览指标卡。

### 根因：宿主对 stdio MCP 是**按需 spawn 进程**的

```
daemon.log:
  [MCP-AuthDiag] stdio-connected {"pid":30200,"pidSpawned":16440}
  [MCP-AuthDiag] stdio-connected {"pid":30200,"pidSpawned":4556}
  [MCP-AuthDiag] stdio-connected {"pid":13876,"pidSpawned":32072}
  [MCP-AuthDiag] stdio-connected {"pid":13876,"pidSpawned":21000}
                                              ↑ 每次都不同
```

⇒ **「agent 调 `plan_trip` 的进程」与「面板调 `get_plan` 的进程」未必是同一个。**
而 `SESSION.last_plan` 原本只是**进程内存**（注释还写着「进程重启即重置，正合语义」），
面板所在的新进程里它是 `None` → `get_plan` 回 `hasPlan:false` → 面板渲染不出行程。

### 修法：状态分两类，别一刀切

| 状态 | 持久性 | 理由 |
|---|---|---|
| `opened_any` / `instances`（A+C 分流） | **进程级，放内存** | 纯控制流，生命周期短；宿主起新进程时只会「少判一次已开」，不会读到**脏数据** |
| `last_plan`（面板取数） | **🔴 必须落盘** | 「一次调用写、另一次调用读」，跨进程 ⇒ 纯内存必空 |

- 落盘位置 `output/mcp_last_plan.json`（`output/` 已在 `.gitignore`，不会误入库）
- 用 `@property` 实现「内存优先、未命中读盘」，所有读写点零改动
- 写入走 **临时文件 + `os.replace` 原子替换**，避免面板读到半截 JSON

### 同时加的诊断设施（宿主环境拿不到 stderr，只能落盘）

- **`output/mcp_debug.log`** —— 每行带 `pid`，一眼看出「这次调用是不是新进程」：
  ```
  [14:38:40] pid=34256 tools/call {"name":"tripplanner_plan_trip","planOnDisk":false}
  [14:38:45] pid=34256 last_plan.write.ok {"city":"杭州","bytes":4687}
  [14:38:46] pid=17660 tools/call {"name":"tripplanner_get_plan","planOnDisk":true}
  [14:38:46] pid=17660 last_plan.read.hit {"city":"杭州"}
  ```
- **面板「协议诊断」区**新增两行：`数据诊断`（收到的字段数 / dayList / timeline / poi 计数）
  与 `照片加载`（成功/失败计数）。数据没到 vs 渲染出错，一眼可分。
- 面板某天 `dayList` 为空时，直接渲染「⚠ 行程数据不完整」并列出**已收到哪些字段**，
  不让用户对着空面板猜。
- 面板还给每个 poi/meal **算出了停留时长**（`start`/`end` 反推，内核 timeline 只给时刻）。

### 回归守卫

`selftest.py` 新增 **阶段四：跨进程取数** —— 两个**独立进程**：A 规划落盘 → B 读取，
断言 `hasPlan=true`、`dayList` 含 `timeline`、poi 带 `photo`。
这是本坑唯一能防住的测试形态（同进程内怎么测都是绿的）。

---

## ✅ M1 验收结果（2026-10-09，Kenny 在 WorkBuddy 内肉眼确认）

| 验收项 | 结果 |
|---|---|
| 一句话需求 → 真实行程 | ✅ 「杭州两日游，喜欢自然风光」→ 2 天 / 0 违规 |
| 面板渲染完整时间线 | ✅ 含每个点的**停留时长**（`start`/`end` 反推） |
| **POI 缩略图** | ✅ **高德直链直接可用**（见下） |
| 每日主题 / 里程 / 违规徽标 / 剔除原因 | ✅ |
| 跨进程取数（`last_plan` 落盘） | ✅ `pid=32688 写 → pid=33516 read.hit` |

### 面板「协议诊断」区的三行是关键排查工具

Kenny 贴出的三行输出一次性定位了两个问题：

| 诊断行 | 读出的结论 |
|---|---|
| `render 入参：plan 为空（hasPlan=false）` | 面板被空结果覆盖 |
| `页面路径：…/mcp-apps/sandbox-proxy.html?csp=…` | **A 方案为何不可用**（见下） |
| `照片加载：成功 16 / 失败 0` | **照片与 CSP 完全正常**；且 16 = 8 poi × 2 次渲染 ⇒ 反证「数据到达过、被覆盖」 |

**🔴 A 方案正式定论：不可用。** 面板 iframe 的 URL 是 `…/mcp-apps/sandbox-proxy.html?csp=…`，
`location.pathname` 里**没有** `appInstanceId` 段；`ui/initialize` 回包也不含它。
⇒ 受宿主实现所限。**C 兜底（`opened_any`）已足够防堆面板，不再纠缠 A。**

**✅ 高德直链照片方案成立**：面板内实测 **16 成功 / 0 失败**，
CSP `resourceDomains` 放行即可，**不需要**改走本地 `/api/photo` 代理。

### 一条反直觉的坑（已加守卫）

`loadPlan()` 原实现在拿到空结果时**无条件 `render(null)`** —— 而 `get_plan` 可能落在
「还没写过行程」的另一个进程上（宿主按需 spawn，见上文）。结果是**已渲染好的时间线被抹掉**。
现象很迷惑：**照片计数 > 0 但画面空白**。
⇒ 修法：`renderedOnce` 守卫，渲染过之后读到空只提示、不清 DOM。

---

## ✅ M2 面板交互（2026-10-09 代码完成，自检 108/108）

### 三个交互 + 一个地图

| 能力 | 面板入口 | 工具 | 守门 |
|---|---|---|---|
| 换点 | 每行尾部 `🔁` | `tripplanner_replace_poi` | **当天景点数不变**；候选会挤掉别处就放弃 |
| 挪时段 | 行首 `⠿` 拖拽 | `tripplanner_move_poi` | **点集必须与当天一致**（防拖拽悄悄丢点） |
| 导出 | 顶部 `⬇ 导出 Markdown` | `tripplanner_export` | 格式白名单（目前仅 markdown） |
| 地图 | 行程上方 | 无（面板内直连高德） | key 缺失 / CSP 拦截时**降级提示，不影响行程** |

### 公共内核：`src/poi_swap.py`（**不是**在 server 里重写一遍）

换点逻辑原先内联在 `webui/server.py` 的 `/api/replace` 处理函数里。M2 需要同一能力，
**没有复制一份**（本项目的既有教训：多副本实现必然漂移），而是抽成 `src/poi_swap.py`
的 `replace_poi()` / `reorder_day()`，**Web UI 与 MCP 共用**：

- `webui/server.py` 的 `/api/replace` 现在只做参数校验 + 调 `poi_swap.replace_poi(...)`，
  响应字段与行为**逐字段不变**（发布门禁四步全绿：21 单测 + 1 前端守卫 + 31 例离线回归 + 31 城校验）。
- 两条守门规则都建在公共模块里，两边同时生效。

### 🔴 CSP 的关键发现（app.asar `buildCspMeta()` 取证）

```js
var directives = [
  "default-src 'none'",
  "script-src 'unsafe-inline' 'unsafe-eval' blob: " + resource,  // resource = csp.resourceDomains
  "style-src  'unsafe-inline' "                     + resource,
  "img-src    data: blob: "                         + resource,
  "connect-src "                                    + connect,   // connect  = csp.connectDomains
  ...
];
```

⇒ **`resourceDomains` 同时控制 `script-src` / `img-src` / `style-src` / `font-src` / `media-src`**。
所以「放行一个域名」就等于「允许从这里加载 JS」—— **面板里能加载高德 JS API 靠的就是这条**。
`connectDomains` 单独控制 `fetch/XHR`，JS API 的服务端请求必须在那里也放行。

现在两个列表都放了高德（`AMAP_DOMAINS`）：`store.is.autonavi.com`（照片）、
`*.amap.com`（JS 载体 + restapi）、`*.autonavi.com`（瓦片）。

⚠️ **漏了 `connectDomains` 会「JS 加载成功但瓦片/接口全被拦」，且不报错** —— 典型静默失败。

### 🔴 iframe sandbox：没有 `allow-downloads`（决定导出怎么做）

```js
innerFrame.setAttribute('sandbox', params.sandbox || 'allow-scripts allow-same-origin allow-forms');
```

- ✅ 有 `allow-same-origin` → Blob / canvas / `navigator.clipboard` 可用
- ❌ **没有 `allow-downloads`** → 传统 `<a download>` + Blob 会被浏览器拦掉
- ❌ 没有 `allow-popups` → `window.open` 不可用

⇒ **导出必须走宿主的 `ui/download-file` 桥接**（能力在 `hostCapabilities.downloadFile` 里声明），
兜底 `navigator.clipboard.writeText`（sandbox 默认下放 `clipboard-write`），再兜底在面板里给出
可直接复制的 textarea。**三级降级**，任何一级都不至于让用户拿不到内容。

### 🔴 地图 key 不入仓库

`panel/plan_panel.html` 里写的是占位符 `__AMAP_JS_KEY__`，**由 `read_panel()` 在
`resources/read` 时替换**成 `secrets.json` 里的 `amap_js_key`。
⇒ 仓库里永远是占位符；自检同时断言「panel 文件用占位符」+「read 返回的 HTML 里已无占位符」。

### 地图坐标是 server 补的

内核 `timeline` 只给 `id / name / start / end`，**没有坐标**。M2 在 `_normalize_day()` 里
从 POI 库补 `lat/lng`（按城市缓存；GCJ-02，可直接给高德，无需转换）。
换点 / 重排写回（`apply_day()`）走同一个归一化函数，所以坐标与照片都不会掉
（`_normalize_day` 因此必须**幂等**，自检专门守这条）。

### 守门为什么这么严

`build_itinerary` 的修复链遇到闭馆 / 餐窗 / 里程违规时会**剔点**（含前置修剪）。
若换点只看「成功没有」，很容易换进一个把当天另一个点挤掉的候选 —— 用户看到的是
「换了个点，怎么少了一个」。所以两个操作都以「**零剔点 + 点集一致**」为接受条件，
全部候选都不行就明确拒绝、**保留原行程**。

### M2 自检（108 项，阶段五专测交互）

换点点数不变 / 换入点不重复 / 写回后仍带 photo / 重排顺序与请求一致 /
点集不一致必须拒绝 / 导出是合法 Markdown 且不含 HTML 标签 / 不支持的格式要报错。

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
  tripplanner_mcp_server.py   # stdio MCP server（纯标准库、零依赖）
  panel/plan_panel.html     # 面板（自包含；地图 key 用占位符，由 server 注入）
  selftest.py               # 不开 WorkBuddy 也能验协议 / 规划 / M2 交互（108 项）
  panel_guard.py            # 面板前端守卫（U+FFFD / 标签配对 / node --check / key 安全）
  register.py               # 注册进用户级 MCP 配置（双写 mcp.json + .mcp.json，合并不覆盖）
src/poi_swap.py             # 换点 / 挪时段公共内核（Web UI 与 MCP **共用一份**）
```

## 快速开始

```bash
PY="D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"

"$PY" mcp/selftest.py          # 1. 本地自检（108 项，含真实规划 + 贫瘠环境 + 跨进程 + M2 交互）
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

### 在 WorkBuddy 里验收（M2）

前置：`~/.workbuddy/mcp-tool-list.json` 里 `tripplanner_*` 应为 **9 个**
（6 = M1 旧快照，需先按上文「重载」一节刷新）。

| 动作 | 期望 |
|---|---|
| 说「杭州2天，喜欢自然风光」 | 面板打开，行程上方出现**地图**（真实底图 + 蓝色路线 + 序号标记） |
| 点某个 POI 行尾 `🔁` | 该点被同类目就近点替换；**当天景点数不变**，其余顺序不变 |
| 拖行首 `⠿` 到同一天另一位置 | 顺序更新；顺序受约束微调时会明确说明 |
| 点顶部 `⬇ 导出 Markdown` | 走系统下载目录；宿主不支持则提示「已复制到剪贴板」 |
| 地图加载失败 | 顶部给出原因，**行程与交互仍可用**（不会白屏） |
| 拖到另一天 / 拖出列表 | 不生效（当前只支持**同一天内**重排） |

⚠️ **地图是 M2 唯一未经实测的环节**（CSP 已按取证配好，但高德 JS API 在宿主双层
iframe 里能否初始化，只有浏览器说了算）。失败时先看「协议诊断 → 地图状态」那一行，
它会区分「key 未配置」「JS 加载失败」「渲染异常」。

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
| **M1** | 最小闭环：`plan_trip` + `list_cities` + `get_plan` + 只读时间轴面板 | ✅ **完成（2026-10-09，自检 73/73）** |
| **M1-fix** | `last_plan` 落盘（跨进程）+ 空结果不清屏 + uri 匹配放宽 | ✅ **完成并肉眼验收通过** |
| **M2** | 交互：面板内换点 / 挪时段 / 导出（AppBridge 回工具）+ 地图（路线 polyline） | ✅ **代码完成（2026-10-09，自检 108/108）；待 WorkBuddy 实测** |
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
