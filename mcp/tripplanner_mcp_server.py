#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TripPlanner MCP App —— M5：**瘦客户端**（后端全走线上，可打包分发）

历史：M0（协议 spike）→ M1（真实行程闭环）→ M2（面板换点/挪时段/导出）→
M2.1（本地 HTTP 兜底）→ M3（交互远端化）→ M4（自建云主机作远端后端）。

**M5 的定位**：本进程退化为「**薄代理**」—— 只做三件事：
  ① MCP 协议 + 面板资源（resources/read，含占位符注入）；
  ② 把规划 / 换点 / 挪时段 / 导出**转发**给云端后端（`remote_request`）；
  ③ 行程落盘（跨进程兜底）+ 会话 id 管理。
规划内核（ortools / POI 库 / 交通缓存 / DeepSeek Key）**全部留在云端** ⇒ 本文件
顶层不 import 它们，HTTP 客户端用标准库 `urllib` ⇒ PyInstaller 打包成单文件 exe、
发给别人「零依赖」即可跑（对方机器上连 `data/` 都没有）。

工具分层（关键：**只有带 _meta.ui.resourceUri 的才会开面板**）：
  tripplanner_open_plan   ★带面板：**面板的唯一入口**（展示当前最新行程；**每个需求**定稿后调一次）
  tripplanner_plan_trip     无面板：做规划（可反复调用，重拍不堆面板；M6.3 起摘掉面板）
  tripplanner_get_plan      无面板：面板握手后主动来拉行程（M1 数据通道）
  tripplanner_list_cities   无面板：列出可规划城市
  tripplanner_panel_ready   无面板：面板自报 appInstanceId（A 方案）
  tripplanner_ping          无面板：连通性对照
  tripplanner_replace_poi   无面板：M2 换点
  tripplanner_move_poi      无面板：M2 挪时段
  tripplanner_export        无面板：M2 导出 Markdown

🔴 M6.3（2026-10-10）：**为什么面板入口只能有一个**。宿主 `McpAppsGateway.handleAgentToolCall()`
的去重键是 `"${sessionId}::${toolCallId}"`（见 mcp/README.md 的取证）—— 即**每一次工具调用
都会新开一扇面板**，宿主只看「工具定义里有没有 `_meta.ui.resourceUri`」，**完全不读**工具
结果里的任何字段。所以早期那套 A+C「返回 panelOpened=false 防堆叠」在宿主层从未生效：
重拍 N 次就叠 N 扇。

🔴 M6.6（2026-10-10，**修正 M6.3 的过度收敛**）：节律的粒度是「**需求**」，不是「会话」。

  · **同一需求内**：思考 / 重拍期间只调 `plan_trip`（它不带面板，调多少次都不堆面板），
    **定稿后**才调 `open_plan` **一次** ⇒ 一扇面板。
  · **新需求**（用户在同一会话里又提了一件新事）：新行程定稿后**再调一次** `open_plan`
    ⇒ 为新方案弹一扇新面板。**这是预期行为，不是缺陷。**

  ⇒ 服务端**根本无法**阻止宿主开面板（它只看工具定义）；唯一能控制的只有「模型**调不调**
    `open_plan`」，而那是**事前**决策 ⇒ 节律只能写进**工具描述**（见 OPEN_TOOL / PLAN_TOOL
    的 description），服务端只回最新行程 + 诊断计数 —— **不做任何判重或劝阻**。

  ⚠️ M6.3 初版把节律实现成「服务端会话级封锁」（`_Session.opened_any` 一开就不再开），
    后果是**同一会话里第二个需求永远出不来面板**（2026-10-10 报障）。那种"封锁"拦不住
    宿主，只会拦住模型自己 —— 等于把功能砍掉。现已整体删除（`should_open_panel` 已不存在）。

⚠️ M2.1（2026-10-09，**M5 起已不适用**）：面板 → 宿主 → server 的 `tools/call` 通道
在本宿主上是**死的**（宿主有 `rpc.callTool`/`proxyAppToolCall.ok`，但请求从未到达任何
server 进程、12ms 假成功、structuredContent 被剥）。M2.1 曾用 `mcp/local_api.py`（本地
常驻 HTTP，端口 51900 起）兜底；**M5 起面板后端全走线上**，`local_api.py` **已整体删除**
（它 `subprocess` 拉起自身 + import 内核 ⇒ PyInstaller 会把 `src/`+`ortools` 打进 exe，
是打包地雷），本文件里的 local-api helper 也一并删除 —— 主链路与打包链均零调用。

设计要点（照 app.asar 源码挖出的硬约束）：
  - 工具是否「带面板」**完全取决于** tool._meta.ui.resourceUri；不用手写 isApp
  - **面板开几次由宿主说了算**：去重键 `${sessionId}::${toolCallId}` ⇒ 每次工具调用各一扇，
    服务端返回什么字段都拦不住（见 M6.3 / M6.6 说明）⇒ 节律只能靠 `open_plan` 的
    **工具描述**约定（「每个需求定稿后调一次」），服务端不做拦截
  - resources/read 返回的 HTML 有 256KiB 预取阈值 → 面板必须自包含、不引兄弟资源
  - 宿主用 document.write 注入（非 srcdoc）→ 相对路径会 404，故面板不做任何外部引用
  - 图片走高德直链 store.is.autonavi.com → 必须写进 csp.resourceDomains 才不被静默拦掉
  - **面板要 fetch 的线上 base，其 host 必须写进 csp.connectDomains** —— 漏了不是报错，
    是**静默拦**（面板只表现为「后端不可达」）
  - stdout 只能输出 JSON-RPC；任何 print 都会让宿主断连 → 规划期整体改道 stderr

配置（base + Basic 凭据，按优先级）：
  ① 环境变量 `TRIPPLANNER_REMOTE_BASE` / `TRIPPLANNER_REMOTE_AUTH`
  ② 与 exe 同目录的 `remote.json`（**随分享包分发**）{"base": "...", "auth": "user:pass"}
  ③ `secrets.json: remote_basic_auth`（开发机兜底）+ `REMOTE_API_DOMAINS[0]`

用法：由 WorkBuddy 以 stdio 方式拉起，不需要手工运行。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time

SERVER_NAME = "tripplanner"
SERVER_VERSION = "0.6.0-m5"
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    """是不是打包后的可执行文件（PyInstaller 会设 `sys.frozen`）。

    ⚠️ 2026-10-10：**本地打包已退役**（MCP 走远端 Streamable HTTP，零安装分享），
    所以本函数与下面的 `_MEIPASS` 分支在生产路径上不会再走到。**刻意保留**：
    它只有三行、非 frozen 时恒为 False，删掉反而要在 `bundle_dir()` /
    `project_root()` 里改路径推断逻辑（那才是真正危险的地方）。
    """
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """**只读**资源目录（面板 HTML 等随包内容）。

    源码模式 = 本文件所在目录；打包后 = PyInstaller 解包目录 `_MEIPASS`。
    ⚠️ 刻意**不用 `__file__` 推断**：onefile 下 `__file__` 的位置取决于打包布局
    （主脚本被塞进归档根），是打包事故的常见来源，改用官方的 `_MEIPASS`。
    """
    if is_frozen():
        return (getattr(sys, "_MEIPASS", "") or
                os.path.dirname(os.path.abspath(sys.executable)))
    return HERE


PANEL_PATH = os.path.join(bundle_dir(), "panel", "plan_panel.html")

# ⚠️ uri 里带版本号是**刻意的**：宿主按 uri 缓存面板 HTML（resolver.resolve.cacheHit），
# 改了 panel/plan_panel.html 却沿用旧 uri，用户看到的仍是旧面板。
# 之后每次改面板 → 把 PANEL_VERSION +1，即可保证宿主重新拉取。
PANEL_VERSION = "m23"
VIEWER_URI = f"ui://tripplanner/plan-{PANEL_VERSION}"
UI_MIME = "text/html;profile=mcp-app"

# 宿主 CSP 构造（2026-10-09 从 app.asar `buildCspMeta()` 取证，非推测）：
#   "script-src 'unsafe-inline' 'unsafe-eval' blob: " + resourceDomains
#   "style-src  'unsafe-inline' "                    + resourceDomains
#   "img-src    data: blob: "                        + resourceDomains
#   "font-src   data: blob: "                        + resourceDomains
#   "connect-src "                                   + connectDomains
# ⇒ **resourceDomains 同时管 script/img/style/font/media**，
#   所以把高德域名放进去 ⇒ 面板内可加载高德 JS API（M2 交互地图靠这条）。
#   connectDomains 单独管 fetch/XHR/WebSocket ⇒ JS API 的服务端请求要在这里放行。
AMAP_DOMAINS = [
    "https://store.is.autonavi.com",      # 图片直链（POI 缩略图）
    "https://store.is.autonavi.com:*",
    "https://*.amap.com",                 # webapi/restapi：JS API 载体 + 服务端接口
    "https://*.autonavi.com",             # 地图瓦片 webrd0*.is.autonavi.com
]

# M3：面板交互的**远端兜底**后端（线上已部署的 Web UI）。
# 面板侧「本地优先、线上兜底」—— 本地常驻进程被宿主回收时，交互自动切到线上，
# 用户不再看到「点了没反应」。域名只需进 `connectDomains`（走 fetch/XHR）；
# 面板并不 iframe 线上页面，故无需 frameDomains。
REMOTE_API_DOMAINS = [
    # 2026-10-10（M4）：**自建云主机**（腾讯云 Lighthouse）作长期主站 + M3 稳定远端后端。
    #   ⚠️ 当前是**零成本实测**阶段：直接用 **http + 公网 IP** 当远端 base，
    #      目的是绕开 sslip 域名的「未备案拦截」（见下面第 2 条）。云主机侧配
    #      nginx HTTP Basic 认证（凭据经 `__REMOTE_API_AUTH__` 注入面板）。
    #   🔴 唯一未决风险＝**混合内容（Mixed Content）**：
    #      浏览器把 `http://127.0.0.1` / `localhost` 视为 *potentially trustworthy*
    #      （安全上下文）⇒ 在 https 页面里 fetch 它**不算**混合内容（故本地后端一直能通）；
    #      而 `http://<公网IP>` **不享受这个豁免** ⇒ 若面板 iframe 是 https 上下文，
    #      浏览器会把它当 **active mixed content 直接 block**（CSP 放行也拦不住）。
    #      ⇒ 本条需宿主实测：通了＝零成本；报 Mixed Content＝实锤必须上 HTTPS。
    "http://101.35.47.213",                                  # 🖥️ 自建云主机（IP 直连；实测中）
    "https://101-35-47-213.sslip.io",                        # ⛔ 同一台云主机，但域名被腾讯云「未备案」拦截（保留作对照）
    "https://tripagent-planner2-80644.app.workbuddy.host",   # CloudStudio 线上 Web UI（m3；第二远端兜底）
    # ⛔ 2026-10-10（Kenny 确认）：旧域名 `tripagent-planner2.app.workbuddy.host`
    #    （早期遗留应用）**已下线且不再维护** ⇒ 条目已删（它此前仅作 connectDomains 放行）。
]

UI_RESOURCE_META = {
    "ui": {
        "csp": {
            "frameDomains": ["http://127.0.0.1:*"],
            "connectDomains": ["http://127.0.0.1:*"] + AMAP_DOMAINS + REMOTE_API_DOMAINS,
            "resourceDomains": ["http://127.0.0.1:*"] + AMAP_DOMAINS,
        },
        "permissions": {},
        "prefersBorder": False,
    }
}

