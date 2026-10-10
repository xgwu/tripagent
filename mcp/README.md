# TripPlanner MCP App

把 TripPlanner 接进 WorkBuddy 对话：一句话生成行程，并在对话下方渲染**可视化行程面板**。

当前阶段：**M0 ✅ → M1-pre ✅ → M1 主体 ✅ → M1 修正 ✅ → M1 验收 ✅（Kenny 肉眼确认）→ M2 代码完成 ✅ → M2.1 通路修复 ✅ → M2.2 实测复修 ✅ → M2.3 死因取证 + 面板自愈 ✅ → M2 验收 ✅（换点/拖拽均可用了，Kenny 确认）→ M3 面板交互远端化 ✅（自检 176/176）→ 线上已部署 ✅（`tripagent-planner2-80644…` / `/api/health` = m3）→ M4 云主机 + CORS + Basic ✅ → M5 瘦客户端 ✅ → M5.x 云端 `/mcp`（Streamable HTTP）✅ 零安装分享 ✅ → M6 面板（定稿开一次 / 地图真实路网 / 标点 / 高度上报）✅（`PANEL_VERSION` m18）→ M6.4 换点不丢当日主题 ✅（`PANEL_VERSION` m19 / `WEBUI_VERSION` m7）→ ⛔ **本地打包（exe / mac 源码包）已退役（2026-10-10）**：分享一律走远端 URL，见「⛔ 已退役」一节**

**M1 已在 WorkBuddy 内实测通过**：一句话需求 → 真实行程 → 面板渲染完整时间线
（含每个点的**停留时长**）、每日主题/里程/违规徽标、**POI 缩略图**、被剔除点及原因。
数据来自 **31 城 1193 POI 库 + 交通/路线缓存**（M5 起这些都在**云端**，见下）。

**M2 新增（面板内交互）**：🔁 换点、⠿ 拖拽调整当天顺序、⬇ 导出 Markdown、
🗺 高德 JS API 交互地图（路线 polyline + 序号标记）。
三个交互工具都带**守门**：换点不得改变当天景点数，重排不得丢点 —— 宁可操作失败，也不悄悄改坏行程。

**🔴 M2.1（2026-10-09）**：实测发现**宿主 → MCP server 的 `tools/call` 通道在本机是死的**
（面板 ping 10s 超时；宿主日志记了 `rpc.callTool` 却从未到达 server，12ms 假成功）。
那一天改走 `mcp/local_api.py` 开在 **`127.0.0.1`** 的本地 HTTP。
> ⛔ **该方案已于 M5 下线**（`mcp/local_api.py` 文件已删除）。本节保留是因为它记录了一条
> **仍然成立**的否定性结论（宿主 `tools/call` 死通道），以及「本地面板后端」那条路的完整死因。
> 现行通路见下方「M5」一节。

**✅ M5（2026-10-10）**：**去掉本地服务**，MCP server 退化为**瘦客户端**（薄代理），
规划/换点/挪时段/导出**全部走云端**。这是后面两条路的地基：
**云端 `/mcp`（Streamable HTTP，零安装分享）** + **瘦客户端本身**。详见下方「M5」一节。

> ⛔ **本地打包（exe / mac 源码包）已于 2026-10-10 退役** —— MCP 已在远端，分享改为
> 「贴 URL + 头 → 点信任」，不再产出本地文件。见「⛔ 已退役」一节。

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
| 1 | 面板会堆叠（每次调 `open_plan` 都新开一扇） | ~~**A + C 组合分流**（见下）~~ 🔴 **2026-10-10 证伪**：该机制只在**服务端**成立，宿主根本不看 ⇒ 见「M6.3」 |
| 2 | 高德直链照片会被 CSP 拦 | `_meta.ui.csp.resourceDomains` 追加高德域名 |
| 3 | `appInstanceId` server 侧拿不到 | 新增 `tripplanner_panel_ready` 工具，由**面板自报** |

### A + C 分流（Kenny 2026-10-09 拍板）—— 🔴 2026-10-10 证伪，仅保留作诊断

| 方案 | 角色 | 机制 |
|---|---|---|
| **A**（精确） | 主判据 | 面板握手成功后调 `tripplanner_panel_ready`，把宿主注入的 `appInstanceId` 报给 server；server 维护「活跃实例集合」。`open_plan` 带该 id 且已登记 ⇒ `panelOpened=false` |
| **C**（兜底） | 保险丝 | 模块级 `SESSION.opened_any`：**本进程只要开过任意一扇面板，后续 `open_plan` 一律判为「已打开」**。防实例 id 取不到时仍堆面板 |

> 🔴 **证伪（2026-10-10，报障「重拍时面板叠了好几扇」后从 `app.asar` 取证）**：
> 宿主 `McpAppsGateway.handleAgentToolCall()` 的去重键是 **`${sessionId}::${toolCallId}`**，
> 即「**每一次工具调用 = 一扇新面板**」；它只按**工具定义**里有没有 `_meta.ui.resourceUri`
> 判断这工具算不算 App，**完全不读**工具结果里的任何字段。
> ⇒ `panelOpened` / `reason` 这类字段**只有诊断价值，拦不住宿主**，A+C 从未真正防住堆叠。
> 实测证据：同一 `sessionId` 连调 3 次 `plan_trip`，宿主日志出现 3 条 `openHostApp.created`
> （3 个不同 `toolCallId` / `appInstanceId`）。
> **真正的修法见「M6.3」：把会开面板的工具收成一个（`open_plan`），并只在定稿后调一次。**

> ⚠️ **为什么判据不能只用 `appInstanceId`**：它由宿主中间层生成（日志里的 `openHostApp.created`），
> `rpc.openHostApp params` 里**不含**它，`proxyAppToolCall` 转发给 MCP server 的 `tools/call`
> 也只有 `name` + `arguments` ⇒ **server 侧根本看不到**。所以只能由面板自报（A），且必须有 C 兜底。

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
| `tripplanner_open_plan` | ★开 | **面板的唯一入口**。展示当前最新行程（还没有行程则提示先调 `plan_trip`）。同一对话**只调一次** |
| `tripplanner_plan_trip` | — | **主入口**。一句话需求 → 真实行程（**不开面板**，可反复调用/重拍） |
| `tripplanner_get_plan` | — | 面板握手后主动拉行程（M1 数据通道） |
| `tripplanner_list_cities` | — | 列出 31 个可规划城市 + POI 数 |
| `tripplanner_panel_ready` | — | 面板自报 `appInstanceId`（A 方案，诊断用） |
| `tripplanner_ping` | — | 连通性对照组 |
| `tripplanner_replace_poi` | — | **M2** 换点（面板「🔁」按钮） |
| `tripplanner_move_poi` | — | **M2** 挪时段（面板拖拽排序） |
| `tripplanner_export` | — | **M2** 导出 Markdown 文本 |

> 🔴 **M6.3 起 `plan_trip` 不再是面板入口**（2026-10-10）。原因：宿主按 `toolCallId` 去重
> ⇒ **每次调用各开一扇**、服务端拦不住 ⇒ 重拍就叠面板。把入口收成 `open_plan` 一个之后，
> `plan_trip` 调多少次都不会堆面板。守卫：`scripts/test_panel_single_open.py`（+ 红队
> `mcp/redteam_panel_single.py`）。

> ⚠️ **M2.1 起**：这三个交互工具面板**不再直接调**（宿主 `tools/call` 通道在本机不通，
> 见「M2.1」一节），改走 `127.0.0.1` 的本地 HTTP。工具本身保留 ——
> 面板侧仍以它们为**兜底**，模型或其它 MCP 客户端也可直接调。

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

## 🔴 M2.1：面板交互改走本地 HTTP（2026-10-09，Kenny 实测报障后定位）

> ⛔ **本节方案已于 M5 下线**（`mcp/local_api.py` 已删除、面板不再拉起任何本地服务）。
> 保留原因：① 它记录的「宿主 → server `tools/call` 在本机是死通道」这条**否定性结论仍然成立**，
> 是理解后续全部设计（面板为何不靠 `tools/call`）的前提；② 它完整记录了本地常驻进程那条路的
> 死因（端口文件 / token 漂移 / 进程被宿主 job 回收），排查宿主按需 spawn 问题时依然有用。
> **现行通路见上方「M5」一节。**

### 报障

> 「更换景点按钮不起作用，拖拽更改景点顺序也不起作用」

### 取证结论：**按钮点了，请求发出了，但那条通道是死的**

不是前端 bug，也不是工具没注册。四条硬证据：

| # | 事实 | 数据 |
|---|---|---|
| 1 | 工具注册**正常** | `~/.workbuddy/mcp-tool-list.json` 里 `tripplanner_*` = **9** ✅ |
| 2 | 面板**确实发了请求** | 宿主 `~/.workbuddy/logs/mcp-apps-diag.log` 有 `rpc.callTool toolName=tripplanner_replace_poi args={...}`，参数完全正确（3×replace + 2×move） |
| 3 | 🔴 **请求从未到达 MCP server** | `output/mcp_debug.log` 里那几次调用**一条都没有**（时间轴直接跳过）；`output/mcp_last_plan.json` 未改写 |
| 4 | 🔴 **宿主回的不是我们的结果** | 返回体恒为 `contentCount=1, textLen=43, structuredContentKeys=[]`；而实测我们的 `panel_ready` 才是 43 字符、`get_plan` 有行程时是 **286**/无行程 **34** ⇒ 不可能是我们返回的 |

**决定性判据（一个动作就能定论）**：面板「协议诊断 → 调 tripplanner_ping」→ **10s 超时**。

补充证据：宿主从 `callTool` 到 `.ok` 只花 **12ms** —— 而真实换点要跑一遍 TOPTW 求解（几百 ms 起）。
另外 `tripplanner_export` 在宿主诊断日志里**一次都没出现**。

> 由此还纠正了一个 M1 以来的误判：**面板一直能显示行程，靠的是宿主 bootstrap 推送**
> （`ui/notifications/tool-result`，那条**带** `structuredContent`），
> **而不是** app→server 的 `tools/call`。也就是说这条通道从来没被真正验证过。

### 方案：`mcp/local_api.py`（独立常驻进程）

| 决策 | 理由 |
|---|---|
| **开在 `127.0.0.1` 的本地 HTTP** | 宿主 CSP 的 `connectDomains` 本来就放行了 `http://127.0.0.1:*`（M2 时为地图加的），无需改宿主 |
| **独立 detached 常驻进程**（不是 server 内开线程） | 宿主对 stdio server 是**按需 spawn**（`mcp_debug.log` 里 pid 每隔几秒就换）⇒ 挂在 server 进程里的 HTTP 会随它一起死 |
| **业务逻辑全部复用 server 模块** | `local_api.py import tripplanner_mcp_server`，调 `kernel()` / `apply_day()` / `plan_to_markdown()`，**不复制一份** |
| **状态仍是 `output/mcp_last_plan.json`** | 磁盘是跨进程唯一真相；面板改的与助手改的是同一份 |
| **端口优先 51900 起** | 让面板 URL 跨重启稳定；被占用才退化随机端口 |
| **`X-Tripplanner-Token` 校验** | 随机 32 hex，写进 portfile、随面板 HTML 注入 ⇒ 本机其它网页无法驱动行程 |

端点（形状与工具的 `structuredContent` **一致**，面板侧一套代码可切换通路）：

```
GET  /api/health    → {ok, server, version, stage, pid, ts}     ← 不校验 token（探活用）
GET  /api/plan      → {ok, hasPlan, plan}
POST /api/replace   → {ok, swap, day, plan}      | {ok:false, error}
POST /api/move      → {ok, order_respected, day, plan} | {ok:false, error}
POST /api/export    → {ok, filename, mimeType, text}   | {ok:false, error}
```

server 只在 `resources/read` 时做两件事：`ensure_local_api()`（不在跑就拉起）+
把 `__LOCAL_API_BASE__` / `__LOCAL_API_TOKEN__` 替换成真实值 —— 与地图 key 同一套「占位符不入库」手法。

### 🔴 两条踩过的坑（都已加守卫）

1. **常驻进程必须比对版本，否则「改了代码没生效」且不报错**
   `ensure_local_api()` 读 `/api/health` 的 `version`，与 `SERVER_VERSION` 不一致就**杀掉重拉**。
   ⇒ **改 `tripplanner_mcp_server.py` 或 `local_api.py` 都必须 +1 `SERVER_VERSION`**
   （`local_api.py` import 了 server 模块，所以 server 的改动同样影响常驻进程）。
   自检阶段六会断言「常驻进程版本 == server 版本」。

2. **跨进程读状态必须「始终读磁盘」，不能内存优先**
   本轮自检**抓到真 bug**：`read_plan_state()` 原是内存优先，而常驻的 local_api 进程内存
   永不失效 ⇒ 助手（另一个进程）刚生成的新行程，面板读到的还是旧的那份。
   已改为**读一律走磁盘**（写仍是「落盘 + 同步本进程缓存」）。

### 面板侧：三条通路，如实显示走哪条

