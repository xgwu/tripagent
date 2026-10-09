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
SERVER_VERSION = "0.1.0-m1"
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_PATH = os.path.join(HERE, "panel", "plan_panel.html")

# ⚠️ uri 里带版本号是**刻意的**：宿主按 uri 缓存面板 HTML（resolver.resolve.cacheHit），
# 改了 panel/plan_panel.html 却沿用旧 uri，用户看到的仍是旧面板。
# 之后每次改面板 → 把 PANEL_VERSION +1，即可保证宿主重新拉取。
PANEL_VERSION = "m1"
VIEWER_URI = f"ui://tripplanner/plan-{PANEL_VERSION}"
UI_MIME = "text/html;profile=mcp-app"

# 宿主侧读取的就是这一段（_meta.ui.csp.*）—— 缺了它 iframe 会被静默拦掉
# resourceDomains 必须放行高德图片域名，否则 <img src="https://store.is.autonavi.com/...">
# 会被宿主的两层 CSP（HTTP 头 + 注入 meta）静默拦掉 —— 不报错，只是图不显示。
AMAP_IMG_DOMAINS = [
    "https://store.is.autonavi.com",
    "https://store.is.autonavi.com:*",
    "https://*.amap.com",
    "https://*.autonavi.com",
]

UI_RESOURCE_META = {
    "ui": {
        "csp": {
            "frameDomains": ["http://127.0.0.1:*"],
            "connectDomains": ["http://127.0.0.1:*"],
            "resourceDomains": ["http://127.0.0.1:*"] + AMAP_IMG_DOMAINS,
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
        from src import poi_db, m1_planner, llm_client, query_days  # noqa
        _KERNEL = {"root": root, "poi_db": poi_db, "planner": m1_planner,
                   "llm": llm_client, "query_days": query_days}
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
# A + C 面板分流状态 + M1 行程缓存
# ---------------------------------------------------------------------------
class _Session:
    """本 server 进程内的一次「会话」状态。stdio server 由宿主按需拉起，
    一个进程对应宿主侧的一个连接；进程重启即重置，正合语义。

    C（兜底）：opened_any  —— 本进程是否已经开过面板。
    A（精确）：instances   —— 面板自报的 appInstanceId 集合（谁在看着这个面板）。
    M1：last_plan          —— 最近一次规划结果，供面板 get_plan 拉取。
    """

    def __init__(self) -> None:
        self.opened_any = False
        self.instances: set[str] = set()
        self.proposals: int = 0
        self.last_plan: dict | None = None

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


def send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def ok(id_, res):
    return {"jsonrpc": "2.0", "id": id_, "result": res}


def fail(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def read_panel() -> str:
    try:
        with open(PANEL_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log("panel read failed", err=str(e), path=PANEL_PATH)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'></head>"
            "<body style=\"font-family:system-ui;padding:20px\">"
            "<h3>TripPlanner 面板文件读取失败</h3>"
            f"<pre>{e}</pre><p>路径：{PANEL_PATH}</p></body></html>"
        )


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


def normalize_plan(raw: dict, city: str, city_note: str) -> dict:
    """把 m1_planner 的原始输出压成「面板 + 模型」都好消化的形状。

    只做两件事：① 补齐面板要展示但内核没给的字段（照片、POI 分类）；
    ② 裁掉面板用不上的诊断噪音，保证 structuredContent 不臃肿。
    """
    itin = raw.get("itinerary") or {}
    days = []
    for d in itin.get("days", []):
        tl = []
        for s in d.get("timeline", []):
            item = {"type": s.get("type"), "name": s.get("name"),
                    "start": s.get("start"), "end": s.get("end")}
            if s.get("type") == "poi":
                item["id"] = s.get("id")
                item["photo"] = photo_of(s.get("id") or "")
            if s.get("type") == "hop":
                item["min"] = s.get("min")
                item["km"] = s.get("km")
            tl.append(item)
        days.append({
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
        })

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


def handle(msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    is_notification = id_ is None
    log("<-", method=method, id=id_)

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
        uri = params.get("uri")
        if uri != VIEWER_URI:
            return fail(id_, -32602, f"Resource not found: {uri}")
        text = read_panel()
        log("resources/read", uri=uri, html_bytes=len(text.encode("utf-8")))
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