OPEN_TOOL = "tripplanner_open_plan"
PING_TOOL = "tripplanner_ping"
PANEL_READY_TOOL = "tripplanner_panel_ready"
LIST_CITIES_TOOL = "tripplanner_list_cities"
PLAN_TOOL = "tripplanner_plan_trip"
GET_PLAN_TOOL = "tripplanner_get_plan"
# M2：面板内的交互（换点 / 挪时段 / 导出）
REPLACE_TOOL = "tripplanner_replace_poi"
MOVE_TOOL = "tripplanner_move_poi"
# 🔴 2026-10-13（宿主多轮审阅的第 2 条）：**减点原语**。
#   背景：move_poi / replace_poi 都**要求点集不变**（守门「不改变当天景点数」）⇒
#   当 agent 想把一个"装不下的点"放进来时，它唯一的出路是重拍整份行程（重算）。
#   现有工具集缺一个「主动减点」的动作：去掉某天一个不重要的点，腾出时间预算，
#   让被剔除的点能进来（实测：时长是唯一有效的腾挪维度，见 2026-10-13 调研）。
REMOVE_TOOL = "tripplanner_remove_poi"
EXPORT_TOOL = "tripplanner_export"
# Phase 0（2026-10-11）：POI 发现 —— 见 TOOLS 里 FIND_POIS_TOOL 的 description
FIND_POIS_TOOL = "tripplanner_find_pois"

# ---------------------------------------------------------------------------
# 「面板的调用节律」—— 服务端拦不住宿主，节律只能靠工具描述约定（M6.3 / M6.6）
#
# 🔴 宿主侧的硬事实（从 app.asar 源码取证，见 mcp/README.md）：
#     `McpAppsGateway.handleAgentToolCall()` 的去重键是 **`${sessionId}::${toolCallId}`**，
#     即「**每一次**工具调用 = 一扇新面板」。它对**工具定义**里的 `_meta.ui.resourceUri`
#     判定「这算不算 App」，**完全不读** 工具结果里的任何字段。
#     ⇒ 服务端**没有**任何办法阻止宿主再开一扇（历史 A+C「防堆叠」正是错在这）。
#
#     能控制的只有「模型**调不调** `open_plan`」这一件事，而这是**事前**决策 ⇒
#     节律写进工具描述（见 OPEN_TOOL / PLAN_TOOL 的 description），这里只是配套文案：
#       · 同一需求内：思考 / 重拍期间不调，**定稿后调一次**  ⇒ 一扇
#       · 新需求完成新规划后：**再调一次**  ⇒ 新面板（预期行为）
#     ⚠️ 别再加「同一会话只开一次」的服务端封锁：它拦不住宿主，只会让**第二个需求
#        永远没有面板**（2026-10-10 报障，已回退）。
# ---------------------------------------------------------------------------
PANEL_OPEN_HINT = (
    "（行程已生成，面板尚未打开。请**确认这一版就是最终方案后**，在给出答复前调用 "
    "`tripplanner_open_plan` **一次**：它是面板的唯一入口，同一需求内反复调整 / 重拍期间"
    "不要调（每次调用宿主都会新开一扇面板）。若用户随后在同一会话里提出**新需求**，"
    "新行程定稿后**再调一次**，即为新方案弹出新面板。）"
)

# 面板上「天数」的合理区间；query_days 里有同名常量，这里取值保持一致
MIN_DAYS, MAX_DAYS = 1, 7

# ---------------------------------------------------------------------------
# `initialize.instructions`（MCP 标准字段）—— 注入宿主系统提示的「用法总纲」。
# 2026-10-11 回退（Kenny）：Phase 2.5 曾用三层引导（instructions + description 硬化
# + prompts 模板）把宿主 agent 推向「agent 提案」路径，**实测效果不佳** ⇒ 撤掉引导，
# **默认走服务端自己提案**：agent 只负责理解需求 + 调 plan_trip。此处改为中性总纲。
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = (
    "Trip Planner 行程规划服务使用要点：\n"
    "1. 直接调用 tripplanner_plan_trip 生成行程：把用户的完整需求原话传给 query 即可"
    "（城市、天数、日期、住宿、偏好都能自动识别）；"
    "**服务端会自己完成点位提案与排程** —— 你不需要自己挑点位，也不需要构造 proposal。\n"
    "2. 行程**定稿后**调一次 tripplanner_open_plan 出面板（唯一面板入口，每个需求一次）；"
    "调整、重拍期间不要调它。\n"
    "3. 重拍 / 探索阶段可传 copy_mode=\"lazy\" 让行程更快出来（跳过逐日文案）；"
    "用户满意、定稿那次用默认（full），open_plan 会自动为最终行程补全文案。"
)


# ---------------------------------------------------------------------------
# 项目内核：懒加载（initialize 阶段不拖慢；首次规划才 import ortools 等）
# ---------------------------------------------------------------------------
def project_root() -> str:
    """**可写**根：`remote.json`、`output/`（行程落盘 + 诊断日志）都在这下面。

    优先级：`TRIPPLANNER_HOME`（宿主 / 自检会设）> 打包后 exe 所在目录 > 源码仓库根。
    ⚠️ 打包后**不能**用 `dirname(HERE)` —— 那是临时解包目录的父目录，既非预期也可能只读。
    """
    env_home = os.environ.get("TRIPPLANNER_HOME") or ""
    if env_home and os.path.isdir(env_home):
        return env_home
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# ☠️ M5 已删除：`kernel()` / `_plan_view()` / `photo_of()` / `poi_coords()`
#
# **为什么必须删、而不是留着当兼容壳**：PyInstaller 的依赖分析**会**跟进函数体里的
# `from src import ...`。留着这几个壳，`src/` → `ortools` 就会被一起打进 exe
# （体积从 ~10MB 涨到几百 MB，且可能直接打包失败）。而它们**一个调用点都没有**：
#   - 规划/换点/挪时段/导出 → 云端（`remote_request`）
#   - 归一化（补照片/坐标） → 云端 `src/plan_view.py`
# 删掉它们还有第二个好处：**「不依赖 src/」变成可静态证明的**（见自检里的
# 「瘦客户端源码里没有任何 `from src` / `import ortools`」那条守卫）。
# ---------------------------------------------------------------------------


def amap_js_key() -> str:
    """面板地图用的**高德 Web 端 JSAPI** key。

    刻意不放进 `panel/*.html`（那会随 git 入库）；改由 `read_panel()` 在
    resources/read 时替换占位符 —— key 只在传输时出现，仓库里始终是占位符。

    两级取值（M5 新增第二级）：
      ① 本地 `secrets.json: amap_js_key` —— 开发机 / 自建部署。
      ② 本地没有（**非开发机**上不会有 secrets.json）⇒ 问云端 `GET /api/cities`
         （它本就回带 `amap_key`）。
    ⚠️ 为什么经后端下发没问题：高德 JSAPI key 是**设计为前端可见**的公开 key
       （它本来就直接写在页面里），所以这不是新增的泄露面；反而带来一个好处 ——
       **换 key 不必再动客户端**（面板与 MCP server 都不用改）。
    ⚠️ 必须 try/except 包住：这一句跑在 `resources/read` 里，网络抖动**不能**
       让面板打不开（地图缺失是可接受的降级，面板打不开不是）。
    """
    global _AMAP_KEY_CACHE
    if _AMAP_KEY_CACHE is not None:
        return _AMAP_KEY_CACHE
    with _CACHE_LOCK:                      # 并发首访只放一个进来（见 _CACHE_LOCK 注释）
        if _AMAP_KEY_CACHE is not None:
            return _AMAP_KEY_CACHE
        key = ""
        try:
            with open(os.path.join(project_root(), "secrets.json"), encoding="utf-8") as f:
                key = str((json.load(f) or {}).get("amap_js_key") or "").strip()
        except Exception as e:
            log("本地 secrets.json 未取到 amap_js_key（改用云端）", err=str(e))
        if not key:
            try:
                res = remote_request("/api/cities", timeout=6.0)
                key = str((res or {}).get("amap_key") or "").strip()
                dbg("amap_js_key.from_remote", ok=bool(key))
            except Exception as e:  # noqa: BLE001 —— 面板开不了比地图缺失严重得多
                dbg("amap_js_key.remote.fail", err=str(e))
                # ⚠️ 远端失败 ⇒ **不缓存空值**，下次请求重试（否则网络抖动会被钉成永久空键）
                return ""
        if not key:
            log("⚠️ 未取得 amap_js_key（面板地图将不可用，其余功能不受影响）")
            return ""                       # 本地+远端都明确没有 → 也不缓存（给未来配置机会）
        _AMAP_KEY_CACHE = key
        return key


_REMOTE_AUTH_CACHE = None


def remote_basic_auth() -> str:
    """远端后端（云主机）的 HTTP Basic 认证头，形如 `Basic <base64(user:pass)>`。

    取值来源：`secrets.json` 的 `remote_basic_auth`
      - "user:pass"      → 现场 base64 成 `Basic ...`
      - "Basic xxxx"     → 已是成品头，原样返回
      - 缺失/为空        → 返回 ""（面板退化为「不带凭据」，线上后端若开了
                           Basic 认证则会 401 —— 面板会如实报「后端不可达」，
                           绝不静默。这正是我们想要的：配错要看得见）。
    与 amap key 一样**只经占位符注入**，不落仓库。
    """
    global _REMOTE_AUTH_CACHE
    if _REMOTE_AUTH_CACHE is not None:
        return _REMOTE_AUTH_CACHE
    with _CACHE_LOCK:                      # 并发首访只放一个（见 _CACHE_LOCK 注释）
        if _REMOTE_AUTH_CACHE is not None:
            return _REMOTE_AUTH_CACHE
        val = ""
        try:
            with open(os.path.join(project_root(), "secrets.json"), encoding="utf-8") as f:
                val = str((json.load(f) or {}).get("remote_basic_auth") or "").strip()
        except Exception as e:
            log("remote_basic_auth 读取失败（线上后端将不带凭据）", err=str(e))
        if not val:
            _REMOTE_AUTH_CACHE = ""
        elif val[:6].lower() == "basic ":
            _REMOTE_AUTH_CACHE = val
        elif ":" in val:
            import base64 as _b64
            _REMOTE_AUTH_CACHE = "Basic " + _b64.b64encode(val.encode("utf-8")).decode("ascii")
        else:
            # 既不是成品头、又没有冒号 —— 无法组成 Basic，视为未配置（报错可见）
            log("remote_basic_auth 格式无效（应为 user:pass）", chars=len(val))
            _REMOTE_AUTH_CACHE = ""
        return _REMOTE_AUTH_CACHE


# ---------------------------------------------------------------------------
# M5：远端后端（瘦客户端）—— 云端地址 / 凭据 / 会话 id
#
# 为什么要有这一层：面板从 M5 起**后端全走线上**，规划、换点、导出都在云端，
# 本进程退化成「薄代理」（只做 MCP 协议 + 面板资源 + 落盘行程）。
# 于是：
#   - 不再需要 ortools / POI 库 / 交通缓存 / DeepSeek Key ⇒ 分享给别人时
#     对方零 pip 依赖、不必带 2.6M 数据、不用自己的 API Key。
#   - 云端地址与 Basic 凭据由 **remote.json** 提供（打包后与 exe 同目录，
#     随分享包一起发），开发机回落 secrets.json。
#   - 云端是**无状态**的（入参自包含），所以「我这份行程现在是什么」需要一把
#     钥匙 —— 用落盘的 session_id，plan / move / replace / 拉行程 全程带着它。
# ---------------------------------------------------------------------------
REMOTE_JSON_NAME = "remote.json"          # 分享包自带：{"base": "...", "auth": "user:pass"}
_REMOTE_CFG_CACHE = None
_CFG_LOCK = threading.Lock()   # remote_config 专用（内部调 remote_basic_auth，不可与 _CACHE_LOCK 共用）