| 通道 | 说明 |
|---|---|
| **1（主）本地 HTTP** | `/api/plan` 拉行程；换点 / 挪时段 / 导出都走这里 |
| **2（兜底）宿主 `tools/call`** | 本机实测不通，保留作为兜底与对照 |
| **3（补）bootstrap 推送** | 宿主把 `plan_trip` 的结果推给面板；**这条带 `structuredContent`**，M1 以来一直靠它 |

诊断区新增一行「**本地 API**」+ 按钮「**测试本地 API**」，会明确显示
`✅ 本地 API 可用 http://127.0.0.1:51900（pid=… / v…）` 或
`⚠ 本地 API 不可用：<原因> → 互动将回落宿主 tools/call（该通道在本机已实测不通）`。
**不再有「点了没反应但什么都不说」的情况。**

### M2.1 自检（阶段六）

health 探活 / 无 token 403 / 有 token 通过 / 无行程时导出明确报错 /
本地 API 读到与 MCP 工具**同一份**行程（跨进程） / HTTP 换点点数不变 + 结果**真的落盘** /
HTTP 重排顺序一致 + 点集不一致拒绝 / HTTP 导出是合法 Markdown + 文件名 `.md` /
不支持的格式报错 / 未知路径 404 / **常驻进程版本 == server 版本** /
**token 已落盘且与端口文件一致**（跨重启稳定） / `/api/log` 收面板轨迹、无 token 拒绝。

---

## 🔴 M2.2：二次报障复修（2026-10-09，Kenny 实测「还是不起作用」后定位）

### 报障

> 换点和拖拽还是不起作用；诊断区点「测试本地 API」**显示本地 API 可用**。

「诊断说可用、功能就是不动」——这句话本身就是最强的线索：**探活与实调用走的不是同一条校验路径**。

### 取证（四条，都在日志里）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 面板启动时的 `GET /api/plan` **从未发出** | `mcp_debug.log` 里浏览器来的只有 `OPTIONS/GET /api/health`，一条 `/api/plan` 都没有 |
| 2 | 🔴 根因 A：**`fetch` 在 GET 上带 body 会同步抛 TypeError** | `httpJson` 无条件 `if (body) opt.body = ...`，而 `loadPlan` 传的是 `{}`（truthy）⇒ 异常在 `.catch` 挂上之前就抛出，**整条「探通路 → 拉行程」链静默断掉** |
| 3 | 🔴 根因 B：**常驻进程会无声消失，而 token 每次重启都换** | 32348 被版本闸门杀掉（有日志）；**25064、11892 没有任何 kill 记录就不见了**（宿主连进程树回收）。token 是 `token_hex(16)` 随机 ⇒ 面板手里那份立刻过期 |
| 4 | `health` **刻意不校验 token** | 于是「测试本地 API」成功、`/api/plan` 与换点却 403/连不上 —— 与报障完全吻合 |

> 顺带纠正 M1 以来的一个误判：面板**一直**能显示行程，靠的是宿主 bootstrap 推送
> （`ui/notifications/tool-result`，那条**带** `structuredContent`）；启动链的 `/api/plan`
> 其实一直是断的，只是被 bootstrap 掩盖了。

### 修法（四条）

1. **GET/HEAD 绝不带 body**（`plan_panel.html`）—— 并加守卫锁死（见下）。
2. **token 落盘、跨重启稳定**：`srv.local_api_token()` 生成一次写 `output/mcp_local_api_token.txt`，
   常驻进程与面板 HTML 读同一个文件 ⇒ API 被杀重启后面板**不用重新注入**照样能用。
   （端口本来就固定 51900，两者合起来才成立。）
3. **拉起方式加固**：`CREATE_BREAKAWAY_FROM_JOB` + **经 launcher 中转**（真正的 API 进程父进程
   立刻退出 ⇒ 变孤儿，宿主按进程树枚举也找不到）。`local_api.spawn` 现在会记 `mode`，
   `local_api.listen` 会记 `in_job` —— 下次再被杀，一眼能看出是哪层没兜住。
4. **失败必须看得见**（观测缺陷也是缺陷）：
   - 面板顶部新增**红色失败横幅**（以前只在 11px 灰字里说一句）；
   - `window.onerror` + `unhandledrejection` 全部上报到横幅（这次根因 A 就是靠这个才不会再隐形）；
   - 工具栏常驻**通道徽标**（本地服务就绪 / 已停止 / 回落宿主通道）；
   - 成功或失败都往 `POST /api/log` 打一条 `panel.trace` ⇒ 下次「点了没反应」时，
     能直接分辨「按钮没触发」「本地服务死了」「服务端拒绝」；
   - 失败不再静默回落到**已知不通**的宿主通道（那只会换来 10s 静默等待），
     改为「重试一次 → 复探健康 → 响亮报错并给出恢复方法」。
   - `/api/plan`、`/api/replace` 等被 token 拒绝时**也落日志**（`local_api.token.reject`）。

### 版本

`SERVER_VERSION` → **0.2.4-m2.2**、`PANEL_VERSION` → **m6**
（**M2.2 当时值**；当前值见 M3 节 → `0.3.1-m3` / `m9`）。
⚠️ 仍是那条铁律：**改 `local_api.py` 或 `tripplanner_mcp_server.py` 都要 +1 `SERVER_VERSION`**。

### M2.2 自检（143 项；阶段六扩到 35 项）

新增：token 已落盘且与端口文件一致 / `POST /api/log` 可写、无 token 被拒。
面板守卫新增：`#alert` 横幅、`unhandledrejection`、`panel.boot` 轨迹、**GET/HEAD 不带 body 的守卫**。

---

## 🔴 M2.3：把「常驻进程会死」变成**可判定的事实** + 面板自愈（2026-10-09）

### 报障

> M2.2 面板显示「读取失败：本地服务未运行（已尝试自动重连）」+ 红色横幅。

### 先纠正一个误判

现场日志显示**当时功能其实是通的**：`16:43:41 replace.ok 花港观鱼→太子湾公园`、
`16:43:44 move.ok respected=true`。用户看到的是**服务被回收的那两分钟**里的状态，
之后任一 MCP 工具调用把它重新拉起来了（`16:42:43 spawn → 16:42:45 listen`）。

⇒ 真正要治的不是「面板写错了」，而是**常驻进程为什么会无声消失**。

### 已有的**否定性**结论（实验，不是推测）

- `_jobtest.py`：三种拉起方式（direct-break / launcher-break / WMI）子进程 `in_job` **全 = 1**
  ⇒ **`CREATE_BREAKAWAY_FROM_JOB` 在本环境无效**。
- `explorer.exe` 拉起：rc=1，进程根本没建起来（不可用）。
- 面板发起的 `resources/read` **不会**到达 server（那次 `spawn` 的进程日志里没有
  `panel 本地 API 注入`）⇒ **不能**把「远程重启本地服务」当作可靠恢复手段。
- 存活观测：1.5 ~ 5 分钟不等，**没有任何 kill 日志**（我们自己的版本闸门 kill 是会记日志的）。

### 修法：先取证，再谈根治

**1）常驻进程装「死因取证」**（`mcp/local_api.py`）—— 判据表：

| 日志现象 | 结论 |
|---|---|
| `local_api.exit reason=atexit` | 走到收尾逻辑（非硬杀） |
| `local_api.exit reason=signal-15` | 被 `taskkill`（不带 `/F`）等**优雅**结束 |
| `local_api.crash` + traceback | 自身异常崩了 |
| **只有心跳，之后什么都没有** | **硬杀**（job 关闭 / `taskkill /F`）⇒ 只能靠外部代理 |

- `local_api.heartbeat` **每 60s 一条** ⇒ 最后一条心跳的时间就是死亡窗口起点，
  可与 `~/.workbuddy/logs/daemon.log`、`mcp-apps-diag.log` 对齐找凶手。
- `stderr/stdout` 落盘 `output/mcp_local_api.err.log`（**以前是 DEVNULL，崩溃现场全丢**）；
  server 侧拉起时也把 stderr 接到同一文件 —— import 阶段的崩溃同样看得见。
- `sys.excepthook` / `threading.excepthook` / `atexit` / SIGTERM·SIGINT·SIGBREAK 全部埋点。
- `local_api.listen` 现在带 `ppid` + `ppid_name`（判「谁创建了我」）。

**2）复活窗口最小化**：`ensure_local_api()` 挂到 `handle()` **总入口**
（除 `notifications/*` 外**所有**方法：`initialize` / `tools/list` / `tools/call` / `resources/read`）。
宿主每碰一次 MCP ⇒ 本地服务复活一次（内部 30s 缓存，命中时零成本）。

**3）面板自愈**（`panel_guard` 已加锚点守卫）：

- `waitForApi()`：掉线后**不立刻报错**，每 2s 复探、横幅显示进度，最长等 60s；
  **请求尚未发出**，服务一恢复就立刻续做 ⇒ 用户不用重新点。
- `watchdogTick()` 每 15s 轻探一次：掉线**响亮提示**、恢复**自动重拉行程**（磁盘是唯一真相）。
- `retryApi()`：诊断区按钮从「测试本地 API」升级为「测试 / 重试」，会**等它回来并自动刷新**。
- `reviveViaHostBestEffort()` 增加一条 fire-and-forget 的 `tools/list`
  （只读幂等，比再发一次 `initialize` 安全；宿主若代理就顺手唤醒了服务）。

### 版本

`SERVER_VERSION` → **0.2.5-m2.3**、`PANEL_VERSION` → **m7**
（**M2.3 当时值**；当前值见 M3 节 → `0.3.1-m3` / `m9`）。

### M2.3 自检（150 项；已并入 M3 的 176 项）

新增 7 项**源码级守护**：退出取证 / 崩溃取证 / 心跳 / stderr 落盘（两端）/
server 不再把 stderr 指向 DEVNULL / 总入口确保本地 API / 面板「等恢复再发请求」+ 看门狗。

### ⚠️ 仍未根治

常驻进程**依然会被宿主回收**（取证只是让它可判定）。要根治只有两条路：

1. **外部代理拉起**（计划任务 / WMI 之外的服务）—— 让它活在宿主 job 之外；
2. **M3 走线上后端** —— 面板直连已部署服务，不再依赖任何本地进程。
   ⇒ **Kenny 2026-10-09 拍板选这条**（见下节 M3）。

---

## 🟢 M3：面板交互远端化（2026-10-09，自检 176/176）

### 要解决的问题

M2.3 把「常驻进程会死」变成了**可判定的事实**，但**没根治**：`CREATE_BREAKAWAY_FROM_JOB`
在本环境无效，宿主回收 job 时本地 API 一起走。根治只有两条路 —— 外部代理（计划任务）
或**走线上后端**。Kenny 选了后者。

### 定案（Kenny 2026-10-09 拍板）

| 议题 | 定案 |
|---|---|
| 路线 | **面板交互远端化**（保留本地 stdio MCP 生成行程：快、可离线） |
| 通路优先级 | **本地优先，线上兜底** |

⇒ 本地 `127.0.0.1:51900` 仍是首选（无网络往返）；本地一死（被回收），
交互**自动切到线上已部署的 Web UI**，用户不再看到「点了没反应」。

### 线上现状（2026-10-09 部署后实测）

| 域名 | 状态 |
|---|---|
| `tripagent-planner2-80644.app.workbuddy.host`（**新，本仓库目录对应的 app**） | 🟢 **m3 已部署**：`/api/health` 200 `{version:"m3",stage:"M3"}`、`/api/cities` 200；`/api/move`、`/api/export` 已随本次部署上线 |
| ~~`tripagent-planner2.app.workbuddy.host`（旧）~~ | ⛔ **已下线、不再维护（2026-10-10 Kenny 确认）** —— 早期遗留应用，从未跟上 M3，已从 `REMOTE_API_DOMAINS` 删除 |

🔴 ⇒ `REMOTE_API_DOMAINS` 曾必须把**新域名排在首位**（`REMOTE_API_BASE_DEFAULT = REMOTE_API_DOMAINS[0]`），
否则面板兜底会探活到旧应用的 `/api/health` 404，被误判为「后端不可用」。
**该约束现已消失**（首条＝自建云主机 `http://101.35.47.213`）。教训保留：**探活基准必须是列表首条，
且列表里不能留「跑旧代码的域名」** —— 它是静默兜底失败源。

线上原本**缺**三个端点 —— M3 补齐（改 `webui/server.py`）：

| 端点 | 作用 | 实现 |
|---|---|---|
| `GET /api/health` | 探活（无需鉴权） | 回 `{ok, server, version, stage, ts}` |
| `POST /api/move` | 挪时段（线上无状态） | 复用 `src/poi_swap.reorder_day` + 点集守门 + `plan_view.normalize_day` 补照片/坐标 |
| `POST /api/export` | 导出 Markdown（线上无状态） | 复用 `src/md_export.plan_to_markdown`；**plan 由面板带上** |

线上端点**无需会话状态**：入参自包含（`day_ids` / `ordered_ids` / `plan` 全由面板给），
所以天然支持多用户并发。⚠️ 现有 `/api/replace` **未动** —— webui 前端依赖内核 day 的
`tips` 等字段，归一化会丢，别顺手改它。

### 关键设计：一条实现，两处跑

M3 让「同一天的处理」同时跑在**本地 MCP server** 与**线上 Web UI** 里。
若各写一份必然漂移（本项目既有教训），所以抽成 `src/` 单一实现、两边 import：

