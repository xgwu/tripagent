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
  tripplanner_plan_trip   ★带面板：做规划 + 开面板
  tripplanner_open_plan   ★带面板：只开面板（复用上次行程；M0 遗留 + 分流对照）
  tripplanner_get_plan      无面板：面板握手后主动来拉行程（M1 数据通道）
  tripplanner_list_cities   无面板：列出可规划城市
  tripplanner_panel_ready   无面板：面板自报 appInstanceId（A 方案）
  tripplanner_ping          无面板：连通性对照
  tripplanner_replace_poi   无面板：M2 换点
  tripplanner_move_poi      无面板：M2 挪时段
  tripplanner_export        无面板：M2 导出 Markdown

⚠️ M2.1（2026-10-09，**M5 起已不适用**）：面板 → 宿主 → server 的 `tools/call` 通道
在本宿主上是**死的**（宿主有 `rpc.callTool`/`proxyAppToolCall.ok`，但请求从未到达任何
server 进程、12ms 假成功、structuredContent 被剥）。M2.1 曾用 `mcp/local_api.py`（本地
常驻 HTTP，端口 51900 起）兜底；**M5 起面板后端全走线上**，`local_api.py` **已整体删除**
（它 `subprocess` 拉起自身 + import 内核 ⇒ PyInstaller 会把 `src/`+`ortools` 打进 exe，
是打包地雷），本文件里的 local-api helper 也一并删除 —— 主链路与打包链均零调用。

设计要点（照 app.asar 源码挖出的硬约束）：
  - 工具是否「带面板」**完全取决于** tool._meta.ui.resourceUri；不用手写 isApp
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
import sys
import time

SERVER_NAME = "tripplanner"
SERVER_VERSION = "0.5.0-m5"
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    """是不是打包后的可执行文件（PyInstaller 会设 `sys.frozen`）。"""
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
PANEL_VERSION = "m13"
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
    "https://tripagent-planner2.app.workbuddy.host",         # 旧域名（遗留应用，旧代码；仅作 connectDomains 放行）
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
EXPORT_TOOL = "tripplanner_export"