def remote_config_path() -> str:
    """remote.json 路径 —— 与 `project_root()` 同一套规则（打包后 = exe 同目录）。

    刻意复用 `project_root()` 而不是重复写一遍 frozen 判断：两处漂移过一次就会
    出现「配置读得到、状态写别处」这种极难查的错位。
    """
    return os.path.join(project_root(), REMOTE_JSON_NAME)


def session_id() -> str:
    """当前会话的 id —— 「这份行程属于谁」的唯一真相。

    🔴 2026-10-12 P0 安全修复：**此前从全局单文件 `mcp_session_id.txt` 读**，
    云端多用户共用同一个文件 ⇒ 一把钥匙开所有人的柜子（Kenny 实测跨用户串行程：
    A 规划贵州 → B 规划杭州 → A 面板显示杭州）。现改为**从 `SESSION.sid` 取**：
    云端 `/mcp` 按 `Mcp-Session-Id` 隔离会话，每个 `_Session` 自带 sid ⇒ 天然隔离。

    ⚠️ **不做模块级缓存**（`_SESSION_ID_CACHE` 已废弃）：云端是单进程多会话，
    `SESSION` 会被 `mcp_endpoint._handle_one` 逐请求换掉（`m.SESSION = sess`）⇒
    任何模块级缓存都会「把第一个会话的 sid 粘给所有会话」，正是本次漏洞的同类错误。
    每次调用都从 `SESSION` 现取，开销可忽略（仅属性读 + 字符串）。

    落盘（`mcp_session_id.txt`）仅在 **stdio 单用户模式**下作为兜底保留 ——
    该模式宿主按需 spawn 进程、内存不跨进程，故首次仍生成并落盘。
    远端 http 模式（当前主线）下云端是单进程长驻，`SESSION.sid` 恒有值，不再落盘，
    也就不再有任何跨用户共享点。

    环境变量 `TRIPPLANNER_SESSION_ID` 可覆盖（自检用它做**用例间**隔离；
    它在每用例显式设置，不受缓存污染影响）。
    """
    # ① 会话对象自带 sid（远端 http 模式：由 mcp_endpoint 按 Mcp-Session-Id 注入）
    sess_sid = getattr(SESSION, "sid", "") or ""
    if sess_sid:
        return sess_sid
    # ② 环境变量覆盖（自检/多用例隔离）
    env_sid = (os.environ.get("TRIPPLANNER_SESSION_ID") or "").strip()
    if env_sid:
        return env_sid
    # ③ stdio 单用户兜底：落盘一次，跨「按需 spawn 的新进程」稳定
    path = os.path.join(state_dir(), "mcp_session_id.txt")
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    import uuid as _uuid
    v = _uuid.uuid4().hex[:16]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(v)
        os.replace(tmp, path)
        dbg("session_id.created", sid=v)
    except Exception as e:
        dbg("session_id.write.fail", err=str(e))
    return v


def remote_config() -> dict:
    """远端后端配置 `{"base":…, "auth":"Basic …"}`。

    优先级（分享包友好）：
      ① 环境变量 TRIPPLANNER_REMOTE_BASE / TRIPPLANNER_REMOTE_AUTH
      ② `remote.json`（打包后=exe 同目录）—— **随分享包分发**
      ③ `secrets.json: remote_basic_auth`（开发机兜底）+ `REMOTE_API_DOMAINS[0]`
    """
    global _REMOTE_CFG_CACHE
    if _REMOTE_CFG_CACHE is not None:
        return _REMOTE_CFG_CACHE
    # ⚠️ 用**独立**的锁：本函数内部会调 `remote_basic_auth()`（它用 `_CACHE_LOCK`）
    #    若共用一把不可重入锁会自锁死。
    with _CFG_LOCK:
        if _REMOTE_CFG_CACHE is not None:
            return _REMOTE_CFG_CACHE
        base = (os.environ.get("TRIPPLANNER_REMOTE_BASE") or "").strip()
        auth = (os.environ.get("TRIPPLANNER_REMOTE_AUTH") or "").strip()
        try:
            with open(remote_config_path(), encoding="utf-8") as f:
                d = json.load(f) or {}
            base = base or str(d.get("base") or d.get("url") or "").strip()
            auth = auth or str(d.get("auth") or d.get("basic_auth") or "").strip()
            log("remote.json 已加载", path=remote_config_path())
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 —— 配置坏了要看得见，不能静默退化成默认
            log("remote.json 读取失败", err=str(e), path=remote_config_path())
        base = (base or REMOTE_API_DEFAULT_BASE).rstrip("/")
        if not auth:
            auth = remote_basic_auth()
        if auth and not auth[:6].lower().startswith("basic "):
            import base64 as _b64
            auth = "Basic " + _b64.b64encode(auth.encode("utf-8")).decode("ascii")
        _REMOTE_CFG_CACHE = {"base": base, "auth": auth}
        log("远端后端配置", base=base, has_auth=bool(auth),
            src="remote.json" if os.path.exists(remote_config_path()) else "secrets/默认")
        return _REMOTE_CFG_CACHE