| 模块 | 内容 | 谁用 |
|---|---|---|
| `src/plan_view.py` | `photo_of` / `poi_coords` / `normalize_day` / `apply_day` | MCP server（薄委托）+ Web UI |
| `src/md_export.py` | `plan_to_markdown` / `export_filename`（纯函数） | MCP server（薄委托）+ Web UI |

MCP server 侧保留**同名薄封装**（内部调用点零改动），且 `src/` 依然**懒加载**（不拖慢 `initialize`）。

### 面板侧：双后端 + 前端写回

- `BACKENDS = [{本地}, {线上}]` —— `pickBackend()` **本地优先**，20s 内探活过就直接用；
  否则按顺序探活，单后端失败立即试下一个。
- `apiCall()` 连接级失败 ⇒ **排除该后端、换下一个再试**；全不可用才响红色横幅。
- 两后端的返回**形态不同**（本地回完整 `plan`；线上回改动的 `day`）⇒ 面板统一收敛：
  `applyActionResult(sc)` 有 `plan` 就用，只有 `day` 时走 `applyDayToPlan()` 在前端
  **复现 `apply_day`**（并从已渲染行程里按 id 补 `photo`/坐标 —— 线上 `/api/replace` 回的是内核 day）。
- `loadPlan()` 标记 `localOnly` —— 线上后端**无会话状态**，给不出「当前行程」，
  所以「拉行程」只在本地有意义；线上兜底负责的是换点 / 挪时段 / 导出。
- 导出统一把 `plan: LAST` 带上（本地也以传入的为准 ⇒ 导出的一定是用户眼前这份）。
- `local_api.py` 的 `POST /api/export` 同样支持 `plan` 入参（缺省回落磁盘，向后兼容）。

### CSP

`connectDomains` 追加 `REMOTE_API_DOMAINS`（线上域名）。**不需要** `frameDomains` ——
面板不 iframe 线上页面，只是 `fetch` 它。若漏了 `connectDomains`，远端兜底会被**静默拦**
（JS 不报错、请求根本没发出去）。

### 版本

`SERVER_VERSION` → **0.3.1-m3**、`PANEL_VERSION` → **m9**；`webui` 侧 `WEBUI_VERSION = "m3"`。
（`0.3.1-m3`/`m9` 是因为部署后**新域名才是 m3 后端**，把 `REMOTE_API_DOMAINS` 新域名前置 ⇒ 注入的
base 变了 ⇒ 面板 HTML 必须换 uri 重取，故 `PANEL_VERSION` +1。）

### M3 自检（176 项；阶段七专测远端化）

公共内核存在 / 两边**引用同一份**（非复制）/ server 不再内联渲染实现 /
`normalize_day` **幂等** / `apply_day` 不改原对象 + 刷新总里程 / md 合法且无 HTML 标签 /
Web UI 三个端点源码锚点 + 复用公共内核 / 面板远端占位符 + 本地在前 + 双形态消费 /
CSP `connectDomains` 放行线上且**不放** `frameDomains`。

面板守卫同步新增：`__REMOTE_API_BASE__` 占位符 / `BACKENDS` 本地在前 /
导出带 `plan` / 拉行程带 `localOnly` / `pickBackend` / `applyDayToPlan`。

### ⚠️ 仍未做

- **线上部署已完成**（2026-10-09）：`https://tripagent-planner2-80644.app.workbuddy.host/`，`/api/health` 回 `version=m3`；
  新域名（本目录对应 app）前置进 `REMOTE_API_DOMAINS` ⇒ `SERVER_VERSION 0.3.1-m3` / `PANEL_VERSION m9`。
  仍未做：**WorkBuddy 内实测**（面板标签应 `M3`、掉线徽标切「线上后端」）。
- 地图仍未实测（见文末）。

---

## 🟡 M4：云主机接入远端后端 + CORS 补缺 + Basic 认证（2026-10-10）

### 🔴 先修的真缺陷：M3 的远端兜底**其实从没生效过**

M3 把面板后端做成了「本地优先 → 线上兜底」，但 **`webui/server.py` 一个 CORS 头都没有**
（只有 `mcp/local_api.py` 有）。面板 iframe 的 origin ≠ 线上域名 ⇒ 跨域 `fetch` 的响应
会被浏览器**同源策略直接丢弃**（JS 侧只看到 `TypeError: Failed to fetch`，很容易被误读成
「后端不可达 / 冷启动」）。因为「WorkBuddy 内实测 M3」一直没做，这个缺口一直没暴露。

**M4 修法**：`webui/server.py` 新增 `_cors()`（`Access-Control-Allow-Origin: *` +
`Allow-Methods: GET, POST, OPTIONS` + `Allow-Headers: Content-Type, Authorization,
X-Tripplanner-Token`）并在 `_json`/`_html`/分享响应统一调用；新增 `do_OPTIONS` 回 204。
`WEBUI_VERSION` m3 → **m4**。
（用 `*` 而非回显 Origin：请求不带 Cookie，凭据走 `Authorization` 头，`*` 即可，
且能兼容面板 iframe 的 opaque/自定义 origin。）

### 接入方式：自建云主机成为 `REMOTE_API_DOMAINS[0]`

云主机（腾讯云 Lighthouse `101.35.47.213`，详见仓库 `deploy/cvm/`）跑的是**本仓库同一份代码**，
所以 M4 也顺带把它接成远端后端：

- `REMOTE_API_DOMAINS[0] = "https://101-35-47-213.sslip.io"`（其后仍是 CloudStudio 两个域名）。
- 新增占位符 **`__REMOTE_API_AUTH__`**：panel 侧 `BACKENDS` 的远端项带 `auth` 字段，
  `httpJsonOn()` / `trace()` 在 `bk.auth` 存在时发 `Authorization` 头。
  值由 `remote_basic_auth()` 从 `secrets.json` 的 `remote_basic_auth`（`"user:pass"`）现场
  base64 成 `Basic …`，**只在 `resources/read` 时注入** —— 仓库里永远只有占位符
  （与 `amap_js_key` / 本地 token 同一套「凭据不入库」做法）。

### nginx HTTP Basic 认证（`deploy/cvm/setup_server.sh`）

用 `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` 环境变量开关（**须 `sudo -E`** 透传）：
装 `apache2-utils`、写 `/etc/nginx/.htpasswd_tripagent`。关键一条：

```nginx
location / {
    # OPTIONS = 浏览器 CORS 预检。面板远端请求带 Authorization（非简单请求）⇒
    # 一定先发 OPTIONS；预检若也 401，请求根本发不出去（表现为「线上后端不可达」）。
    limit_except OPTIONS {
        auth_basic "TripAgent";
        auth_basic_user_file /etc/nginx/.htpasswd_tripagent;
    }
    proxy_pass http://127.0.0.1:3000;
}
```

`limit_except` 里允许 `auth_basic`（nginx 文档明确）⇒ 认证只对「非 OPTIONS」生效。
自检三连：无凭据 `401` / 带凭据 `200` / `OPTIONS` `204` —— **实测全部符合**。

### 🔴 未完成：HTTPS 被「未备案域名拦截」挡住（实测证据）

本想用 `*.sslip.io`（免费泛解析）+ Let's Encrypt，`certbot --nginx` HTTP-01 **失败**：
Let's Encrypt 收到的是腾讯云的 `dnspod.qcloud.com/.../webblock.html?d=101-35-47-213.sslip.io`。
本机复核确认为**腾讯云境内节点对「未备案域名」的拦截**：

| 访问 | 结果 |
|---|---|
| `http://101.35.47.213/api/health`（按 IP） | ✅ `401` + `WWW-Authenticate: Basic realm="TripAgent"` |
| `http://101-35-47-213.sslip.io/...`（按域名） | ❌ 空响应（被拦） |

且 **sslip.io 无法备案**：① 备案要求「域名持有人 = 备案主体」，sslip.io 注册人不是使用者；
② `.io` 不在工信部批复的可备案后缀名单里。
⇒ 想用域名只有三条路：**买可备案域名 + 备案（7–20 工作日）** / **换境外节点（香港、新加坡等，无需备案）** /
**Cloudflare Tunnel**（`cloudflared` 出站映射出 https 地址，免备案、免开 443）。
另有一条零成本验证：面板 iframe 上下文若非 https，则**根本没有混合内容问题**，直接用
`http://101.35.47.213` 即可（尚未实测）。

**现状（M5 更新）**：云主机 = **纯 IP + Basic 认证**（`http://101.35.47.213/`，用户 `trip`）；
443 未开、无证书；`REMOTE_API_DOMAINS[0]` 已回落到 **`http://101.35.47.213`**。
**M5 实测判定：域名 / HTTPS 都非必需** —— 宿主面板 iframe 的上下文是 **`http:`**
（`panel.boot` 实测 `proto=http:`、`origin=http://127.0.0.1:<随机端口>`），
**根本不触发混合内容**（浏览器只在 https 页面里拦 `http://` 子请求）。
原「必须 https」的推断据此作废。
> 🔑 顺带写死一条容易反复搞错的判据：`http://127.0.0.1` / `localhost` 属于 *potentially trustworthy*，
> 在 **https 页面**里 fetch 它**不算**混合内容（这就是「本地后端一直能通」的原因）；
> `http://<公网IP>` 没有这个豁免 ⇒ **只在 https 上下文才被 block** —— 而宿主是 http，故不适用。

### 版本

`SERVER_VERSION` → **0.4.0-m4**、`PANEL_VERSION` → **m10**、面板 `appInfo.version` → `0.4.0-m4`、
`WEBUI_VERSION` → **m4**。`panel_guard.py` 新增：`__REMOTE_API_AUTH__` / `REMOTE_AUTH` 占位符守卫 +
「`httpJsonOn` 给远端后端带 `Authorization`」结构守卫。

---

## 🟢 M5：瘦客户端（＋ ⛔ 已退役的「单文件 exe 分发」）（2026-10-10，自检 211/211）

> ⛔ 本节是**当时的决策记录**：其中「本地打包 / 单文件 exe」那一半已于**同日退役**
> （见「⛔ 已退役：本地打包」一节）；**「瘦客户端」这一半是现行架构**，保留全文。

### 起因（Kenny 原话）

> 「我不想用本地服务，后端都走线上，因为后面我需要把 mcp app 打包分享出去，
> 让其他用 workbuddy 的人也可以使用」

M1~M4 的形态是「MCP server 直接 import `src/` 内核」+「面板后端本地优先 → 线上兜底」。
这条路**分享不出去**：对方机器上没有 `src/`、没有 `data/`（31 城 POI 库、交通缓存）、
没有 `secrets.json`、没有 DeepSeek Key，也没装 `ortools`。三条路摆在一起：

| 方案 | 结论 |
|---|---|
| 继续「本地内核优先」 | ❌ 要分享就得带上 GB 级数据 + `ortools` + 密钥，等于给对方发一份完整仓库 |
| **瘦客户端 + 云端后端** | ✅ **选定** —— server 只做代理，内核/数据/密钥全留云端 |
| 重写成 Web App | ❌ 放弃了「在对话里渲染面板」这个 MCP App 的核心卖点 |

定案三项：**瘦客户端** + **Basic 凭据随包分发** + **打包成单文件 exe**
（⛔ 第三项已于 2026-10-10 退役 —— 分享改用**远端 `/mcp` + URL/Header**，连「包」都不必发）。

### 架构：从「胖客户端 + 双后端」到「瘦客户端 + 单后端」

```
M1~M4：  WorkBuddy ── stdio ──> MCP server ──import──> src/ 内核（POI 库 / TOPTW / DeepSeek）
                                    └─ 面板 ──> 本地 local_api.py ──> 兜底：线上后端

M5：     WorkBuddy ── stdio ──> MCP server（薄代理，纯标准库）
                                    └─ 面板 ──────────────────────> 线上后端（唯一通路）
                                       （规划 / 换点 / 挪时段 / 导出 全在云端）
```

server 侧**只剩四件事**：MCP 协议、面板资源（含占位符注入）、行程落盘（跨进程）、会话 id 管理。

### 关键实现要点

1. **零依赖是硬前提，且必须是「实际 import」层面的**
   瘦客户端真实 import 只有 `__future__/base64/datetime/io/json/os/sys/time/urllib/uuid`。
   一旦残留 `from src import ...`，PyInstaller 的依赖分析会把 `src/` → `ortools` 一起打进 exe
   （体积暴涨 / 打包失败）。判定用 **AST**，不能用字符串搜 —— 解释用法的注释里就写着
   `` `from src import ...` ``，字符串搜必假阳性（这个坑本项目踩过四次）。
   守卫：`selftest.py` 阶段七 AST 白名单
   （打包链路退役前还有一层 `build_exe.py` 打包前自查，已随打包一起删除）。
   **再加一层运行时证据**（阶段六）：干净子进程里加载 server 模块后 `sys.modules` 不得含 `src`/`ortools`。

