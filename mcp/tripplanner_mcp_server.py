#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TripPlanner MCP App —— M1 主体：真实行程闭环

M0（协议 spike）与 M1-pre（CSP + A+C 分流）已验收，本轮把面板里的假数据换成
真实行程：对话 → 城市识别 → m1_planner 规划内核 → 时间轴面板。

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

⚠️ M2.1（2026-10-09）：面板 → 宿主 → server 的 `tools/call` 通道在本宿主上是**死的**
（宿主有 `rpc.callTool`/`proxyAppToolCall.ok`，但请求从未到达任何 server 进程、
12ms 假成功、structuredContent 被剥）。上面的 replace/move/export 工具保留，
但**面板实际走 `mcp/local_api.py` 开在 127.0.0.1 的本地 HTTP**。
本文件负责「确保那个进程在跑」并把 `端口 + token` 注入面板 HTML。

设计要点（照 app.asar 源码挖出的硬约束）：
  - 工具是否「带面板」**完全取决于** tool._meta.ui.resourceUri；不用手写 isApp
  - resources/read 返回的 HTML 有 256KiB 预取阈值 → 面板必须自包含、不引兄弟资源
  - 宿主用 document.write 注入（非 srcdoc）→ 相对路径会 404，故面板不做任何外部引用
  - 图片走高德直链 store.is.autonavi.com → 必须写进 csp.resourceDomains 才不被静默拦掉
  - stdout 只能输出 JSON-RPC；规划内核若有任何 print 都会让宿主断连 → 规划期整体改道 stderr

用法：由 WorkBuddy 以 stdio 方式拉起，不需要手工运行。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

SERVER_NAME = "tripplanner"
SERVER_VERSION = "0.2.5-m2.3"
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_PATH = os.path.join(HERE, "panel", "plan_panel.html")

# ⚠️ uri 里带版本号是**刻意的**：宿主按 uri 缓存面板 HTML（resolver.resolve.cacheHit），
# 改了 panel/plan_panel.html 却沿用旧 uri，用户看到的仍是旧面板。
# 之后每次改面板 → 把 PANEL_VERSION +1，即可保证宿主重新拉取。
PANEL_VERSION = "m7"
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