def remote_request(path: str, params: dict | None = None, payload: dict | None = None,
                   method: str = "GET", timeout: float = 180.0) -> dict:
    """调云端后端，返回解析后的 JSON dict。

    网络/协议类失败**抛 RuntimeError**（调用方转成用户可读错误，绝不静默）；
    HTTP 4xx/5xx 返回后端给的 JSON（可能含 error 字段），同样带 `ok=False`。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    cfg = remote_config()
    url = cfg["base"] + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json",
               # CloudStudio 等前置代理有时按 UA 拦截，给一个明确标识便于排查
               "User-Agent": f"tripplanner-mcp/{SERVER_VERSION}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cfg["auth"]:
        headers["Authorization"] = cfg["auth"]
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            d = json.loads(body)
            if not isinstance(d, dict):
                d = {}
        except Exception:  # noqa: BLE001
            d = {}
        d.setdefault("ok", False)
        if not d.get("error"):
            hint = "（后端 Basic 凭据缺失或错误）" if e.code == 401 else ""
            d["error"] = f"后端返回 HTTP {e.code}{hint}"
        dbg("remote.request.http_error", path=path, code=e.code)
        return d
    except Exception as e:  # noqa: BLE001 —— 连不上必须说清楚连的是谁
        raise RuntimeError(f"无法连接后端 {cfg['base']}（{type(e).__name__}: {e}）") from e
    try:
        return json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"后端返回了非 JSON 内容（{len(body)} 字节）：{body[:200]}") from e



_AMAP_KEY_CACHE = None
# 🔴 2026-10-12 并发安全（MCP 去掉全串行锁后，多处惰性缓存会被并发首访）：
#   这些缓存都是「读配置 → 幂等结果」，但仍需防两类并发问题：
#     ① 多个线程同时首次读 → 重复读文件/重复打远端（浪费，但结果一致）；
#     ② **失败结果被缓存**（如 amap key 取不到时缓存 `""`）→ 首访若恰逢网络抖动，
#        后续所有请求都被这个空值污染（当日地图永久不可用）。故加锁 + 失败不缓存。
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# ☠️ M5 已删除：`city_names()` / `poi_count()` / `resolve_city()`
#
# 城市解析已**全部交给云端**：城市清单走 `GET /api/cities`，需求→城市的判定在
# `/api/plan` 内的 `plan_route_decision`（含 `strict_city=1` 的 agent 面向语义）。
# 两个删除理由：
#   ① `city_names()` 扫的是**本地** `data/` —— 分享包里根本没这个目录，它会静默
#      返回 `[]`，把「没数据」伪装成「配置问题」（极难查）。而且 `poi_count()` 会拉
#      内核 ⇒ 又把 `src/`+ortools 拖进打包。
#   ② 城市清单是**有版本的最小真相**，只能有一份（云端）。客户端缓存一份必然漂移
#      （加城后客户端还是旧清单 ⇒ 「这个城市不支持」的假报错）。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 跨进程状态落盘 + 文件诊断日志
# ---------------------------------------------------------------------------
# ⚠️ 为什么要落盘（2026-10-09 实测）：
#   宿主（WorkBuddy）对 stdio MCP server 是**按需 spawn 进程**的 —— `daemon.log` 里
#   `[MCP-AuthDiag] stdio-connected {"pidSpawned":16440 / 4556 / 32072 / 21000}` 每次都不一样。
#   于是「agent 调 plan_trip 的进程」与「面板调 get_plan 的进程」**未必是同一个**，
#   纯进程内存的 last_plan 会读到空 ⇒ 面板渲染不出行程（本次线上就是这个现象）。
#   结论：**凡是「一次调用写、另一次调用读」的状态，都必须落盘，不能只放进程内存。**
def state_dir() -> str:
    return os.path.join(project_root(), "output")


def plan_state_path(sid: str = "") -> str:
    """当前行程的落盘路径 —— **按会话隔离**（见文件头 P0 安全修复）。

    🔴 2026-10-12 P0 修复（Kenny 实测「贵州5日 → 面板显示杭州2日」）：
    原先是**全局单文件** `mcp_last_plan.json` —— 云端单进程多用户下，
    任何会话写一次就覆盖所有人 ⇒ **跨用户行程数据泄露**。
    故落盘按 sids 分文件（`mcp_last_plan_<sid>.json`）。
    sid 为空（stdio 单用户 / 测试）回退旧单文件名，保持兼容。
    """
    if sid:
        safe = re.sub(r"[^0-9a-zA-Z_-]", "", str(sid))[:40]
        if safe:
            return os.path.join(state_dir(), f"mcp_last_plan_{safe}.json")
    return os.path.join(state_dir(), "mcp_last_plan.json")


def debug_log_path() -> str:
    return os.path.join(state_dir(), "mcp_debug.log")


def dbg(event: str, **kw) -> None:
    """写文件诊断日志（output/mcp_debug.log），每行带 pid。

    宿主环境下 stderr 拿不到，跨进程问题只能靠落盘日志才看得见；
    有了 pid，一眼就能判断「这次调用是不是新起的进程」。
    """
    try:
        path = debug_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        extra = " " + json.dumps(kw, ensure_ascii=False) if kw else ""
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_now()}] pid={os.getpid()} {event}{extra}\n")
    except Exception:
        pass


def _plan_state_read(sid: str = ""):
    try:
        with open(plan_state_path(sid), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("city"):
            dbg("last_plan.read.hit", sid=sid[:8], city=data.get("city"),
                days=data.get("days"))
            return data
        dbg("last_plan.read.miss", sid=sid[:8], reason="empty-or-bad")
        return None
    except FileNotFoundError:
        dbg("last_plan.read.miss", sid=sid[:8], reason="no-file")
        return None
    except Exception as e:
        log("last_plan 读盘失败", err=str(e))
        dbg("last_plan.read.miss", sid=sid[:8], reason=str(e))
        return None


def _plan_state_write(value, sid: str = "") -> None:
    if value is None:
        return
    try:
        path = plan_state_path(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, path)          # 原子替换，避免面板读到半截 JSON
        dbg("last_plan.write.ok", sid=sid[:8], city=value.get("city"),
            days=value.get("days"), bytes=os.path.getsize(path))
    except Exception as e:
        log("last_plan 落盘失败", err=str(e))
        dbg("last_plan.write.fail", sid=sid[:8], err=str(e))


def read_plan_state():
    """当前行程 —— 读**本会话**的落盘副本。

    ⚠️ 历史（2026-10-09）：本函数曾「始终读磁盘」，因为 stdio 模式下宿主按需 spawn
    进程、跨进程只能靠磁盘。**该理由已随远端 http 模式退役**（云端单进程长驻），
    但读磁盘仍保留为「进程重启/多 worker」的兜底 —— 关键是**按会话分文件**，
    不再有全局共享点（见 §P0 安全修复）。
    """
    sid = getattr(SESSION, "sid", "") or ""
    val = _plan_state_read(sid)
    if val is not None:
        SESSION._last_plan = val
        SESSION._plan_loaded = True
    return val


def write_plan_state(value) -> None:
    """写回当前行程（落盘到本会话文件 + 同步本进程缓存）。"""
    SESSION.last_plan = value


# ---------------------------------------------------------------------------
# 面板注入占位符（仓库里只放占位符，真值在 `resources/read` 时替换）
#
# ☠️ M5 已删除本地 HTTP 那一整块（`local_api_*` / `ensure_local_api`）：面板后端全走
#    线上，本进程不再拉起任何本地服务。删除理由不只是「没有调用点」——
#    `ensure_local_api` 会 `subprocess` 拉起 `local_api.py`，而那个文件又 import 本模块
#    取内核 ⇒ PyInstaller 的依赖分析会把 `src/`+ortools 一起打进 exe。留着就是打包地雷。
#    那套历史设计（本地常驻进程 + 端口文件 + 稳定 token + 死因取证）的判据仍写在
#    `mcp/README.md` 的 M2.1 一节 —— 排查「宿主按需 spawn / 进程被回收」时依然有用。
# ---------------------------------------------------------------------------
# M3：线上后端 base（面板要 fetch 的就是它）。与 amap key 同一套「占位符不入库」手法。
REMOTE_API_BASE_PLACEHOLDER = "__REMOTE_API_BASE__"
REMOTE_API_DEFAULT_BASE = REMOTE_API_DOMAINS[0]
# M4：远端后端的 **HTTP Basic 认证头**（云主机 nginx auth_basic）。
#   面板要带凭据才访问得了线上后端 ⇒ 值由 secrets.json 的 `remote_basic_auth`
#   （形如 "user:pass"）在 **resources/read 时注入**，仓库里永远只是占位符。
REMOTE_API_AUTH_PLACEHOLDER = "__REMOTE_API_AUTH__"
# M5：`__SESSION_ID__` —— 云端无状态，面板「重新拉取行程 / 换点 / 挪时段」要一把钥匙
#   说明「我这份行程」，故把落盘的 session_id 注入面板。
# `__PLAN_JSON__` —— 渲染时注入**当前行程**：冷启动 / 云端会话过期时的兜底渲染源
#   （首屏仍走宿主 bootstrap 通道，这里只是让「没有云端会话」也不至于空白）。
SESSION_ID_PLACEHOLDER = "__SESSION_ID__"
PLAN_JSON_PLACEHOLDER = "__PLAN_JSON__"
# `__PANEL_VERSION__` —— 面板自报版本（`panel.boot` 轨迹里那行 `v=…`）。
#   为什么走注入而不是让面板自己写死：那个字符串曾经写死成 `v=m12`，`PANEL_VERSION`
#   涨到 m13 后它就悄悄过期了 —— 正是本项目「同一口径多处各写必漂移」的老坑。
#   ⇒ 面板里只留占位符，真实版本号唯一来源就是上面的 `PANEL_VERSION`。
PANEL_VERSION_PLACEHOLDER = "__PANEL_VERSION__"


class _Session:
    """一次「会话」的状态。

    🔴 2026-10-12 新增 `sid`（P0 安全修复）：会话 id 由**会话对象自身**携带，
    落盘路径 / 面板注入 / 云端会话键全部从它派生 ⇒ 不再有任何全局共享状态。
    此前 `sid` 来自**全局单文件** `mcp_session_id.txt`（`session_id()`），
    导致云端多用户共用一把钥匙 ⇒ 「A 规划贵州 → B 规划杭州 → A 面板显示杭州」
    （Kenny 实测，跨用户行程泄露）。

    ⚠️ **持久性分两类**（2026-10-09 修正，2026-10-12 复核）：
      - **进程级**：`opened_any` / `instances` / `panel_opens` —— 只做诊断，生命周期短，
        放内存即可。
      - **落盘兜底**：`last_plan` —— 远端 http 模式下云端是**单进程长驻**，内存本已够用；
        落盘仅为「进程重启 / 多 worker」兜底，且**必须按 sid 分文件**（见 `_plan_state_*`）。

    ⚠️ M6.6（2026-10-10）：**本类不再参与「要不要开面板」的判定**。曾经的
    `should_open_panel()` 一旦 `opened_any=True` 就永不再开 ⇒ 把同一会话的**第二个需求**
    也一并封死了（服务端拦不住宿主，那种"封锁"只拦住了模型自己）。面板节律已改由
    `open_plan` 的**工具描述**约定（每个需求定稿后调一次）。这里只留诊断计数：

    sid          —— 本会话的 id（**唯一真相**，取代全局 session_id 文件）。
    opened_any   —— 本会话是否展示过面板（`resources/read` 会回，供诊断）。
    instances    —— 面板自报的 appInstanceId 集合（由 `panel_ready` 登记）。
    panel_opens  —— `open_plan` 被调用的次数（>1 既可能是「用户提了新需求」，
                    也可能是「同一需求内重复调」；服务端不判、也不拦）。
    last_plan    —— 最近一次规划结果，面板 get_plan 拉取（内存优先、本会话磁盘兜底）。
    """

    def __init__(self, sid: str = "") -> None:
        self.sid = sid or ""
        self.opened_any = False
        self.instances: set[str] = set()
        self.panel_opens: int = 0
        self._last_plan = None
        self._plan_loaded = False
        self.pid = os.getpid()

    @property
    def last_plan(self):
        """内存未命中就回**本会话**磁盘文件读一次（跨进程/重启兜底）。"""
        if not self._plan_loaded:
            self._plan_loaded = True
            self._last_plan = _plan_state_read(self.sid)
            dbg("last_plan.property.load", sid=self.sid[:8], pid=self.pid,
                hit=self._last_plan is not None)
        return self._last_plan

    @last_plan.setter
    def last_plan(self, value):
        self._last_plan = value
        self._plan_loaded = True
        _plan_state_write(value, self.sid)

    def register_panel(self, instance_id: str | None, label: str = "") -> dict:
        iid = (instance_id or "").strip()
        if iid:
            self.instances.add(iid)
        self.opened_any = True
        return {
            "openedAny": self.opened_any,
            "instances": sorted(self.instances),
            "count": len(self.instances),
            "label": label,
        }

    def note_panel_shown(self, instance_id: str | None) -> int:
        """记一次「展示面板」，返回这是本会话第几次。

        ⚠️ **纯诊断**：不返回"要不要开"，更不做拦截 —— 宿主对每一次调用都会新开一扇，
        服务端说什么都不影响它（见文件头 M6.3 / M6.6）。之所以还记这个数，是因为
        「第几次」是唯一能帮模型/诊断区分「新需求」与「重复调用」的信号。
        """
        iid = (instance_id or "").strip()
        if iid:
            self.instances.add(iid)
        self.opened_any = True
        self.panel_opens += 1
        return self.panel_opens


class _SessionProxy:
    """`SESSION` 的**线程局部**代理（P0 安全修复，2026-10-12）。

    背景：`SESSION` 原先是**模块级全局单例**，`mcp_endpoint._handle_one` 每请求
    `m.SESSION = sess` 覆盖它 —— 这是「串数据」的结构性根因：
      · 任何一处忘记先赋值，就会脏读**上一个会话**的状态；
      · 多线程（`ThreadingHTTPServer`）下若并发进入，A 会话可能读到 B 的 SESSION。

    做法：把真实 `_Session` 存进 `threading.local()`。`mcp_endpoint` 用
    `m.SESSION.bind(sess)` 绑定（替代原先的 `m.SESSION = sess`）；
    `handle()` 内部所有 `SESSION.xxx` 读取只看到本线程的会话。
    ⇒ 模块级不再有任何可变共享状态，跨会话/跨用户串数据的结构基础被移除。

    `get()` 兜底：线程内尚未绑定 → 返回一个**临时空会话**（而非全局单例）：
    宁可「这个线程还没行程」，也绝不「读到别的会话的行程」。
    """

    def __init__(self) -> None:
        self._tls = threading.local()

    def bind(self, sess: "_Session") -> None:
        """把本线程的 SESSION 绑定到指定会话（`mcp_endpoint` 每请求调用）。"""
        self._tls.sess = sess

    def get(self) -> "_Session":
        s = getattr(self._tls, "sess", None)
        if s is None:
            s = _Session()          # 本线程尚未绑定 → 给个空会话，绝不复用他会话
            self._tls.sess = s
        return s

    def __getattr__(self, name):
        # 未显式定义的属性一律透传给**本线程**的会话（handle 里的 SESSION.xxx）
        return getattr(self.get(), name)

    def __setattr__(self, name, value):
        """🔴 关键：**禁止**在 proxy 自身落属性。

        若缺少本方法，`SESSION.last_plan = plan` 这类赋值会在 proxy **实例**上
        创建 `last_plan` 属性（`__getattr__` 只在「属性不存在」时触发）⇒
        之后 `SESSION.last_plan` 直接读到 proxy 上那份、**再也不看会话** ⇒
        状态写进了一个所有线程共享的地方 —— 正是要消灭的串数据形态
        （实测：`test_panel_single_open` 的 C 用例因此失败，`panel_opens` 恒 0）。
        故除了 `_tls` 自身，一切赋值透传给**本线程**的会话对象。
        """
        if name == "_tls":
            object.__setattr__(self, name, value)
        else:
            setattr(self.get(), name, value)


SESSION = _SessionProxy()

TOOLS = [
    {
        "name": PLAN_TOOL,
        "description": (
            "【主入口】按用户的一句话需求生成真实行程（**不打开面板**）。\n"
            "例：「杭州2天，带8岁孩子，想看西湖和灵隐寺」。\n"
            "会从需求里自动识别城市与天数（也可显式传 city / days）。\n"
            "规划在云端完成（POI 库 / 交通缓存 / LLM 都在线上），本进程只做代理。\n"
            "⚠️ 本工具**不打开行程面板**，所以可以放心反复调用（调整需求、重拍都不会堆面板）。\n"
            "⏩ 重拍 / 探索阶段可传 `copy_mode=\"lazy\"` 跳过逐日文案生成（更快）；"
            "**定稿那次用默认**（不传），open_plan 时会自动为最终行程补全文案。\n"
            "行程定稿后如需展示面板，调用 `tripplanner_open_plan`（**每个需求定稿后调一次**）："
            "同一需求内反复重拍时不要调它；用户在同一会话里提出**新需求**时，新行程定稿后"
            "**再调一次**，即为新方案弹出新面板。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的原始需求（尽量原样传入，城市与天数从中识别）",
                },
                "city": {"type": "string", "description": "可选。显式指定城市，如「杭州」"},
                "days": {"type": "integer", "description": f"可选。天数 {MIN_DAYS}-{MAX_DAYS}，不传则从需求识别"},
                "date": {"type": "string", "description": "可选。起始日期 YYYY-MM-DD（启用闭馆日约束）"},
                "hotel": {"type": "string", "description": "可选。住宿锚点（酒店名或「名称@lng,lat」）"},
                "copy_mode": {
                    "type": "string",
                    "enum": ["full", "lazy"],
                    "description": (
                        "可选（默认 full）。**重拍 / 探索阶段建议传 \"lazy\"**：跳过逐日"
                        "文案生成（占总耗时约一半），行程更快出来；点位/时间轴不受影响，"
                        "只有 theme 理由和 tips 暂缺。**用户满意、定稿那次不要传**——"
                        "调用 tripplanner_open_plan 时会自动为最终行程补全文案。"
                        "同一需求反复重拍时：探索轮 lazy + 定稿轮 full 是最省的组合。"
                    ),
                },
                "app_instance_id": {"type": "string", "description": "兼容字段，已不使用（保留以免旧调用报错）"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        # 🔴 **刻意不带** `_meta.ui.resourceUri`：带了宿主就会「每次调用开一扇面板」⇒ 重拍时堆面板。
        #    面板入口见 OPEN_TOOL。由 scripts/test_panel_single_open.py 守卫。
    },
    {
        "name": GET_PLAN_TOOL,
        "description": (
            "【面板内部调用为主】取回最近一次生成的行程（完整 JSON）。"
            "面板握手成功后用它拉数据；模型想看明细也可调。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": LIST_CITIES_TOOL,
        "description": "列出当前可规划的城市及其 POI 数量。无法确定城市时先调它。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": FIND_POIS_TOOL,
        "description": (
            "【发现工具】查**库内真实存在**的 POI —— 在推荐 / 挑选景点前用它对齐事实，"
            "别凭世界知识直接报点名字（库里没有的点排不进行程）。\n"
            "用途：① 想知道某座城库里有哪些点；② 确认某个景点是否被收录（名字对不对）；"
            "③ 按关键词 / 类目找同类替代点。\n"
            "参数：`city` 城市（可用 `+` 连多城，如「贵阳+安顺」；**留空 = 全库搜**）；"
            "`keyword` 关键词（匹配名称 / 标签 / 备注）；`category` 类目"
            "（nature/history/culture/food/photo/family/shopping/nightlife/outdoor/view）；"
            "`limit` 最多返回条数（默认 40，上限 100）。\n"
            "返回每个点的：`id` / 名称 / 城市 / 类目 / 建议时长 / 建议时段 / 开放时间 /"
            "闭馆日 / 亲子适配 / 价格 / 评级 / 标签 / 备注。\n"
            "⚠️ 参数留空且不传 keyword / category 会返回错误提示（无条件的全库罗列没有意义）。\n"
            "本工具**只读、不规划、不开面板**，可放心反复调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string",
                         "description": "城市名；多城用 `+` 连接（如「贵阳+安顺」）。留空 = 全库"},
                "keyword": {"type": "string",
                            "description": "关键词，匹配 POI 名称 / 标签 / 备注（如「西湖」「古镇」「亲子」）"},
                "category": {"type": "string",
                             "description": "类目过滤：nature/history/culture/food/photo/family/shopping/nightlife/outdoor/view"},
                "limit": {"type": "integer",
                          "description": "最多返回条数（默认 40，上限 100）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": OPEN_TOOL,
        "description": (
            "展示 TripPlanner 行程面板（渲染当前最新的行程；还没有行程则提示先调 "
            "tripplanner_plan_trip）。\n"
            "⚠️ 这是**面板的唯一入口**，而且**每次调用宿主都会新开一扇面板**"
            "（宿主按「单次工具调用」去重，服务端拦不住）。\n"
            "调用节律 —— **每个需求调一次**：\n"
            "  ① 同一需求内：先让 `tripplanner_plan_trip` 把需求拍定稿，**最后**再调本工具一次；"
            "思考 / 重拍过程中不要调（否则会一扇扇地堆）。\n"
            "  ② 用户在同一会话里提出**新需求**时：新行程定稿后**应当再调一次** ——"
            "这会给新方案弹出新面板，是预期行为，**不要**因为「刚才已经开过」就跳过。\n"
            "  ③ 只想刷新**当前**面板（同一行程，用户点了换点 / 挪时段）→ 不要调本工具，"
            "让用户点面板里的「重新拉取行程」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_instance_id": {
                    "type": "string",
                    "description": "可选。面板侧自报的宿主实例 id，用于精确判重",
                }
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"resourceUri": VIEWER_URI, "visibility": ["model", "app"]}},
    },
    {
        "name": PING_TOOL,
        "description": (
            "对照组：与 open_plan 同处一个 server，但**没有** _meta.ui.resourceUri。"
            "用来确认「只有带 resourceUri 的工具才开面板」；同时也是面板内反向调用的靶子。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "任意回显内容，用于确认参数链路"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": PANEL_READY_TOOL,
        "description": (
            "【面板内部调用，模型不要直接调】面板握手成功后自报家门："
            "把宿主注入的 appInstanceId 交给 server，server 据此建立「活跃面板实例」名单。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_instance_id": {"type": "string", "description": "宿主的 appInstanceId"},
                "label": {"type": "string", "description": "可选备注，便于日志排查"},
            },
            "additionalProperties": False,
        },
    },
    # ---------------- M2：面板内交互（换点 / 挪时段 / 导出） ----------------
    {
        "name": REPLACE_TOOL,
        "description": (
            "【面板「🔁 换一个」按钮调用，模型一般不直接调】把当天某个点换成"
            "同类目、就近的另一个点，并重排当天。\n"
            "守门：换点不得改变当天景点数 —— 会挤掉别处的候选直接放弃（宁可换不成也不破坏行程）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"},
                "day": {"type": "integer", "description": "第几天（1 起）"},
                "poi_id": {"type": "string", "description": "要换掉的 POI id"},
                "day_ids": {"type": "array", "items": {"type": "string"},
                            "description": "当天全部 POI id（按当前顺序）"},
                "used_ids": {"type": "array", "items": {"type": "string"},
                             "description": "全程已用 POI id（避免换成重复点）"},
                "query": {"type": "string", "description": "原需求，用于主题/节奏口径"},
                "date0": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "hotel_text": {"type": "string", "description": "住宿锚点"},
            },
            "required": ["city", "day", "poi_id", "day_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": MOVE_TOOL,
        "description": (
            "【面板拖拽排序调用】按给定的新顺序重排某一天（挪时段）。\n"
            "硬约束（餐窗 / 闭馆 / 里程）若必须微调，返回里的 order_respected 会是 false。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"},
                "day": {"type": "integer", "description": "第几天（1 起）"},
                "ordered_ids": {"type": "array", "items": {"type": "string"},
                                "description": "该天 POI id 的新顺序（点集必须与原来一致）"},
                "query": {"type": "string", "description": "原需求，用于主题/节奏口径"},
                "date0": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "hotel_text": {"type": "string", "description": "住宿锚点"},
            },
            "required": ["city", "day", "ordered_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": REMOVE_TOOL,
        "description": (
            "【调减点】从某一天**去掉一个停留点**并重排该天（腾出时间预算）。\n"
            "用途：当 plan_trip 回执里有「时间预算装不下」被剔除的点、而你希望保留它时，"
            "用本工具先去掉当天一个较不重要的点腾出时长，再重新规划或换点。\n"
            "注意：move_poi / replace_poi 都要求点集不变；**只有本工具能主动减点**。\n"
            "传 day_ids（当天当前全部 id）+ poi_id（要去掉的那个）；返回重排后的当天。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"},
                "day": {"type": "integer", "description": "第几天（1 起）"},
                "poi_id": {"type": "string", "description": "要去掉的点的 id"},
                "day_ids": {"type": "array", "items": {"type": "string"},
                            "description": "该天当前全部 POI id（去掉 poi_id 后重排）"},
                "query": {"type": "string", "description": "原需求，用于主题/节奏口径"},
                "date0": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "hotel_text": {"type": "string", "description": "住宿锚点"},
            },
            "required": ["city", "day", "poi_id", "day_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": EXPORT_TOOL,
        "description": (
            "【面板「导出」按钮调用】把当前行程导出为 Markdown 文本。\n"
            "面板拿到 text 后走宿主 ui/download-file 落盘；宿主不支持时退化为复制到剪贴板。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string",
                           "description": "导出格式，目前仅支持 markdown（默认）"},
            },
            "additionalProperties": False,
        },
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def log(msg: str, **kw) -> None:
    """日志一律走 stderr —— stdout 被 JSON-RPC 独占，写脏一个字节宿主就断连。"""
    extra = " " + json.dumps(kw, ensure_ascii=False) if kw else ""
    try:
        sys.stderr.write(f"[tripplanner-mcp] {msg}{extra}\n")
        sys.stderr.flush()
    except Exception:
        pass


def tool_error(msg: str) -> dict:
    """统一的「工具执行失败」返回。

    ⚠️ 刻意**不用** JSON-RPC error：面板走的是 AppBridge 的 tools/call，
    返回 error 会让面板的 Promise 直接 reject、连提示都拿不到；
    统一用 result + structuredContent.ok=false，面板可以照常渲染一句人话。
    """
    return {"content": [{"type": "text", "text": msg}],
            "structuredContent": {"ok": False, "error": msg}}


def send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def ok(id_, res):
    return {"jsonrpc": "2.0", "id": id_, "result": res}


def fail(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


# 面板里的地图 key 占位符 —— 由 read_panel() 在返回前替换成真实 key。
# 这样 **key 不进仓库**（panel/*.html 里永远是占位符，只有传输时才出现真实值）。
AMAP_KEY_PLACEHOLDER = "__AMAP_JS_KEY__"


def read_panel() -> str:
    try:
        with open(PANEL_PATH, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        log("panel read failed", err=str(e), path=PANEL_PATH)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'></head>"
            "<body style=\"font-family:system-ui;padding:20px\">"
            "<h3>TripPlanner 面板文件读取失败</h3>"
            f"<pre>{e}</pre><p>路径：{PANEL_PATH}</p></body></html>"
        )
    if AMAP_KEY_PLACEHOLDER in html:
        key = amap_js_key()
        html = html.replace(AMAP_KEY_PLACEHOLDER, key)
        log("panel 地图 key 注入", has_key=bool(key))
    # 面板自报版本（`panel.boot` 轨迹）—— **唯一来源是 PANEL_VERSION**。
    # 无条件替换：它是常量、不涉及 IO，且「漏替换 ⇒ 面板里出现裸占位符」比慢一点更糟。
    if PANEL_VERSION_PLACEHOLDER in html:
        html = html.replace(PANEL_VERSION_PLACEHOLDER, PANEL_VERSION)
    # M5：面板后端**全走线上** —— 线上 base + Basic 凭据都从 remote_config() 取
    # （打包后 = exe 同目录的 remote.json，随分享包分发；开发机回落 secrets.json）。
    # 本地常驻 API（local_api.py）自 M5 起**不再拉起** ⇒ 不再注入本地 base/token。
    if REMOTE_API_BASE_PLACEHOLDER in html or SESSION_ID_PLACEHOLDER in html:
        cfg = remote_config()
        html = html.replace(REMOTE_API_BASE_PLACEHOLDER, cfg["base"])
        html = html.replace(REMOTE_API_AUTH_PLACEHOLDER, cfg["auth"])
        html = html.replace(SESSION_ID_PLACEHOLDER, session_id())
        log("panel 线上后端注入", base=cfg["base"], has_auth=bool(cfg["auth"]),
            session=session_id())
    # 🔴🔴 2026-10-13 **已移除**（「规划武汉、面板出苏州」报障）：
    #   行程种子是把**某一次行程的快照**刻进面板 HTML。而宿主把面板 HTML 当
    #   **静态资源跨会话缓存**（实证 `resolver.resolve.cacheHit shared=true`）
    #   ⇒ 下一个会话复用这份 HTML 时，种子就是**上一个会话的行程**。
    #   叠加「注入的 SESSION_ID 同样陈旧 → 云端判会话过期 → 面板只能用种子兜底」
    #   这条链，结果是**渲染出别人的城市而毫无报错**。
    #   ⇒ 台词：把「每次都不同的会话数据」烤进「会被缓存的静态资源」，生命周期
    #   天然错配。种子带来的兜底价值（后端不可达时还能看）远小于它造成的串号风险。
    #   去掉后：面板一律用**运行时索取**的本会话 sid 去问云端；失败就明确显示
    #   「暂无行程」，而不是显示一份旧的别人的行程 —— 错得可见优于错得安静。
    return html


# ---------------------------------------------------------------------------
# M1 规划：调内核 → 归一化 → 缓存
# ---------------------------------------------------------------------------
# 一次规划的云端超时。LLM 提案 + 落地 + TOPTW 求解，慢时 60~120s ⇒ 给足余量；
# 超时按「连不上」如实报错，不静默降级。
PLAN_TIMEOUT_S = 240.0


def run_plan(query: str, days: int | None, city_arg: str | None,
             date0: str | None, hotel_text: str | None,
             copy_mode: str | None = None) -> dict:
    """跑一次真实规划 —— **M5 起整个委托给云端后端**（瘦客户端）。

    本进程不再加载规划内核（ortools / POI 库 / 交通缓存 / DeepSeek Key）：
    全部在云端。分享给别人时对方只需要 Python，连 `data/` 都不必带。
    返回统一结构：{"ok":…, "error":…, "plan":…}（plan 为**已归一化**的行程）。

    文案懒生成（Phase 3）：`copy_mode="lazy"` 跳过文案 LLM（重拍/探索用），
    定稿后 open_plan 自动对最终行程补跑（`POST /api/copy`）。
    """
    # `strict_city=1`：MCP App 是 **agent 面向**的 —— 需求里没提城市时必须明确报错，
    #   不能像 Web UI 那样落默认城市（否则 agent 会拿到一份用户没要的行程）。
    params = {"shape": "mcp", "query": query, "llm": "1", "session": session_id(),
              "strict_city": "1"}
    if days:
        params["days"] = int(days)
    if city_arg:
        params["city"] = city_arg
    if date0:
        params["date"] = date0
    if hotel_text:
        params["hotel"] = hotel_text
    if copy_mode:
        params["copy_mode"] = copy_mode
    t0 = time.time()
    try:
        res = remote_request("/api/plan", params=params,
                             timeout=PLAN_TIMEOUT_S)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "云端规划失败"}
    plan = res.get("plan")
    if not isinstance(plan, dict) or not plan.get("dayList"):
        return {"ok": False, "error": "云端未返回有效行程（plan.dayList 为空）"}
    plan["latency_s"] = round(time.time() - t0, 1)   # 端到端耗时（含网络）
    # 显式回传「LLM 到底用上了没」——离线降级是可用的，但不能静默发生。
    # ⚠️ 不能用 `mode != "llm"` 判降级：云端成功的 LLM 运行会回 "m7_proposal"，
    #    那样会被误判成降级（自检实测踩到）。只认明确的降级/兜底标记。
    mode = str(plan.get("mode") or "")
    plan["llmEnabled"] = True                     # 我们总是请求 llm=1
    plan["degraded"] = any(k in mode.lower() for k in ("degrad", "offline", "fallback"))
    if plan["degraded"]:
        log("⚠️ 降级：请求用 LLM 但云端返回 mode 带降级标记", mode=mode)
    return {"ok": True, "plan": plan}


def _merge_day_into_plan(plan: dict, day_obj: dict) -> dict:
    """把「改动后的一天」写回整份行程 —— **不依赖内核**（瘦客户端没有 src/data）。

    与内核 `plan_view.apply_day`、面板 `applyDayToPlan`、webui `swapPoi` **同一套语义**：
    按 day 号替换该天、按 id 回填 photo / 坐标（云端 /api/replace 回的是内核形状的
    day，没有这些字段）、**保留当日主题**、并重算总里程。四处必须一致，故这里刻意逐条对齐。

    ⚠️ 「保留当日主题」是 2026-10-10 报障（换点导致当日主题丢失）的修法：内核
    `build_itinerary` 产不出 theme（主题由 compose 阶段按最终时间轴生成），云端归一化
    把它补成空串 ⇒ 整日替换后标题退化成「未命名主题」。只保 theme，**不保** reason /
    tips（逐点点名，换点后即过期）。新结果明确给了 theme 时以新值为准。
    """
    if not isinstance(plan, dict) or not isinstance(day_obj, dict):
        return plan
    by_id = {}
    for d in plan.get("dayList") or []:
        for s in (d.get("timeline") or []):
            if s.get("type") == "poi" and s.get("id") and s["id"] not in by_id:
                by_id[s["id"]] = s
    for s in (day_obj.get("timeline") or []):
        if s.get("type") != "poi":
            continue
        o = by_id.get(s.get("id"))
        if not o:
            continue
        if not s.get("photo") and o.get("photo"):
            s["photo"] = o["photo"]
        if s.get("lat") is None and o.get("lat") is not None:
            s["lat"], s["lng"] = o.get("lat"), o.get("lng")
    d0 = day_obj.get("day")
    old = next((x for x in (plan.get("dayList") or []) if x.get("day") == d0), None)
    if isinstance(old, dict) and not day_obj.get("theme") and old.get("theme"):
        day_obj["theme"] = old["theme"]
    days = [(day_obj if x.get("day") == d0 else x) for x in (plan.get("dayList") or [])]
    new = dict(plan)
    new["dayList"] = days
    new["totalTravelKm"] = round(sum(float(x.get("travel_km") or 0) for x in days), 1)
    new["updatedAt"] = _now()
    return new


def _sync_plan_from_remote() -> None:
    """把「本会话的当前行程」从云端拉回来，刷新磁盘副本。

    云端会话是**唯一**反映了面板侧改动（换点 / 挪时段）的真相；拉不到
    （会话过期 / 后端不可达）就静默保留磁盘副本 —— get_plan 的价值在于
    「有东西可给」，不能因为一次网络抖动就把已渲染的行程清空。
    """
    try:
        res = remote_request("/api/plan",
                             params={"shape": "mcp", "session": session_id()},
                             timeout=20.0)
    except RuntimeError as e:
        dbg("get_plan.sync.skip", err=str(e))
        return
    plan = res.get("plan") if isinstance(res, dict) else None
    if isinstance(res, dict) and res.get("ok") and isinstance(plan, dict) \
            and plan.get("dayList"):
        write_plan_state(plan)
        dbg("get_plan.sync.ok", city=plan.get("city"))
    else:
        dbg("get_plan.sync.miss", err=(res or {}).get("error"))


# ☠️ M5 已删除：`_normalize_day()` / `apply_day()` / `normalize_plan()`
#    （原先是指向 `src/plan_view.py` 的薄包装）。归一化现在由**云端**做
#    （`?shape=mcp` ⇒ `plan_view.normalize_plan`），瘦客户端只做 `_merge_day_into_plan`
#    这一件不依赖内核的事。同前：留着这些壳就会把 `src/` 拖进打包。


def plan_summary(plan: dict) -> str:
    """给模型/用户看的文字摘要（面板里的结构化内容另走 structuredContent）。

    ⚠️ 一律用 `.get`：行程可能来自云端会话 / 磁盘旧副本，字段未必齐全 ——
    摘要缺一个字段就让整次调用崩掉是不可接受的（探针实测踩过 KeyError('latency_s')）。
    """
    plan = plan or {}
    days_list = plan.get("dayList") or []
    dropped = plan.get("droppedPois") or []
    # Phase 1 提案来源：agent = 调用方拟的点位清单；server = 服务端提案；缺省 = 旧数据无此字段
    _src = {"agent": "调用方 agent", "server": "服务端"}.get(plan.get("planSource"), "-")
    lines = [
        f"已生成【{plan.get('city')}】{plan.get('days')} 天行程"
        f"（模式={plan.get('mode')}，提案来源={_src}，"
        f"候选 {plan.get('candidates')} 个点，"
        f"耗时 {plan.get('latency_s', 0)}s）",
        f"总里程 {plan.get('totalTravelKm')}km ｜ 硬约束违规 {plan.get('totalViolations')}"
        f" ｜ 被剔除 {len(dropped)} 个点",
    ]
    # 文案懒生成（Phase 3）：lazy 行程 reason/tips 空，提醒模型「重拍可继续 lazy、
    # 定稿调 open_plan 会自动补文案」，避免模型误以为行程残缺。
    if plan.get("copyMode") == "lazy":
        lines.append("⏩ 文案懒生成模式：各日理由/tips 暂缺（正常）。"
                     "继续调整请继续传 copy_mode=\"lazy\"；用户满意后调用 "
                     "tripplanner_open_plan，会自动补全最终文案。")
    for d in days_list:
        pois = [f"{s.get('start')} {s.get('name')}"
                for s in (d.get("timeline") or []) if s.get("type") == "poi"]
        lines.append(f"Day{d.get('day')}｜{d.get('theme')}｜{d.get('travel_km')}km"
                     f"｜收工 {d.get('finish')}")
        lines.append("   " + " → ".join(pois) if pois else "   （该日无点位）")
    if dropped:
        # 🔴 2026-10-13：**必须给出原因**（宿主多轮审阅的第 1 条）。此前只报名字，
        #   agent 看不到「为什么这个点没进来」⇒ 反复重拍重算。
        #   原因口径（内核填）：求解器弃点=「时间预算内装不下」；修复链=具体违规
        #   （超出营业时间/闭店前玩不完/里程超预算/餐窗满…）。按原因分组，一眼可辨。
        _by_reason = {}
        for p in dropped:
            _by_reason.setdefault((p.get("reason") or "未说明"), []).append(p)
        for reason, items in _by_reason.items():
            names = "、".join((p.get("name") or "?") for p in items[:6])
            more = f" 等 {len(items)} 个" if len(items) > 6 else ""
            lines.append(f"剔除 {len(items)} 个：{names}{more}")
            lines.append(f"    原因：{reason}")
        # 给宿主的**可操作提示**：装不下的点不是"被刁难"，而是当日已满。
        if any("时间预算" in (p.get("reason") or "") for p in dropped):
            lines.append("⚠️ 上列「时间预算装不下」的点：当日时长已排满，"
                         "硬约束无法违反。若必须保留，请改用 tripplanner_remove_poi "
                         "去掉当日一个点腾出时间，或把它改排到其他天。")
    return "\n".join(lines)


def _pois_text(data: dict) -> str:
    """`find_pois` 的人类可读回执（模型读这段；结构化清单另走 structuredContent）。

    紧凑格式是刻意的：agent 要把这些点喂进上下文，一行一个点比 JSON 省 token，
    但字段不能少 —— 少一个「闭馆日」或「亲子不适」就会让 agent 排出排不进的点。
    """
    pois = data.get("pois") or []
    scope = "、".join(data.get("cities") or []) or "全库"
    bits = []
    if data.get("keyword"):
        bits.append(f"关键词「{data['keyword']}」")
    if data.get("category"):
        bits.append(f"类目 {data['category']}")
    cond = ("，" + "，".join(bits)) if bits else ""
    total = data.get("total") or len(pois)
    head = f"{scope} 命中 {total} 个 POI{cond}"
    if total > len(pois):
        head += f"（只显示前 {len(pois)} 个，可加 keyword 缩小范围）"
    if data.get("unknown"):
        head += f"；未收录：{'、'.join(data['unknown'])}"
    lines = [head + "："]
    for p in pois:
        meta = []
        if p.get("duration_h") is not None:
            meta.append(f"约{p['duration_h']}h")
        if p.get("best_time"):
            meta.append(f"宜{p['best_time']}")
        if p.get("open") and p.get("close"):
            meta.append(f"{p['open']}-{p['close']}")
        if p.get("closed_days"):
            meta.append("闭馆 " + "/".join(str(x) for x in p["closed_days"]))
        meta.append("免费" if not p.get("price") else f"¥{p['price']}")
        meta.append("亲子OK" if p.get("family_ok") else "不适合小孩")
        if p.get("tags"):
            meta.append("标签 " + "/".join(str(x) for x in p["tags"]))
        lines.append(f"- {p.get('id')} {p.get('name')} [{p.get('category')}] "
                     + " · ".join(meta))
        if p.get("note"):
            lines.append(f"    {p['note']}")
    return "\n".join(lines)


# ☠️ M5 已删除：`_md_export()` / `plan_to_markdown()` / `export_filename()`
#    （原先是指向 `src/md_export.py` 的薄包装）。导出改由**云端**做
#    （`POST /api/export` ⇒ `md_export.plan_to_markdown`），瘦客户端只负责把
#    Markdown 文本与文件名原样交给宿主下载。


def handle(msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    is_notification = id_ is None
    log("<-", method=method, id=id_)

    # M5：本地常驻 API（`local_api.py`）已**下线** —— 面板后端全走线上，
    # 本进程不再拉起任何本地服务（`local_api.py` 仅作开发期对照保留，不在主链路调用）。

    if method == "initialize":
        pv = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return ok(id_, {
            "protocolVersion": pv,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # MCP 标准字段（中性总纲）：支持的宿主会把它注入 LLM 系统提示 —— 只讲
            # 「直接调 plan_trip、服务端自己提案」，**不再引导 agent 走提案路径**
            # （2026-10-11 回退，见 SERVER_INSTRUCTIONS 注释）。
            "instructions": SERVER_INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else ok(id_, {})

    if method == "tools/list":
        return ok(id_, {"tools": TOOLS})

    if method == "resources/list":
        return ok(id_, {"resources": [{
            "uri": VIEWER_URI,
            "name": "tripplanner-plan-panel",
            "mimeType": UI_MIME,
        }]})

    if method == "resources/templates/list":
        return ok(id_, {"resourceTemplates": []})

    # 2026-10-11 回退：不再提供 prompts（曾用 plan-trip 模板引导 agent 走提案路径，
    # 实测效果不佳 ⇒ 撤掉）。capabilities 已不声明 prompts ⇒ 宿主不会列出/调用；
    # 万一被显式调用，落到末尾的 -32601 fallback（Method not found）。

    if method == "resources/read":
        uri = params.get("uri") or ""
        # ⚠️ 容忍**任意** plan-* 版本（不只当前 VIEWER_URI）。
        # 为什么：改 PANEL_VERSION 后宿主**注册表里仍是旧 uri**（注册表要等宿主重连才更新），
        # 若这里严格比对，就会出现「宿主拿旧 uri 来请求 → 404 → 面板彻底打不开」的窗口期。
        # 放宽后无论 m1/m2/m3 来请求，都返回磁盘上的最新面板（内容始终是最新的）。
        if not str(uri).startswith("ui://tripplanner/plan"):
            return fail(id_, -32602, f"Resource not found: {uri}")
        text = read_panel()
        log("resources/read", uri=uri, current=VIEWER_URI,
            html_bytes=len(text.encode("utf-8")))
        return ok(id_, {"contents": [{
            "uri": VIEWER_URI,
            "mimeType": UI_MIME,
            "text": text,
            "_meta": UI_RESOURCE_META,
        }]})

    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            return fail(id_, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        # 诊断：记录 pid 与「磁盘上是否已有行程」——前者判断是否跨进程，后者判断取数是否命中
        dbg("tools/call", name=name, pid=os.getpid(),
            planOnDisk=os.path.exists(plan_state_path()),
            openedAny=SESSION.opened_any)

        # 本地 API 的确保已在 handle() 总入口做过（覆盖全部方法），这里不再重复。

        # ---------------- M1：列城市（M5：改问云端，本地不再读 data/） ----------------
        if name == LIST_CITIES_TOOL:
            try:
                res = remote_request("/api/cities", timeout=20.0)
            except RuntimeError as e:
                return ok(id_, {
                    "content": [{"type": "text", "text": f"取城市列表失败：{e}"}],
                    "structuredContent": {"ok": False, "error": str(e)},
                })
            items = [{"city": c.get("name"), "pois": c.get("n_pois")}
                     for c in (res.get("cities") or []) if c.get("name")]
            txt = "、".join(f"{i['city']}({i['pois']})" for i in items) or "（无可用城市数据）"
            return ok(id_, {
                "content": [{"type": "text", "text": f"可规划 {len(items)} 个城市：{txt}"}],
                "structuredContent": {"count": len(items), "cities": items,
                                      "llm": res.get("llm")},
            })

        # ---------------- Phase 0：POI 发现（只读；M5 瘦客户端 ⇒ 库在云端） ----------------
        #
        # 为什么要有它：调用方 agent / 用户想了解「库内真实有哪些点」时直接查，
        # 不必凭世界知识猜名字。自身只读、零风险、不改 plan_trip。
        # （原「Agent 提案外包」Phase 1 已退役：plan_trip 的 proposal/dry_run 参数已于
        #   2026-10-11 移除 —— agent 只理解需求 + 调 plan_trip，提案由服务端自己做。）
        if name == FIND_POIS_TOOL:
            city = (args.get("city") or "").strip()
            keyword = (args.get("keyword") or "").strip()
            category = (args.get("category") or "").strip()
            if not (city or keyword or category):
                return ok(id_, tool_error(
                    "至少提供 city / keyword / category 之一"
                    "（如 city=「杭州」、keyword=「西湖」、category=「nature」）。"
                    "不确定有哪些城市可先调 tripplanner_list_cities。"))
            params = {}
            if city:
                params["city"] = city
            if keyword:
                params["keyword"] = keyword
            if category:
                params["category"] = category
            if args.get("limit") is not None:
                params["limit"] = args.get("limit")
            try:
                data = remote_request("/api/pois", params=params, timeout=30.0)
            except RuntimeError as e:
                return ok(id_, tool_error(str(e)))
            if not data.get("ok"):
                msg = data.get("error") or "查询失败"
                sup = data.get("supported") or []
                if sup:
                    msg += f"（已收录：{'、'.join(sup)}）"
                return ok(id_, tool_error(msg))
            pois = data.get("pois") or []
            log("find_pois", city=city, keyword=keyword, category=category,
                total=data.get("total"), returned=len(pois))
            sc = {"ok": True, "count": len(pois), "total": data.get("total"),
                  "cities": data.get("cities") or [], "unknown": data.get("unknown") or [],
                  "pois": pois, "server": SERVER_NAME, "version": SERVER_VERSION}
            if not pois:
                return ok(id_, {
                    "content": [{"type": "text",
                                 "text": "没有匹配的 POI。可换个关键词 / 类目，"
                                         "或用 tripplanner_list_cities 看看有哪些城市。"}],
                    "structuredContent": sc,
                })
            return ok(id_, {
                "content": [{"type": "text", "text": _pois_text(data)}],
                "structuredContent": sc,
            })

        # ---------------- M1：真实规划（带面板） ----------------
        if name == PLAN_TOOL:
            query = (args.get("query") or "").strip()
            if not query:
                return ok(id_, {
                    "content": [{"type": "text", "text": "缺少必填参数 query（用户的行程需求）"}],
                    "structuredContent": {"ok": False, "error": "missing query"},
                })
            res = run_plan(query, args.get("days"), args.get("city"),
                           args.get("date"), args.get("hotel"),
                           copy_mode=args.get("copy_mode"))

            if not res.get("ok"):
                # 失败只回文字：本工具**不再是面板入口**（M6.3）⇒ 不碰 opened_any，
                # 免得把「面板已开过」写脏、污染后面 open_plan 的判重文案。
                log("plan_trip 失败", err=res.get("error"))
                return ok(id_, {
                    "content": [{"type": "text", "text": f"行程生成失败：{res['error']}"}],
                    "structuredContent": {"ok": False, "error": res["error"],
                                          "server": SERVER_NAME, "version": SERVER_VERSION},
                })

            plan = res["plan"]
            SESSION.last_plan = plan          # 面板（open_plan）之后靠它/云端会话取数
            log("plan_trip 完成", city=plan["city"], days=plan["days"],
                mode=plan["mode"], km=plan["totalTravelKm"],
                src=plan.get("planSource"))
            return ok(id_, {
                "content": [{"type": "text", "text": plan_summary(plan) + "\n\n" + PANEL_OPEN_HINT}],
                "structuredContent": {
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "stage": "M1",
                    "ok": True,
                    # 本工具**永远不会**开面板（见 TOOLS 里 PLAN_TOOL 的注释）；
                    # 面板入口是 OPEN_TOOL。这两个字段是给模型/诊断看的引导。
                    "panelOpened": False,
                    "panelVia": OPEN_TOOL,
                    "panelOpenHint": PANEL_OPEN_HINT,
                    "plan": plan,
                },
            })

        # ---------------- M1：面板拉行程 ----------------
        if name == GET_PLAN_TOOL:
            # M5：先问云端（面板侧的换点/挪时段都写进云端会话，它才是最鲜的一份）
            _sync_plan_from_remote()
            if SESSION.last_plan is None:
                return ok(id_, {
                    "content": [{"type": "text", "text": "还没有生成过行程，请先调 tripplanner_plan_trip"}],
                    "structuredContent": {"hasPlan": False},
                })
            return ok(id_, {
                "content": [{"type": "text", "text": plan_summary(SESSION.last_plan)}],
                "structuredContent": {"hasPlan": True, "plan": SESSION.last_plan},
            })

        # ---------------- M0/M1pre：开面板（展示当前最新行程） ----------------
        #
        # ⚠️ M6.6（2026-10-10）：服务端**不判重、不劝阻**。宿主对**每一次**调用都会新开一扇
        #    面板，服务端返回什么字段都拦不住（见文件头 M6.3 / M6.6）。所以这里只做两件事：
        #    ① 记诊断计数（本会话第几次展示）；② 回最新行程 + 调用节律提示。
        #    曾经有过「本会话已开过 ⇒ 回劝阻文案」的分支，代价是**同一会话的第二个需求
        #    永远出不来面板** ⇒ 已整体删除。节律改由工具描述约定（每个需求定稿后一次）。
        if name == OPEN_TOOL:
            iid = args.get("app_instance_id")
            nth = SESSION.note_panel_shown(iid)
            # 文案懒生成（Phase 3）：重拍轮次 copy_mode=lazy 的行程 reason/tips 为空，
            # 定稿展示前**在这里**补跑（云端 POST /api/copy，逐天并行 LLM）——
            # 「用户满意、开面板前才生成最终文案」正是 lazy 的设计意图。失败不拦开面板：
            # 面板拿 lazy 行程照常渲染（理由位空），文案可下次重试。
            if SESSION.last_plan is not None \
                    and SESSION.last_plan.get("copyMode") == "lazy":
                try:
                    _cres = remote_request("/api/copy",
                                           payload={"session": session_id()},
                                           method="POST", timeout=120.0)
                    _cplan = _cres.get("plan") if isinstance(_cres, dict) else None
                    if _cres.get("ok") and isinstance(_cplan, dict) and _cplan.get("dayList"):
                        SESSION.last_plan = _cplan
                        write_plan_state(_cplan)
                        log("open_plan: lazy 文案已补", days=_cres.get("days"))
                except Exception as e:  # noqa: BLE001 —— 补文案失败不拦面板
                    dbg("open_plan.copy.skip", err=str(e))
            log("open_plan: 展示面板", instance=iid, n=nth)
            # 「第几次」是唯一能区分「新需求」与「同一需求内重复调」的信号 —— 交给模型判：
            # 新需求 ⇒ 第 N 次属预期；重复调 ⇒ 提示别再调。**不改变 panelOpened**（宿主说了算）。
            text = "行程面板已打开，展示的是当前最新行程。"
            if nth > 1:
                text += (f"（这是本会话第 {nth} 次展示：若用户提出了**新需求**，属预期行为；"
                         f"若只是同一需求内的重复调用，请勿再调本工具。）")
            return ok(id_, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "server": SERVER_NAME, "version": SERVER_VERSION, "stage": "M1",
                    # ⚠️ 别把这当「宿主一定会开面板」的保证：宿主只按 toolCallId 去重、且
                    #    只读工具定义里的 _meta.ui ⇒ 这里恒为 True，纯诊断。
                    "panelOpened": True, "appInstanceId": iid or "",
                    "panelOpenCount": nth,
                    "hasPlan": SESSION.last_plan is not None,
                    "plan": SESSION.last_plan,
                    # 🔴 2026-10-13（「规划武汉、面板出苏州」）：**本会话的 sid**。
                    #   面板 HTML 是宿主**跨会话缓存**的静态资源（实测
                    #   `resolver.resolve.cacheHit shared=true`）⇒ 里面注入的
                    #   SESSION_ID 是烤死那一刻的值，下个会话复用就成了别人的钥匙
                    #   ⇒ 拿它问云端只会「会话已过期」⇒ 面板回落到烤死的行程种子
                    #   ⇒ 渲染出别人城市。修法＝**运行时索取**：面板启动/会话失效时
                    #   经宿主 tools/call 调本工具（每次针对当前 request 重新执行，
                    #   天然新鲜）拿到本会话 sid，再去云端 /api/plan 取自己的行程。
                    "session": session_id(),
                },
            })

        if name == PING_TOOL:
            note = args.get("note") or "(无)"
            log("ping hit", note=note)
            return ok(id_, {
                "content": [{"type": "text", "text": f"pong <- {note}"}],
                "structuredContent": {
                    "pong": True, "note": note,
                    "server": SERVER_NAME, "version": SERVER_VERSION, "ts": _now(),
                },
            })

        if name == PANEL_READY_TOOL:
            iid = args.get("app_instance_id")
            label = args.get("label") or ""
            info = SESSION.register_panel(iid, label)
            log("panel_ready", instance=iid, label=label, count=info["count"])
            missing = "" if (iid or "").strip() else "（未取到 appInstanceId，已退化为 C 兜底）"
            return ok(id_, {
                "content": [{
                    "type": "text",
                    "text": f"面板已登记：{info['count']} 个活跃实例 {missing}".strip(),
                }],
                "structuredContent": {
                    "registered": True,
                    "appInstanceId": iid or "",
                    "knownInstances": info["instances"],
                    "count": info["count"],
                    "hasInstanceId": bool((iid or "").strip()),
                    "policy": "A+C",
                    "hasPlan": SESSION.last_plan is not None,
                    "server": SERVER_NAME,
                },
            })

        # ---------------- M2：换点（面板「🔁 换一个」按钮） ----------------
        if name == REPLACE_TOOL:
            city = (args.get("city") or "").strip()
            day = args.get("day")
            poi_id = (args.get("poi_id") or "").strip()
            day_ids = args.get("day_ids") or []
            if not (city and day and poi_id and day_ids):
                return ok(id_, tool_error("参数缺失（需要 city / day / poi_id / day_ids）"))
            # M5：委托云端 —— 瘦客户端没有规划内核 / POI 库（分享给别人时也不会装）
            cur = SESSION.last_plan or {}
            try:
                data = remote_request("/api/replace", method="POST", payload={
                    "city": city, "day": day, "poi_id": poi_id, "day_ids": day_ids,
                    "used_ids": args.get("used_ids") or [],
                    "date0": args.get("date0"), "query": args.get("query") or "",
                    "hotel_text": args.get("hotel_text"),
                    "session": session_id(), "plan": cur,
                })
            except RuntimeError as e:
                return ok(id_, tool_error(str(e)))
            if not data.get("ok"):
                log("replace_poi 失败", city=city, day=day, err=data.get("error"))
                return ok(id_, tool_error(data.get("error") or "换点失败"))
            plan = _merge_day_into_plan(cur, data.get("day") or {})
            SESSION.last_plan = plan
            sw = data.get("swap") or {}
            log("replace_poi 成功", city=city, day=day, frm=sw.get("from"), to=sw.get("to"))
            return ok(id_, {
                "content": [{"type": "text",
                             "text": f"已换点：{sw.get('from')} → {sw.get('to')}"
                                     + ("（同类目）" if sw.get("same_category") else "（就近）")}],
                "structuredContent": {"ok": True, "swap": sw,
                                      "day": data.get("day"), "plan": plan,
                                      "server": SERVER_NAME, "version": SERVER_VERSION},
            })

        # ---------------- M2：挪时段（面板拖拽排序） ----------------
        if name == MOVE_TOOL:
            city = (args.get("city") or "").strip()
            day = args.get("day")
            ordered = [str(x) for x in (args.get("ordered_ids") or [])]
            if not (city and day and ordered):
                return ok(id_, tool_error("参数缺失（需要 city / day / ordered_ids）"))
            # 点集校验：拖拽只改顺序，**不允许**悄悄丢点或加点
            cur = SESSION.last_plan or {}
            day_ids = []
            for d in cur.get("dayList") or []:
                if d.get("day") == day:
                    day_ids = [s.get("id") for s in (d.get("timeline") or [])
                               if s.get("type") == "poi"]
            if day_ids and set(ordered) != set(day_ids):
                return ok(id_, tool_error(
                    f"新顺序的点集与当天不一致（原 {len(day_ids)} 个，收到 {len(ordered)} 个）"))
            # M5：委托云端（同 replace —— 瘦客户端没有求解器）
            try:
                data = remote_request("/api/move", method="POST", payload={
                    "city": city, "day": day, "ordered_ids": ordered,
                    "day_ids": day_ids,
                    "date0": args.get("date0"), "query": args.get("query") or "",
                    "hotel_text": args.get("hotel_text"),
                    "session": session_id(), "plan": cur,
                })
            except RuntimeError as e:
                return ok(id_, tool_error(str(e)))
            if not data.get("ok"):
                log("reorder_day 失败", city=city, day=day, err=data.get("error"))
                return ok(id_, tool_error(data.get("error") or "调整顺序失败"))
            plan = _merge_day_into_plan(cur, data.get("day") or {})
            SESSION.last_plan = plan
            respected = bool(data.get("order_respected"))
            log("reorder_day 成功", city=city, day=day, respected=respected)
            return ok(id_, {
                "content": [{"type": "text",
                             "text": "顺序已更新。" if respected
                                     else "顺序已更新（部分受开放时间/餐窗约束微调）。"}],
                "structuredContent": {"ok": True, "order_respected": respected,
                                      "day": data.get("day"), "plan": plan,
                                      "server": SERVER_NAME, "version": SERVER_VERSION},
            })

        # ---------------- 2026-10-13：减点（腾时间预算） ----------------
        # 🔴 为什么要有它：move_poi / replace_poi 的守门都是「点集不变」——
        #   agent 想让一个"时间预算装不下"的点进来时，**没有工具能主动减点**，
        #   只能重拍整份行程（重算 20-60s）。本工具补上这个动作。
        # 实现：复用 `poi_swap.reorder_day`（去掉 poi_id 后按剩余顺序重排该天）。
        #   它不是"新算法"，而是把相同的重排能力**开一个允许减点的口子**；
        #   硬约束仍在（去掉后仍可能被微调，`order_respected` 如实回报）。
        if name == REMOVE_TOOL:
            city = (args.get("city") or "").strip()
            day = args.get("day")
            poi_id = (args.get("poi_id") or "").strip()
            day_ids = [str(x) for x in (args.get("day_ids") or [])]
            if not (city and day and poi_id and day_ids):
                return ok(id_, tool_error(
                    "参数缺失（需要 city / day / poi_id / day_ids）"))
            if poi_id not in day_ids:
                return ok(id_, tool_error(f"待删点 {poi_id} 不在当天 day_ids 里"))
            remain = [i for i in day_ids if i != poi_id]
            if not remain:
                return ok(id_, tool_error("当天只剩这一个点，删掉后该天为空，已拒绝"))
            cur = SESSION.last_plan or {}
            # 走云端 `/api/remove`（复用 reorder_day，但**不套** move 的「点集必须一致」
            # 守门 —— 那守门正是减点的目的所在）。云端另加「真子集」底线防误用。
            try:
                data = remote_request("/api/remove", method="POST", payload={
                    "city": city, "day": day, "ordered_ids": remain,
                    "day_ids": day_ids,
                    "date0": args.get("date0"), "query": args.get("query") or "",
                    "hotel_text": args.get("hotel_text"),
                    "session": session_id(), "plan": cur,
                })
            except RuntimeError as e:
                return ok(id_, tool_error(str(e)))
            if not data.get("ok"):
                log("remove_poi 失败", city=city, day=day, err=data.get("error"))
                return ok(id_, tool_error(data.get("error") or "减点失败"))
            plan = _merge_day_into_plan(cur, data.get("day") or {})
            SESSION.last_plan = plan
            # 回执确认「真的少了那个点」（不能只靠请求成功）
            still = [s.get("id") for s in ((data.get("day") or {}).get("timeline") or [])
                     if s.get("type") == "poi"]
            gone = poi_id not in still
            log("remove_poi 成功", city=city, day=day, removed=poi_id, gone=gone)
            return ok(id_, {
                "content": [{"type": "text",
                             "text": f"已从 Day{day} 去掉 1 个点"
                                     f"（剩余 {len(still)} 个）。当天时间预算已释放，"
                                     f"可继续 plan_trip / replace_poi / open_plan。"}],
                "structuredContent": {"ok": True, "removed": poi_id, "removedOk": gone,
                                      "day": data.get("day"), "plan": plan,
                                      "server": SERVER_NAME, "version": SERVER_VERSION},
            })

        # ---------------- M2：导出 Markdown ----------------
        if name == EXPORT_TOOL:
            plan = SESSION.last_plan
            if not plan:
                return ok(id_, tool_error("还没有行程可导出，请先生成行程"))
            fmt = str(args.get("format") or "markdown").lower()
            if fmt not in ("markdown", "md"):
                return ok(id_, tool_error(f"暂不支持的导出格式：{fmt}（目前仅 markdown）"))
            # M5：委托云端（md_export 属于 src/，瘦客户端不打包它）
            try:
                data = remote_request("/api/export", method="POST", payload={
                    "plan": plan, "format": "markdown", "session": session_id(),
                })
            except RuntimeError as e:
                return ok(id_, tool_error(str(e)))
            if not data.get("ok"):
                return ok(id_, tool_error(data.get("error") or "导出失败"))
            text = data.get("text") or ""
            fname = data.get("filename") or "TripPlanner.md"
            log("export ok", city=plan.get("city"), chars=len(text))
            return ok(id_, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"ok": True, "filename": fname,
                                      "mimeType": "text/markdown", "text": text},
            })

    if is_notification:
        return None
    return fail(id_, -32601, f"Method not found: {method}")


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    # 幂等 reconfigure：既保证 UTF-8，也不会像重建 TextIOWrapper 那样在 GC 时关掉底层 buffer
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except Exception:
            pass

    log("start", pid=os.getpid(), version=SERVER_VERSION, panel=PANEL_PATH,
        root=project_root())

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log("bad json", err=str(e), raw=line[:200])
            continue

        try:
            out = handle(msg)
        except Exception as e:  # 任何异常都不能让进程崩掉，否则面板直接消失
            log("handler crash", err=repr(e), method=msg.get("method"))
            out = fail(msg.get("id"), -32603, f"internal error: {e}")

        if out is not None:
            send(out)

    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