2. **路径必须分两类**（打包退役后仍成立）
   - `bundle_dir()`：**只读资源**（面板 HTML）。源码模式 = 本文件所在目录；
     frozen（PyInstaller onefile）= `sys._MEIPASS` —— 该分支**已无生产调用方**，
     按 `tripplanner_mcp_server.py` 里的注释**刻意保留**（三行、非 frozen 恒为 False；
     删它反而要改 `bundle_dir()` / `project_root()` 的路径推断，那才是危险的地方）。
   - `project_root()`：**可写根**，优先级 `TRIPPLANNER_HOME` > exe 同目录 > 仓库根。
     `remote.json`、`output/` 都挂它 —— 状态必须落在进程自己那层，**不能**回头写仓库。

3. **`?shape=mcp`：归一化必须在云端**
   面板要的是带 `photo` / 坐标的视图形状，而这两样分别来自 `data/photo_cache.json` 与 POI 库 ——
   瘦客户端都没有。故云端 `/api/plan` 支持 `shape=mcp`，直接回 `{"ok":true,"plan":<已归一化>}`。
   同理 **`/api/replace` 也必须归一化**（见「本轮踩的坑 ②」）。

4. **`?strict_city=1`：agent 面向 vs 对话面向**
   Web UI 是对话式的，「没提地名 → 落默认城市上海」是合理兜底；但 MCP App 是 **agent 面向**，
   静默落默认城市会掩盖「识别失败」。故云端加开关：`strict_city=1` 时若走了「默认城市」分支
   就直接 400 报错（`plan_route_decision()` 用 `defaulted` 标记该分支）。MCP server 固定带上它，
   Web UI 行为不变。

5. **mode 降级判定不能用 `== "llm"`**
   云端 LLM 成功回的是 `"m7_proposal"`，不是 `"llm"`。判定应看是否含
   `degrad` / `offline` / `fallback` 关键词（自检侧封装为 `_not_degraded()`）。

6. **`_merge_day_into_plan()`：瘦客户端自己合并单日**
   云端 `/api/replace`、`/api/move` 只回**改动的那一天**。瘦客户端没有内核的 `apply_day()`
   可复用，故按 day 号替换、按 id 回填 photo/坐标、重算总里程 —— 语义与
   `src/plan_view.apply_day`、面板 `applyDayToPlan` **三处保持一致**。

7. **地图 key 两级取值**
   ① 本地 `secrets.json: amap_js_key`（开发机）→ ② 本地没有（非开发机）就问云端
   `GET /api/cities` 回带的 `amap_key`。
   为什么不担心泄露：高德 JSAPI key **本就设计为前端可见**（它本来就直接写在页面里）。
   好处是**换 key 不用重新分发已发出的包**。
   ⚠️ 必须 `try/except` 包住 —— 这段跑在 `resources/read` 里，网络抖动**不能**让面板打不开
   （地图缺失可接受，面板打不开不可接受）。

8. **云端侧配套**（`webui/server.py`）
   - `/api/health` 的 `stage` → `"M5"`（自检有断言）；`WEBUI_VERSION` → `m5`。
   - 限流改**环境变量可配**：`TRIPAGENT_RATE_LIMIT_N` / `TRIPAGENT_RATE_LIMIT_WINDOW_S`
     （默认仍是 8 次 / 60s）。理由很现实：多租户 NAT 共用出口 IP 时会互相撞限流。
   - 无状态 + 会话表：`_SESSIONS`（OrderedDict，200 条 / TTL 2h），key = session id；
     `?shape=mcp&session=` 可回读本会话行程。

## 🟢 M5.x：云端 `/mcp`（Streamable HTTP）—— 零安装分享（2026-10-10）

**动机**：M5 之前，「分享给别人」只能靠发包（exe / zip）。而**自定义 MCP 通道本身就支持远端 URL**
⇒ 只要云端能说 MCP 协议，对方填一个 URL + Header 就能用 —— **零安装、零文件分发**（路线 A）。

**实现（不复制口径）**：`webui/mcp_endpoint.py` 用 **importlib 按文件路径**加载
`mcp/tripplanner_mcp_server.py`，直接复用它的 `TOOLS` / `_meta` / `handle()` / `read_panel()`
⇒ **同一份 server，两种 transport**（本地 stdio + 云端 Streamable HTTP）。
`webui/server.py` 挂路由：`POST /mcp`（主）、`GET /mcp`、`do_DELETE /mcp`；
`/api/health` 增 `mcp_remote` 字段，`WEBUI_VERSION` → `m6`。

**🔴 三个「错了不报错」的协议细节**（都踩过）：

| 细节 | 正确 | 错了会怎样 |
|---|---|---|
| `GET /mcp` | **405**（不是 404） | SDK 靠 405/404 判断端点是否支持 SSE 流；404 会让它当「端点不存在」 |
| notification（无 `id` 的请求） | **202 + 空 body** | 回 JSON 会让客户端把通知当响应解析 |
| 响应类型 | `application/json` 就够 | **不必**实现 SSE 流式；做了反而引入状态 |

**🔴 必须按 `Mcp-Session-Id` 隔离会话**：`handle()` 里的 `SESSION` 是**模块全局**，且
一旦 `opened_any=True` 就**永不再开面板** ⇒ 不隔离的话，**第二个用户永远看不到面板**
（第一个人的状态把他的面板入口吃掉了）。云端适配层按请求头建立独立 `_Session` 解决。

**实测**：本地 **21/21**、公网 **22/22**（含「面板入口唯一」）、云端 `plan_trip` **10.5s**
出真实行程、同会话 `get_plan` 复取命中。

**用法**（分享路线见「快速开始 → B」）：

```json
{ "mcpServers": { "tripplanner-http": {
    "url": "http://101.35.47.213/mcp",
    "headers": { "Authorization": "Basic <base64(trip:口令)>" } } } }
```

> 裸 `url` 即 **Streamable HTTP**；远端条目的**信任指纹 = URL 的 origin**
> ⇒ 换地址/换口令都要回 UI 再信任一次（见上节静默陷阱）。
> ⚠️ 明文 http + 明文口令，**只发给信任的人**（原因见 M4 节）。

---

## ⛔ 已退役：本地打包（exe / mac 源码包）—— 2026-10-10

**Kenny 拍板**：

> 「mcp 已经是远程的了，不需要再做本地文件打包，所以 windows 上的 exe，
> 以及 Mac 上的源码包，以后不需要再打了」

原因：分享路线已收敛到**远端 Streamable HTTP**（上文「M5.x」一节）——
对方只需在「自定义 MCP」里填一个 **URL + Authorization 头**、点「信任」，**零安装、零文件分发**。
比发 exe/zip 更短，也更好维护：**改服务端即全员生效**，不必重发包、不必管对方机器有没有 python3。

**随之删除的东西**（要找回看 git 历史，如 `git show <commit>:mcp/build_bundle.py`）：
`mcp/build_bundle.py`、`mcp/build_exe.py`、`mcp/smoke_exe.py`、`mcp/bundle_check.py`、
`mcp/bundle_redteam.py`；`release_gate.py --mcp` 的「铺包自检」「exe 冒烟」两步；
`register.py --exe` 打包模式；`check_live.py` 的 exe 形态判据（remote.json 同目录 /
「源码比 exe 新＝没重打包」）。

**但有三条教训与打包无关、仍然成立**：

1. **只有「坏了不出声」的地方才值得写守卫**。当时出包链路恰好全是静默坑：
   ① zip 丢可执行位（解压后 `Permission denied`，宿主只报一句 spawn 失败）
   ② `.sh` 被写成 CRLF（mac/linux 报 `\r: command not found`）
   ③ `mcp.json` 占位符少一处（收包人照抄注册，`command/cwd/env` 指着错目录）。
   今天的同类物是**面板 uri 缓存**与**信任指纹**（同样是「错一点就静默」）。
2. **守卫自己必须能被击穿**（红队）：断言写错（锚点漂移 / 条件恒真）会永远打绿，
   比没有守卫更危险 ⇒ 红队一律**复制成同目录临时变体**注入坏实现，**绝不改真文件**。
3. **PyInstaller 不能跨系统编译**，且依赖分析会顺着函数体里的 `from src import ...`
   把 `src/` → `ortools` 一起打进产物 —— 这正是「瘦客户端必须在**实际 import** 层面
   零依赖」那条硬约束的来源（判定用 **AST**，不能用字符串搜，见 M5 关键要点 1）。

### 🔴 宿主侧实况检查（`mcp/check_live.py`）

`selftest.py` **自己把 server 拉起来**，完全绕过宿主；宿主这一侧只能靠三份落地文件交叉比对，
且**必须重载**（宿主只在 bootstrap 读一次 `mcp.json`）：

```bash
PY="D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"
"$PY" mcp/register.py      # ① 写注册表（源码形态；远端 /mcp 形态在 UI 里填 URL + 头）
#  ② ★重新信任：连接器 → 自定义 MCP → 找到 tripplanner(-http) → 保存/信任
#     （表单通道立即生效、自动授权；改了 command/url/headers ⇒ 指纹变 ⇒ 必须重存一次）
"$PY" mcp/check_live.py    # ③ 判宿主是否已加载当前版本
```

`check_live.py` 的**判据**（这是本工具存在的全部理由）：

| 看什么 | 一致 | 不一致的含义 |
|---|---|---|
| `mcp.json` 的 `command` / `url` | 期望形态 | 注册了别的形态（`.exe` ⇒ **已退役**的打包形态，改回源码或远端 /mcp） |
| **`mcp-approvals.json` 信任时刻 vs `mcp.json` mtime** | 信任更新 | **改了配置没重新信任 ⇒ 宿主静默丢弃整个 server**（见下节） |
| 工具表 `plan_trip` 描述 vs 源码 | 一致 | 宿主拿着**旧版本**（改了没重载）—— 怎么调都不是新逻辑 |
| 工具数 | 9 | 少 ⇒ 模块级初始化被打断（参见 `check_orphans.py`） |

重载后**最硬的证据只能来自宿主自己的日志**（`daemon.log` 里的 `stdio-connected`）：

```bash
grep -o 'stdio-connected {"pid":[0-9]*,"id":"custom-mcp:tripplanner"' ~/.workbuddy/logs/daemon.log | tail -1
```

（远端 `/mcp` 形态是 Streamable HTTP、**没有 stdio 子进程** ⇒ 那一行不会出现；
改看 `~/.workbuddy/logs/mcp-apps-diag.log` 里 `openHostApp.created` 的 `resourceUri`。）

⚠️ **别拿 server 自己那侧的 `output/mcp_debug.log` 当「宿主拉起过」的证据** ——
自检 / 手工探针同样会往那里写。2026-10-10 我正是被这条**假绿**骗过：日志 mtime 晚于注册表，
看着像宿主已加载，实际那次是**我自己跑探针写的**。`check_live.py` 已改成查宿主日志。

### 🔴🔴 M5 实测挖出的静默陷阱：换 command ⇒ 旧「信任」失效 ⇒ 宿主丢弃整个 server

**现象**（2026-10-10，Kenny 执行方案 B 完全重启后）：代码/配置全都对、自检全绿、
启动时刻也晚于配置改动，但宿主就是**不加载 tripplanner** —— 工具表里凭空少一项，
`daemon.log` / `mcp-apps-diag.log` **一句报错都没有**。对比日志：

```
15:22:54  [MCP][resolveAgentForSession] mcpServers=[…,tripplanner,…] (count=6)   ← 重启前（python 形态）
15:34:47  [MCP][resolveAgentForSession] mcpServers=[connector-proxy,agent-mail,…] (count=5)  ← 重启后，tripplanner 消失
```

**根因**：宿主把「已信任」记在 `~/.workbuddy/mcp-approvals.json`，**键 = `<配置指纹>::<server 名>`**：

```json
{ "4ae34be8f72fe4a81519923dccaf1152000141b0c6d1b7aa900eb84d51ef7208::tripplanner": 1791517646425 }
```

指纹随 `command/args/cwd/env` 变化 ⇒ 当年 `register.py --exe` 把 command 从 python 换成 exe 的那一刻，
**旧信任对新配置无效**，宿主启动时直接把它过滤掉。（打包已退役 ⇒ **现在的等价场景是改
`url` / `headers`**：换远端地址或换口令同样会让旧信任作废。）
这也解释了为什么 `register.py`（纯文件写入）**只在配置被 UI 信任过一次之后才管用**。

**修**：改完配置必须在 UI 里重新信任一次（连接器 → 自定义 MCP → tripplanner → 保存），
表单通道会写新指纹并立即生效。

⚠️ **不要试图自己写 `mcp-approvals.json`**：指纹不是对配置 JSON 做 sha256 ——
2026-10-10 试过 **103,168 种**序列化组合（字段子集 × 键序 × 分隔符 × ensure_ascii ×
路径斜杠 × 前后缀 × `_workbuddyMcpOrigin`/`defer_loading` 附加字段），**全部不命中**，
显然掺了 uid / 项目路径这类不可复现的输入。

肉眼验收四项（沿用 M0 的 V1–V3，外加 M5 的云端项）：

- [ ] **E1** 工具表描述变成「规划在云端完成（…）本进程只做代理」⇒ 宿主吃到了 M5
- [ ] **E2** 说一句「杭州2天，喜欢自然风光」→ 面板自动打开且**渲染完整时间线**（非降级页）
- [ ] **E3** 面板里「换一个 / 拖拽 / 导出」可用、**缩略图与地图点不消失**（验云端 `/api/replace` 归一化）
- [ ] **E4** `tripplanner_ping` 回 `version=0.5.0-m5`