UI_RESOURCE_META = {
    "ui": {
        "csp": {
            "frameDomains": ["http://127.0.0.1:*"],
            "connectDomains": ["http://127.0.0.1:*"] + AMAP_DOMAINS,
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
    env_home = os.environ.get("TRIPPLANNER_HOME") or ""
    if env_home and os.path.isdir(env_home):
        return env_home
    return os.path.dirname(HERE)


_KERNEL = None


def kernel():
    """懒加载 TripPlanner 内核（src/）。失败返回 (None, err_msg)。"""
    global _KERNEL
    if _KERNEL is not None:
        return _KERNEL, None
    root = project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from src import config as _config
        # ⚠️ 必须有这一步：load_config() 只返回 dict、不写 os.environ，而 llm_client
        # 惰性只读 os.environ。宿主（WorkBuddy）拉起本进程时 env 里只有 TRIPPLANNER_HOME，
        # 不注入就会「静默降级」到离线链路 —— 0.x 秒跑完、结果看着还挺合理，极难发现。
        injected = _config.apply_env()
        log("config.apply_env 注入", keys=injected)
    except Exception as e:
        log("config.apply_env 失败（将只依赖进程环境）", err=str(e))
    try:
        from src import poi_db, m1_planner, llm_client, query_days, poi_swap  # noqa
        _KERNEL = {"root": root, "poi_db": poi_db, "planner": m1_planner,
                   "llm": llm_client, "query_days": query_days, "poi_swap": poi_swap}
        return _KERNEL, None
    except Exception as e:  # 内核挂了也不能让 server 崩 —— 面板还要能开、能报错
        return None, f"{type(e).__name__}: {e}"


_PHOTO_CACHE = None


def photo_of(poi_id: str) -> str:
    """POI 照片（高德直链）。查不到返回空串 —— 面板侧据此隐藏 img，不留占位框。"""
    global _PHOTO_CACHE
    if not poi_id:
        return ""
    if _PHOTO_CACHE is None:
        path = os.path.join(project_root(), "data", "photo_cache.json")
        try:
            with open(path, encoding="utf-8") as f:
                _PHOTO_CACHE = json.load(f)
        except Exception as e:
            log("photo_cache 不可用", err=str(e))
            _PHOTO_CACHE = {}
    url = _PHOTO_CACHE.get(poi_id) or ""
    return url if isinstance(url, str) and url.startswith("http") else ""


_COORD_CACHE: dict = {}


def poi_coords(city: str) -> dict:
    """{poi_id: (lat, lng)} —— M2 地图需要坐标，而内核 timeline 不提供。

    坐标是 **GCJ-02**（POI 库来源即高德），可直接交给高德 JS API，无需转换。
    按城市缓存：一次规划后的多次换点 / 重排都复用，不必反复解析整个库。
    """
    if not city:
        return {}
    if city in _COORD_CACHE:
        return _COORD_CACHE[city]
    k, err = kernel()
    m: dict = {}
    if err:
        log("poi_coords：内核不可用", err=err)
    else:
        try:
            city_data = k["poi_db"].load_city(city)
            for p in city_data.get("pois", []):
                pp = k["poi_db"].parse_poi(p, city_data)
                if pp.get("id") is not None:
                    m[pp["id"]] = (pp.get("lat"), pp.get("lng"))
        except Exception as e:
            log("poi_coords 加载失败", city=city, err=str(e))
    _COORD_CACHE[city] = m
    return m


def amap_js_key() -> str:
    """从 secrets.json 取**Web 端 JSAPI** key（面板地图用）。

    刻意不放进 panel/*.html 文件（那会随 git 入库）；改由 `read_panel()` 在
    resources/read 时注入占位符 —— key 只在传输时出现，仓库里始终是占位符。
    """
    global _AMAP_KEY_CACHE
    if _AMAP_KEY_CACHE is not None:
        return _AMAP_KEY_CACHE
    key = ""
    try:
        with open(os.path.join(project_root(), "secrets.json"), encoding="utf-8") as f:
            key = str((json.load(f) or {}).get("amap_js_key") or "").strip()
    except Exception as e:
        log("amap_js_key 读取失败（地图将不可用）", err=str(e))
    _AMAP_KEY_CACHE = key
    return key


_AMAP_KEY_CACHE = None


# ---------------------------------------------------------------------------
# 城市：data/<城市>_pois.json
# ---------------------------------------------------------------------------
_CITY_CACHE = None


def city_names() -> list:
    """扫描 data/ 得到可规划城市名（按拼音排序无所谓，稳定即可）。"""
    global _CITY_CACHE
    if _CITY_CACHE is None:
        d = os.path.join(project_root(), "data")
        try:
            _CITY_CACHE = sorted(
                f[: -len("_pois.json")] for f in os.listdir(d) if f.endswith("_pois.json")
            )
        except OSError as e:
            log("data 目录不可用", err=str(e), dir=d)
            _CITY_CACHE = []
    return _CITY_CACHE


def poi_count(city: str) -> int:
    k, err = kernel()
    if err:
        return 0
    try:
        return len(k["poi_db"].load_city(city).get("pois", []))
    except Exception:
        return 0


def resolve_city(city_arg: str | None, query: str) -> tuple:
    """从显式参数或需求文本里定城市。返回 (city_name, note)。

    判据：显式参数优先；否则取「在 query 中出现且最长」的城市名
    —— 最长匹配避免「上海」命中「海上」这类子串误判，也让「六盘水」压过「水」。
    """
    names = city_names()
    if not names:
        return "", "城市库不可用（data 目录为空或内核加载失败）"
    c = (city_arg or "").strip()
    if c:
        if c in names:
            return c, "explicit"
        # 容忍「杭州市」这类带后缀的写法
        for n in names:
            if n in c or c.rstrip("市区域县") in n:
                return n, "explicit-fuzzy"
        return "", f"未知城市：{c}。可用：{'、'.join(names)}"
    q = query or ""
    hits = [n for n in names if n in q]
    if hits:
        best = max(hits, key=len)
        return best, "from-query"
    return "", ("没能从需求里识别出城市。请在需求里写明城市，或传 city 参数。"
                f"当前支持：{'、'.join(names)}")


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
# M2.1：本地 HTTP API（面板 ↔ 服务端的备用通路）
#
# 为什么另起一个常驻进程，而不是在本进程开个线程：
#   宿主对 stdio MCP server 是**按需 spawn**、随时回收（见 _Session 注释里
#   「pidSpawned 每次都不同」）。挂在 server 进程内的 HTTP 会随它一起死，
#   面板下一次点击就 fetch 不到。故由本地 `mcp/local_api.py` detached 常驻，
#   本文件只负责「确保它在跑」并把端口 + token 注入面板 HTML。
# ---------------------------------------------------------------------------
LOCAL_API_BASE_PLACEHOLDER = "__LOCAL_API_BASE__"
LOCAL_API_TOKEN_PLACEHOLDER = "__LOCAL_API_TOKEN__"
LOCAL_API_DEFAULT_PORT = 51900          # 与 local_api.PREFERRED_PORTS 起点一致


def local_api_portfile_path() -> str:
    return os.path.join(state_dir(), "mcp_local_api.json")


def local_api_token_path() -> str:
    return os.path.join(state_dir(), "mcp_local_api_token.txt")


def local_api_token() -> str:
    """本地 API 的**稳定** token：跨进程重启保持不变（首次生成后落盘）。

    ⚠️ 为什么必须稳定（2026-10-09 实测抓到的坑，别改回随机）：
      宿主会连同 MCP server 进程树回收常驻的 `local_api.py`（日志实证：无 kill
      记录却消失，端口文件停在旧 pid，几分钟就换一次）。若 token 每次启动随机，
      面板 HTML 里注入的那份就立刻过期；而 `/api/health` **不校验 token**，
      于是「诊断区测试本地 API = 可用」与「换点/拖拽 403 全灭」同时成立，
      用户看到的就是「诊断说没问题，功能就是不动」。
      token 稳定 + 端口稳定（51900）⇒ 面板即使经历 API 重启也照样能用。
    """
    path = local_api_token_path()
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except Exception:
        pass
    import secrets as _secrets
    t = _secrets.token_hex(16)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(t)
        os.replace(tmp, path)
        dbg("local_api.token.created", chars=len(t))
    except Exception as e:
        dbg("local_api.token.write.fail", err=str(e))
    return t


def _local_api_read_portfile():
    try:
        with open(local_api_portfile_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("port"):
            return d
    except Exception:
        pass
    return None


def _local_api_info(base: str, timeout: float = 0.6):
    """读 /api/health 的 JSON；不通返回 None。health 不校验 token，专供探活。"""
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/health", timeout=timeout) as r:
            if int(getattr(r, "status", 0) or 0) != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _local_api_alive(base: str, timeout: float = 0.6) -> bool:
    return _local_api_info(base, timeout) is not None


def _local_api_kill(pid: int) -> None:
    """干掉一个**确认是我们自己的**旧实例（代码升级后用，见 ensure_local_api）。

    Windows 下 os.kill(pid, SIGTERM) 等价于 TerminateProcess，可直接用。
    """
    if pid <= 0:
        return
    try:
        import signal as _signal
        os.kill(pid, _signal.SIGTERM)
        dbg("local_api.kill", pid=pid, reason="version-mismatch")
    except Exception as e:
        dbg("local_api.kill.fail", pid=pid, err=str(e))


_LOCAL_API_CACHE = {"base": "", "token": "", "checked": 0.0}

# 拉起本地 API 用的「中转 launcher」：自己立刻退出，把真正的服务进程变成孤儿，
# 这样宿主按进程树回收 MCP server 时找不到它。见 ensure_local_api 的注释。
# ⚠️ 第 2 个参数是 stderr 落盘路径：**崩溃现场不能再丢给 DEVNULL**
#    （踩过：进程无声消失，什么都没有，连是崩了还是被杀了都判不出来）。
_SPAWN_LAUNCHER_SRC = (
    "import subprocess,sys\n"
    "fl=int(sys.argv[1]);errp=sys.argv[2];cmd=sys.argv[3:]\n"
    "try:\n"
    "    f=open(errp,'a',encoding='utf-8',errors='replace',buffering=1)\n"
    "except Exception:\n"
    "    f=subprocess.DEVNULL\n"
    "kw=dict(stdin=subprocess.DEVNULL,stdout=f,stderr=f,close_fds=True)\n"
    "try:\n"
    "    subprocess.Popen(cmd,creationflags=fl,**kw)\n"
    "except OSError:\n"
    "    subprocess.Popen(cmd,creationflags=fl & ~0x01000000,**kw)\n"
)

# 落盘用的文件句柄要留住，否则 Python 会把它 GC 掉（Popen 只是复制句柄，
# 父进程这边关掉不影响子进程，但关的时机若不明确就容易查出「stderr 有时有、有时没有」）。
_KEEP_HANDLES: list = []


def local_api_errlog_path() -> str:
    return os.path.join(state_dir(), "mcp_local_api.err.log")


def ensure_local_api(force: bool = False) -> tuple:
    """确保本地 HTTP API 在跑。返回 `(base, token)`；失败返回 `("", "")`。

    幂等且便宜：缓存命中（默认 30s）或 portfile + 健康检查通过时都不重新拉起。

    ⚠️ **版本闸门**：常驻进程会一直活着，若只改代码不 bump `SERVER_VERSION`，
    旧进程会继续用旧逻辑服务面板（表现为「改了没生效」，极难排查）。
    故健康检查里比对 `version`，不一致就杀掉重拉。
    🔴 **改 `tripplanner_mcp_server.py` 或 `local_api.py` 都必须 +1** ——
    `local_api.py` 会 import 本模块取内核，所以本文件的改动同样影响常驻进程。
    """
    now = time.time()
    c = _LOCAL_API_CACHE
    if not force and c["base"] and now - c["checked"] < 30:
        return c["base"], c["token"]

    pf = _local_api_read_portfile()
    if pf:
        base = f"http://127.0.0.1:{int(pf['port'])}"
        info = _local_api_info(base)
        if info and str(info.get("server") or "") == SERVER_NAME:
            if str(info.get("version") or "") == SERVER_VERSION:
                c.update(base=base, token=str(pf.get("token") or ""), checked=now)
                dbg("local_api.reuse", base=base, pid=pf.get("pid"))
                return base, c["token"]
            log("本地 API 版本不符，杀掉旧实例重拉",
                old=info.get("version"), want=SERVER_VERSION)
            _local_api_kill(int(pf.get("pid") or 0))
            try:
                os.remove(local_api_portfile_path())
            except Exception:
                pass
            time.sleep(0.3)          # 等端口释放

    try:
        import subprocess
        script = os.path.join(HERE, "local_api.py")
        env = dict(os.environ)
        env["TRIPPLANNER_HOME"] = project_root()
        try:
            errf = open(local_api_errlog_path(), "a", encoding="utf-8",
                        errors="replace", buffering=1)
            _KEEP_HANDLES.append(errf)
        except Exception:
            errf = subprocess.DEVNULL
        popen_kw = dict(cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                        stdout=errf, stderr=errf, close_fds=True)
        if os.name == "nt":
            # 两层保险，缺一层都会被宿主顺手回收（2026-10-09 实测）：
            #  ① CREATE_BREAKAWAY_FROM_JOB：宿主若用 job object 管 MCP 子进程，
            #     不脱离就会随 job 一起没。job 不允许 breakaway 时 Popen 抛 OSError。
            #  ② **经 launcher 中转**：真正的 API 进程的父进程是一个「立刻退出」的
            #     小 launcher ⇒ 变成孤儿，宿主按「进程树」枚举也找不到它。
            # 失败一律降级并如实落日志（mode），不静默。
            flags_detach = 0x00000008 | 0x00000200          # DETACHED | NEW_GROUP
            flags_break = flags_detach | 0x01000000         # + BREAKAWAY_FROM_JOB
            mode = ""
            for tag, fl in (("launcher+breakaway", flags_break),
                            ("launcher", flags_detach)):
                try:
                    subprocess.Popen([sys.executable, "-c", _SPAWN_LAUNCHER_SRC,
                                      str(fl), local_api_errlog_path(),
                                      sys.executable, script],
                                     creationflags=fl, **popen_kw)
                    mode = tag
                    break
                except OSError as e:
                    dbg("local_api.spawn.try.fail", mode=tag, err=str(e))
            if not mode:
                subprocess.Popen([sys.executable, script],
                                 creationflags=flags_detach, **popen_kw)
                mode = "direct"
        else:
            subprocess.Popen([sys.executable, script], start_new_session=True, **popen_kw)
            mode = "setsid"
        dbg("local_api.spawn", script=script, python=sys.executable, mode=mode)
    except Exception as e:
        log("local_api 拉起失败", err=str(e))
        dbg("local_api.spawn.fail", err=str(e))
        return "", ""

    for _ in range(40):                  # 最多等 ~4s
        time.sleep(0.1)
        pf = _local_api_read_portfile()
        if pf:
            base = f"http://127.0.0.1:{int(pf['port'])}"
            if _local_api_alive(base, timeout=0.4):
                c.update(base=base, token=str(pf.get("token") or ""), checked=now)
                dbg("local_api.ready", base=base, pid=pf.get("pid"))
                return base, c["token"]
    dbg("local_api.timeout")
    log("local_api 启动超时（面板将降级到宿主通道）")
    return "", ""


# ---------------------------------------------------------------------------
# A + C 面板分流状态 + M1 行程缓存
# ---------------------------------------------------------------------------
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
            "数据来自本地 POI 库 + 交通/路线缓存，无需联网。\n"
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
    # M2.1：注入本地 HTTP API 的 base + token（面板交互的唯一可靠通路）。
    # 注入失败就留空串 —— 面板侧会退化为默认端口 / 回落宿主通道并**如实报错**，
    # 绝不静默。
    if LOCAL_API_BASE_PLACEHOLDER in html or LOCAL_API_TOKEN_PLACEHOLDER in html:
        base, _tok = ensure_local_api()
        # 注入失败也要给出**可用**的默认值：端口固定 51900，token 稳定 ⇒ 面板照样能连上。
        # （只注入空串的话，面板只能降级到已经实测不通的宿主通道。）
        # token 一律从「落盘文件」取 —— 常驻进程读的是同一个文件，重启也不换。
        base = base or f"http://127.0.0.1:{LOCAL_API_DEFAULT_PORT}"
        token = local_api_token()
        html = html.replace(LOCAL_API_BASE_PLACEHOLDER, base)
        html = html.replace(LOCAL_API_TOKEN_PLACEHOLDER, token)
        log("panel 本地 API 注入", base=base,
            has_token=bool(token), default_port=LOCAL_API_DEFAULT_PORT)
    return html


# ---------------------------------------------------------------------------
# M1 规划：调内核 → 归一化 → 缓存
# ---------------------------------------------------------------------------
def run_plan(query: str, days: int | None, city_arg: str | None,
             date0: str | None, hotel_text: str | None) -> dict:
    """跑一次真实规划。返回统一结构：{"ok":…, "error":…, "plan":…}。

    stdout 保护：内核里任何 print / 第三方输出都会写脏 stdout 导致宿主断连，
    所以规划期间把 sys.stdout 临时指向 stderr，结束后必还原。
    """
    k, err = kernel()
    if err:
        return {"ok": False, "error": f"规划内核加载失败：{err}"}

    city, note = resolve_city(city_arg, query)
    if not city:
        return {"ok": False, "error": note}

    qd = k["query_days"]
    try:
        n = int(days) if days else int(qd.extract_days(query) or 0)
    except Exception:
        n = 0
    if n <= 0:
        n = int(getattr(qd, "DEFAULT_DAYS", 2) or 2)
    n = max(MIN_DAYS, min(MAX_DAYS, n))

    llm_on = bool(k["llm"].llm_available())
    t0 = time.time()
    real_stdout = sys.stdout
    sys.stdout = sys.stderr          # ← 内核输出一律改道，绝不污染 JSON-RPC 通道
    try:
        city_data = k["poi_db"].load_city(city)
        raw = k["planner"].plan(city_data, query, n,
                                use_llm=llm_on,
                                date0=date0 or None, hotel_text=hotel_text or None)
    except Exception as e:
        return {"ok": False, "error": f"规划失败：{type(e).__name__}: {e}"}
    finally:
        sys.stdout = real_stdout

    plan = normalize_plan(raw, city, note)
    plan["latency_s"] = round(time.time() - t0, 1)
    # 显式回传「LLM 到底用上了没」——离线降级是可用的，但不能静默发生
    plan["llmEnabled"] = llm_on
    plan["degraded"] = bool(llm_on and raw.get("mode") != "llm")
    if plan["degraded"]:
        log("⚠️ 降级：请求用 LLM 但结果 mode 不是 llm", mode=raw.get("mode"))
    return {"ok": True, "plan": plan}


def _normalize_day(d: dict, coords: dict | None = None) -> dict:
    """把内核一天的原始编排压成面板/模型好消化的形状。

    ⚠️ M2 起被 `normalize_plan` 与「换点 / 挪时段」的写回共用 —— 必须**幂等**：
    已归一化过的一天再走一遍，结果要与原来逐字段一致（否则重排后的那天会掉字段）。

    `coords`：{poi_id: (lat, lng)}，用来给 poi 项补坐标（面板地图需要，
    而内核 timeline 只有 id/name/时刻）。
    """
    tl = []
    for s in d.get("timeline", []):
        item = {"type": s.get("type"), "name": s.get("name"),
                "start": s.get("start"), "end": s.get("end")}
        if s.get("type") == "poi":
            item["id"] = s.get("id")
            item["photo"] = photo_of(s.get("id") or "")
            c = (coords or {}).get(s.get("id"))
            if c and c[0] is not None and c[1] is not None:
                item["lat"], item["lng"] = c[0], c[1]
        if s.get("type") == "hop":
            item["min"] = s.get("min")
            item["km"] = s.get("km")
        tl.append(item)
    return {
        "day": d.get("day"),
        "theme": d.get("theme") or "",
        "reason": d.get("reason") or "",
        "weekday": d.get("weekday"),
        "travel_km": round(float(d.get("travel_km") or 0), 1),
        "travel_h": round(float(d.get("travel_h") or 0), 1),
        "finish": d.get("finish"),
        "violations": d.get("violations") or [],
        "dropped": d.get("dropped") or [],
        "reordered": bool(d.get("reordered")),
        "timeline": tl,
    }


def apply_day(plan: dict, day_obj: dict) -> dict:
    """把换点 / 挪时段后的一天写回整个行程，并刷新总里程。

    返回**新的** plan（不改原对象），面板拿到后整体重渲染即可。
    """
    if not isinstance(plan, dict):
        return plan
    nd = _normalize_day(day_obj, poi_coords(plan.get("city") or ""))
    new = dict(plan)
    days = [(nd if d.get("day") == nd.get("day") else d)
            for d in (plan.get("dayList") or [])]
    new["dayList"] = days
    new["totalTravelKm"] = round(sum(float(d.get("travel_km") or 0) for d in days), 1)
    new["updatedAt"] = _now()
    return new


def normalize_plan(raw: dict, city: str, city_note: str) -> dict:
    """把 m1_planner 的原始输出压成「面板 + 模型」都好消化的形状。

    只做两件事：① 补齐面板要展示但内核没给的字段（照片、坐标、POI 分类）；
    ② 裁掉面板用不上的诊断噪音，保证 structuredContent 不臃肿。
    """
    itin = raw.get("itinerary") or {}
    coords = poi_coords(city)
    days = [_normalize_day(d, coords) for d in itin.get("days", [])]

    dropped = []
    for p in itin.get("dropped_pois", []) or []:
        dropped.append({"name": p.get("name"), "reason": p.get("reason")})

    return {
        "city": city,
        "cityFrom": city_note,
        "query": raw.get("query"),
        "days": raw.get("days"),
        "date0": raw.get("date0"),
        "mode": raw.get("mode"),
        "candidates": raw.get("candidates"),
        "hotel": raw.get("hotel"),
        "invalidPoiIds": raw.get("invalid_poi_ids") or [],
        "llmRawViolations": raw.get("llm_raw_violations"),
        "substitutes": itin.get("substitutes") or [],
        "totalViolations": itin.get("total_violations"),
        "totalTravelKm": round(float(itin.get("total_travel_km") or 0), 1),
        "droppedPois": dropped,
        "dayList": days,
        "generatedAt": _now(),
    }


def plan_summary(plan: dict) -> str:
    """给模型/用户看的文字摘要（面板里的结构化内容另走 structuredContent）。"""
    lines = [
        f"已生成【{plan['city']}】{plan['days']} 天行程"
        f"（模式={plan['mode']}，候选 {plan['candidates']} 个点，耗时 {plan['latency_s']}s）",
        f"总里程 {plan['totalTravelKm']}km ｜ 硬约束违规 {plan['totalViolations']}"
        f" ｜ 被剔除 {len(plan['droppedPois'])} 个点",
    ]
    for d in plan["dayList"]:
        pois = [f"{s['start']} {s['name']}" for s in d["timeline"] if s.get("type") == "poi"]
        lines.append(f"Day{d['day']}｜{d['theme']}｜{d['travel_km']}km｜收工 {d['finish']}")
        lines.append("   " + " → ".join(pois) if pois else "   （该日无点位）")
    if plan["droppedPois"]:
        names = "、".join(p["name"] for p in plan["droppedPois"][:3])
        lines.append(f"剔除：{names}" + ("等" if len(plan["droppedPois"]) > 3 else ""))
    return "\n".join(lines)


def _to_min(t):
    """HH:MM → 分钟数；非法返回 None。"""
    p = str(t or "").split(":")
    if len(p) != 2:
        return None
    try:
        return int(p[0]) * 60 + int(p[1])
    except ValueError:
        return None


def _dur_text(a, b) -> str:
    """由 start/end 反推停留时长文本（内核 timeline 只给时刻，不给时长）。"""
    x, y = _to_min(a), _to_min(b)
    if x is None or y is None:
        return ""
    d = y - x
    if d <= 0:
        d += 24 * 60            # 跨零点兜底
    if d <= 0:
        return ""
    hh, mm = divmod(d, 60)
    return (f"{hh} 小时" if hh else "") + (f"{mm} 分" if mm else "")


TYPE_ICON = {"poi": "📍", "hop": "→", "meal": "🍽", "hotel": "🏨"}


def plan_to_markdown(plan: dict) -> str:
    """把行程导出成 Markdown（面板「导出」用；也可直接粘进文档 / 微信 / 笔记）。"""
    md = []
    md.append(f"# 【{plan.get('city', '')}】{plan.get('days', '')} 天行程")
    md.append("")
    mode = str(plan.get("mode") or "")
    mode_txt = "LLM 规划" if mode.startswith("llm") else f"离线兜底（{mode or '未知'}）"
    md.append(f"**需求**：{plan.get('query') or '—'}  ")
    md.append(f"**模式**：{mode_txt} ｜ **总里程**：{plan.get('totalTravelKm')} km ｜ "
              f"**硬约束违规**：{plan.get('totalViolations')} ｜ "
              f"**候选池**：{plan.get('candidates')} 个点  ")
    stamp = str(plan.get("generatedAt") or "")
    if plan.get("updatedAt"):
        stamp += f"（更新于 {plan['updatedAt']}）"
    md.append(f"**生成时间**：{stamp}")
    if plan.get("hotel"):
        h = plan["hotel"]
        md.append(f"**住宿锚点**：{h.get('name')}（{h.get('resolved')}）")
    md.append("")
    md.append("---")

    for d in plan.get("dayList") or []:
        md.append("")
        md.append(f"## Day {d.get('day')} · {d.get('theme') or '未命名主题'}")
        meta = [f"日交通 {d.get('travel_km')} km / {d.get('travel_h')} h",
                f"收工 {d.get('finish') or '—'}"]
        if d.get("weekday"):
            meta.append(str(d["weekday"]))
        md.append("")
        md.append("> " + " ｜ ".join(meta))
        md.append("")
        for s in d.get("timeline") or []:
            t = s.get("type")
            icon = TYPE_ICON.get(t, "·")
            span = f"`{s.get('start') or ''}`"
            if s.get("end"):
                span = f"`{s.get('start')}–{s.get('end')}`"
            name = s.get("name") or ""
            if t == "hop":
                extra = []
                if s.get("km") is not None:
                    extra.append(f"{s['km']} km")
                if s.get("min") is not None:
                    extra.append(f"{s['min']} 分钟")
                md.append(f"- {span} {icon} 前往 {name}"
                          + (f"（{' · '.join(extra)}）" if extra else ""))
            else:
                dur = _dur_text(s.get("start"), s.get("end")) if t in ("poi", "meal") else ""
                md.append(f"- {span} {icon} **{name}**" + (f"（{dur}）" if dur else ""))
        if d.get("reason"):
            md.append("")
            md.append(f"*{d['reason']}*")
        if d.get("violations"):
            md.append("")
            md.append("> ⚠️ 违规：" + "；".join(str(v) for v in d["violations"]))
        if d.get("dropped"):
            md.append("")
            md.append("> ✕ 当天剔除：" + "；".join(
                f"{p.get('name')}（{p.get('reason')}）" for p in d["dropped"]))

    if plan.get("droppedPois"):
        md.append("")
        md.append("---")
        md.append("")
        md.append("## 被剔除的点")
        md.append("")
        for p in plan["droppedPois"]:
            md.append(f"- {p.get('name')} —— {p.get('reason')}")

    md.append("")
    md.append("---")
    md.append("")
    md.append("*由 TripPlanner 生成*")
    return "\n".join(md)


def handle(msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    is_notification = id_ is None
    log("<-", method=method, id=id_)

    # 每次**请求**都顺手确保本地 API 在跑（内部带 30s 缓存，命中时几乎零成本）。
    # 为什么挂在总入口而不是 tools/call 分支：宿主对 stdio server 是**按需 spawn**
    # （`initialize` / `tools/list` / `tools/call` / `resources/read` 任一都会拉起一个
    # 新进程），而常驻的本地 API 会被宿主回收。挂满所有入口 ⇒ 宿主每碰一次 MCP，
    # 本地服务就被复活一次，恢复窗口最小。见 `local_api.py` 顶部「死因取证」。
    if method and not str(method).startswith("notifications/"):
        try:
            ensure_local_api()
        except Exception as e:                                  # noqa: BLE001
            dbg("ensure_local_api.fail", method=method, err=repr(e))

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

        # ---------------- M1：列城市 ----------------
        if name == LIST_CITIES_TOOL:
            names = city_names()
            items = [{"city": c, "pois": poi_count(c)} for c in names]
            txt = "、".join(f"{i['city']}({i['pois']})" for i in items) or "（无可用城市数据）"
            return ok(id_, {
                "content": [{"type": "text", "text": f"可规划 {len(items)} 个城市：{txt}"}],
                "structuredContent": {"count": len(items), "cities": items},
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
            k, kerr = kernel()
            if kerr:
                return ok(id_, tool_error(f"规划内核加载失败：{kerr}"))
            _code, data = k["poi_swap"].replace_poi(
                city, poi_id, day, day_ids,
                used_ids=args.get("used_ids") or [],
                date0=args.get("date0"),
                query=args.get("query") or "",
                hotel_text=args.get("hotel_text"))
            if not data.get("ok"):
                log("replace_poi 失败", city=city, day=day, err=data.get("error"))
                return ok(id_, tool_error(data.get("error") or "换点失败"))
            plan = apply_day(SESSION.last_plan, data["day"])
            SESSION.last_plan = plan
            sw = data["swap"]
            log("replace_poi 成功", city=city, day=day, frm=sw["from"], to=sw["to"])
            return ok(id_, {
                "content": [{"type": "text",
                             "text": f"已换点：{sw['from']} → {sw['to']}"
                                     + ("（同类目）" if sw.get("same_category") else "（就近）")}],
                "structuredContent": {"ok": True, "swap": sw,
                                      "day": data["day"], "plan": plan,
                                      "server": SERVER_NAME},
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
            k, kerr = kernel()
            if kerr:
                return ok(id_, tool_error(f"规划内核加载失败：{kerr}"))
            _code, data = k["poi_swap"].reorder_day(
                city, day, ordered,
                date0=args.get("date0"),
                query=args.get("query") or "",
                hotel_text=args.get("hotel_text"))
            if not data.get("ok"):
                log("reorder_day 失败", city=city, day=day, err=data.get("error"))
                return ok(id_, tool_error(data.get("error") or "调整顺序失败"))
            plan = apply_day(SESSION.last_plan, data["day"])
            SESSION.last_plan = plan
            respected = bool(data.get("order_respected"))
            log("reorder_day 成功", city=city, day=day, respected=respected)
            return ok(id_, {
                "content": [{"type": "text",
                             "text": "顺序已更新。" if respected
                                     else "顺序已更新（部分受开放时间/餐窗约束微调）。"}],
                "structuredContent": {"ok": True, "order_respected": respected,
                                      "day": data["day"], "plan": plan,
                                      "server": SERVER_NAME},
            })

        # ---------------- M2：导出 Markdown ----------------
        if name == EXPORT_TOOL:
            plan = SESSION.last_plan
            if not plan:
                return ok(id_, tool_error("还没有行程可导出，请先生成行程"))
            fmt = str(args.get("format") or "markdown").lower()
            if fmt not in ("markdown", "md"):
                return ok(id_, tool_error(f"暂不支持的导出格式：{fmt}（目前仅 markdown）"))
            text = plan_to_markdown(plan)
            fname = f"TripPlanner-{plan.get('city') or 'trip'}-{plan.get('days') or ''}天.md"
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