# 面板上「天数」的合理区间；query_days 里有同名常量，这里取值保持一致
MIN_DAYS, MAX_DAYS = 1, 7


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
      ② 本地没有（**分享包**里不会有 secrets.json）⇒ 问云端 `GET /api/cities`
         （它本就回带 `amap_key`）。
    ⚠️ 为什么经后端下发没问题：高德 JSAPI key 是**设计为前端可见**的公开 key
       （它本来就直接写在页面里），所以这不是新增的泄露面；反而带来一个好处 ——
       **换 key 不用重新打包分发**（所有已发出的包自动跟着换）。
    ⚠️ 必须 try/except 包住：这一句跑在 `resources/read` 里，网络抖动**不能**
       让面板打不开（地图缺失是可接受的降级，面板打不开不是）。
    """
    global _AMAP_KEY_CACHE
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
    if not key:
        log("⚠️ 未取得 amap_js_key（面板地图将不可用，其余功能不受影响）")
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
_SESSION_ID_CACHE = None
_REMOTE_CFG_CACHE = None


def remote_config_path() -> str:
    """remote.json 路径 —— 与 `project_root()` 同一套规则（打包后 = exe 同目录）。

    刻意复用 `project_root()` 而不是重复写一遍 frozen 判断：两处漂移过一次就会
    出现「配置读得到、状态写别处」这种极难查的错位。
    """
    return os.path.join(project_root(), REMOTE_JSON_NAME)


def session_id() -> str:
    """面板 ↔ 云端之间那把「当前行程」的钥匙：生成一次后落盘，跨进程/重启稳定。

    与「宿主**按需 spawn** 本进程（每次 pid 都不同）」这条已知事实同一套理由：
    若每次随机，面板手里的那份立刻过期；落盘才能让「谁在用这份行程」跨进程一致。
    （M2.1 的本地 HTTP token 当年也是为此落盘，那套已随 `local_api.py` 一起下线。）
    """
    global _SESSION_ID_CACHE
    if _SESSION_ID_CACHE:
        return _SESSION_ID_CACHE
    # 允许环境变量覆盖（自检用它做**会话隔离**：多个用例共用一台云端时不互相串行程）
    env_sid = (os.environ.get("TRIPPLANNER_SESSION_ID") or "").strip()
    if env_sid:
        _SESSION_ID_CACHE = env_sid
        return env_sid
    path = os.path.join(state_dir(), "mcp_session_id.txt")
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            _SESSION_ID_CACHE = v
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
    _SESSION_ID_CACHE = v
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


def plan_state_path() -> str:
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


def _plan_state_read():
    try:
        with open(plan_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("city"):
            dbg("last_plan.read.hit", city=data.get("city"), days=data.get("days"))
            return data
        dbg("last_plan.read.miss", reason="empty-or-bad")
        return None
    except FileNotFoundError:
        dbg("last_plan.read.miss", reason="no-file")
        return None
    except Exception as e:
        log("last_plan 读盘失败", err=str(e))
        dbg("last_plan.read.miss", reason=str(e))
        return None


def _plan_state_write(value) -> None:
    if value is None:
        return
    try:
        path = plan_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, path)          # 原子替换，避免面板读到半截 JSON
        dbg("last_plan.write.ok", city=value.get("city"),
            days=value.get("days"), bytes=os.path.getsize(path))
    except Exception as e:
        log("last_plan 落盘失败", err=str(e))
        dbg("last_plan.write.fail", err=str(e))


def read_plan_state():
    """当前行程 —— **始终读磁盘**。

    ⚠️ 为什么不用内存缓存（2026-10-09，自检阶段六抓到）：本函数的主要调用方之一是
    **常驻**的 `mcp/local_api.py`。它的进程内存跨请求永不失效，而行程是 **MCP server
    进程**（另一个进程，宿主按需 spawn）写进磁盘的。若这里内存优先，就会出现
    「助手刚生成新行程 → 面板读到的还是上一份旧行程」，且完全不报错。
    磁盘是跨进程的唯一真相，所以读一律走磁盘；写仍是「落盘 + 同步本进程缓存」。
    """
    val = _plan_state_read()
    if val is not None:
        SESSION._last_plan = val
        SESSION._plan_loaded = True
    return val


def write_plan_state(value) -> None:
    """写回当前行程（落盘 + 同步本进程缓存）。"""
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


class _Session:
    """一次「会话」的状态。

    ⚠️ **持久性分两类**（2026-10-09 修正）：
      - **进程级**：`opened_any` / `instances` —— 控制流用，生命周期短，
        放内存即可（宿主按需起进程时它只会「少判一次已开」，不会读到脏数据）。
      - **必须落盘**：`last_plan` —— 「一次调用写、另一次调用读」，而宿主
        按需 spawn 进程（pidSpawned 每次都不同）⇒ 纯内存必空。见 `_plan_state_*`。

    C（兜底）：opened_any  —— 本进程是否已经开过面板。
    A（精确）：instances   —— 面板自报的 appInstanceId 集合（谁在看着这个面板）。
    M1：last_plan          —— 最近一次规划结果，面板 get_plan 拉取（内存优先、磁盘兜底）。
    """

    def __init__(self) -> None:
        self.opened_any = False
        self.instances: set[str] = set()
        self.proposals: int = 0
        self._last_plan = None
        self._plan_loaded = False
        self.pid = os.getpid()

    @property
    def last_plan(self):
        """内存未命中就回磁盘读一次 —— 跨进程取数靠它。"""
        if not self._plan_loaded:
            self._plan_loaded = True
            self._last_plan = _plan_state_read()
            dbg("last_plan.property.load", pid=self.pid, hit=self._last_plan is not None)
        return self._last_plan

    @last_plan.setter
    def last_plan(self, value):
        self._last_plan = value
        self._plan_loaded = True
        _plan_state_write(value)

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

    def should_open_panel(self, instance_id: str | None) -> bool:
        """要不要**新开**一扇面板。

        面板侧已在看（实例已登记）→ 不开。
        本进程已经开过任意一扇    → 不开（C 兜底，防实例 id 拿不到时堆面板）。
        否则                      → 开。
        """
        iid = (instance_id or "").strip()
        if iid and iid in self.instances:
            return False
        if self.opened_any:
            return False
        return True


SESSION = _Session()

TOOLS = [
    {
        "name": PLAN_TOOL,
        "description": (
            "【主入口】按用户的一句话需求生成真实行程，并打开行程面板。\n"
            "例：「杭州2天，带8岁孩子，想看西湖和灵隐寺」。\n"
            "会从需求里自动识别城市与天数（也可显式传 city / days）。\n"
            "规划在云端完成（POI 库 / 交通缓存 / LLM 都在线上），本进程只做代理。\n"
            "⚠️ 面板已打开时再次调用不会新开一扇，只回摘要（A+C 分流）。"
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
                "app_instance_id": {"type": "string", "description": "可选。面板自报的实例 id，用于精确判重"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "_meta": {"ui": {"resourceUri": VIEWER_URI, "visibility": ["model", "app"]}},
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
        "name": OPEN_TOOL,
        "description": (
            "打开 TripPlanner 行程面板（展示最近一次行程；若还没有行程则提示先调 "
            "tripplanner_plan_trip）。注意：面板已打开时再次调用**不会**再开一扇（A+C 分流）。"
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
    # M5：注入**当前行程** —— 冷启动 / 云端会话过期时的兜底渲染源。
    #   ⚠️ JSON 里把 `<` 转义掉，防止行程文本里出现 `</script>` 之类把 HTML 提前截断。
    if PLAN_JSON_PLACEHOLDER in html:
        cur = read_plan_state()
        blob = (json.dumps(cur, ensure_ascii=False).replace("<", "\\u003c")
                if cur else "null")
        html = html.replace(PLAN_JSON_PLACEHOLDER, blob)
        log("panel 行程注入", has_plan=cur is not None)
    return html


# ---------------------------------------------------------------------------
# M1 规划：调内核 → 归一化 → 缓存
# ---------------------------------------------------------------------------
# 一次规划的云端超时。LLM 提案 + 落地 + TOPTW 求解，慢时 60~120s ⇒ 给足余量；
# 超时按「连不上」如实报错，不静默降级。
PLAN_TIMEOUT_S = 240.0


def run_plan(query: str, days: int | None, city_arg: str | None,
             date0: str | None, hotel_text: str | None) -> dict:
    """跑一次真实规划 —— **M5 起整个委托给云端后端**（瘦客户端）。

    本进程不再加载规划内核（ortools / POI 库 / 交通缓存 / DeepSeek Key）：
    全部在云端。分享给别人时对方只需要 Python，连 `data/` 都不必带。
    返回统一结构：{"ok":…, "error":…, "plan":…}（plan 为**已归一化**的行程）。
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
    t0 = time.time()
    try:
        res = remote_request("/api/plan", params=params, timeout=PLAN_TIMEOUT_S)
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

    与内核 `plan_view.apply_day`、面板 `applyDayToPlan` **同一套语义**：
    按 day 号替换该天、按 id 回填 photo / 坐标（云端 /api/replace 回的是内核形状的
    day，没有这些字段）、并重算总里程。三处必须一致，故这里刻意逐条对齐。
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
    lines = [
        f"已生成【{plan.get('city')}】{plan.get('days')} 天行程"
        f"（模式={plan.get('mode')}，候选 {plan.get('candidates')} 个点，"
        f"耗时 {plan.get('latency_s', 0)}s）",
        f"总里程 {plan.get('totalTravelKm')}km ｜ 硬约束违规 {plan.get('totalViolations')}"
        f" ｜ 被剔除 {len(dropped)} 个点",
    ]
    for d in days_list:
        pois = [f"{s.get('start')} {s.get('name')}"
                for s in (d.get("timeline") or []) if s.get("type") == "poi"]
        lines.append(f"Day{d.get('day')}｜{d.get('theme')}｜{d.get('travel_km')}km"
                     f"｜收工 {d.get('finish')}")
        lines.append("   " + " → ".join(pois) if pois else "   （该日无点位）")
    if dropped:
        names = "、".join(p.get("name") or "" for p in dropped[:3])
        lines.append(f"剔除：{names}" + ("等" if len(dropped) > 3 else ""))
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

    if method == "prompts/list":
        return ok(id_, {"prompts": []})

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

        # ---------------- M1：真实规划（带面板） ----------------
        if name == PLAN_TOOL:
            query = (args.get("query") or "").strip()
            if not query:
                return ok(id_, {
                    "content": [{"type": "text", "text": "缺少必填参数 query（用户的行程需求）"}],
                    "structuredContent": {"ok": False, "error": "missing query"},
                })
            iid = args.get("app_instance_id")
            res = run_plan(query, args.get("days"), args.get("city"),
                           args.get("date"), args.get("hotel"))

            if not res.get("ok"):
                # 规划失败也要能开面板把错误讲清楚 —— 面板比一行 JSON 友好
                SESSION.opened_any = True
                if iid:
                    SESSION.instances.add(str(iid).strip())
                log("plan_trip 失败", err=res.get("error"))
                return ok(id_, {
                    "content": [{"type": "text", "text": f"行程生成失败：{res['error']}"}],
                    "structuredContent": {"ok": False, "error": res["error"],
                                          "server": SERVER_NAME, "version": SERVER_VERSION},
                })

            plan = res["plan"]
            # 判重要在「登记本实例」之前算，否则自己刚登记完必然命中「已存在」
            should_open = SESSION.should_open_panel(iid)
            SESSION.last_plan = plan
            SESSION.opened_any = True
            if iid:
                SESSION.instances.add(str(iid).strip())
            log("plan_trip 完成", city=plan["city"], days=plan["days"],
                mode=plan["mode"], km=plan["totalTravelKm"], opened=should_open)
            return ok(id_, {
                "content": [{"type": "text", "text": plan_summary(plan)}],
                "structuredContent": {
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "stage": "M1",
                    "ok": True,
                    "panelOpened": should_open,
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

        # ---------------- M0/M1pre：开面板（复用上次行程） ----------------
        if name == OPEN_TOOL:
            iid = args.get("app_instance_id")
            if SESSION.should_open_panel(iid):
                SESSION.opened_any = True
                if iid:
                    SESSION.instances.add(str(iid).strip())
                log("open_plan: 首次开面板", instance=iid, opened_any=True)
                return ok(id_, {
                    "content": [{"type": "text", "text": "行程面板已打开。"}],
                    "structuredContent": {
                        "server": SERVER_NAME, "version": SERVER_VERSION, "stage": "M1",
                        "panelOpened": True, "appInstanceId": iid or "",
                        "hasPlan": SESSION.last_plan is not None,
                        "plan": SESSION.last_plan,
                    },
                })
            log("open_plan: 面板已存在，回摘要", instance=iid,
                known=len(SESSION.instances), opened_any=SESSION.opened_any)
            return ok(id_, {
                "content": [{
                    "type": "text",
                    "text": ("行程面板**已经打开了**，没有重复开启。"
                             "如需重算行程，调 tripplanner_plan_trip 并说明新需求。"),
                }],
                "structuredContent": {
                    "server": SERVER_NAME, "version": SERVER_VERSION, "stage": "M1",
                    "panelOpened": False, "reason": "panel-already-open",
                    "knownInstances": sorted(SESSION.instances),
                    "hasPlan": SESSION.last_plan is not None,
                    "plan": SESSION.last_plan,
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