### 本轮踩的坑（都已加守卫）

① 🔴 **整段误删 228 行**：清理死码时把 `mcp/tripplanner_mcp_server.py` 里
`class _Session` / `SESSION` / `TOOLS` / `TOOL_BY_NAME` 一起删了 —— 而它们**仍在被使用**。
隐蔽点在于 `TOOL_BY_NAME` 只在 `tools/call` 路径被碰 ⇒ `import` 正常、「拉工具表」也正常
（`TOOLS` 只是被引用，不是被求值），直到**消费方路径真调一次**（当时的 exe 冒烟）才炸出
`NameError: name 'TOOL_BY_NAME' is not defined`。
⇒ 新增守门 **`mcp/check_orphans.py`**：AST 比对 HEAD 与工作区，
找出「已删除、任何作用域都没再赋值、但代码里仍被引用」的顶层名字。
（必须剥掉字符串常量再判 —— 注释里写「☠️ 已删除：`kernel()`…」不算引用。）
已并入 `release_gate.py` 第 5 步。

② 🔴 **`/api/replace` 丢归一化（真回归）**：换点回的 `day` 是内核形状（无 photo / 坐标），
瘦客户端没有 `data/` 补不了 ⇒ 面板「换一个」之后**缩略图与地图点静默消失**。
修法：云端 `/api/replace` 补 `plan_view.normalize_day(...)`（与 `/api/move` 对称），自检加守卫。

③ 🟡 **`strict_city` 语义回归**：M5 把城市解析挪到云端后，「识别不出城市应报错」被
「未提地名 → 落默认城市上海」吃掉（探针实测 `两天随便玩玩` → 上海、`mcp` 侧 mode 变
`m7_degraded`）。修法即上文第 4 条。

④ 🟡 **自检撞云端限流（8/分钟）**：消费方路径模拟（当时叫「分享包模拟」）报「请求过于频繁」。
修法：自检新增 `_http_retry()` / `call_plan()`（**只对限流**重试、等一个窗口，其它错误原样返回），
并把限流阈值改成环境变量可配。

⑤ 🟡 **自检自己的「假提示」**：`check(name, cond, detail)` 的 `detail` 是**无条件打印**的，
于是「move 在候选顺序上成功」通过时也会打出「试了 4 种顺序都被拒」这种失败文案。
⇒ 三处改为分支文案（`move_poi` / `/api/move` / `amapKey`）。
**教训：守卫的报错文案也是守卫的一部分** —— 文案误导等于把红看成绿。

⑥ 🔴 **`mode` 徽标口径三处漂移（本机 exe 实测才暴露的用户可见缺陷）**：
线上**主路径**是 M7 经验提案，成功时 `mode == "m7_proposal"` —— 它**不以 `llm` 开头**。
而「展示 mode」这件事在三个地方各写了一遍：
`webui/index.html`（对，`startsWith("m7")` → 「M7 经验提案」）、
**`src/md_export.py`**（`startswith("llm")` ⇒ 错标「离线兜底」）、
**`mcp/panel/plan_panel.html`**（`/^llm/` ⇒ 面板徽标错显「离线兜底规划」）。
⇒ **每一条 M7 行程，面板与导出都显示错标签**（`m2_toptw` 同样中招）。
修法：导出内核提炼 `md_export.mode_label()` 作**唯一口径**（`m7`→M7 经验提案、
`m2`→M2 库内直选、`llm`→LLM 规划，含 `degrad/fallback/offline` 才追加「（降级）」），
面板同口径改写，`PANEL_VERSION` +1。
**教训：同一个口径被复制成多份时，必须验「行为」而不是 grep 源码** ——
`panel_guard.py` 新增的守卫是把面板那段 JS **用 node 真跑一遍**、喂 6 个真假 mode 断言输出，
红队自测确认它对旧逻辑会红。

### 面板高度：让宿主把 iframe 撑到内容高度（M6，2026-10-10）

**症状**：行程一长（多天时间线动辄上千 px），面板**内部**出现上下滚动条，看行程要来回滚。

**根因**：宿主把 App 放进一个**定高** iframe。MCP Apps 协议为此留了
`ui/notifications/size-changed`（App 上报内容高度 ⇒ 宿主把 iframe 撑到该高度），但面板此前
**从没上报过**，于是只能吃宿主的默认高度。

**修法**（`plan_panel.html` 的 `// ---- panel-size:block ----`）：面板自己测量并上报。

```js
window.parent.postMessage({ jsonrpc: "2.0",
  method: "ui/notifications/size-changed", params: { height: h } }, "*");
```

三条**必须**遵守（错任何一条都只是「又出现滚动条」，**不报错**）：

| 要点 | 为什么 |
|---|---|
| 必须**持续**上报 | 缩略图 / 地图脚本 / 字体都在 render 之后才落位；只报一次会停在半成品高度 |
| 必须能**变矮** | 换天 / 换点后内容变短，收不回去就留一大片空白 ⇒ `contentHeight()` 在「内容比视口矮」时必须走 **body 分支**，不能用根元素 `scrollHeight`（它至少等于视口高度） |
| `method` 名一字不差 | 宿主 bridge 只认 `ui/notifications/size-changed`，写错**静默忽略** |

**与宿主内建测量的关系**（`app.asar` 里 sandbox-proxy 源码取证）：宿主代理自己也会测一次，
但**一旦 App 主动上报**，代理就置 `appOwnsSize = true` 并 `stopMeasuring()` **永久让位** ——
所以接管之后我们就是唯一来源，必须自己保证准确。

**怎么验**：面板「协议诊断 → 面板尺寸」那行会显示 `视口 / 内容 ｜ 第 N 次上报（原因）`。
若宿主确实采纳，**视口应≈内容**（不再小于内容）；否则视口会明显小于内容。

**顺带修的两处**：

- 面板 `panel.boot` 轨迹里的版本号原**写死** `v=m12`，`PANEL_VERSION` 涨到 m13 后它就悄悄过期了
  ⇒ 改为 `__PANEL_VERSION__` 由 server 注入（版本号唯一来源仍是 `PANEL_VERSION`），
  `selftest` 做**交叉校验**：注入进去的版本必须与 `resourceUri` 里的版本一致。
- `selftest.py` 有 2 处用 `print(OK + …)` 直接打 PASS、**不计入总数** ⇒ 「N/N」长期少报 2，
  已改走 `check()`。

⚠️ 改动 `plan_panel.html` ⇒ `PANEL_VERSION` **必须 +1**（宿主按 uri 缓存注入后的成品 HTML）。

### 版本

`SERVER_VERSION` → **0.5.0-m5**、`PANEL_VERSION` → **m19**、`WEBUI_VERSION` → **m7**、
`/api/health.stage` → **M5**。守门：`mcp/panel_guard.py`（全绿，含 mode 徽标、**高度上报**、
**hop 里程不重复**、**地图逐段真实路网**与**换点保留当日主题**的行为验证）、`mcp/selftest.py`（216 项）、
`scripts/release_gate.py`（本地五步门禁，CI 也跑这条；`--mcp` 追加「面板守卫 + MCP 自检」）、
`mcp/check_orphans.py`、`mcp/check_live.py`（宿主侧实况）、
`scripts/test_panel_single_open.py`（**面板入口唯一 / 只开一次**，红队 `mcp/redteam_panel_single.py`）、
`scripts/test_swap_keeps_theme.py`（**换点不丢当日主题**，红队 `mcp/redteam_theme_keep.py`）。
<!-- 2026-10-10：`smoke_exe.py`（17 项，与 `build_bundle.py` 共用 `bundle_check.py`
     的产物断言）与 `mcp/bundle_redteam.py` 已随打包链路一并退役。 -->

> ⚠️ `PANEL_VERSION` 这次**面板 HTML 没变**却仍 +1：因为「哪些工具带面板」变了 ⇒ 宿主缓存的
> 工具目录（appDefinitionSignature）要作废。换 uri 是最直接的「让宿主重拉」信号。

### ✅ 宿主侧实测验收入口（2026-10-10 已跑通）

`register.py --exe` + UI 保存（写信任）后，`mcp/check_live.py` **五项全绿**：
注册形态=exe、信任状态、工具表 9 项且 `plan_trip` 描述与源码一致、宿主
`stdio-connected` 晚于配置改动、**面板版本 = 源码 `PANEL_VERSION`**。
真实调用链路（**服务方是 exe 而非源码**）：`dist/output/mcp_debug.log` 被创建而仓库
`output/mcp_debug.log` mtime **纹丝不动**。9 个工具在 exe 形态下逐个真跑：
规划（杭州2天/南京2天，`m7_proposal`）／回读／城市（31 城 1193 POI）／换点
（茅家埠→满觉陇，同类目）／挪时段／导出 —— 全部正常。

> ⛔ **exe 形态已退役**（2026-10-10，见「⛔ 已退役」一节）；本机现行形态是**远端 `/mcp`**
> （`~/.workbuddy/mcp.json` 里只有 `tripplanner-http`）。上面这段保留为历史记录 ——
> 它证明的是**「瘦客户端 + 远端后端」这条链路本身成立**，退役的只是「怎么把本地进程搬到别人机器上」。

#### 🔴 判「宿主用的是哪一版面板」的**唯一**可靠判据

改面板后**别**再用下面这两个（都踩过）：

| 判据 | 为什么不能用 |
|---|---|
| server 侧 `output/mcp_debug.log` 里的 `resources/read` | `log()` 只写 **stderr**（宿主吞掉），**从不落盘** ⇒ 「没有该记录＝命中缓存」是**假证据** |
| 同一份文件里的任何记录当「宿主拉起过」的证据 | 自检 / 手工探针**也写那里** |

**真判据**：宿主自己的日志 `~/.workbuddy/logs/mcp-apps-diag.log`：

```
openHostApp.created … resourceUri=ui://tripplanner/plan-m13
```

`check_live.py` 的 `[5]` 就是读它并与源码 `PANEL_VERSION` 比对。
实测轨迹：`15:58:03` 还是 `plan-m12` → `16:04` UI 保存后 **`16:05:35` 已请求 `plan-m13`**
⇒ **UI 保存/重新连接会重新描述工具，uri 随之更新（不必重启）**；只有「改了 `mcp.json`
但没让宿主重载」才会沿用旧 uri。

### 地图：逐段真实路网（M6.2，2026-10-10，`PANEL_VERSION` m16→m17）

**背景**：面板地图**从未**移植 webapp 的能力 —— `drawMap()` 只把当天 POI 按顺序连成**一条**
`AMap.Polyline`（直线折线），而 `webui/index.html::drawRealRoutes()` 是**逐段**调后端
`/api/route` 画**真实路网**。这**不是** MCP 化的回归（`git log -S "drawRealRoutes"|"api/route"`
限定面板文件 = **全历史零命中**），而是「同一功能两份渲染代码、面板那份没跟上」——面板受
宿主 iframe / CSP / 尺寸协议约束，搬不动 webapp 的 DOM 结构。

**本次对齐**（口径与 webapp 一致）：

| 能力 | 实现 |
|---|---|
| 逐段真实路网 | `drawLeg()` 每段 `GET /api/route?mode=&o=&d=`（**服务端** amap key ⇒ 面板无需自带 key） |
| 出行方式 | `routeMode(q)`：`骑行\|骑车\|单车\|自行车\|cycling\|bike` → `riding`，否则 `walking` |
| 酒店锚点腿 | 仅当该天时间轴有 `type=hotel` 行 ⇒ 用 `plan.hotel` 坐标补「酒店→…→酒店」 |
| 城际 driving 腿 | `type=transfer` 的天画「上一天末点 → 本天首点」，`driving` 紫色实线。生成点是 `webui/server.py::plan_multi`（多城联游，≥240min 城际段预扣 1 天）→ `_transfer_day()`（:923）。⚠️ 触发条件与 webapp **逐字一致**：该天自身要有 POI —— 短城际段会把 transfer 行**并进某天**（那时生效），**纯转移日**（只有 transfer 行、无 POI）两边都不画 |
| 按天配色 / 显隐 | `DAY_COLORS`（webapp 同 5 色）+ 色块开关 `toggleDayLine()`（折线与标点一并显隐） |
| 失败口径 | 城内腿失败 ⇒ **不画假直线**（与 `47c757a` 定稿口径一致）；城际腿失败 ⇒ 退直线 |
| 竞态保护 | `MAP_TOKEN` 令牌：快速连续重绘时放弃过期渲染 |
| 标点（marker） | `drawMarkers()` 用 **`content`＝纯 HTML 水滴 pin**（`pinContent`，旋转 45° 圆角+角，按天配色+序号）+ hover `label` 显示「Day d·n｜景点名」；**不用高德默认图标**（见下） |

#### M6.2b 标点必须用 `content`（不能用高德默认图标）—— `PANEL_VERSION` m17

**报障**：「真实路网显示出来了，但 marker 没显示」。

**根因**：面板旧代码 `new AMap.Marker({position, map, label:{...}})` —— **不传 `icon`/`content`**
⇒ 高德渲染**默认图标**，而默认图标是一张**外部图片资源**（`webapi.amap.com/theme/...`），在宿主
iframe 的 CSP 下**加载不出来** ⇒ 只剩 HTML `label`（数字）浮在空中、**marker 本体不可见**。
webapp 不受影响，因为 `webui/index.html:412` 一直用 `content`（旋转 45° 的水滴，纯 CSS，零外部依赖）。

**修法**：`drawMarkers()` 改为 `content: pinContent(color, seq)` + `anchor:"bottom-center"`
（与 webapp 逐字同款）；酒店锚点用 `🏨` emoji content，**不入 `DAY_MARKERS`** ⇒ 不随 Day 开关隐藏
（webapp 同口径）。hover 用 `setLabel({offset: new AMap.Pixel(0,-4)})`。

**守门**：`panel_guard.py` 3.9 追加 —— 字面量锚点 `rotate(-45deg)` + **node 真跑**断言
`mkContent=true / mkNoLabelOpt=true（构造时不得传 label）/ mkAnchor=true / mkPin=4 / mkHotel=1 /
mkCounts=[2,2]（酒店不入按天表）`。红队（`dist/_redteam_marker.py`，手动跑）注入旧「默认图标 +
label」实现 ⇒ `pin=0 / anchor=False` 变红。
⚠️ 红队暴露的教训：探针里 `o.content.indexOf(...)` 在 `content` 为 undefined 时会**抛异常致探针崩溃**
（守卫虽红但报「无 JSON 输出」，指向不明）⇒ 探针要用 `String(o.content || '')`，让失败**落在具体断言上**。

**为什么能直接复用后端**：`/api/route`（`webui/server.py:1602`）一直健在（云端实测
`ok=true / 304 点 / 6267m`），且**限流 `rate_allow()` 只在 `/api/plan` 分支被调用** ⇒
逐段拉路网不会被 8次/60s 挡住。

**守门**：`panel_guard.py` 3.9 —— 锚点断言 + **node 真跑**（stub `httpJson`/`AMap`，喂 2 天数据，
断言 `nCalls=4 / nPolys=4 / 按天归组 / 城际腿 driving`）。红队（`dist/_redteam_map.py`，手动跑）
注入旧「单条直线」实现 ⇒ 行为断言变红（`nCalls=0`）。
⚠️ 红队暴露的教训：**字面量断言没红、只有行为验证红了**（`/api/route` 在注释里也出现）
⇒ 「grep 源码不算」再次被证实。

**未实测**：面板在宿主双层 iframe 内的实际渲染（含逐段路网请求的 CORS / 并发）。

### 🔴 M6.3：重拍时面板叠了好几扇 —— 面板入口收敛到 `open_plan`（2026-10-10，`PANEL_VERSION` m17→m18）

**报障（Kenny 原话）**：「当 mcp 被多次调用重拍的时候，面板可能显示多次。这样体验不好。
应该等拍完后，有最终结果的时候，显示一次。」

**取证（宿主 `app.asar`，等价源码）**：

```js
// /main/…  McpAppsGateway
async handleAgentToolCall(e){
  let t = H(e.sessionId, e.toolCallId);      // H(a,b) = `${a}::${b}`
  let n = this.toolCallIdToInstanceId.get(t);
  if (n){ … D('handleAgentToolCall.idempotent' …); return {handled:true, …} }   // 同一个调用 ⇒ 复用
  let r = this.deps.mcpManager.findAppByToolName(e.prefixedToolName);
  if (!r) return {handled:false};            // 不在 app 目录（= 工具没有 _meta.ui.resourceUri）
  … await this.openHostApp({ …, toolCallId: e.toolCallId, … })                 // 新调用 ⇒ 新开一扇
}
```

| 判据 | 事实 |
|---|---|
| **去重键** | `${sessionId}::${toolCallId}` ⇒ **每一次工具调用 = 一扇新面板** |
| **算不算 App** | 只看**工具定义**的 `_meta.ui.resourceUri`（`findAppByToolName` 查 app 目录） |
| **结果里有 `panelOpened=false` 管用吗** | ❌ **完全不读**。宿主不解析工具结果的任何字段来决定渲染 |
| **服务端能拦吗** | ❌ 不能。没有「关闭别人的 app 实例」「复用已有实例」的服务端通道 |

**观测证据**：同一会话（`076cbe41…`）连调 3 次 `tripplanner_plan_trip`，`mcp-apps-diag.log` 出现
**3 条 `openHostApp.created`**，`toolCallId` / `appInstanceId` 各不相同：

```
openHostApp.start   … toolCallId=call_00_fMIymaaslnDS8LUgRTuq5657
openHostApp.created … appInstanceId=0bee7d78-… htmlLen=67494
openHostApp.start   … toolCallId=call_00_6qX74DfX7gSABlUwfiK17438
openHostApp.created … appInstanceId=9df728ec-… htmlLen=68951
```

**修法（唯一有效）**：把「会开面板的工具」收成**一个**，并要求只调一次。

| 工具 | 面板 | 说明 |
|---|---|---|
| `tripplanner_plan_trip` | — | 摘掉 `_meta.ui`。**可以反复调用**（重拍、调需求都不堆面板）；文本里明说「本工具不打开面板」并点名 `open_plan` |
| `tripplanner_open_plan` | ★ | **唯一入口**。描述里写明「每次调用宿主都会新开一扇 ⇒ 同一对话只调一次」；重复调用回**劝阻文案**（讲清机制，不是含糊的「已经打开了」） |

**顺带清理**：`plan_trip` 不再写 `SESSION.opened_any` / `instances`（那是面板入口的状态，
不该被数据工具污染），失败分支同样只回文字、不开面板。

**守门**：
- 🆕 `scripts/test_panel_single_open.py`（**行为级**，monkeypatch `run_plan` 后真调 `handle()`）：
  A 面板入口恰好 1 个且 = `open_plan`；B `plan_trip` 连调 3 次 → `panelOpened=false` 且
  `opened_any` 未被写脏 + 文本引导；B2 失败分支同样不开面板；C 首次 `open_plan` 开、再次劝阻。
- `mcp/selftest.py`：「plan_trip 带面板」改为「**全 9 工具里带面板的恰好 1 个**」+
  「plan_trip 不带 `_meta`」+ 描述文案断言。
- 🔴 红队 `mcp/redteam_panel_single.py`（手动跑）：**不改真文件** —— 复制成同目录临时变体
  （`TRIPPLANNER_SERVER=<变体>` 指过去），注入两种回退 ⇒ **2/2 被捕获**
  （M1 给 `plan_trip` 加回 `_meta`；M2 `plan_trip` 把 `opened_any` 写回 True）。

#### 用法变更（⚠️ 会改变调用习惯，务必同步）

```
旧：plan_trip（开面板）→ 重拍再调 plan_trip ⇒ 叠 N 扇面板
新：plan_trip（可调 N 次，不开面板）→ 定稿后 open_plan 调 1 次 ⇒ 只有 1 扇
```

若模型仍多调了一次 `open_plan`，服务端只能**劝阻**（文案），拦不住宿主。
**为什么不做「面板自我折叠」**：panel 侧看不到兄弟实例；老面板要「知道自己过时」只能轮询，
而面板 → 本地 server 的 `tools/call` 通道在本宿主是死的（见 M2.1），云端又不知道本地那份
面板注册状态（split-brain）⇒ 不做这层，宁可把机制做在「少开一扇」这一侧。

### 🔴 M6.4：换点把「当日主题」清空了（2026-10-10，`PANEL_VERSION` m18→m19）

**报障（Kenny 原话）**：「换点会导致当日主题丢失」。

**根因：数据在**写回**时被清空（不是渲染问题）** —— 三段拼起来是个静默黑洞：

| # | 环节 | 事实 |
|---|---|---|
| ① | 内核 `sequencer.build_itinerary` | 产出的 day **没有** `theme` / `reason` —— 主题是 compose 阶段（`m2_planner.compose`）按**最终时间轴**由 LLM 生成的 |
| ② | 云端 `plan_view.normalize_day` | 为「形状稳定」把缺失字段一律补成**空串** ⇒ `/api/replace` 回的 day 里是 `theme: ""` |
| ③ | 写回端 | 做的是**整日替换**（面板 `applyDayToPlan` / 会话 `apply_day` / 瘦客户端 `_merge_day_into_plan`）⇒ 空串成了「**明确的空**」，当天标题退化成「未命名主题」 |

Web UI 那条更隐蔽：它用的是 `Object.assign(d, r.day)`（本该是合并），但 ② 让 `theme` **恒存在**，
`Object.assign` 一样照盖 —— **归一化把「缺失」变成了「有值且为空」，合并语义随之失效**。

**修法**：换点 / 挪时段**只改时间轴** —— 新结果没给的 `theme` 沿用旧值；明确给了就以新值为准。
**只保 `theme`，不保 `reason` / tips**：那两者逐点点名（「沿湖依次游览苏堤春晓…」），
换掉其中一个点后就是过期信息，宁可没有（webui 早已这么处理）。

四处写回点必须同一套语义（多副本实现 = 本项目既有教训）：

| 处 | 文件 |
|---|---|
| 内核（云端会话写回） | `src/plan_view.py::apply_day`（新增公共函数 `keep_day_meta`） |
| 瘦客户端 | `mcp/tripplanner_mcp_server.py::_merge_day_into_plan` |
| 面板 | `mcp/panel/plan_panel.html::applyDayToPlan` |
| Web UI | `webui/index.html::swapPoi` |

**守门**：
- 🆕 `scripts/test_swap_keeps_theme.py`：走**真实内核**（真换点 → 归一化 → 写回），含反向守卫
  「新主题优先」+「挪时段同理」+ 前提守卫「内核确实不产 theme」（免得哪天内核开始产 theme，
  守卫还在自说自话）。
- 🆕 `mcp/panel_guard.py` **3.10**：面板 `applyDayToPlan` 与 webui `swapPoi` **node 真跑** ——
  `theme1 === '姑苏园林漫步'` 这类断言**grep 字符串验不出来**。
- 🆕 红队 `mcp/redteam_theme_keep.py`（手动跑）：**不改真文件** —— 面板 / webui 各复制临时变体，
  用 `PANEL_GUARD_PANEL=` / `PANEL_GUARD_WEBUI=` 指过去，注入三种坏法（去掉保留 / 改成无条件
  覆盖 / webui 去掉回填）⇒ **3/3 被捕获**。这也是 `panel_guard.py` 首次支持「被测文件可替换」，
  以后前端红队都走这个口子。
- 云端端到端：`dist/_verify_theme_remote.py`（真跑 规划 → 换点 → 回读会话，实测
  「茅家埠 → 太子湾公园，主题『西湖西线茶禅自然之旅』保留」）。
  ⚠️ **不部署就不生效**，而本地单测全绿看不出来 ⇒ `WEBUI_VERSION` m6→m7 正是为此 +1。

**已知取舍**：主题是**日内文案**、由 compose 按最终时间轴生成，故换点后主题会「略微不贴新点」。
要真正贴合需在换点后重跑一次 compose（一次 LLM 调用 + 缓存版本），留作以后；旧主题 ≫ 无主题。

### ⚠️ 仍未做

- **远端 `/mcp` 尚未在别人的 WorkBuddy 里实测**（本机已 21/21、公网 22/22、端到端规划实测通过；
  ⛔ exe 打包形态已于 2026-10-10 退役 ⇒ 「发一包给对方」这条路不再需要验）。
- **云端 `/mcp`（Streamable HTTP）宿主侧待确认**：本机 `mcp.json` 现为 `tripplanner-http`
  （`http://101.35.47.213/mcp` + Basic 头）⇒ 确认「宿主能列出 9 工具」+「`ui://` 面板
  在远端 transport 下能渲染」。⚠️ 改过 `url`/`headers` ⇒ 必须在「自定义 MCP」**再保存一次**
  （指纹变了，旧信任作废）。
- **面板高度上报（M6）待肉眼确认**：宿主日志只能证「请求了 `plan-m17`」，采纳与否要看
  「协议诊断 → 面板尺寸」那行的**视口 vs 内容**。若视口仍明显小于内容 ⇒ 宿主没采纳，
  得改用别的办法（例如把地图卡折叠）。
- ✅ **面板地图（M6.2）路网已肉眼确认**（2026-10-10，Kenny 实测）：宿主 iframe 内出现
  **按天配色的实际路网**（蓝=Day1 / 绿=Day2），`/api/route` 的跨域 GET 在宿主内通畅。
- **面板标点（M6.2b，m17）待肉眼确认**：m17 修复了「marker 本体不可见」，需再保存一次
  「自定义 MCP」换 uri 后确认**水滴 pin 标点出现**（不再是数字浮空）。
- 高德 JS API 在宿主**双层 iframe** 内的行为仍未实测（沿用 M3 的遗留项）。
- ✅ 云端 `src/md_export.py` **已同步**（2026-10-10 跑过 `deploy/cvm/sync_from_local.sh`，
  导出模式行已由「离线兜底（m7_proposal）」变为「**M7 经验提案**」）。

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

### 🔴🔴 但「重载」只解决一半：**改配置还会让「信任」失效**

上表里那个 `11:47:26` 生效时刻，与 `~/.workbuddy/mcp-approvals.json` 里的时间戳
（`1791517646425` = `2026-10-09 11:47:26`）**完全对上** —— 说明 UI 的「刷新/保存」
这一步做的是两件事：**`mcpRefresh` 重读磁盘** + **`mcpApprove` 写入信任**。

`mcp-approvals.json` 的键 = **`<配置指纹>::<server 名>`**：

```json
{ "4ae34be8f72fe4a81519923dccaf1152000141b0c6d1b7aa900eb84d51ef7208::tripplanner": 1791517646425 }
```

⇒ **只要 `command` / `args` / `cwd` / `env` 任一变，指纹就变，旧信任对新配置无效**，
宿主启动时把它**静默过滤掉**：工具表里凭空少一项、日志里一句报错都没有。

这解释了两件事：

1. **为什么纯文件写入（`register.py`）只在「该配置曾被 UI 信任过」之后才管用**；
2. **为什么 M5 换 exe 后重启也无效**：不是没重载，是**新配置没被信任**。

```bash
# 判据：信任时刻 ≥ mcp.json mtime 才算「改完重新信任过」
"$PY" mcp/check_live.py     # [2] 信任状态 一节会自动判并给出修法
```

⚠️ **别自己写 `mcp-approvals.json`**：指纹不是对配置 JSON 做 sha256。
2026-10-10 试了 **103,168 种**序列化组合全部不命中（字段子集 × 键序 × 分隔符 ×
`ensure_ascii` × 路径斜杠 × 前后缀 × 附加 `_workbuddyMcpOrigin`/`defer_loading`）
⇒ 显然掺了 uid / 项目路径等不可复现输入。**改配置后一律回 UI 重新保存一次。**

---

## 为什么用 Python 而不是重写

TripPlanner 的核心资产是 `src/`（TOPTW 求解 + 31 城 1193 POI + 905KB 交通矩阵 + 114KB 照片缓存）。
MCP server 只是一层 stdio JSON-RPC 壳，**M5 起连 import 都不做了**（内核留云端），
重写成 Node/TS 等于把求解器重做一遍。

## 目录

```
mcp/
  tripplanner_mcp_server.py   # MCP server（M5：**纯标准库**瘦客户端，只做代理+注入+落盘）
  check_orphans.py            # M5：AST 比对 HEAD，抓「删了但还在用」的顶层名字（已并入 release_gate 第 5 步）
  check_live.py               # **宿主侧**实况检查（注册形态 / **信任指纹是否过期** / 宿主日志里有没有 stdio-connected / 宿主已加载的工具表是否落后于源码）
  asar_scan.py                # 取证工具：按**原始字节**搜宿主 app.asar 并打印上下文（本项目对宿主行为的结论一律要源码级证据）
  panel/plan_panel.html       # 面板（自包含；线上 base / Basic 凭据 / 地图 key / session 用占位符，由 server 注入）
  selftest.py                 # 不开 WorkBuddy 也能验协议 / 规划 / 交互 / 线上后端 / 消费方路径（216 项）
  panel_guard.py              # 前端行为守卫（U+FFFD / 标签配对 / node --check / 占位符安全 / **node 真跑**：mode 徽标 · 高度上报 · hop 不重复 · 地图路网 · 换点保留主题）
  redteam_theme_keep.py       # 红队：证明「换点保留主题」两层断言能被打红（手册跑；`PANEL_GUARD_PANEL/WEBUI` 指向临时变体，**绝不改真文件**）
  redteam_panel_single.py     # 红队：证明「面板入口唯一」能被打红（手册跑；`TRIPPLANNER_SERVER` 指向临时变体）
  register.py                 # 注册进用户级 MCP 配置（双写 mcp.json + .mcp.json，合并不覆盖）
scripts/test_panel_single_open.py   # 行为级：面板入口恰好 1 个（open_plan）+ plan_trip 连调不开面板
scripts/test_swap_keeps_theme.py    # 行为级：换点 / 挪时段不丢当日主题（真实内核，含反向守卫）
src/poi_swap.py               # 换点 / 挪时段公共内核（Web UI 与云端共用）
src/plan_view.py              # 行程视图公共内核（normalize_day / apply_day；Web UI 与云端共用）
src/md_export.py              # 导出公共内核（plan → Markdown；Web UI 与云端共用）
webui/mcp_endpoint.py         # 云端 `/mcp`（Streamable HTTP）：importlib 复用同一份 server，不复制口径
output/mcp_last_plan.json     # 最近一次行程（自动生成，不入库）
output/mcp_debug.log          # 落盘诊断（宿主拿不到 stderr，只能落盘；不入库）
dist/                         # 本地探针与临时产物（含**明文口令**的 remote.json）—— 已入 .gitignore，绝不入库
```

> 🗑️ **2026-10-10 删除**：`mcp/build_bundle.py` / `build_exe.py` / `smoke_exe.py` /
> `bundle_check.py` / `bundle_redteam.py` —— **本地打包（exe / mac 源码包）已退役**
> （分享走远端 `/mcp` + URL/Header，零安装）。见「⛔ 已退役」一节。

> 🗑️ **M5 删除**：`mcp/local_api.py`（本地常驻 HTTP API）。它会让 PyInstaller 顺着
> `import` 把 `src/` + `ortools` 拖进产物，是打包地雷；且面板后端全走线上后已无调用方。
> `output/mcp_local_api.json` / `output/mcp_local_api_token.txt` 也不再生成。

## 快速开始

### A. 开发机（源码模式，改代码即生效）

```bash
PY="D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe"

"$PY" mcp/selftest.py          # 1. 自检（216 项：协议 / 真实规划 / 交互 / 线上后端 / 消费方路径）
"$PY" mcp/panel_guard.py       # 2. 面板前端守卫（U+FFFD / 标签配对 / node --check / 占位符安全）
"$PY" mcp/check_orphans.py     # 3. 「删了还在用」静态守门
"$PY" mcp/register.py          # 4. 注册进 WorkBuddy 配置（源码模式）
# 5. ⚠️ 重载（见上文顶部）：在「自定义 MCP」表单里加，或重启 WorkBuddy
# 6. WorkBuddy → 连接器 → 右上角「自定义 MCP」→ 找到 tripplanner → 点「信任」
# 7. 对话里说：「打开行程面板」
```

回滚：`"$PY" mcp/register.py --remove`

一条命令跑全套（五步 + MCP 阶段）：

```bash
"$PY" scripts/release_gate.py --mcp
```

### B. 分享给别人（**零安装：贴远端 URL**）

⛔ 旧的「打包 exe / mac 源码包 → 发 zip」已退役（2026-10-10）。现行做法是**什么都不用发**：

1. 对方打开 **连接器 → 自定义 MCP → ＋添加**，类型选 **Streamable HTTP**（或直接填 URL），填：
   - **URL**：`http://101.35.47.213/mcp`
   - **Header**：`Authorization: Basic <base64(trip:口令)>`（口令见本机 `secrets.json: remote_basic_auth`）
2. 保存 → 在列表里点 **「信任」**（信任按**配置指纹**记，改 URL/Header 后必须重存一次）。
3. 对话里说「打开行程面板」，或在模型调 `tripplanner_open_plan` 后看面板是否渲染。

⚠️ 这是**明文 http + 明文口令**（境内节点拦未备案域名，见 M4 节；宿主面板是 http 上下文，
无混合内容问题）⇒ **只发给信任的人**。给外部人用之前建议把 nginx 换成**独立 htpasswd**
（别共用你自己这把），并在 AI 生成内容上留个免责说明。

> 为什么能省掉「包」：MCP server 是一份**纯标准库**代码，云端 `/mcp` 端点用 importlib
> **复用同一份** `mcp/tripplanner_mcp_server.py`（不复制口径）⇒ 客户端侧零安装、零 Python 依赖。
> 详见「M5.x 云端 `/mcp`」一节。

### 改完面板/ server 后怎么让它生效

| 改了什么 | 怎么生效 |
|---|---|
| `tripplanner_mcp_server.py` | 面板里重开一次工具调用（宿主会重启 stdio 子进程），或重启 WorkBuddy |
| `panel/plan_panel.html` | ⚠️ **宿主按 uri 缓存 HTML**（`resolver.resolve.cacheHit`）→ 把 `PANEL_VERSION` +1，再重载 MCP |
| `webui/server.py`（线上后端）或 `src/plan_view.py` / `src/md_export.py` | ⚠️ **必须部署到线上** —— 面板与 MCP 都走**线上那一份**代码；只改本地对线上无感（跑的还是旧行为）。改 `WEBUI_VERSION` 必须同步部署（`selftest` 比对云端 `/api/health.version`） |
| `webui/mcp_endpoint.py`（云端 `/mcp` 适配层） | ⚠️ 同上 —— 它是被**线上**进程 importlib 加载的，必须部署；只改本地对云端 `/mcp` 无感 |
| 远端条目的地址 / 口令变了 | ⚠️ 改 `url` / `headers` ⇒ **信任指纹变** ⇒ 回 UI 再保存/信任一次，否则宿主**静默丢弃**整个 server（见上节静默陷阱） |

### 在 WorkBuddy 里验收（M1）

1. **确认宿主真的重载了**（不重载一定看不到新工具）：

   ```
   连接器 → 自定义 MCP → 刷新
   ```

   验：工具列表里应有 **9 个** `tripplanner_*`（M1 六个 + M2 三个）。
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
   | 城市识别 | 「两天随便玩玩」应**明确报错并列出可用城市**，而不是瞎猜一个 |
   | 报错 | 「火星玩两天」→ `未知城市：火星。可用：…` |

### 在 WorkBuddy 里验收（M2 / M2.1 / M2.2 / M2.3）

前置：`~/.workbuddy/mcp-tool-list.json` 里 `tripplanner_*` 应为 **9 个**
（6 = M1 旧快照，需先按上文「重载」一节刷新）。

先看**面板顶部**：工具栏右侧有**通道徽标**，正常是 `本地服务就绪`（绿）。
再看**面板底部「协议诊断」**：

| 诊断行 | 期望 |
|---|---|
| **本地 API** | `✅ 本地 API 可用　http://127.0.0.1:51900（pid=… / v0.3.1-m3）` |
| ping 回显 | `失败：超时（10s）… —— 这正是 M2.1 改走本地 HTTP 的原因`（**这条不通是预期内的**） |

然后逐条对照：

| 动作 | 期望 |
|---|---|
| 说「杭州2天，喜欢自然风光」 | 面板打开，行程上方出现**地图**（真实底图 + 蓝色路线 + 序号标记） |
| 看顶部横幅 | **不出现**红色横幅（出现即代表有失败，横幅会写明原因） |
| 点某个 POI 行尾 `🔁` | 该点被同类目就近点替换；**当天景点数不变**，其余顺序不变；工具栏出现 `🔁 A → B（同类目）` |
| 拖行首 `⠿` 到同一天另一位置 | 顺序更新；顺序受约束微调时会明确说明 |
| 点顶部 `⬇ 导出 Markdown` | 走系统下载目录；宿主不支持则提示「已复制到剪贴板」 |
| 地图加载失败 | 给出原因，**行程与交互仍可用**（不会白屏） |
| 拖到另一天 / 拖出列表 | 不生效（当前只支持**同一天内**重排） |
| 徽标变成 `本地服务已停止` | 顶部横幅会显示「⏳ 正在等待它恢复…（已等 Ns）」。**M2.3 起面板会自己等并续做**：对助手说一句话（例如「刷新行程」）即可唤醒，服务一回来面板会自动继续，**不用重新点** |
| 等了 60s 仍不通 | 横幅改为「长时间未恢复」并给出恢复方法；点「测试本地 API / 重试」可再等 20s。同时把 `output/mcp_debug.log` 末尾给我 —— 现在每条动作都有 `panel.trace`，且常驻进程有 `local_api.heartbeat / .exit / .crash` **死因取证**，能判到「崩了 / 被优雅杀 / 被硬杀」 |
| 想直接看死因 | `output/mcp_local_api.err.log`（stderr 与崩溃现场）+ `output/mcp_debug.log` 里的 `local_api.*` |

#### M3 专项对照（远端兜底，需**先部署线上**）

| 看什么 | 期望 |
|---|---|
| 面板标题 / 页脚标签 | 显示 `M3`（不是 `M2.3` ⇒ 面板 HTML 已被宿主重读） |
| 通道徽标 | 正常 `本地服务就绪`（绿）；本地被回收后**自动切到 `线上后端`**（不再是「已停止」） |
| 线上兜底时点 `🔁` / 拖拽 | **照常生效**（请求走 `tripagent-planner2-80644.app.workbuddy.host`；本地回来后再切回本地） |
| 线上兜底时点 `⬇ 导出 Markdown` | 照常导出（plan 由面板带上，需**已部署**新版 `webui/server.py`） |
| 线上兜底时「拉行程」 | 仅本地有意义 ⇒ 仍要求本地可用（线上无会话状态，给不出「当前行程」） |
| 面板底部「协议诊断」 | 能看到本地 / 线上两个后端各自的探活结果 |

⚠️ **地图仍是唯一未经实测的环节**（CSP 已按取证配好，但高德 JS API 在宿主双层
iframe 里能否初始化，只有浏览器说了算）。失败时看「协议诊断 → 地图状态」那一行，
它会区分「key 未配置」「JS 加载失败」「渲染异常」。

### 表单添加时填这些

**形态 ①：远端（推荐，零安装）** —— 类型 `Streamable HTTP`（或直接填 URL）

| 字段 | 值 |
|---|---|
| 名称 | `tripplanner-http` |
| URL | `http://101.35.47.213/mcp` |
| Header | `Authorization: Basic <base64(trip:口令)>` |

**形态 ②：本机源码（改代码即生效）** —— 类型 `stdio`

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
| **面板可反向调工具（协议层）** | AppBridge 走 `ui/initialize` → `tools/call`，token 宿主自动带 —— ⚠️ **但本宿主上这条请求到不了 server**（见「M2.1」一节）⇒ 面板交互改走**云端后端**（M5 起；更早是本地 `local_api.py`，已删除） |
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
5. 🔴 **每次调「带面板的工具」= 新开一扇，不复用**（去重键 `${sessionId}::${toolCallId}`）
   → **服务端返回什么字段都拦不住**（早期「A+C 分流」正是错在这，见 M6.3）。
   正确做法：**把会开面板的工具收成一个（`tripplanner_open_plan`）并要求只调一次**。
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
| **M2** | 交互：面板内换点 / 挪时段 / 导出（AppBridge 回工具）+ 地图（路线 polyline） | ✅ **代码完成（2026-10-09，自检 108/108）** |
| **M2.1** | 🔴 实测发现宿主 `tools/call` 通道**不通** ⇒ 面板交互改走 `127.0.0.1` 本地 HTTP（`mcp/local_api.py` 常驻 + token 校验） | ✅ 完成（2026-10-09，自检 139/139）—— ⛔ **M5 起该方案下线**（本地服务整体移除，`local_api.py` 已删除） |
| **M2.2** | 二次报障复修：GET 带 body 的同步异常 + token 跨重启漂移 + 常驻进程被杀；补齐「失败必须看得见」 | ✅ **完成（2026-10-09，自检 143/143）** |
| **M2.3** | 常驻进程**死因取证**（心跳 / atexit / 信号 / 崩溃 + stderr 落盘）+ 把  挂到请求总入口 + 面板**掉线自愈**（等恢复再发请求 / 看门狗 / 重试按钮） | ✅ **完成（2026-10-09，自检 150/150，门禁四步全绿）；待 WorkBuddy 实测** |
| **M3** | 面板交互**远端化**：线上后端补齐 `/api/health`、`/api/move`、`/api/export`；面板改「本地优先 → 线上兜底」；`normalize_day` / `apply_day` / `plan_to_markdown` 抽成 `src/` 公共内核（Web UI 与 MCP **共用一份**） | ✅ **已上线（2026-10-09，自检 176/176，门禁四步全绿，`tripagent-planner2-80644…` /api/health=m3）；待 WorkBuddy 实测** |
| **M4** | 🔴 修 M3 真缺陷（`webui/server.py` **零 CORS** ⇒ 跨域兜底其实没生效）+ 把自建云主机接进 `REMOTE_API_DOMAINS[0]`（`__REMOTE_API_AUTH__` 注入 Basic 头）+ `deploy/cvm` 支持 HTTPS/Basic 认证（`limit_except OPTIONS` 豁免预检） | 🟡 **代码完成并已部署**（自检：无凭据 401 / 带凭据 200 / OPTIONS 204，panel_guard 全绿）；**HTTPS 未通** —— 腾讯云境内节点**拦截未备案域名**，`*.sslip.io` 亦不可备案（见上 M4 节）。后经实测判定**域名/HTTPS 均非必需**（面板是 http 上下文，无混合内容问题）⇒ 改用 `http://101.35.47.213` 直连 |
| **M5** | **瘦客户端**（+ ~~单文件 exe~~）：删掉本地服务（`local_api.py` 整块下线），server 退化为纯标准库代理（规划/换点/挪时段/导出全走云端）；云端 `shape=mcp` 归一化 + `strict_city=1` + `/api/replace` 补归一化；新增 `check_orphans.py` 守门 | ✅ **完成（2026-10-10）** —— 自检 **206/206**、面板守卫全绿、`release_gate.py` 五步全绿。⛔ 其中「`build_exe.py` 打 8 MB 零依赖 exe + `smoke_exe.py` 冒烟 17/17」已于同日**退役**（见「⛔ 已退役」一节） |
| **M5.1** | ~~**跨平台出包**：`build_bundle.py --target win\|mac\|linux` + 共用断言 `bundle_check.py` + 铺包自检 `--self-check` + 红队 `bundle_redteam.py`~~ | ⛔ **已退役（2026-10-10，同日）** —— MCP 已在远端，分享走 URL/Header 零安装，不再出本地产物。文件与门禁两步均已删除，历史见 git |
| **M6** | **面板高度上报**：面板主动发 `ui/notifications/size-changed` 让宿主把 iframe 撑到内容高度（内容长不再出滚动条）；`__PANEL_VERSION__` 占位符注入（修「面板里写死 v=m12 后静默过期」） | ✅ **完成（2026-10-10，`PANEL_VERSION` m14）**；待肉眼确认视口≈内容 |
| **M5.x** | **云端 `/mcp`（Streamable HTTP）**：把 `/api/*` 薄包一层 MCP 协议 ⇒ 「零安装 + 不上架」分享路线 A 的原型。**复用同一份 `mcp/tripplanner_mcp_server.py`**（importlib 按路径加载，不复制口径）；`webui/mcp_endpoint.py` + `webui/server.py` 挂路由 | ✅ **端点已上线并实测**（本地/公网各 **21/21**，云端 `plan_trip` 10.5s 出真实行程）；**待宿主侧实测**（UI 点信任 → 9 工具 + 面板渲染） |
| **M6.1** | 🐞 **修 hop 段里程/时长重复显示**（报障：「→ 车程 15 分钟 · 3.7km　3.7km · 15分钟」）：hop 的 `name` 已由 `sequencer._hop_label()` 自带 km+分钟，而**面板与导出两处**又各拼了一遍 | ✅ **完成（2026-10-10，`PANEL_VERSION` m15）** —— 面板新增**行为守卫**（node 真跑渲染）+ 新单测 `test_hop_label_no_dup.py`；红队证明守卫可被打红 |
| **M6.2** | 🗺️ **地图对齐 webapp**（报障：「地图上景点之间是直线，webapp 是实际导航线路」）：面板 `drawMap()` **从未**移植 webapp 的 `drawRealRoutes()` ⇒ 改**逐段调 `/api/route`** 画真实路网 + 按天配色/显隐（`toggleDayLine`）+ 酒店锚点腿 + 城际 driving 腿（多城联游短城际段生效，生成点 `webui/server.py::plan_multi`）；`PANEL_VERSION` m15→**m16** | ✅ **完成（2026-10-10）** —— `panel_guard` 3.9 **node 真跑**行为验证（`nCalls=4 / nPolys=4 / 按天归组 / 城际 driving`），红队注入旧「单条直线」实现即变红；✅ 宿主 iframe 内**已肉眼确认**（按天配色实际路网） |
| **M6.2b** | 📍 **标点走纯 HTML pin**（报障：「真实路网出来了，但 marker 没显示」）：面板 `AMap.Marker` 不传 `icon/content` ⇒ 用**高德默认图标**（外部图片资源）⇒ 宿主 iframe 内加载不出来、只剩数字 label 浮空。改为 `content: pinContent()`（旋转 45° 水滴，纯 CSS，按天配色+序号）+ `anchor:"bottom-center"`，与 `webui/index.html:412` 逐字同款；酒店锚点 `🏨` 不入 `DAY_MARKERS`（不随 Day 隐藏）；`PANEL_VERSION` m16→**m17** | ✅ **完成（2026-10-10）** —— `panel_guard` 3.9 追加 node 真跑断言（`mkContent / mkNoLabelOpt / mkAnchor / mkPin=4 / mkHotel=1 / mkCounts=[2,2]`），红队注入旧默认图标实现即变红（`pin=0`）；⚠️ 宿主 iframe 内待再确认 |
| **M6.4** | 🔴 **换点不丢「当日主题」**（报障：「换点会导致当日主题丢失」）：根因＝内核 `build_itinerary` 产不出 `theme` → 云端归一化补**空串** → 写回端**整日替换** ⇒ 空串成了「明确的空」。四处写回点（`plan_view.apply_day`（新 `keep_day_meta`）/ `_merge_day_into_plan` / 面板 `applyDayToPlan` / webui `swapPoi`）统一「缺失才沿用旧值」；`PANEL_VERSION` m18→**m19**、`WEBUI_VERSION` m6→**m7** | ✅ **完成（2026-10-10）** —— 🆕 `scripts/test_swap_keeps_theme.py`（真实内核 + 反向守卫）、`mcp/panel_guard.py` 3.10（面板/webui **node 真跑**）、红队 `mcp/redteam_theme_keep.py` **3/3 捕获**；门禁全绿、云端已部署（公网端到端实测「茅家埠→太子湾公园，主题保留」） |
| **M6.3** | 🪟 **面板入口收敛（重拍不再堆面板）**（报障：「mcp 被多次调用重拍时面板显示多次，应该等拍完有最终结果时显示一次」）：从 `app.asar` 取证出宿主去重键＝`${sessionId}::${toolCallId}` ⇒ **每次工具调用各开一扇**且**不读结果字段**（旧 A+C 防堆叠从未生效）⇒ `plan_trip` **摘掉** `_meta.ui`（可反复调）、`open_plan` 成**唯一入口**并要求只调一次、重复调用回劝阻文案；`PANEL_VERSION` m17→**m18** | ✅ **完成（2026-10-10）** —— 🆕 `scripts/test_panel_single_open.py`（**行为级**）+ 红队 `mcp/redteam_panel_single.py` **2/2 捕获**（不改真文件）；门禁全绿、云端已部署（公网 22/22 含「面板入口唯一」）；⏳ 待 Kenny 在 UI 再保存一次后肉眼确认「重拍只出一扇」 |

### 🐞 缺陷记录：hop 段里程/时长重复（2026-10-10 报障，`PANEL_VERSION` m15 修）

**症状**：面板时间轴上出现「→ 车程 15 分钟 · 3.7km　3.7km · 15分钟」（前后各一遍）。

**根因**：hop 的 `name` 已经是 `src/sequencer.py::_hop_label()` 产出的
「车程 15 分钟 · 3.7km」——**自带**出行方式 + 分钟 + 公里；而**两个消费方又各拼了一遍**
结构化 `s.km` / `s.min`：
- `mcp/panel/plan_panel.html`（面板，那段 `.hopx`）
- `src/md_export.py`（导出 Markdown，`前往 {name}（{km} · {min}）`）

**修法**：两处都删掉冗余拼接，**只信 `name`**（`km`/`min` 字段保留供将来用，但不再渲染）。

**守卫（防回归；两条都必须能被打红）**：
- `mcp/panel_guard.py` **3.8**：node **真跑**渲染，喂一天含 hop 的数据，断言 `3.7km` 只出现 **1** 次。
- `scripts/test_hop_label_no_dup.py`：钉两条不变式 —— ① `_hop_label()` 输出**必含** km 与分钟
  （否则「渲染层不再自己拼」会直接丢信息）；② 导出的 hop 行里里程数值只出现一次。

**⚠️ 写守卫本身踩的坑（属「锚点比被测对象更危险」）**：抽 JS 块的锚点第一次写太松 ——
① 开头只写 `dl.forEach(function(d){`，而面板里有 **4 处**同名（675/741/859/870）⇒ 命中错块、探针跑不出结果；
② 结尾写 **6 空格**的 `\n      });`，命中的是**内层** `(d.timeline||[]).forEach(...)` 的闭合 ⇒
抽出的块少一层闭合、node 报 `Unexpected end of input`。
最终锚点 = `dl.forEach(function(d){\n      var poiSeq = 0;` … `\n    });`（**4 空格**，= `dl.forEach` 自己的闭合）。

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

- **开发机（源码模式）**解释器用 tripagent venv：
  `D:/Users/xgwu/.workbuddy/binaries/python/envs/tripagent/Scripts/python.exe`
  （跑 `selftest.py` / `panel_guard.py` 等守门脚本要用它；系统 Python 会假报失败）
- **M5 起，MCP server 自身只需要标准库** —— 不再需要 `ortools`（内核在云端）。
  （⛔ 曾经「打包 exe 也用它里面的 PyInstaller」⇒ 打包退役后，venv 只为跑守门脚本而留。）
- 启动参数加 `-u`（无缓冲），否则 stdio 响应会被 Python 的块缓冲憋住
- 配置双写：`~/.workbuddy/mcp.json`（宿主实际读取）+ `~/.workbuddy/.mcp.json`（兼容兜底）
- **stdout 被 JSON-RPC 独占** —— 任何 `print` 都会让宿主断连，日志一律走 stderr
- 宿主读的是 `~/.workbuddy/mcp.json`（**不是** `.mcp.json`），且 **只在启动时读一次** ⇒ 改完必须重载
- 生效判据：`~/.workbuddy/mcp-tool-list.json` 里 `tripplanner_*` 的**个数 = 9**
- 验真重启：`Get-Process WorkBuddy.exe | Select StartTime`（uptime 大于改文件时间＝**没**重读）
