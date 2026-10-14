# -*- coding: utf-8 -*-
"""TripAgent Web UI 原型：纯 stdlib HTTP 服务（零第三方依赖）。

路由：
  GET /                 → webui/index.html（前端单页）
  GET /api/cities       → 31 城元信息（POI 数、闭馆数、坐标）+ LLM 可用性
  GET /api/pois         → POI 发现（Phase 0：city/keyword/category/limit → 库内真实 POI 清单）
  GET /api/plan         → 规划（city/query/days/date/hotel/mode/llm），包装 m1/m2 链路

启动：python webui/server.py [port]   （默认 8765，绑定 127.0.0.1）
"""
import collections, hashlib, io, json, os, re, sys, threading, time, urllib.parse, uuid
from datetime import date as _date, timedelta as _td
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 控制台 UTF-8（Windows 默认 GBK 会让中文 print 直接 UnicodeEncodeError）。
# 用 reconfigure（原地改编码）而非新建 TextIOWrapper——后者在同一进程里被包装两次时，
# 前一个 wrapper 失去引用即 __del__ 关闭底层 buffer，后续 print 报
# 「I/O operation on closed file」（2026-09-15 报障 18 写 plan_multi 单测时实测：
# 测试脚本先包装、再 `from webui import server` 二次包装 → 第一行 print 即崩）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # 非标准 stdout（被替换为非 TextIOWrapper）时退回旧写法
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src import poi_db, m1_planner, llm_client, poi_swap  # m2_planner 懒加载：云端 ortools 缺失也不阻塞启动
from src import gap_log  # POI 库缺口台账：规划缺口持久化（旁路，失败不拖垮主链路）
from src import intercity  # 城际转移段：跨城驾车时长（travel_cache 不建跨城对）
# M3：面板交互（换点 / 挪时段 / 导出）的**远端兜底**端点共用内核。
# 与本地 MCP server 是**同一份实现**（src/ 单一真相），避免两处漂移。
from src import plan_view  # 行程视图：归一化 / 写回
from src import md_export  # 行程 → Markdown

# M5.x 原型：云端 MCP 端点（`POST /mcp`，Streamable HTTP）——把本服务的 /api/* 薄包一层
# MCP 协议，让宿主「自定义 MCP」用**远端 URL** 直连（零安装分享的验证件）。
# 按**文件路径**加载：`python webui/server.py`（sys.path[0]=webui/）与
# `from webui import server`（webui/ 不在 path）两种入口都能工作，也不污染 sys.path。
import importlib.util as _ilu
_mcp_ep_spec = _ilu.spec_from_file_location(
    "webui_mcp_endpoint", os.path.join(ROOT, "webui", "mcp_endpoint.py"))
mcp_endpoint = _ilu.module_from_spec(_mcp_ep_spec)
_mcp_ep_spec.loader.exec_module(mcp_endpoint)

# ---- 直连 opener：本机代理会拦截外网 API，urllib 需显式绕过 ----
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 合并配置：main() 启动时用 load_config() 覆盖。此处给模块级默认空 dict——
# 否则 CFG 只在 main() 里由 `global CFG` 创建，任何**不经启动**就调用规划函数的
# 路径（单测、脚本直接 import plan_multi）都会 NameError。
CFG: dict = {}

# 本服务的版本标识 —— `GET /api/health` 把它回给面板，用于明确「线上跑的是哪一版」。
# m4（2026-10-10）：补 CORS（面板跨域 fetch 远端后端的前提）+ OPTIONS 预检；
#   云主机接入 M3 远端后端（HTTPS + nginx Basic 认证）。
# m6（2026-10-10）：新增 `POST /mcp`（云端 MCP 端点，Streamable HTTP）——
#   把 /api/* 薄包一层 MCP 协议，供宿主「自定义 MCP」以远端 URL 直连（零安装分享原型）。
#   ⚠️ `mcp/selftest.py` 会比对「云端 /api/health 的 version」与源码 ⇒ 改这里必须同步部署。
# m8（2026-10-10）：多城联游「贵州5日自驾游」面板地图不渲染 —— 根因＝`plan.city` 在
#   多城路径下是 `"+".join(cities)`，`poi_coords` / `/api/replace|move` 却按单城名用
#   ⇒ 归一化补不上坐标、换点/拖拽整条 400。修 `plan_view.poi_coords`（按 + 拆开合并）
#   + 新增 `_day_city`（用 poi id 前缀定位生效城市）。`src/plan_view.py` 云端也在跑 ⇒ 必须部署。
# m9（2026-10-11）：Phase 0 —— 新增 `GET /api/pois`（POI 发现，只读）。它是 MCP 侧新工具
#   `tripplanner_find_pois` 的后端：让**调用方 agent** 在写提案前能查到「库内真实有哪些点」，
#   而不是凭世界知识编名字（见 docs/MCP-Agent提案外包-方案探讨.md §5）。
#   拆在云端而不是客户端读库，因为 M5 起 MCP 是**瘦客户端**（分享包里没有 data/）。
WEBUI_VERSION = "m13"

# ---- 路径规划缓存（P1-1：高德骑行/步行实际路网） ----
ROUTE_CACHE_PATH = os.path.join(ROOT, "data", "route_cache.json")
_route_lock = threading.Lock()
_route_cache = None
SHARES_DIR = os.path.join(ROOT, "data", "shares")

# ---- P0-3 LLM 规划结果缓存（同 query+city+days+date0+hotel+mode 7 天内秒回） ----
LLM_CACHE_PATH = os.path.join(ROOT, "data", "llm_cache.json")
LLM_CACHE_TTL_S = 7 * 86400
_llm_cache: dict | None = None


def _load_llm_cache() -> dict:
    global _llm_cache
    if _llm_cache is None:
        try:
            with open(LLM_CACHE_PATH, encoding="utf-8") as f:
                _llm_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _llm_cache = {}
    return _llm_cache


def _llm_cache_key(cname: str, query: str, days: int, date0: str | None,
                   hotel_text: str | None, mode: str) -> str:
    raw = "|".join([cname, query, str(days), date0 or "", hotel_text or "", mode])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _llm_cache_get(key: str):
    """命中且未过期返回结果副本（打上 cache_hit），否则 None。"""
    with PLAN_LOCK:
        ent = _load_llm_cache().get(key)
    if not ent or time.time() - ent.get("ts", 0) > LLM_CACHE_TTL_S:
        return None
    r = json.loads(json.dumps(ent["result"], ensure_ascii=False))  # 深拷贝防调用方改写
    r["cache_hit"] = True
    return r


def _llm_cache_put(key: str, result: dict) -> None:
    with PLAN_LOCK:
        c = _load_llm_cache()
        c[key] = {"ts": time.time(), "result": result}
        # 只保留最近 300 条，防无限膨胀
        if len(c) > 300:
            for k in sorted(c, key=lambda k: c[k]["ts"])[: len(c) - 300]:
                c.pop(k, None)
        tmp = LLM_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(tmp, LLM_CACHE_PATH)

# ---- POI 实拍照片（P3：高德 place/text photos 字段，磁盘缓存，302 跳转图库直链） ----
PHOTO_CACHE_PATH = os.path.join(ROOT, "data", "photo_cache.json")
_photo_lock = threading.Lock()
_photo_cache = None
_ID_PREFIX_CITY = {"SH": "上海", "HZ": "杭州", "NJ": "南京", "SZ": "苏州", "WH": "武汉", "BJ": "北京", "CD": "成都", "GZ": "广州", "YC": "盐城",
                   "XA": "西安", "CQ": "重庆", "CS": "长沙", "GY": "贵阳", "HG": "黄果树", "XY": "兴义", "WF": "万峰林", "XQ": "小七孔", "LP": "六盘水", "KL": "凯里", "ZG": "镇远古城", "ZY": "遵义", "XJ": "西江千户苗寨", "CH": "赤水", "AS": "安顺", "TR": "铜仁", "FJ": "梵净山", "TY": "中国天眼", "ZJ": "织金洞", "DY": "都匀", "ZX": "肇兴侗寨", "BL": "百里杜鹃", "KN": "喀纳斯", "WL": "乌鲁木齐", "YL": "伊犁", "NL": "那拉提", "TL": "吐鲁番", "KM": "克拉玛依"}
_CITYCODE = {"上海": "021", "杭州": "0571", "南京": "025", "苏州": "0512", "武汉": "027", "北京": "010", "成都": "028", "广州": "020", "盐城": "0515",
             "西安": "029", "重庆": "023", "长沙": "0731", "贵阳": "0851", "黄果树": "0853", "兴义": "0859", "万峰林": "0859", "小七孔": "0854", "六盘水": "0858", "凯里": "0855", "镇远古城": "0855", "遵义": "0852", "西江千户苗寨": "0855", "赤水": "0852", "安顺": "0853", "铜仁": "0856", "梵净山": "0856", "中国天眼": "0854", "织金洞": "0857", "都匀": "0854", "肇兴侗寨": "0855", "百里杜鹃": "0857", "喀纳斯": "0906", "乌鲁木齐": "0991", "伊犁": "0999", "那拉提": "0999", "吐鲁番": "0995", "克拉玛依": "0990"}


def _day_city(city_field: str, poi_ids) -> str | None:
    """解析「换点 / 挪时段」真正作用于的**那一个**城市（多城联游支持）。

    `plan.city` 在多城联游时是 `"+".join(cities)`（见 `plan_multi` 与 `plan_view.split_cities`），
    而这两个动作只改**某一天** —— 那天必然属于其中某一城：
      · 单城：`city_field` 本身就是城名 → 原样返回；
      · 多城：用该天 POI 的 id 前缀定位（`_ID_PREFIX_CITY`，与 `/api/photo` **同一份**注册表）；
      · 解析不出 → `None`（**不猜**：宁可 400，也不要静默换成别的城的点）。

    为什么必须解析出**单城**：`poi_swap.replace_poi(city, …)` 要 `poi_db.load_city(city)`
    才能拿到候选池，传拼接串 ⇒ `load_city` 抛错 / 被换点 404；`/api/move` 同理。
    2026-10-10 报障（「贵州5日自驾游」地图不渲染）顺带暴露了这条：`cname not in CITIES`
    会让多城行程的「🔁 换一个」与拖拽**整条 400**。
    前端 `webui/index.html::swapPoi` 用「主题前缀」做了同一件事，两者互为兜底。

    🔴 **优先级：POI id 说了算**（它才决定 `poi_id` 存在哪座城的库里）。`city_field` 只在
    id 解析不出时兜底 —— 否则 agent 传一个「合法但不对」的单城名（多城行程里只说了一座城）
    会让 `replace_poi` 落到 404「POI 不存在」，比 400 更难查。
    """
    cf = (city_field or "").strip()
    for pid in poi_ids or []:
        c = _ID_PREFIX_CITY.get(str(pid)[:2].upper())
        if c:
            return c
    return cf if cf in CITIES else None


def _load_photo_cache() -> dict:
    global _photo_cache
    if _photo_cache is None:
        try:
            with open(PHOTO_CACHE_PATH, encoding="utf-8") as f:
                _photo_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _photo_cache = {}
    return _photo_cache


def _save_photo_cache():
    global _photo_cache
    try:
        with open(PHOTO_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_photo_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _amap_photo(name: str, city: str, key: str) -> str | None:
    """高德 place/text（extensions=all）取该 POI 实拍图第一张，http 统一升级 https。"""
    url = (f"https://restapi.amap.com/v3/place/text"
           f"?keywords={urllib.parse.quote(name)}&city={_CITYCODE.get(city, '')}"
           f"&key={key}&extensions=all&offset=1&page=1")
    with _NO_PROXY_OPENER.open(url, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("status") != "1":
        return None
    for poi in data.get("pois") or []:
        for ph in poi.get("photos") or []:
            link = ph.get("url") or ""
            if link.startswith("http://"):
                link = "https://" + link[7:]
            if link.startswith("https://"):
                return link
    return None


def _load_route_cache() -> dict:
    global _route_cache
    if _route_cache is None:
        try:
            with open(ROUTE_CACHE_PATH, encoding="utf-8") as f:
                _route_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _route_cache = {}
    return _route_cache


def _norm_ll(s: str) -> str:
    """坐标按 4 位小数归一化（≈11m），提高缓存命中率。"""
    try:
        lng, lat = (float(x) for x in s.split(","))
        return f"{round(lng, 4):.4f},{round(lat, 4):.4f}"
    except (ValueError, AttributeError):
        return s


def _amap_route(mode: str, o: str, d: str, key: str) -> dict | None:
    """高德路径规划：walking(v3) / riding(v4) / driving(v3)。

    driving 是 2026-09-17 新增：跨城转移段（如杭州→苏州 153km）此前无法画真实路线
    —— 前端只有 riding/walking 两档，城际腿只能退化成直线甚至完全不画。
    返回 {distance_m, duration_s, points}。
    """
    if mode == "riding":
        url = (f"https://restapi.amap.com/v4/direction/bicycling"
               f"?origin={o}&destination={d}&key={key}")
    elif mode == "driving":
        # strategy=0 速度优先，与 intercity 建缓存口径一致
        url = (f"https://restapi.amap.com/v3/direction/driving"
               f"?origin={o}&destination={d}&extensions=all&strategy=0&key={key}")
    else:
        url = (f"https://restapi.amap.com/v3/direction/walking"
               f"?origin={o}&destination={d}&key={key}")
    with _NO_PROXY_OPENER.open(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if mode == "riding":
        if data.get("errcode") != 0:
            return None
        paths = (data.get("data") or {}).get("paths") or []
    else:
        if data.get("status") != "1":
            return None
        paths = (data.get("route") or {}).get("paths") or []
    path = paths[0] if paths else None
    if not path:
        return None
    points = []
    for step in path.get("steps") or []:
        for pair in (step.get("polyline") or "").split(";"):
            if not pair or "," not in pair:
                continue
            lng, _, lat = pair.partition(",")
            try:
                points.append([round(float(lng), 6), round(float(lat), 6)])
            except ValueError:
                continue
    if len(points) < 2:
        return None
    return {"distance_m": int(float(path.get("distance") or 0)),
            "duration_s": int(float(path.get("duration") or 0)),
            "points": points}

# 天数提取公共化：实现移至 src/query_days.py（CLI main.py 共用同一实现）
from src.query_days import (extract_days, clamp_days, days_cap_for, MAX_DAYS,  # noqa: E402
                            DAYS_CAP_MULTI_CITY, DEFAULT_DAYS)


# P2-2 多城联游：查询中出现 ≥2 个城市（或「苏杭」类别名）→ 跨城规划
CITY_ALIAS = {"苏杭": ["苏州", "杭州"], "杭苏": ["杭州", "苏州"]}


def detect_cities(query: str) -> list:
    """P4 纯自然语言：识别需求里提到的城市（含「苏杭」类别名），单城也返回。

    多城时按用户提及顺序排列（如「苏州和杭州」→ ['苏州','杭州']）。
    """
    found = [c for c in CITIES if c in (query or "")]
    if found:
        return sorted(found, key=lambda c: (query or "").index(c))
    for alias, pair in CITY_ALIAS.items():
        if alias in (query or "") and not any(c in query for c in pair):
            return pair
    return []


def detect_multi_city(query: str) -> list:
    found = detect_cities(query)
    return found if len(found) >= 2 else []


# ---- 目的地解析（2026-09-21）：把「区域/省份」也解析成受支持的目的地 ----
# 背景：原先只有两个出口——精确命中白名单，否则回落 DEFAULT_CITY。于是
#   ① 「贵州5日自驾游」→ 400 暂不支持（用户可见，但功能缺失）
#   ② 「川西4日游」  → **静默排成上海**（更糟：不报错、还标 from_query=true）
# 解析层把「目的地」与「城市名」解耦：省级/区域需求交给 LLM 从受支持清单里挑，
# 挑不出就明确报错，绝不回落默认城市。
_DEST_META: list | None = None


def dest_meta() -> list:
    """[(city, province, n_pois)]，按 CITIES 顺序；只取三个字段，不加载整城 POI。"""
    global _DEST_META
    if _DEST_META is None:
        out = []
        for c in CITIES:
            try:
                with open(os.path.join(ROOT, "data", f"{c}_pois.json"),
                          encoding="utf-8") as f:
                    d = json.load(f)
                out.append((c, (d.get("province") or "").strip(), len(d.get("pois") or [])))
            except (OSError, ValueError):
                out.append((c, "", 0))
        _DEST_META = out
    return _DEST_META


# 离线路径的「地名」判据：出现这些后缀/大区词却解析不出目的地 → 明确报错，
# 绝不回落默认城市（实测「川西4日游」曾被静默排成上海）
_PLACE_SUFFIX_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}(?:省|自治区|自治州|地区)")
_REGION_WORDS = ("黔东南", "黔西南", "黔南", "黔北", "川西", "川南", "川北",
                 "江南", "华东", "华南", "华中", "华北", "西北", "西南", "东北",
                 "中原", "岭南", "闽南", "湘西", "赣南", "皖南", "苏北", "苏南",
                 "浙南", "浙北", "塞北", "坝上", "滇西")


def look_place_like(query: str) -> str:
    """需求里提到的「地名片段」（但我们解析不出受支持目的地）。没有则返回 ""。"""
    q = query or ""
    m = _PLACE_SUFFIX_RE.search(q)
    if m:
        return m.group(0)
    for w in _REGION_WORDS:
        if w in q:
            return w
    return ""


def _dest_capacity() -> dict:
    """{city: n_pois} —— 降级路径按库容挑目的地时用。"""
    return {c: n for c, _p, n in dest_meta()}


# 大区词 → 所属省：仅用于「不支持该区域」的报错里给替代建议（川西 → 四川 → 成都）
_REGION_PROVINCE = {"川西": "四川", "川南": "四川", "川北": "四川",
                    "黔东南": "贵州", "黔西南": "贵州", "黔南": "贵州", "黔北": "贵州",
                    "苏北": "江苏", "苏南": "江苏", "浙南": "浙江", "浙北": "浙江",
                    "湘西": "湖南"}


def suggest_for_region(unsupported: str) -> list:
    """该地名若属于我们已覆盖的省份，返回该省受支持目的地（用于报错里的替代建议）。"""
    u = unsupported or ""
    if not u:
        return []
    prov = _REGION_PROVINCE.get(u, "")
    if not prov:
        for p in {p for _c, p, _n in dest_meta() if p}:
            if p in u or u in p:
                prov = p
                break
    if not prov:
        return []
    return [c for c, p, _n in dest_meta() if p == prov][:6]


def resolve_destinations(query: str, nl: dict | None, days: int) -> dict:
    """把需求解析成受支持目的地列表。返回 {dests, method, unsupported}。

    四级（越靠前越确定）：
      ① explicit —— 需求里直接写了受支持城市/景区名（子串命中，不花 LLM）
      ② llm      —— LLM 从受支持清单里挑（含省份/区域需求，如「贵州5日自驾」）
      ③ province —— LLM 不可用：按需求里出现的省份机械展开（降级，会出 notice）
      ④ none     —— 解析不出，由调用方决定报错还是用默认城市
    """
    found = detect_cities(query)
    if found:
        return {"dests": found, "method": "explicit", "unsupported": ""}
    nl = nl or {}
    cap = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    picked = [c for c in (nl.get("destinations") or []) if c in CITIES]
    if not picked:  # 兼容既有字段（P5 起就有，不能丢）
        picked = [c for c in (nl.get("multi_cities") or []) if c in CITIES]
    seen, uniq = set(), []
    for c in picked:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    if uniq:
        return {"dests": uniq[:cap], "method": "llm", "unsupported": ""}
    provs = {p for _c, p, _n in dest_meta() if p}
    hit = [p for p in sorted(provs, key=len, reverse=True) if p in (query or "")]
    if hit:
        cap_n = _dest_capacity()
        want = [c for c, p, _n in dest_meta() if p in hit]
        want.sort(key=lambda c: -cap_n.get(c, 0))
        # 降级路径宁少勿滥：LLM 能考虑城际驾车时间挑 2~4 个，机械展开没这个判断力，
        # 取库容最大的前几个（至少 1 个），并靠 notice 明确告知这是降级结果
        return {"dests": want[:max(1, min(cap, 3))], "method": "province", "unsupported": ""}
    return {"dests": [], "method": "none",
            "unsupported": (nl.get("unsupported_region") or "").strip()
            or look_place_like(query)}


def plan_route_decision(query: str, nl: dict | None, days: int,
                        city_param: str = "") -> dict:
    """「这次规划去哪里」的决策——集中成一个**纯函数**，便于单测。

    返回 {multi, cname, notice, error}：multi 非空（≥2）走城际；cname 有效走单城；
    error 非空时调用方直接 400。

    ⚠️ 额外带 `defaulted`（仅「需求里**完全未提**地名 ⇒ 落默认城市」那一条分支为 True）：
    供 `?strict_city=1` 用 —— MCP App 是 **agent 面向**的，识别不出城市应当明确报错；
    而 Web UI 是对话式输入，写「随便玩玩」时落默认城市是既有体验。两种语义都要留，
    故由调用方显式选择，而不是改掉任何一边。

    ⚠️ 为什么必须集中：解析分支（显式 / LLM / 省份降级 / 报错）与「单城校验」耦合在
    HTTP handler 里时，多城路径会把 cname 留成空串，随后被 `cname not in CITIES`
    误判成 400「未能识别」——2026-09-21 实测「苏杭3日游」（别名多城）整条链路被打回，
    「贵州5日自驾游」同样中招。抽成纯函数后这类接线错能被单测拦住。
    """
    _unknown = (f"未能识别目的地城市（当前支持：{'、'.join(CITIES)}）。"
                "试试在需求里写明，如「杭州3天…」")
    city_param = (city_param or "").strip()
    if city_param and city_param not in CITIES:
        return {"multi": [], "cname": "", "notice": [], "error": _unknown}
    if city_param:   # 显式 ?city= 参数优先，但查询本身是多城时仍走城际（既有行为）
        return {"multi": detect_multi_city(query), "cname": city_param,
                "notice": [], "error": None}
    res = resolve_destinations(query, nl, days)
    dests = res["dests"]
    notice = []
    if res["method"] == "province":
        notice.append({
            "kind": "destinations_degraded",
            "message": (f"LLM 未给出目的地，已按省份机械展开为{'、'.join(dests)}"
                        "（仅按库容挑，未考虑行程动线），可自行指定想去的地方")})
    if len(dests) >= 2:
        return {"multi": dests, "cname": "", "notice": notice, "error": None}
    if len(dests) == 1:
        return {"multi": [], "cname": dests[0], "notice": notice, "error": None}
    if res["unsupported"]:
        _alt = suggest_for_region(res["unsupported"])
        _tip = (f"；我们支持同在{_REGION_PROVINCE.get(res['unsupported'], '该省')}的"
                f"{'、'.join(_alt)}" if _alt else "")
        return {"multi": [], "cname": "", "notice": notice,
                "error": (f"暂不支持目的地「{res['unsupported']}」{_tip}。"
                          f"当前支持：{'、'.join(CITIES)}")}
    # 需求里完全未提地名 → 保持既有默认城市行为（不得扩展到「提了但解析不出」的情形）
    # `defaulted=True` 专门标出这一条：`?strict_city=1` 的调用方（MCP App）据此报错。
    return {"multi": [], "cname": DEFAULT_CITY, "notice": notice, "error": None,
            "defaulted": True}


# P4 纯自然语言交互：目的地城市/出发日期/住宿锚点均可从需求文字提取
DEFAULT_CITY = "上海"
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def extract_date(query: str, today: _date | None = None) -> str | None:
    """从自然语言提取出发日期 → ISO（YYYY-MM-DD）。提不到返回 None。

    支持：「2026-10-01」「10月1日/号」「明天/后天/大后天」「下周六」「周六」「这周末」。
    """
    q = (query or "").strip()
    if not q:
        return None
    today = today or _date.today()
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", q)
    if m:  # 完整 ISO / 「2026年10月1日」
        try:
            return _date(*map(int, m.groups())).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", q)
    if m:  # 「9月20日」：已过去的月日视为明年
        try:
            dt = _date(today.year, int(m.group(1)), int(m.group(2)))
            if dt < today:
                dt = _date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return dt.isoformat()
        except ValueError:
            pass
    m = re.search(r"(大后天|后天|明天|今天|今晚)", q)
    if m:
        off = {"今天": 0, "今晚": 0, "明天": 1, "后天": 2, "大后天": 3}[m.group(1)]
        return (today + _td(days=off)).isoformat()
    # 「下周六」「下下周三」「周六」——排除「玩一周」这类时长表述
    m = re.search(r"(?<![\d一两])(?:(?:(下下|下|本|这)(?:周|星期))|(?:周|星期))([一二三四五六日天])(?!末)", q)
    if m:
        wd = _WEEKDAY[m.group(2)]
        if m.group(1) == "下":
            nxt = today + _td(days=7 - today.weekday())  # 下周一
            return (nxt + _td(days=wd)).isoformat()
        if m.group(1) == "下下":
            nxt = today + _td(days=14 - today.weekday())
            return (nxt + _td(days=wd)).isoformat()
        return (today + _td(days=(wd - today.weekday()) % 7)).isoformat()
    if re.search(r"(这|本)?周末", q):
        return (today + _td(days=(5 - today.weekday()) % 7)).isoformat()  # 最近周六
    return None


def extract_hotel(query: str) -> str | None:
    """从自然语言提取住宿锚点 → 传给 hotel.resolve_hotel。提不到返回 None。

    支持：「住外滩华尔道夫」「住在西湖边/西湖国宾馆附近」「酒店订在南京路」
    「入住：全季酒店」以及「名称@lng,lat」显式坐标透传。
    """
    q = (query or "").strip()
    if not q:
        return None
    pats = [
        r"住(?:宿|在|进)?[：:]?\s*([^\s，。,；;]{1,24}?)(?:附近|旁边|边上|一带)",
        r"酒店(?:订|定)?在[：:]?\s*([^\s，。,；;@]{2,24}(?:@[0-9.]+,[0-9.]+)?)",
        r"(?:入住|住宿)[：:]\s*([^\s，。,；;]{2,24})",
        r"(?<![记留])住(?:宿|在|进)?[：:]?\s*([^\s，。,；;@]{2,24}(?:@[0-9.]+,[0-9.]+)?)",
    ]
    for p in pats:
        m = re.search(p, q)
        if m:
            t = m.group(1).strip()
            # 「住杭州」是停留城市不是酒店名；显式坐标除外
            if t and t not in CITIES and t not in CITY_ALIAS:
                return t
    return None


# ---- P5 NL 提取健壮化：正则未命中时 LLM 结构化抽取兜底 ----
NL_CACHE_PATH = os.path.join(ROOT, "data", "nl_cache.json")
NL_CACHE_TTL_S = 7 * 86400
_nl_cache: dict | None = None


def _nl_cache_get(qkey: str):
    global _nl_cache
    if _nl_cache is None:
        try:
            with open(NL_CACHE_PATH, encoding="utf-8") as f:
                _nl_cache = json.load(f)
        except Exception:  # noqa
            _nl_cache = {}
    ent = _nl_cache.get(qkey)
    if not ent or time.time() - ent.get("ts", 0) > NL_CACHE_TTL_S:
        return None
    return ent.get("v", {})  # 空 dict = 曾抽不到，避免重复打 LLM


def _nl_cache_put(qkey: str, val: dict) -> None:
    global _nl_cache
    if _nl_cache is None:
        _nl_cache = {}
    _nl_cache[qkey] = {"ts": time.time(), "v": val}
    tmp = NL_CACHE_PATH + ".tmp"
    try:
        json.dump(_nl_cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, NL_CACHE_PATH)
    except OSError:
        pass


def _llm_preflight(query: str) -> dict | None:
    """P0-3 合并前置：一次 LLM 同时完成「结构化抽取 + 澄清判断」（原为两次串行调用）。

    - 仅在 LLM 可用时调用；结果按 sha1(query) 缓存 7 天（含空结果，防重复消耗 token）
    - /api/clarify 与 /api/plan 共享同一次调用的结果，新查询省一次 LLM 往返（约 2~5s）
    - 任何异常静默返回 None，规划流程回退正则/默认值
    """
    import hashlib
    from src import llm_client
    if not (query or "").strip() or not llm_client.llm_available():
        return None
    # 缓存键带 schema 版本：2026-09-21 前置解析新增 destinations/unsupported_region
    # 两个字段。不升版本的话，旧结构（无这两个字段）会被直接复用 →
    # 「贵州5日自驾游」仍走老路径报 400（实测踩过）。输出结构变更必须升版本。
    qkey = "P2:" + hashlib.sha1(query.encode("utf-8")).hexdigest()
    hit = _nl_cache_get(qkey)
    if hit is not None:
        return hit or None
    today = _date.today().isoformat()
    # 受支持目的地清单（带省份）——LLM 只能从这里挑，库内没有的目的地排不了
    _dlist = "、".join(f"{c}（{p}）" if p else c for c, p, _n in dest_meta())
    sys_p = (
        "你是旅行行程助手的前置分析器，对用户需求一次完成「参数抽取」和「澄清判断」，"
        '只输出 JSON：{"city": "目的地城市名或null", "days": 行程天数整数或null, '
        '"date0": "出发日期YYYY-MM-DD或null", "hotel": "住宿酒店名或区域名或null", '
        '"multi_cities": ["多城联游时的城市列表或null"], '
        '"destinations": ["从下方受支持清单里挑出的目的地，按行程先后排序；挑不出填 null"], '
        '"unsupported_region": "需求提到但我们未覆盖的地名（如 川西），没有就 null", '
        '"need": 是否需要追问布尔值, "question": "一句话追问或null", '
        '"options": ["选项1", "选项2"]}。\n'
        "抽取规则：相对日期（明天/下周六/月底/国庆等）以今天为基准换算；"
        "「住的地方离西湖近点」这类模糊住宿描述抽出区域名（如 西湖）；"
        "hotel 仅当需求明确表达住宿意图（出现住/酒店/民宿/宾馆等字样）时抽取，"
        "行程范围或主题描述（如「都在太湖边」「环湖骑行」）不是住宿，必须 null；"
        "「玩一周」=7天但上限按5算；没有明确信息就填 null，不要猜。\n"
        "澄清判断规则（保守，大多数需求应 need=false 直接生成）：\n"
        "- 行程天数完全未提及（如只说「去杭州玩」）→ 可以问；已写「3天」「周末」等则不问\n"
        "- 同行人员完全未提及且明显影响节奏（亲子/老人/情侣/团队）→ 可以问；已提及则不问\n"
        "- 用户表达了强偏好但存在明显歧义（如「热闹的地方」不知指夜市还是商圈）→ 可以问\n"
        "不问的：目的地城市（未提及会用默认城市）、预算、交通方式、住宿（未提及就不排酒店）、"
        "具体日期（未提及就按近期规划）。只问最关键的一个问题，选项 2~4 个、每个不超过 12 字；"
        "不追问时 question/options 填 null。\n"
        f"受支持目的地清单（destinations 只能从这里选，不得编造）：{_dlist}\n"
        "目的地选择规则（destinations 字段）：\n"
        "- 用户提到省/大区（贵州、江南、黔东南…）而不是具体城市 → 从清单里挑出 2~4 个，"
        "按行程先后排序；要考虑城际驾车时间，不要贪多\n"
        "- 行政区同名但实际相距很远的不算同一趟（如「川西」不能用「成都」代替，"
        "应填 unsupported_region）\n"
        "- 清单里没有该区域的任何目的地 → destinations 填 null，并把该地名填进 "
        "unsupported_region\n"
        "- 用户已点名具体城市/景区时，destinations 与 city 保持一致\n"
        f"今天是 {today}。"
    )
    try:
        txt = llm_client.chat([{"role": "system", "content": sys_p},
                               {"role": "user", "content": query}],
                              temperature=0.0, timeout=15, retries=1)
        obj = json.loads(txt) if isinstance(txt, str) else (txt or {})
    except Exception:  # noqa: 网络限流/解析失败 → 静默回退
        return None
    nl: dict = {}
    clarify: dict = {"need": False}
    if isinstance(obj, dict):
        c = obj.get("city")
        if isinstance(c, str) and c.strip():
            nl["city_raw"] = c.strip()[:12]
            if c.strip() in CITIES:
                nl["city"] = c.strip()
        mc = obj.get("multi_cities")
        if isinstance(mc, list):
            mc = [x for x in mc if isinstance(x, str) and x.strip() in CITIES]
            if len(mc) >= 2:
                nl["multi_cities"] = mc
        ds = obj.get("destinations")
        if isinstance(ds, list):
            ds = [x.strip() for x in ds
                  if isinstance(x, str) and x.strip() in CITIES]
            if ds:
                nl["destinations"] = ds[:MAX_DAYS]
        ur = obj.get("unsupported_region")
        if isinstance(ur, str) and ur.strip() and ur.strip() not in CITIES:
            nl["unsupported_region"] = ur.strip()[:12]
        d = obj.get("days")
        # 上界用 DAYS_CAP_MULTI_CITY（最宽的天数上限）——这里只做**合法性**过滤，
        # 真正的「按目的地类型收窄」由 days_cap_for/clamp_days 在规划时做。
        # ⚠️ 原硬编码 `1 <= d <= 5` 会让 LLM 提取的「8天」被静默丢弃（新疆线必然踩）。
        if isinstance(d, int) and 1 <= d <= DAYS_CAP_MULTI_CITY:
            nl["days"] = d
        dt = obj.get("date0")
        if isinstance(dt, str):
            try:
                nl["date0"] = _date.fromisoformat(dt).isoformat()
            except ValueError:
                pass
        h = obj.get("hotel")
        if isinstance(h, str) and h.strip():
            nl["hotel"] = h.strip()[:24]
        if obj.get("need") is True:
            question = obj.get("question")
            options = [o for o in (obj.get("options") or [])
                       if isinstance(o, str) and o.strip()]
            if isinstance(question, str) and question.strip() and 2 <= len(options) <= 4:
                clarify = {"need": True, "question": question.strip()[:60],
                           "options": [o.strip()[:12] for o in options]}
    out = {"nl": nl, "clarify": clarify}
    _nl_cache_put(qkey, out)
    return out


def llm_extract(query: str) -> dict | None:
    """LLM 结构化抽取兜底：正则未命中的 city/days/date0/hotel 从 DeepSeek 拿。

    P0-3 起内部转调合并前置 _llm_preflight（抽取+澄清一次调用），结果共享缓存。
    """
    pre = _llm_preflight(query)
    return (pre or {}).get("nl") or None


def llm_clarify(query: str) -> dict | None:
    """多轮澄清（对照文档 P1）：LLM 判断需求是否缺失会实质影响行程设计的信息。

    P0-3 起内部转调合并前置 _llm_preflight（抽取+澄清一次调用），结果共享缓存。
    - 仅在 LLM 可用时调用；任何异常静默返回 None（前端视为无需追问，直接规划）
    """
    pre = _llm_preflight(query)
    if pre is None:
        return None
    return pre.get("clarify") or {"need": False}


_hotel_probe_cache: dict = {}   # (city, text) → bool；只缓存 status=1 的确定性结果
_amap_last_ts = [0.0]           # 高德 QPS 节流：个人 Key 连发会被限流（status=0）


def _hotel_name_match(q: str, name: str) -> bool:
    """名称相似判定：结果名对查询名的 bigram 覆盖率 ≥0.6 视为同一地点。

    防模糊匹配误归属（报障 13 实测）：高德 city=苏州 搜「西湖国宾馆」会返回
    「昆山西湖宾馆」（cityname=苏州市，行政区划校验挡不住）——bigram 覆盖
    「西湖/宾馆」2/4=0.5 < 0.6 拒绝；「杭州西湖国宾馆」4/4=1.0 通过。
    """
    q2 = re.sub(r"附近|旁边|边上|周边", "", (q or "")).strip()
    if not q2:
        return False
    if q2 in name:
        return True
    grams = [q2[i:i + 2] for i in range(len(q2) - 1)] or [q2]
    hit = sum(1 for g in grams if g in name)
    return hit / len(grams) >= 0.6


def _hotel_city_probe(cname: str, text: str) -> bool:
    """探测住宿文本归属哪座城市（报障 13 改进 B 的前置）。

    L0：库内地标匹配（「迪士尼附近」类）；L1：高德 city 限定地点搜索，
    并校验返回 POI 的行政区划 = 目标城市 **且** 名称相似（_hotel_name_match，
    防「昆山西湖宾馆」类同城模糊匹配误归属）。
    两级都不命中返回 False——resolve_hotel 自带市中心兜底（恒非 None），
    不能用它做归属判定。
    """
    from src import hotel as _hotel_mod
    ck = (cname, text)
    if ck in _hotel_probe_cache:
        return _hotel_probe_cache[ck]
    city = poi_db.load_city(cname)
    if _hotel_mod.match_landmark_poi(city, text):
        _hotel_probe_cache[ck] = True
        return True
    result = False
    key = os.environ.get("AMAP_KEY", "")
    if key:
        gap = time.time() - _amap_last_ts[0]
        if gap < 0.35:
            time.sleep(0.35 - gap)
        try:
            _amap_last_ts[0] = time.time()
            url = _hotel_mod.AMAP_PLACE.format(kw=urllib.parse.quote(text),
                                               city=urllib.parse.quote(cname), key=key)
            with _NO_PROXY_OPENER.open(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "1":
                for p in (data.get("pois") or [])[:5]:
                    adm = (p.get("cityname") or p.get("pname") or "")
                    if (cname in adm) and _hotel_name_match(text, p.get("name") or ""):
                        result = True
                        break
        except Exception:  # noqa：Key 失效/网络问题按未命中处理
            pass
    if result:  # 只缓存确定性命中；限流/异常的未命中不缓存（下次重试）
        _hotel_probe_cache[ck] = True
    return result


def _lift_seg_notices(cname: str, off: int, notices: list) -> list:
    """把单城分段的 notices 上浮到多城结果（报障 18 同族缺陷）。

    单城 `plan()` 会返回 notices（点名点未排入 / 日内空档披露 / 需求未满足），
    多城此前**整块丢弃** → 用户看不到任何解释。且 notice 里的 Day 号是**段内局部号**
    （杭州段第 1 天在多城里其实是 Day 2），直接合并会指错天 → 按偏移 off 重写。
    其余类型（named_lost 等）无 day 字段，带上城市标签即可区分。
    """
    out = []
    for n in notices or []:
        m = dict(n)
        if isinstance(m.get("day"), int):
            m["day"] += off
        if isinstance(m.get("days"), list):   # weather.trip_notices 的降雨天列表
            m["days"] = [x + off if isinstance(x, int) else x for x in m["days"]]
        msg = m.get("message") or ""
        if off and msg.startswith("Day"):
            m["message"] = re.sub(r"^Day(\d+)", lambda x: f"Day{int(x.group(1)) + off}", msg)
        m.setdefault("city", cname)
        out.append(m)
    return out


_CN_D = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}


def parse_city_days(query: str, cities: list) -> dict:
    """从需求文字解析「每城各玩几天」。返回 {城市名: 天数}，解析不到的城市不出现。

    2026-09-17 报障：「苏杭3日自驾游，第一天在杭州，晚上9点开车去苏州，然后苏州玩2天」
    被排成「杭州 2 天 + 苏州 1 天」—— plan_multi 用 divmod 盲分天数，余数给列表里
    靠前的城市，**完全不读用户明说的每城天数**。用户把话说得这么具体还被忽略，
    是最伤信任的一类失败。

    支持的表述（确定性正则，零 LLM 成本）：
      「苏州玩2天」「苏州2天」「在杭州待三天」「杭州1日」
      「第一天在杭州」→ 杭州至少 1 天（弱信号，仅在无显式天数时用）
    """
    out, weak = {}, {}
    q = query or ""
    _N = r"([1-9一二两三四五])"
    # 「上海成都5天自驾」里的「5天」是**总天数**，不是每城天数 —— 城市名紧邻数字时
    # 必须要求中间有动词（玩/待/留/住/游/逛）或「在…」结构，否则会把总天数
    # 误当成两座城各自的天数（实测：上海=5 且 成都=5）。
    _VERB = r"(?:玩|待|留|住|游|逛|安排|停留)"
    for c in cities:
        m = (re.search(rf"{re.escape(c)}\s*{_VERB}\s*{_N}\s*[天日]", q)
             or re.search(rf"在\s*{re.escape(c)}[^，。；;、]{{0,4}}?{_VERB}?\s*{_N}\s*[天日]", q)
             # 「苏州2天杭州2天」：城市紧邻数字，但同一句里每座城各带一个数字
             or (re.search(rf"{re.escape(c)}\s*{_N}\s*[天日]", q)
                 if len(re.findall(rf"{_N}\s*[天日]", q)) >= len(cities) else None))
        if not m:
            # 反序表述：「玩2天苏州」
            m = re.search(rf"{_VERB}\s*{_N}\s*[天日][^，。；;、]{{0,4}}?{re.escape(c)}", q)
        if m:
            v = m.group(1)
            out[c] = int(v) if v.isdigit() else _CN_D.get(v, 0)
            continue
        # 弱信号：「第一天在杭州」这类只说明该城占某一天
        if re.search(rf"第\s*([1-9一二两三四五])\s*[天日][^，。；;、]{{0,4}}?{re.escape(c)}", q):
            weak[c] = 1
    for c, v in weak.items():
        out.setdefault(c, v)
    return {c: v for c, v in out.items() if v >= 1}


def allocate_city_days(cities: list, days: int, wanted: dict) -> list:
    """按用户诉求分配每城天数；未指定的城市均分剩余；总数守恒到 days。

    冲突处理（诉求总和 ≠ 总天数）由调用方读 alloc 与 wanted 的差异后告知用户。
    """
    n = len(cities)
    alloc = [0] * n
    named = [(i, c) for i, c in enumerate(cities) if c in wanted]
    rest = [i for i, c in enumerate(cities) if c not in wanted]
    for i, c in named:
        alloc[i] = max(1, int(wanted[c]))
    used = sum(alloc)
    if rest:
        left = max(0, days - used)
        base, rem = divmod(left, len(rest))
        for k, i in enumerate(rest):
            alloc[i] = base + (1 if k < rem else 0)
    # 总数守恒：超出则从「天数最多且非用户指定」的城市开始扣；仍超则等比压缩指定城
    def _total():
        return sum(alloc)
    guard = 0
    while _total() > days and guard < 100:
        guard += 1
        pool = [i for i in rest if alloc[i] > 1] or [i for i in rest if alloc[i] > 0]
        if not pool:
            pool = [i for i, _ in named if alloc[i] > 1]
        if not pool:
            break
        alloc[max(pool, key=lambda i: alloc[i])] -= 1
    while _total() < days and guard < 200:
        guard += 1
        pool = rest or [i for i, _ in named]
        alloc[min(pool, key=lambda i: alloc[i])] += 1
    # 每城至少 1 天（0 天的城市等于没去，应由调用方决定是否剔除该城）
    for i in range(n):
        if alloc[i] < 1 and _total() < days:
            alloc[i] = 1
    return alloc


def _night_transfer_hour(query: str, to_city: str) -> float | None:
    """解析「晚上9点开车去苏州」类夜间转移的出发时刻（小时浮点）；无则 None。

    夜间转移与「抵达日早上开车」是两种不同形态：前者转移发生在出发城当天夜里，
    抵达日整天可用；后者吃掉抵达日上午。用户明说时间时必须按他说的排。
    """
    q = query or ""
    _H = r"(?:[0-9]{1,2}|十[一二]?|[一二三四五六七八九])"
    m = re.search(
        rf"(晚上|夜里|傍晚|下午)?\s*({_H})\s*[点时][^，。；;]{{0,8}}?"
        rf"(?:开车|自驾|出发|走|去|前往|驱车)[^，。；;]{{0,6}}?{re.escape(to_city)}", q)
    if not m:
        m = re.search(
            rf"(?:开车|自驾|驱车|出发)[^，。；;]{{0,6}}?{re.escape(to_city)}"
            rf"[^，。；;]{{0,8}}?(晚上|夜里|傍晚|下午)?\s*({_H})\s*[点时]", q)
    if not m:
        return None
    period, _hs = m.group(1) or "", m.group(2)
    _CNH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
            "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    hh = int(_hs) if _hs.isdigit() else _CNH.get(_hs, -1)
    if hh < 0:
        return None
    if period in ("晚上", "夜里") and hh < 12:
        hh += 12
    elif period in ("下午", "傍晚") and hh < 12:
        hh += 12
    if not (0 <= hh <= 23):
        return None
    # 只有真正处于「夜间」才按夜转处理；上午/白天的时刻仍走抵达日顺延口径
    return float(hh) if hh >= 17 else None


def _weekday_of(date0: str | None, offset_days: int) -> str | None:
    """date0 起第 offset_days 天的「周X」（与 sequencer 的 weekday 口径一致）。"""
    if not date0:
        return None
    try:
        d = _date.fromisoformat(date0) + _td(days=offset_days)
    except (ValueError, TypeError):
        return None
    return "周" + "一二三四五六日"[d.weekday()]


def _hm(h: float, wrap: bool = False) -> str:
    """小时浮点 → HH:MM。

    wrap=False：钳位在 24h 内（防「29:42」这类非法时刻）。
    wrap=True ：跨零点按次日时刻回绕并加「次日」前缀（夜间转移 23:00 出发 +2h）。
    """
    h = float(h)
    if wrap and h >= 24.0:
        h -= 24.0
        return f"次日 {int(h):02d}:{int(round((h - int(h)) * 60)):02d}"
    h = max(0.0, min(23.99, h))
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)):02d}"


# ---- 城际交通方式（2026-09-21 用户报障）：只认用户**明说**的方式，没提就留空 ----
# 原先转移行一律写「自驾 X → Y」并在通知里写「驾车到」——用户没说自驾时，行程里
# 凭空多出一个自驾设定（实测「苏杭3天联游」Day3 被标成「自驾 苏州 → 杭州」）。
# 改为：未指定则**留空 + 明确标注「方式待定」**；时长仍用驾车口径估算（时间轴必须
# 预留，否则「上午还在苏州、下午已在杭州玩 5 个点」物理上不成立），并如实披露口径。
# 以后接入高铁/飞机时长模型时，按 mode 填入即可。
INTERCITY_MODE_PATTERNS = [
    ("driving", re.compile(r"自驾|自己开车|开车|驾车|驱车|租车|road\s*trip|driving", re.I)),
    ("rail", re.compile(r"高铁|动车|火车|铁路|城际列车", re.I)),
    ("flight", re.compile(r"飞机|航班|机票|乘机|飞过去", re.I)),
]
_MODE_ROW = {"driving": "自驾", "rail": "高铁", "flight": "飞机"}
_MODE_VERB = {"driving": "驾车约", "rail": "乘高铁约", "flight": "乘飞机约"}


def intercity_mode(query: str) -> str | None:
    """用户明说的城际交通方式（driving/rail/flight）；**未指定返回 None**。"""
    for mode, pat in INTERCITY_MODE_PATTERNS:
        if pat.search(query or ""):
            return mode
    return None


def intercity_leg_name(mode: str | None, city_from: str, city_to: str) -> str:
    """转移行名称。mode=None 时**不写「自驾」**，明确标注方式待定。"""
    tag = _MODE_ROW.get(mode or "")
    return (f"{tag} {city_from} → {city_to}" if tag
            else f"{city_from} → {city_to}（城际交通待定）")


def intercity_verb(mode: str | None) -> str:
    """时长短语的前缀（已含「约」）：驾车约 / 乘高铁约 / 乘飞机约 / 城际交通约。"""
    return _MODE_VERB.get(mode or "", "城际交通约")


def intercity_disclaim(mode: str | None) -> str:
    """时长口径披露。驾车是本色不必说；留空 / 高铁 / 飞机都要说清是驾车口径估算。"""
    if mode == "driving":
        return ""
    if mode is None:
        return "（未指定城际交通方式，时长按驾车口径估算；说明高铁/飞机等可据此重排）"
    return f"（时长仍按驾车口径估算，{_MODE_ROW[mode]}实际时长以购票为准）"


def _transfer_day(day_no: int, city_from: str, city_to: str, tr: dict,
                  city: dict, weekday: str | None,
                  leg: int = 1, n_legs: int = 1, mode: str | None = None) -> dict:
    """长途转移日：整天只有一条 transfer 行，不排景点。

    为什么独占一天而不是塞进抵达城的第一天：4 小时以上车程 + 取还车 + 路上用餐，
    真实可游玩时间已不足半天；若仍按整天规划该城首日，时间轴会与现实脱节
    （这正是改造前「赤水→兴义 580km/6.7h 被当作不存在」的根因）。

    leg/n_legs：车程超过 DRIVE_DAY_MAX_MIN（10h）时一天开不完，拆成 n_legs 天，
    本函数产出其中第 leg 天。每天的行驶时长按 min(剩余, 单日上限) 计，
    时刻一律钳在当日 day_start~day_end 内。
    """
    total = float(tr["minutes"])
    per = total / n_legs if n_legs > 1 else total
    sh = poi_db.hhmm_to_h(city.get("day_start") or "09:00")
    eh_cap = poi_db.hhmm_to_h(city.get("day_end") or "21:30")
    eh = min(sh + per / 60.0, eh_cap)
    seg = f"（第 {leg}/{n_legs} 段）" if n_legs > 1 else ""
    return {
        "day": day_no,
        "theme": f"{city_from} → {city_to}｜城际转移{seg}",
        "timeline": [{
            "type": "transfer",
            "name": intercity_leg_name(mode, city_from, city_to) + seg,
            "start": _hm(sh), "end": _hm(eh),
            "min": int(round(per)),
            "km": round(float(tr["km"]) / max(n_legs, 1), 1),
            "from": city_from, "to": city_to, "source": tr["source"],
            "leg": leg, "n_legs": n_legs,
            # 方式字段：未指定时为 None（留空），以后按用户指定的高铁/飞机填入
            "mode": mode,
        }],
        "travel_km": round(float(tr["km"]) / max(n_legs, 1), 1),
        "travel_h": round(per / 60.0, 2),
        "violations": [], "repairs": [],
        "finish": _hm(eh),
        "gaps": [], "weekday": weekday,
        "transfer_day": True,
    }


NEAR_MERGE_KM = 25.0   # 中心相距小于此值的目的地视为同一片区域 → 合并为一次规划


def merge_near_destinations(cities: list, km: float = NEAR_MERGE_KM) -> tuple:
    """把中心过近的目的地合并（保留景点更多的一方）。返回 (kept, merged)。

    为什么需要（2026-09-17 方案B 开城的副作用）：景区与市区各自独立成城，且
    **允许 POI 重叠**——万峰林的招牌点同时也是兴义的招牌点（id 前缀不同：XY/WF）。
    用户同时点名「兴义万峰林」会走多城路径，逐城规划时同一片景区被走两遍
    （实测 Day1/Day3 重复万峰林景区、八卦田、上下纳灰村）。

    阈值 25km 只覆盖这两对「其实是一地」的目的地：
      兴义↔万峰林 9.6km、凯里↔西江千户苗寨 20.7km → 合并；
      安顺↔黄果树 40km、铜仁↔梵净山 49km → 不合并（真两地，且半径
      28/20km 下彼此库内并无对方点位）。
    取不出中心的城市一律保留：宁可多跑一城，不可静默丢城。
    """
    info = {}
    for c in cities:
        try:
            _c = poi_db.load_city(c)
            info[c] = (_c.get("center"), len(_c.get("pois") or []))
        except (OSError, KeyError, ValueError):
            info[c] = (None, 0)
    kept, merged = [], []
    for c in cities:
        cen, n_pois = info[c]
        near = None
        if cen:
            for k in kept:
                kc = info[k][0]
                if kc and poi_db.haversine_km(cen["lat"], cen["lng"],
                                              kc["lat"], kc["lng"]) < km:
                    near = k
                    break
        if near is None:
            kept.append(c)
        elif n_pois > info[near][1]:     # 本次提及的库更大 → 用它替换，位置不变
            kept[kept.index(near)] = c
            merged.append((c, near))
        else:
            merged.append((near, c))
    return kept, merged


def plan_multi(cities: list, query: str, days: int, date0: str | None,
               use_llm: bool, planner, hotel_text: str | None = None,
               progress=None, skip_copy: bool = False) -> dict:
    """跨城规划：按天均分逐城走完整规划链，合并时间轴/统计/city_meta。

    planner 为单城规划模块（m7/m1）。住宿锚点是城市专属概念（2026-09-15 报障 13
    改进 B：此前跨城直接忽略 hotel_text）——用 _hotel_city_probe 探测归属城市，
    命中段传 hotel_text、其余段不传。改进 C：住宿城市天数 +1（从天数最多的其他
    城扣 1，每城至少保留 1 天），使「住西湖国宾馆附近」的杭州段多分到时间。

    `skip_copy`（文案懒生成，Phase 3.1）：逐段把该参数透传给段内核——段内核
    支持才传（proposal_planner 有；m1 无 ⇒ 该段回落 full）。顶层 `copy_mode`
    以「每一段都真的懒成」为准，任一回落即 full，绝不谎报 lazy。

    城际转移段：段边界按 src/intercity 的三级模型算真实驾车时长。
      - 长途（≥ LONG_TRANSFER_MIN=240min）：占用抵达城 1 天作为纯转移日
        （alloc 预扣，该城少排一天景点），时间轴只有一条 type=transfer 行；
      - 短途（<240min）：不占天，改为在 notices 里给出「出发时刻 = 抵达城首个
        活动时刻 − 车程」，抵达日的景点安排不变。
    """
    n = min(len(cities), max(days, 1))
    use = cities[:n]
    # 近邻目的地合并：必须在天数分配之前——否则会给即将被合并掉的城市先分好天，
    # 合并后那些天无处安放（「兴义万峰林3日游」会变成只排 2 天）。
    use, _merged_near = merge_near_destinations(use)
    n = len(use)
    # 天数分配：优先尊重用户明说的每城天数（「苏州玩2天」），其余城市均分剩余。
    # 旧实现是 divmod 盲分 + 余数给靠前城市，会把「第一天在杭州…苏州玩2天」
    # 排成「杭州2天+苏州1天」（2026-09-17 报障）。
    _wanted = parse_city_days(query, use)
    alloc = allocate_city_days(use, days, _wanted)
    _alloc_notice = []
    for _keep, _drop in _merged_near:
        _alloc_notice.append({
            "kind": "city_merged_near",
            "message": (f"「{_keep}」与「{_drop}」在同一片区域内，已合并为一次规划"
                        f"（按景点更全的「{_keep}」安排），避免同一景区被走两遍")})
    if _wanted:
        _got = {c: alloc[i] for i, c in enumerate(use)}
        _conflict = {c: (v, _got.get(c)) for c, v in _wanted.items() if _got.get(c) != v}
        if _conflict:
            _alloc_notice.append({
                "kind": "city_days_adjusted",
                "message": "；".join(
                    f"你要求{c} {w} 天，实际安排 {g} 天" for c, (w, g) in _conflict.items())
                + f"（总天数 {days} 天的约束下）"})

    hotel_city = None
    if hotel_text:
        for i, cname in enumerate(use):
            if _hotel_city_probe(cname, hotel_text):
                hotel_city = i
                break
    # 住宿城天数倾斜（改进 C）：仅在用户**没有**明说每城天数时生效——
    # 用户已给出「苏州玩2天」时，酒店启发式不得推翻它
    if hotel_city is not None and n > 1 and not _wanted:
        donors = [i for i in range(n) if i != hotel_city and alloc[i] > 1]
        if donors:
            donor = max(donors, key=lambda i: alloc[i])
            alloc[donor] -= 1
            alloc[hotel_city] += 1

    # ---- 城际转移段：先算各段边界车程，长途段预扣抵达城 1 天做纯转移日 ----
    # 必须在规划前扣天：抵达城拿到的 di 已是「扣掉转移日之后」的天数，
    # 该城才会按更少的天数排点；事后插入转移日会让景点总量超出真实可用时间。
    # 用户明说的城际方式（未说明则为 None，转移行留空不写自驾）
    _mode = intercity_mode(query)
    _centers = {}
    for cname in use:
        try:
            _centers[cname] = poi_db.load_city(cname)["center"]
        except (OSError, KeyError, ValueError):
            _centers[cname] = None
    _amap = CFG.get("amap_key") or None
    transfers = {}      # 段下标 i（≥1） → {"minutes","km","source"}
    transfer_days = {}  # 段下标 i → 该段前面插几天纯转移日
    night_from = {}     # 段下标 i → 该城最后一天须在 HH（小时浮点）前收尾（夜间转移）
    for i in range(1, n):
        ca, cb = _centers.get(use[i - 1]), _centers.get(use[i])
        if not ca or not cb:
            continue
        tr = intercity.transfer(use[i - 1], ca, use[i], cb, amap_key=_amap)
        transfers[i] = tr
        # 夜间转移必须**在出发城那一段规划之前**就确定：它要给出发城最后一天
        # 设 day_end（提前收尾到出发时刻）。若等到处理抵达段时才发现，出发段
        # 早已按 21:30 排完，末个景点会与出发时刻重叠（实测苏堤春晓 20:00-21:30
        # 撞上 21:00 出发）。
        _nh = _night_transfer_hour(query, use[i])
        if _nh is not None and not intercity.is_long_transfer(tr["minutes"]):
            night_from[i - 1] = _nh   # 键是**出发城**的段下标
        # 需要几个转移日（>10h 车程一天开不完，按单日上限拆）。
        # 最多只能占到「该段天数 - 1」——至少留 1 天给抵达城，否则这座城
        # 变成纯路过、一个景点都排不了。占不下的部分在 notice 里明确告知。
        want = intercity.days_needed(tr["minutes"])
        if want and alloc[i] > 1:
            take = min(want, alloc[i] - 1)
            alloc[i] -= take
            transfer_days[i] = take

    merged_days, merged_pois, merged_gaps = [], {}, []
    # 分配期通知（近邻目的地合并 / 每城天数被调整）必须并进来：
    # 此前 _alloc_notice 只有构建、没有消费点，用户永远收不到
    # 「你要求苏州 2 天，实际安排 1 天」这类提示（静默改需求）。
    merged_notices = list(_alloc_notice)
    tot = {"violations": 0, "km": 0.0, "dup": 0, "cands": 0, "lat": 0.0,
           "proposed": 0, "unmatched": 0, "rate_w": 0.0}
    _copy_modes = set()  # 各段实际落账的 copy_mode：顶层 lazy 须「段段皆 lazy」
    seg_date0 = _date.fromisoformat(date0) if date0 else None
    day_no = 0
    hotel_out = None  # 住宿锚点（报障 18）：单城 plan() 会透出 hotel，多城此前整个丢掉
    for i, cname in enumerate(use):
        di = alloc[i]
        if di < 1:
            continue
        city = poi_db.load_city(cname)
        # ---- 短途转移（<240min，不占天）：抵达日起始时刻顺延到「到达时刻」----
        # 抵达日的点位必须按真实可用时长来排（否则会按整天打包，再靠一条 notice
        # 让用户自己 06:57 出发）。city dict 带 day_start_by_day，sequencer 与
        # toptw 共用 day_start_of() 读取，保证求解预算与时间轴口径一致。
        _short = (i in transfers and not transfer_days.get(i))
        _arrive_h = None
        # 夜间转移：用户明说「晚上9点开车去苏州」——转移发生在**出发城当天夜里**，
        # 抵达日整天可用，不该顺延。此时把 transfer 行挂到上一天的末尾。
        _night_h = night_from.get(i - 1) if _short else None
        # 本城最后一天当晚要开去下一城 → 该天提前收尾到出发时刻，
        # 让求解器与排序器都按缩短后的时长排点（day_end_by_day 由两者共用读取）
        if i in night_from:
            city = dict(city)
            city["day_end_by_day"] = {di: _hm(night_from[i])}
        # 必须在本段的天被 append 之前抓住「出发城最后一天」——否则 merged_days[-1]
        # 拿到的是抵达城自己的最后一天（实测：夜转行被挂到 Day3 而不是 Day1）
        _depart_day = merged_days[-1] if merged_days else None
        if _short and _night_h is None:
            _tr = transfers[i]
            _dep = poi_db.hhmm_to_h(city.get("day_start") or "09:00")
            _arrive_h = _dep + float(_tr["minutes"]) / 60.0
            city = dict(city)
            city["day_start_by_day"] = {1: _hm(_arrive_h)}  # 段内第 1 天 = 抵达日
        # ---- 长途转移日：占本段前 take 天，每天一条 transfer 行 ----
        _take = transfer_days.get(i) or 0
        if _take:
            tr = transfers[i]
            _want = intercity.days_needed(tr["minutes"])
            _first_td = day_no + 1
            for _leg in range(1, _take + 1):
                day_no += 1
                merged_days.append(_transfer_day(
                    day_no, use[i - 1], cname, tr, city,
                    _weekday_of(date0, day_no - 1), leg=_leg, n_legs=_take, mode=_mode))
                if seg_date0:
                    seg_date0 = seg_date0 + _td(days=1)
            tot["km"] += float(tr["km"])
            _days_txt = (f"Day{_first_td}" if _take == 1
                         else f"Day{_first_td}–Day{day_no}")
            _msg = (f"{_days_txt} 为城际转移日：{use[i - 1]} → {cname} "
                    f"{intercity_verb(_mode)} {intercity.duration_text(tr['minutes'])}"
                    f"（{tr['km']:.0f}km），期间不安排景点{intercity_disclaim(_mode)}")
            if _want > _take:
                # 想占 _want 天但只挤出 _take 天：必须说清，不能让用户以为开得完
                _msg += (f"；按单日最多 {intercity.DRIVE_DAY_MAX_MIN // 60} 小时驾驶计，"
                         f"这段实际需要 {_want} 天，当前行程只挤出 {_take} 天")
            if intercity.needs_rail_advice(tr["minutes"]):
                # 口径随 mode 变：用户没提自驾时，不能说「超出合理自驾范围」（我们并没假定自驾）
                _msg += ("。该距离已超出合理自驾范围，建议改乘高铁/飞机（跨城公共交通衔接尚未接入规划）"
                         if _mode == "driving" else
                         "。该距离城际交通耗时很长，建议改乘高铁/飞机（跨城公共交通衔接尚未接入规划）")
            merged_notices.append({
                "kind": "intercity_transfer", "day": _first_td, "city": cname,
                "days": list(range(_first_td, day_no + 1)), "message": _msg,
            })
        d0 = seg_date0.isoformat() if seg_date0 else None
        seg_hotel = hotel_text if (hotel_text and hotel_city == i) else None
        # 分阶段进度（2026-09-21 修）：原先多城路径**根本没透传 progress**（那个
        # `_plan_extra_kw` 只用在单城两处），于是前端进度条停在「解析需求」不动。
        # 这里按段包装：带上「第 i/n 段 · 哪座城」，否则进度条会在每城的
        # proposal→regen 之间来回跳，看不出整体进度。
        _seg_cb = (None if progress is None else
                   (lambda st, _i=i: progress(st, {"i": _i + 1, "n": n, "city": cname})))
        r = planner.plan(city, query, di, use_llm=use_llm, date0=d0,
                         hotel_text=seg_hotel,
                         **_plan_extra_kw(planner, _seg_cb,
                                          {"skip_copy": True} if skip_copy else None))
        _copy_modes.add(r.get("copy_mode") or "full")
        if seg_hotel and r.get("hotel"):
            hotel_out = r["hotel"]  # 住宿城市段的解析结果（名称/经纬度/解析来源）
        it = r["itinerary"]
        _seg_start = day_no + 1                 # 本段第一天在全局的 day 号
        _seg_first_day = None
        for d in it["days"]:
            day_no += 1
            d["day"] = day_no
            d["theme"] = f"{cname}｜{d.get('theme') or cname}"
            merged_days.append(d)
            if _seg_first_day is None:
                _seg_first_day = d
        # ---- 短途转移（<240min，未占天）：抵达日时间轴**首行**插入 transfer 行 ----
        # 该天的点位已按顺延后的起始时刻求解（见上方 day_start_by_day），
        # 所以这里插入的行与后续时刻是一致的，不是事后硬塞。
        if _short and _night_h is not None and _depart_day is not None:
            # 夜间转移：挂到出发城最后一天末尾，抵达日整天可用
            tr = transfers[i]
            _prev_day = _depart_day
            _end_h = _night_h + float(tr["minutes"]) / 60.0
            (_prev_day.setdefault("timeline", [])).append({
                "type": "transfer",
                "name": intercity_leg_name(_mode, use[i - 1], cname) + "（夜间转移）",
                "start": _hm(_night_h), "end": _hm(_end_h, wrap=True),
                "min": int(round(float(tr["minutes"]))),
                "km": round(float(tr["km"]), 1),
                "from": use[i - 1], "to": cname, "source": tr["source"],
                "leg": 1, "n_legs": 1, "night": True, "mode": _mode,
            })
            _prev_day["travel_km"] = round(
                float(_prev_day.get("travel_km") or 0) + float(tr["km"]), 1)
            _prev_day["night_transfer"] = True
            merged_notices.append({
                "kind": "intercity_transfer", "city": cname,
                "day": _prev_day["day"],
                "message": (f"Day{_prev_day['day']} 夜间 {use[i - 1]} → {cname}"
                            f"：{_hm(_night_h)} 出发、"
                            f"{intercity_verb(_mode)} {intercity.duration_text(tr['minutes'])}"
                            f"（{tr['km']:.0f}km）后抵达"
                            + ("，次日凌晨到达，注意休息" if _end_h >= 24 else "")
                            + f"；Day{_prev_day['day'] + 1} 起在 {cname} 全天游玩"
                            + intercity_disclaim(_mode)),
            })
            tot["km"] += float(tr["km"])
        elif _short and _seg_first_day:
            tr = transfers[i]
            _dep_s = city.get("day_start") or "09:00"
            (_seg_first_day.setdefault("timeline", [])).insert(0, {
                "type": "transfer", "name": intercity_leg_name(_mode, use[i - 1], cname),
                "start": _dep_s, "end": _hm(_arrive_h),
                "min": int(round(float(tr["minutes"]))),
                "km": round(float(tr["km"]), 1),
                "from": use[i - 1], "to": cname, "source": tr["source"],
                "leg": 1, "n_legs": 1, "mode": _mode,
            })
            _seg_first_day["travel_km"] = round(
                float(_seg_first_day.get("travel_km") or 0) + float(tr["km"]), 1)
            _seg_first_day["travel_h"] = round(
                float(_seg_first_day.get("travel_h") or 0)
                + float(tr["minutes"]) / 60.0, 2)
            _seg_first_day["arrive_transfer"] = True
            merged_notices.append({
                "kind": "intercity_transfer", "city": cname,
                "day": _seg_first_day["day"],
                "message": (f"Day{_seg_first_day['day']} {use[i - 1]} → {cname}："
                            f"{intercity_verb(_mode)} {intercity.duration_text(tr['minutes'])}"
                            f"（{tr['km']:.0f}km），{_dep_s} 出发、"
                            f"{_hm(_arrive_h)} 抵达，当天景点从抵达后开始安排"
                            + intercity_disclaim(_mode)),
            })
            tot["km"] += float(tr["km"])
        meta = city_meta(cname)
        merged_pois.update(meta["pois"])
        g = r.get("grounding") or {}
        _off = _seg_start - 1                   # 段内局部 day 号 → 全局号 的偏移
        for _g in (g.get("gaps") or []):
            _gg = dict(_g)
            if isinstance(_gg.get("day"), int):
                _gg["day"] += _off              # 缺口台账/披露的 Day 引用须是全局号
            merged_gaps.append(_gg)
        merged_notices.extend(_lift_seg_notices(cname, _off, r.get("notices")))
        tot["violations"] += it.get("total_violations", 0)
        tot["km"] += it.get("total_travel_km", 0.0)
        tot["dup"] += r.get("n_dup_across_days", 0) or 0
        tot["cands"] += r.get("candidates", 0) or 0
        tot["lat"] += r.get("latency_s", 0.0)
        tot["proposed"] += g.get("n_proposed", 0) or 0
        tot["unmatched"] += g.get("unmatched", 0) or 0
        tot["rate_w"] += (g.get("grounding_rate", 0.0) or 0.0) * (g.get("n_proposed", 0) or 0)
        # 下一段出发日期 = 本段起始 + 本段天数
        if seg_date0:
            from datetime import timedelta as _td
            seg_date0 = seg_date0 + _td(days=di)
    grounding = None
    if tot["proposed"] or merged_gaps:
        grounding = {"grounding_rate": (tot["rate_w"] / tot["proposed"]) if tot["proposed"] else 0.0,
                     "gaps": merged_gaps, "n_proposed": tot["proposed"],
                     "unmatched": tot["unmatched"]}
    # 文案懒生成（Phase 3.1）：多城逐段各自 compose ⇒ 只有**每一段都真的懒成**
    # 才算 lazy；任一段内核不支持（LLM 不可用降级 m1）→ 顶层回落 full，绝不谎报
    # （谎报会让 open_plan 白调一次 /api/copy，且面板拿到「假装待补」的行程）。
    _top_copy = "lazy" if _copy_modes == {"lazy"} else "full"
    return {
        "mode": "m7_multi",
        "days": days,
        "query": query,
        "date0": date0,
        "cities": use,
        "hotel": hotel_out,
        "copy_mode": _top_copy,
        "notices": merged_notices,
        "itinerary": {"days": merged_days, "total_violations": tot["violations"],
                      "total_travel_km": round(tot["km"], 1), "dropped_pois": []},
        "grounding": grounding,
        "n_dup_across_days": tot["dup"],
        "candidates": tot["cands"] or None,
        "latency_s": round(tot["lat"], 1),
    }

WEBUI_DIR = os.path.dirname(os.path.abspath(__file__))
CITIES = ["杭州", "南京", "上海", "苏州", "武汉", "成都", "北京", "广州", "盐城", "西安", "重庆", "长沙", "贵阳", "黄果树", "兴义", "万峰林", "小七孔", "六盘水", "凯里", "镇远古城", "遵义", "西江千户苗寨", "赤水", "安顺", "铜仁", "梵净山", "中国天眼", "织金洞", "都匀", "肇兴侗寨", "百里杜鹃", "喀纳斯", "乌鲁木齐", "伊犁", "那拉提", "吐鲁番", "克拉玛依"]
CITY_META = {}  # 懒加载缓存
PLAN_LOCK = threading.Lock()  # 访客自带 Key 时临时注入环境变量，加锁防并发串包

# ---- 护栏：公开链接防滥用 ----
# M5：这套服务现在也是**分享出去的 MCP App** 的后端 ⇒ 同一公司 NAT 后多个使用者
# 会共用一个出口 IP，8/分钟 会互相误伤。故 N / 窗口都允许运维用环境变量放宽
# （默认值不变，保持原有的防滥用强度）。真实 IP 由 nginx 的 X-Forwarded-For 透传。
RATE_LIMIT_N = int(os.environ.get("TRIPAGENT_RATE_LIMIT_N") or 8)
RATE_LIMIT_WINDOW_S = float(os.environ.get("TRIPAGENT_RATE_LIMIT_WINDOW_S") or 60.0)
_rate_lock = threading.Lock()
_rate = {}                # ip -> [timestamp,...]（滑动窗口，启动起累计，进程生命周期内）
_stats_lock = threading.Lock()
_stats = {"plan_total": 0, "plan_llm": 0, "plan_rate_limited": 0, "plan_error": 0,
          "export_total": 0, "start_ts": time.time(), "latency_sum": 0.0, "latency_max": 0.0}

# ---- P2 分阶段进度：前端携带 sid 发起 /api/plan，期间轮询 /api/progress 拿实时阶段 ----
# 结构 {sid: {"stage": str, "ts": float}}；规划结束即删除条目（进程生命周期内自清理）
_plan_stages: dict = {}
_plan_stages_lock = threading.Lock()


def _stage_put(sid: str, stage: str, **extra) -> None:
    """记录当前阶段。extra 可带段信息——多城联游时前端要显示「第 i/n 段 · 城市」，
    否则用户只看到进度条在几个阶段间来回跳（每城都从 proposal 重新开始），
    无法判断整体走到哪了。"""
    with _plan_stages_lock:
        ent = {"stage": stage, "ts": time.time()}
        ent.update(extra)
        _plan_stages[sid] = ent


def _stage_pop(sid: str) -> None:
    with _plan_stages_lock:
        _plan_stages.pop(sid, None)


def _plan_extra_kw(planner, progress_cb, extra=None) -> dict:
    """规划器签名兼容：仅透传其 plan() 真正接受的参数（m1 无 progress/skip_copy）。

    `extra` 供多城逐段透传可选参数（如 skip_copy）——**必须逐项过签名**，否则把
    skip_copy 丢给不支持的内核会 ValueError/TypeError（多城 lazy 的降级边界：
    LLM 不可用时 planner 降级 m1，lazy 请求应静默回落 full 而非报错）。
    """
    kw = {}
    try:
        import inspect
        params = inspect.signature(planner.plan).parameters
    except (TypeError, ValueError):  # noqa: 内置/异常签名 → 什么都不透传
        return kw
    if progress_cb and "progress" in params:
        kw["progress"] = progress_cb
    for k, v in (extra or {}).items():
        if k in params:
            kw[k] = v
    return kw


def rate_allow(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_LIMIT_WINDOW_S]
        if len(hits) >= RATE_LIMIT_N:
            _rate[ip] = hits
            return False
        hits.append(now)
        _rate[ip] = hits
        return True


def stats_bump(field: str, inc: int = 1):
    with _stats_lock:
        _stats[field] = _stats.get(field, 0) + inc


def stats_latency(sec: float):
    with _stats_lock:
        _stats["latency_sum"] = round(_stats["latency_sum"] + sec, 1)
        _stats["latency_max"] = round(max(_stats["latency_max"], sec), 1)


def load_config() -> dict:
    """读取合并配置（secrets.json 密钥覆盖 config.json 业务配置），仅注入缺失的环境变量。"""
    from src.config import load_config as _load_merged
    merged = _load_merged()
    cfg = {k: v.strip() for k, v in merged.items()
           if not k.startswith("_") and isinstance(v, str) and v.strip()}
    for env_key, cfg_key in (("DEEPSEEK_API_KEY", "deepseek_api_key"),
                             ("AMAP_KEY", "amap_key"),
                             ("M1_MODEL", "m1_model")):
        if cfg.get(cfg_key) and not os.environ.get(env_key):
            os.environ[env_key] = cfg[cfg_key]
    # 布尔开关：住宿锚点硬保障（注入 + TOPTW 必选点），默认关闭
    if not os.environ.get("ANCHOR_HARD_GUARANTEE") and merged.get("anchor_hard_guarantee"):
        os.environ["ANCHOR_HARD_GUARANTEE"] = "1"
    # 布尔开关：TOPTW 忠实执行（默认开）——与锚点开关相反的注入方向：代码侧
    # env 未设置视为 true，仅 config 显式 false 才注入 "0" 一键回退现行为
    if (merged.get("toptw_faithful_mode") is False
            and not os.environ.get("TOPTW_FAITHFUL_MODE")):
        os.environ["TOPTW_FAITHFUL_MODE"] = "0"
    return cfg


def city_meta(name: str) -> dict:
    if name not in CITY_META:
        city = poi_db.load_city(name)
        CITY_META[name] = {
            "name": name,
            # province 透出给前端：区域（省）也作为一等入口展示/分组
            "province": (city.get("province") or "").strip(),
            "n_pois": len(city["pois"]),
            "n_closed": sum(1 for p in city["pois"] if p.get("closed_days")),
            "pois": {p["id"]: {"id": p["id"], "name": p["name"], "lat": p["lat"], "lng": p["lng"],
                               "category": p.get("category")}
                     for p in city["pois"]},
        }
    return CITY_META[name]


# ---------------------------------------------------------------------------
# Phase 0（2026-10-11）：POI 发现 —— `GET /api/pois`
#
# 它存在的唯一理由：给**调用方 agent** 一个「查真库」的通道。
# MCP 侧新工具 `tripplanner_find_pois` 就是它的薄壳。没有它，agent 想配合
# 「自己出提案」就只能凭世界知识编点名字 ⇒ 落地匹配失败 ⇒ 触发修正轮 ⇒ 反而更贵
# （见 docs/MCP-Agent提案外包-方案探讨.md §5「契约发现」）。
#
# 为什么是云端端点而不是客户端读库：M5 起 MCP 是**瘦客户端**（分享包里没有 data/），
# 任何「按库取数」都必须在云端做。这与 `/api/cities`、`/api/replace` 同一套理由。
#
# ⚠️ 匹配口径**必须**与全库其它环节共用 `poi_db.norm_name` / `core_name` ——
#    本项目吃过「两套归一化必然漂移」的亏（落地匹配 vs 点名识别）。
# ---------------------------------------------------------------------------
_POI_DETAIL_CACHE: dict = {}


def _city_pois_cached(city: str) -> list:
    """某城 POI 原始列表（缓存）。缺文件返回 [] —— 发现工具不该因一座城炸掉整次查询。"""
    if city not in _POI_DETAIL_CACHE:
        try:
            _POI_DETAIL_CACHE[city] = poi_db.load_city(city).get("pois") or []
        except Exception:  # noqa: BLE001
            _POI_DETAIL_CACHE[city] = []
    return _POI_DETAIL_CACHE[city]


def _poi_brief(p: dict, city: str) -> dict:
    """发现工具下发的 POI 形状：只带「选点 / 说明」要用的字段。

    不含坐标（面板地图才需要坐标，走 poi_coords）—— 省下的体积对 agent 的上下文有意义。
    """
    return {
        "id": p.get("id"), "name": p.get("name"), "city": city,
        "category": p.get("category"), "tags": list(p.get("tags") or []),
        "duration_h": p.get("duration_h"),
        "best_time": p.get("best_time"),
        "open": p.get("open"), "close": p.get("close"),
        "price": p.get("price"), "rating": p.get("rating"),
        "family_ok": poi_db.is_family_ok(p),
        "closed_days": list(p.get("closed_days") or []),
        "note": p.get("note") or "",
    }


def search_pois(city: str = "", keyword: str = "", category: str = "",
                limit: int = 40) -> dict:
    """在（单城 / 多城 `A+B` / 全库）范围内发现 POI。

    范围：`city` 空 ⇒ 全库（`CITIES` 全部）。多城用 `+` 连接，与 `plan.city` 同口径。
    过滤：`category` 精确匹配；`keyword` 规范化后命中 name / name 主干 / tags / note 任一。
    排序：name 命中 > tags 命中 > note 命中；同级按 rating 降序。
    """
    cities = plan_view.split_cities(city) or list(CITIES)
    unknown = [c for c in cities if c not in CITIES]
    cities = [c for c in cities if c in CITIES]
    if city and not cities:
        return {"ok": False, "error": f"未收录的城市：{'、'.join(unknown)}",
                "supported": list(CITIES)}
    kw = poi_db.norm_name(keyword)
    cat = (category or "").strip()
    try:
        limit = int(limit or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 100))

    scored, total = [], 0
    for c in cities:
        for p in _city_pois_cached(c):
            if cat and (p.get("category") or "") != cat:
                continue
            if kw:
                nm = p.get("name") or ""
                hit_name = (kw in poi_db.norm_name(nm)
                            or kw in poi_db.norm_name(poi_db.core_name(nm)))
                hit_tags = any(kw in poi_db.norm_name(t) for t in (p.get("tags") or []))
                hit_note = kw in poi_db.norm_name(p.get("note") or "")
                if not (hit_name or hit_tags or hit_note):
                    continue
                rank = 0 if hit_name else (1 if hit_tags else 2)
            else:
                rank = 0
            total += 1
            scored.append((rank, -(p.get("rating") or 0), p.get("id") or "", c, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return {
        "ok": True, "count": min(total, limit), "total": total,
        "cities": cities, "unknown": unknown,
        "keyword": keyword or "", "category": cat,
        "pois": [_poi_brief(p, c) for _r, _g, _i, c, p in scored[:limit]],
    }


# ---------------------------------------------------------------------------
# M5：会话表（面板 ↔ 云端之间的「当前行程」）
#
# 面板后端改「全走线上」后，云端必须能回答「我这份行程现在是什么」 —— 否则
# 「重新拉取行程」无从取值（`/api/plan` 不带 query 只会新规划一份，不是取回）。
# 但云端**刻意保持无状态**（入参自包含，天然支持多用户/多面板并发），所以这里
# 用一张**有上限的内存表**兜住：
#   - key = session id（由每个 MCP 客户端生成、落盘、跨重启稳定）
#   - value = 最近一次归一化行程（plan / move / replace 都会更新它）
#   - 上限 200 条 / TTL 2 小时，超限丢最旧 ⇒ 多人共享也不会吃满内存。
# 不做持久化：真源头仍是各客户端自己磁盘上的副本，这里只是「面板与云端之间」的
# 短期真相；云端重启后客户端拉不到会话会自动回落到自己磁盘的那份。
_SESSIONS = collections.OrderedDict()
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 2 * 3600.0
_SESSION_MAX = 200


def _session_put(sid: str, plan) -> None:
    if not sid or not isinstance(plan, dict):
        return
    with _SESSION_LOCK:
        _SESSIONS[sid] = (time.time(), plan)
        _SESSIONS.move_to_end(sid)
        while len(_SESSIONS) > _SESSION_MAX:
            _SESSIONS.popitem(last=False)


def _session_get(sid: str):
    if not sid:
        return None
    with _SESSION_LOCK:
        v = _SESSIONS.get(sid)
        if not v:
            return None
        ts, plan = v
        if time.time() - ts > _SESSION_TTL:
            _SESSIONS.pop(sid, None)
            return None
        _SESSIONS.move_to_end(sid)
        return plan


def _regen_one_day_copy(cname: str, query: str, nd: dict):
    """换点 / 挪时段后对受影响天补跑文案（LLM 单天 ~1-1.5s）。

    为什么必须有（2026-10-11 报障「换点后当天文案消失」）：
      `keep_day_meta` 只沿用 theme（2026-10-10 决策：旧 reason/tips 逐点点名，
      换掉一个点后就是过期信息，宁可没有）⇒ 换点/挪时段后 reason/tips 必然为空、
      面板出现空洞。正解不是沿用旧文案，而是**按新时间轴重写**——与 compose 阶段
      同一套 `_regen_reasons`（only_days 单天口径），「文案对齐最终时间轴」铁律不变。
    lazy 行程不补：copyMode=lazy 本来就没有文案，open_plan 定稿时统一补。
    失败 fail-open 返回 None（维持空文案现状，不拖垮换点交互）。
    """
    try:
        from src import m2_planner as _m2
        c = poi_db.load_city(cname)
        all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, c) for p in c["pois"])}
        regen, okr = _m2._regen_reasons(
            c, query or "", {"days": [nd]}, all_pois, only_days=[nd.get("day")])
        if okr:
            return regen.get(nd.get("day"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _session_apply(sess: str, plan, day) -> None:
    """把「改动后的一天」写回会话里的整份行程。

    面板在 move / replace 时把**当前整份 plan** 一并带上（它手上就有一份），
    云端据此复现 `apply_day`，让会话表里的「当前行程」跟着用户操作一起走 ——
    否则「重新拉取行程」会拉回改动之前的那份。
    """
    if not sess or not isinstance(plan, dict) or not isinstance(day, dict):
        return
    try:
        _session_put(sess, plan_view.apply_day(plan, day))
    except Exception:  # noqa: BLE001 —— 会话同步是旁路，失败不能拖垮交互本身
        pass


def _panel_log(payload: dict) -> None:
    """面板动作轨迹落盘（面板是黑盒：失败时得留下证词）。

    M5 前这些 trace 打进本地 `local_api` 的 mcp_debug.log；面板改走线上后，
    落点跟着搬到云端 —— 否则「点了没反应」将无从判断是没发还是没收到。
    """
    try:
        d = os.path.join(ROOT, "output")
        os.makedirs(d, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + \
            json.dumps(payload, ensure_ascii=False) + "\n"
        with open(os.path.join(d, "webui_panel.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 —— 观测旁路绝不拖垮主链路
        pass


class Handler(BaseHTTPRequestHandler):
    # ---- CORS（m4）----
    # 🔴 为什么必须加：M3 面板的「本地优先 → 线上兜底」是**跨域 fetch**
    #   （面板 iframe 的 origin ≠ 本服务域名）。本服务原先一个 CORS 头都没有，
    #   浏览器的同源策略会**直接丢弃响应体**（且 UA 控制台才看得到，面板只表现为
    #   「后端不可达」）。所以远端兜底能否生效，**取决于这里**。
    #   放行 Authorization 是为了配 nginx Basic 认证（面板带凭据访问线上后端）。
    #   ⚠️ 用 `*` 而非回显 Origin：请求不带 Cookie（凭据走 Authorization 头），
    #      `*` 即可，且能兼容面板 iframe 的 opaque/自定义 origin。
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Tripplanner-Token")
        self.send_header("Access-Control-Max-Age", "600")

    # 浏览器对「带 Authorization / application/json 的跨界请求」会先发 OPTIONS 预检。
    # nginx 侧已把 OPTIONS 从 Basic 认证里豁免（否则预检被 401，请求根本发不出去）。
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---- M5.x：云端 MCP 端点（Streamable HTTP）----
    # 为什么把 transport 适配放进独立模块：工具表 / 面板资源 / 处理逻辑**复用** stdio 侧
    # 同一份 `mcp/tripplanner_mcp_server.py`（唯一口径，避免复制后漂移）；这里只做 HTTP 搬运。
    def _mcp(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if 0 < n <= 4_000_000 else b""
        try:
            res = mcp_endpoint.dispatch(body, self.headers.get("Mcp-Session-Id") or None)
        except Exception as e:  # noqa: BLE001 —— 端点异常要如实回 JSON-RPC error，不能断连
            res = {"status": 500, "session_id": None, "body": json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32603,
                           "message": f"internal error: {type(e).__name__}: {e}"}},
                ensure_ascii=False).encode("utf-8")}
        payload = res.get("body") or b""
        self.send_response(res.get("status") or 200)
        # MCP 走 JSON（非 SSE）：客户端发的 `Accept: application/json, text/event-stream` 两者都收。
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if res.get("session_id"):  # 仅「该带而未带」时下发，见 mcp_endpoint.dispatch 的说明
            self.send_header("Mcp-Session-Id", res["session_id"])
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_DELETE(self):
        # MCP 客户端 `terminateSession` 会发 `DELETE /mcp`：清掉该会话对象即可（幂等回 204）。
        u = urllib.parse.urlparse(self.path)
        if u.path != "/mcp":
            return self._json({"error": "not found"}, code=404)
        mcp_endpoint.drop_session(self.headers.get("Mcp-Session-Id") or "")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _html(self, path: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "index.html missing"}, code=500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        # 🔴 MCP Streamable HTTP 的 GET ＝「打开 standalone SSE 流」，本端点**不提供**。
        #   必须回 **405**（不是 404）：@modelcontextprotocol/sdk 的
        #   StreamableHTTPClientTransport 靠 405 判定「服务端不开 SSE 流」并继续走 POST；
        #   回 404 会被当成「端点不存在」而直接报连不上。
        if u.path == "/mcp":
            return self._json(
                {"error": "MCP endpoint is POST-only (no standalone SSE stream)"}, code=405)
        if u.path == "/" or u.path == "/index.html":
            return self._html(os.path.join(WEBUI_DIR, "index.html"))
        if u.path == "/api/health":  # 面板后端探活端点（只回版本/时间，不泄露任何数据）
            return self._json({"ok": True, "server": "tripagent-webui",
                               "version": WEBUI_VERSION, "stage": "M5",
                               # M5.x：云端 MCP 端点（/mcp）是否可用（只回布尔，不带业务数据）
                               "mcp_remote": mcp_endpoint.available(),
                               "ts": int(time.time())})
        if u.path == "/api/cities":
            return self._json({"cities": [city_meta(c) for c in CITIES],
                               "llm": llm_client.llm_available(),
                               "amap_key": CFG.get("amap_js_key") or ""})
            # 安全：前端只拿 JSAPI key（本就设计为公开）；Web 服务 key（amap_key）
            # 不出服务端——原 staticmap_key 字段为死代码已移除，js_key 缺失时
            # 也不再 fallback 到 Web 服务 key。
        if u.path == "/api/pois":  # Phase 0：POI 发现（MCP 侧 find_pois 工具的后端）
            try:
                _lim = int(q.get("limit") or 40)
            except ValueError:
                _lim = 40
            return self._json(search_pois(city=q.get("city") or "",
                                          keyword=q.get("keyword") or "",
                                          category=q.get("category") or "",
                                          limit=_lim))
        if u.path == "/api/route":  # P1-1：实际路网路径（骑行/步行），带磁盘缓存
            mode = q.get("mode", "walking")
            o, d = _norm_ll(q.get("o") or ""), _norm_ll(q.get("d") or "")
            key = CFG.get("amap_key", "")
            if mode not in ("walking", "riding", "driving") or not o or not d:
                return self._json({"ok": False, "error": "参数错误（mode/o/d）"}, code=400)
            if not key:
                return self._json({"ok": False, "error": "服务端未配置高德 Key"}, code=400)
            ck = f"{mode}|{o}|{d}"
            with _route_lock:
                hit = _load_route_cache().get(ck)
            if hit:
                return self._json({"ok": True, "cached": True, **hit})
            try:
                r = _amap_route(mode, o, d, key)
            except Exception as e:  # noqa: 网络/服务异常 → 前端回退直线
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            if not r:
                return self._json({"ok": False, "error": "路径规划失败"})
            with _route_lock:
                cache = _load_route_cache()
                cache[ck] = r
                try:
                    with open(ROUTE_CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False)
                except OSError:
                    pass
            return self._json({"ok": True, **r})
        if u.path == "/api/photo":  # P3：POI 实拍缩略图（302 → 高德图库直链；404 → 前端回退瓦片）
            pid = q.get("id", "")
            prefix = pid[:2].upper() if len(pid) > 2 else ""
            if prefix not in _ID_PREFIX_CITY:
                return self._json({"ok": False, "error": "参数错误（id）"}, code=400)
            with _photo_lock:
                cache = _load_photo_cache()
                url = cache.get(pid)
            if url is None:  # 未缓存：现查一次并落盘（空串标记无图，避免重复打 API）
                cname = _ID_PREFIX_CITY[prefix]
                name = next((p["name"] for p in poi_db.load_city(cname)["pois"]
                             if p["id"] == pid), None)
                url = ""
                if name and CFG.get("amap_key"):
                    try:
                        url = _amap_photo(name, cname, CFG["amap_key"]) or ""
                    except Exception:  # noqa: 网络异常 → 前端回退瓦片
                        url = ""
                with _photo_lock:
                    cache = _load_photo_cache()
                    cache[pid] = url
                    _save_photo_cache()
            if url:
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json({"ok": False, "error": "无实拍图"}, code=404)
        m = re.match(r"^/api/share/([A-Za-z0-9_-]{4,32})$", u.path)  # P1-2：读取分享
        if m:
            path = os.path.join(SHARES_DIR, m.group(1) + ".json")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"ok": False, "error": "分享不存在或已过期"}, code=404)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/stats":
            with _stats_lock:
                st = dict(_stats)
            st["uptime_h"] = round((time.time() - st.pop("start_ts")) / 3600, 2)
            n = max(st["plan_total"], 1)
            st["avg_latency_s"] = round(st.pop("latency_sum") / n, 1)
            st["p_max_latency_s"] = st.pop("latency_max")
            return self._json(st)
        if u.path == "/api/progress":  # P2 分阶段进度：配合 /api/plan 的 sid 使用
            sid = (q.get("sid") or "")[:40]
            with _plan_stages_lock:
                ent = _plan_stages.get(sid)
            if not ent:
                return self._json({"ok": True, "stage": "done"})
            return self._json({"ok": True, "stage": ent["stage"], "seg": ent.get("seg"),
                               "age_s": round(time.time() - ent["ts"], 1)})
        if u.path == "/api/clarify":  # P1 多轮澄清：规划前 LLM 判断是否需要追问
            stats_bump("clarify_total")
            if q.get("llm", "1") != "1" or not llm_client.llm_available():
                return self._json({"ok": True, "need": False})
            try:
                c = llm_clarify(q.get("query") or "")
            except Exception:  # noqa
                c = None
            return self._json({"ok": True, **(c or {"need": False})})
        if u.path == "/api/plan":
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
                 self.client_address[0]
            # M5：MCP App **瘦客户端**要的归一化形状 + 会话键。
            #   客户端不带 `data/`（2.6M POI 库 + 照片缓存）也没有求解器，
            #   所以「补照片 / 坐标」必须在云端做（`?shape=mcp`）。
            shape_mcp = (q.get("shape") or "").lower() == "mcp"
            sess_key = (q.get("session") or "").strip()[:64]

            # 「拉行程」：带 session、不带 query ⇒ 回**本会话当前那份**行程，不重新规划。
            # 只读操作，故放在限流**之前**（用户狂点刷新不该被当成刷规划）。
            if shape_mcp and sess_key and not (q.get("query") or "").strip():
                stored = _session_get(sess_key)
                if stored:
                    return self._json({"ok": True, "plan": stored})
                return self._json({"ok": False,
                                   "error": "会话已过期（云端保留 2 小时），请重新生成行程"})

            # ---- Agent-Proposal（Phase 1，docs/MCP-Agent提案外包-方案探讨.md §6）----
            # 可选 agent 提案 + dry_run 校验回执。不传 proposal ⇒ 一切照旧（共存不替换）。
            proposal_data = None
            if (q.get("proposal") or "").strip():
                try:
                    _p = json.loads(q["proposal"])
                except (ValueError, TypeError):
                    return self._json({"ok": False, "error": "proposal 不是合法 JSON"}, code=400)
                # 宽容归一化：契约面（MCP schema / 方案稿）是「按天对象数组」，
                # 内核是 {"days":[…]} ⇒ 数组进来包一层，别让调用方踩两种形状。
                if isinstance(_p, list):
                    _p = {"days": _p}
                if not isinstance(_p, dict):
                    return self._json({"ok": False,
                                       "error": "proposal 需要是数组（逐天对象）或含 days 数组的对象"},
                                      code=400)
                proposal_data = _p
            dry_run = q.get("dry_run") == "1"
            # 文案懒生成（Phase 3）：重拍/探索阶段 copy_mode=lazy 跳过文案 LLM（占
            # 总调用 ~45%），定稿后 open_plan 自动 POST /api/copy 对最终行程补跑。
            copy_mode = (q.get("copy_mode") or "full").strip().lower()
            if copy_mode not in ("full", "lazy"):
                return self._json({"ok": False,
                                   "error": "copy_mode 只支持 full/lazy"}, code=400)
            proposal_mode = (q.get("proposal_mode") or "auto").strip().lower()
            if proposal_mode not in ("auto", "server", "agent"):
                return self._json({"ok": False,
                                   "error": "proposal_mode 只支持 auto/server/agent"}, code=400)
            if proposal_mode == "agent" and proposal_data is None:
                return self._json({"ok": False,
                                   "error": "proposal_mode=agent 需要同时传 proposal"}, code=400)

            if not rate_allow(ip):
                stats_bump("plan_rate_limited")
                return self._json({"ok": False,
                                   "error": f"请求过于频繁（每 {int(RATE_LIMIT_WINDOW_S)} 秒最多 {RATE_LIMIT_N} 次规划），请稍后再试"}, code=429)
            stats_bump("plan_total")

            def _shape_mcp(payload, city_name, note=""):
                """?shape=mcp → {"ok":true,"plan":<归一化行程>}；其它情况原样返回。"""
                if not shape_mcp or not payload.get("ok") or not payload.get("result"):
                    return payload
                try:
                    plan = plan_view.normalize_plan(payload["result"], city_name, note)
                except Exception as e:  # noqa: BLE001 —— 归一化失败必须如实报，不能静默
                    return {"ok": False, "error": f"归一化失败：{type(e).__name__}: {e}"}
                _session_put(sess_key, plan)      # 记住「这份 = 本会话的当前行程」
                return {"ok": True, "plan": plan}
            # P2 分阶段进度：前端带 sid 发起，规划期间可轮询 /api/progress?sid=
            sid = (q.get("sid") or "")[:40]
            if sid:
                _stage_put(sid, "parse")
            progress_cb = (lambda s: _stage_put(sid, s)) if sid else None
            # 多城路径的进度回调多一个「段」参数（单城没有段概念，故两个回调并存）
            progress_cb_m = ((lambda s, seg=None: _stage_put(sid, s, seg=seg))
                             if sid else None)
            try:
                query = q.get("query") or ""
                # ---- P5 NL 提取健壮化：正则未命中时 LLM 结构化抽取兜底（llm=0 离线路径不触发） ----
                nl = llm_extract(query) if (q.get("llm", "1") == "1" and query) else None
                # ---- 目的地解析（2026-09-21）：显式城市 → LLM 挑子集 → 省份降级 → 明确报错 ----
                _d_hint = extract_days(query) or (nl or {}).get("days") or DEFAULT_DAYS
                _days_r, _c_r, _w_r = clamp_days(_d_hint)
                _dec = plan_route_decision(query, nl, _days_r, q.get("city") or "")
                if _dec["error"]:
                    return self._json({"ok": False, "error": _dec["error"]}, code=400)
                multi, cname = _dec["multi"], _dec["cname"]
                _resolve_notice = _dec["notice"]
                # M5：`?strict_city=1`（MCP App 专用）—— 需求里完全没提地名时**明确报错**，
                # 而不是静默给个默认城市。为什么不能两边统一：Web UI 的对话式输入
                # （「随便玩玩」）落默认城市是既有体验，改掉它是破坏性的；MCP App 由
                # agent 驱动，静默换城市＝用户拿到一份自己没要的行程。故用开关分开。
                if q.get("strict_city") == "1" and _dec.get("defaulted"):
                    return self._json({"ok": False, "error": (
                        "没能从需求里识别出城市。请在需求里写明城市，或传 city 参数。"
                        f"当前支持：{'、'.join(CITIES)}")}, code=400)
                if not query:
                    query = f"{cname}2天经典深度游"
                # ---- P2-2 多城联游：查询出现 ≥2 城（或「苏杭」别名）→ 跨城合并规划 ----
                if len(multi) >= 2:
                    if proposal_data is not None:
                        return self._json({"ok": False, "error":
                                           "多城联游暂不支持 proposal（提案按天带城属后续阶段）"},
                                          code=400)
                    # 文案懒生成（Phase 3.1，2026-10-11）：多城**已支持** copy_mode=lazy —
                    # 逐段各自 compose 时透传 skip_copy（段段懒成）；定稿 /api/copy 按天
                    # 反查城市分组补文案（_day_city + _ID_PREFIX_CITY，与 /api/photo 同源）。
                    d = extract_days(query) or (nl or {}).get("days")
                    _cap_m = days_cap_for(cities, query)   # 多城/自驾 → 放宽（新疆线）
                    days, _clamped, _want = clamp_days(d, cap=_cap_m)
                    _days_notice = ([{"kind": "days_clamped", "message":
                                      f"需求是 {_want} 天，当前单次规划上限 {_cap_m} 天，"
                                      f"已按 {_cap_m} 天安排"}] if _clamped else [])
                    use_llm_m = q.get("llm", "1") == "1" and llm_client.llm_available()
                    try:
                        from src import proposal_planner as _pp
                        planner_m = _pp if use_llm_m else m1_planner
                    except ImportError:
                        planner_m = m1_planner
                    stats_bump("plan_total")
                    try:
                        m_date0 = q.get("date") or extract_date(query) or (nl or {}).get("date0")
                        # 住宿文本：与单城段同款解析（参数 > 正则 > NL 抽取），传给 plan_multi
                        m_hotel_text = q.get("hotel") or extract_hotel(query)
                        if not m_hotel_text and re.search(r"住|酒店|民宿|宾馆|客栈", query):
                            m_hotel_text = (nl or {}).get("hotel")
                        r = plan_multi(multi, query, days, m_date0,
                                       use_llm=use_llm_m, planner=planner_m,
                                       hotel_text=m_hotel_text,
                                       progress=progress_cb_m,
                                       skip_copy=(copy_mode == "lazy"))
                        merged_meta = {"name": "+".join(multi), "n_pois": 0, "n_closed": 0,
                                       "pois": {}}
                        for c in multi:
                            m = city_meta(c)
                            merged_meta["pois"].update(m["pois"])
                            merged_meta["n_pois"] += m["n_pois"]
                            merged_meta["n_closed"] += m["n_closed"]
                        stats_latency(r.get("latency_s", 0))
                        if _days_notice:  # 天数被上限截断须显式告知，不能静默改需求
                            r["notices"] = _days_notice + _resolve_notice + (r.get("notices") or [])
                        # 缺口台账：跨城合并后的各城缺口一并落账
                        if gap_log.append_gap_record(None, multi, query, days,
                                                     "m7_multi", r.get("grounding")):
                            stats_bump("plan_with_gaps")
                        return self._json(_shape_mcp(
                            {"ok": True, "city_meta": merged_meta, "result": r,
                             "days_source": "query" if extract_days(query) else "default",
                             "parsed": {"city": "+".join(multi), "date0": m_date0,
                                        "hotel_text": None}},
                            "+".join(multi), _resolve_notice))
                    except Exception as e:  # noqa
                        stats_bump("plan_error")
                        import traceback
                        return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                           "trace": traceback.format_exc()[-900:]}, code=500)
                city = poi_db.load_city(cname)
                # 天数优先从需求文字里识别（「3天」「两日」…）；显式 days 参数仅作兼容保留；都没有则默认 2 天
                days_param = (q.get("days") or "").strip()
                _clamped, _want = False, None
                # 🔴 2026-10-14：天数上限按目的地类型动态（自驾/多城放宽，见 days_cap_for）
                _cap_1 = days_cap_for([cname], query)
                if days_param:
                    days, days_src = max(1, min(_cap_1, int(days_param))), "param"
                else:
                    d = extract_days(query)
                    if d:
                        # 2026-09-17：此前这里直接 days=d **没有 clamp**（多城路径反而
                        # clamp 了），「8天」会把 8 一路传进求解层
                        days, _clamped, _want = clamp_days(d, cap=_cap_1)
                        days_src = "query"
                    elif (nl or {}).get("days"):
                        days, _clamped, _want = clamp_days(nl["days"], cap=_cap_1)
                        days_src = "llm"
                    else:
                        days, days_src = DEFAULT_DAYS, "default"
                _days_notice = ([{"kind": "days_clamped", "message":
                                  f"需求是 {_want} 天，当前单次规划上限 {_cap_1} 天，"
                                  f"已按 {_cap_1} 天安排"}] if _clamped else [])
                # P4：日期/住宿优先显式参数，缺省时从需求文字提取（正则 miss 再用 LLM 兜底）
                date0 = q.get("date") or extract_date(query) or (nl or {}).get("date0")
                # 住宿意图防线：NL LLM 抽取会把行程范围/主题描述（「都在太湖边」）误判为
                # 住宿区域 → 库内地标匹配「太湖」⊂「太湖西山岛」凭空出住宿锚点。
                # nl.hotel 只在 query 明确含住宿字样时采纳（正则通道 extract_hotel
                # 本身要求「住」字，天然免疫；显式 hotel 参数不受影响）。
                hotel_text = q.get("hotel") or extract_hotel(query)
                if not hotel_text and re.search(r"住|酒店|民宿|宾馆|客栈", query):
                    hotel_text = (nl or {}).get("hotel")
                llm_key = self.headers.get("X-LLM-Key", "").strip()  # 访客自带 Key（不落盘）
                use_llm = q.get("llm", "1") == "1" and bool(llm_key or llm_client.llm_available())
                mode = q.get("mode", "m7")  # 默认走 M7 经验提案；显式 mode 保留兼容（eval 脚本）
                planner = None
                if mode == "m7":  # M7 经验提案（需 LLM；不可用由其内部降级 M1）
                    try:
                        from src import proposal_planner
                        planner = proposal_planner
                    except ImportError:
                        planner = None
                if planner is None and mode == "m2":
                    try:
                        from src import m2_planner
                    except ImportError as e:  # ortools 未装：优雅降级为 M1
                        m2_planner = None
                    planner = m2_planner if m2_planner is not None else m1_planner
                if planner is None:
                    planner = m1_planner
                # ---- Agent-Proposal（Phase 1）：可选 agent 提案进入 m7 链路 ----
                # 共存不替换：不传 proposal / mode!=m7 / 内核缺失 ⇒ 一切照旧。
                if proposal_data is not None and mode != "m7":
                    return self._json({"ok": False,
                                       "error": "proposal 仅支持 mode=m7（经验提案链路）"}, code=400)
                # 文案懒生成：依赖 m7 内核的 compose(skip_copy)，且多城合并路径没有
                # 单一 city 可 load（/api/copy 补文案要重建 all_pois）⇒ 显式拒绝
                if copy_mode == "lazy":
                    if mode != "m7":
                        return self._json({"ok": False,
                                           "error": "copy_mode=lazy 仅支持 mode=m7（经验提案链路）"}, code=400)
                    # 多城联游在上面独立分支已处理（逐段懒成）；此处仅单城。
                    if planner is not proposal_planner:
                        return self._json({"ok": False,
                                           "error": "懒文案规划内核不可用（proposal_planner 导入失败）"},
                                          code=500)
                if proposal_data is not None and planner is not proposal_planner:
                    return self._json({"ok": False,
                                       "error": "提案规划内核不可用（proposal_planner 导入失败）"},
                                      code=500)
                use_prop = (proposal_data is not None and proposal_mode != "server"
                            and mode == "m7" and planner is proposal_planner)
                if dry_run:
                    if proposal_data is None:
                        return self._json({"ok": False,
                                           "error": "dry_run=1 需要同时传 proposal"}, code=400)
                    return self._json({"ok": True, "dry_run": True, "city": cname,
                                       "days": days,
                                       "validation": proposal_planner.validate_proposal(
                                           city, proposal_data, days)})
                # P0-3 LLM 结果缓存：m7+LLM 路径且非访客 Key 时，7 天内同参数直接秒回（?fresh=1 跳过）。
                # Agent-Proposal：结果取决于调用方给的提案（每次都可能不同）⇒ 该请求绕过缓存。
                # 文案懒生成：lazy 行程文案未生成，入/读缓存都会串味（lazy 结果被 full
                # 查询复用、或反之）⇒ lazy 请求绕过缓存读写（cache_key 保持 None）。
                cache_key = None
                if (use_llm and mode == "m7" and not llm_key and not use_prop
                        and copy_mode != "lazy"
                        and q.get("fresh", "0") != "1"):
                    cache_key = _llm_cache_key(cname, query, days, date0, hotel_text, mode)
                    cached = _llm_cache_get(cache_key)
                    if cached is not None:
                        stats_bump("plan_cache_hit")
                        stats_latency(cached.get("latency_s", 0))
                        # 缺口台账：缓存回放同样代表真实用户需求缺口
                        if gap_log.append_gap_record(cname, None, query, days, mode,
                                                     cached.get("grounding")):
                            stats_bump("plan_with_gaps")
                        if _days_notice:  # 缓存命中也要带上天数截断提示
                            cached["notices"] = _days_notice + (cached.get("notices") or [])
                        return self._json(_shape_mcp(
                            {"ok": True, "city_meta": city_meta(cname), "result": cached,
                             "days_source": days_src,
                             "parsed": {"city": cname, "date0": date0,
                                        "hotel_text": hotel_text,
                                        "from_query": bool(query and (extract_days(query) or date0 or hotel_text))}},
                            cname, _resolve_notice))
                # Agent-Proposal：提案经 kw 透传（就地组参数，避免用全局变量传请求态——
                # 规划可并发，全局会串请求）
                _plan_args = {"use_llm": use_llm, "date0": date0, "hotel_text": hotel_text,
                              **_plan_extra_kw(planner, progress_cb)}
                if use_prop:
                    _plan_args["agent_proposal"] = proposal_data
                if copy_mode == "lazy":
                    _plan_args["skip_copy"] = True
                if llm_key:
                    # 临时注入访客 Key → 规划 → 恢复环境（锁内串行，防并发互相覆盖）
                    with PLAN_LOCK:
                        old = os.environ.get("DEEPSEEK_API_KEY")
                        os.environ["DEEPSEEK_API_KEY"] = llm_key
                        try:
                            r = planner.plan(city, query, days, **_plan_args)
                        finally:
                            if old is None:
                                os.environ.pop("DEEPSEEK_API_KEY", None)
                            else:
                                os.environ["DEEPSEEK_API_KEY"] = old
                    cache_key = None  # 访客 Key 结果不落缓存
                else:
                    r = planner.plan(city, query, days, **_plan_args)
                stats_latency(r.get("latency_s", 0))
                # P2 观测：备选补位命中率累计（命中=确定性补位，miss=LLM 兜底）
                _as = r.get("alt_sub") or {}
                if _as.get("hit") or _as.get("miss"):
                    stats_bump("alt_sub_hit", int(_as.get("hit") or 0))
                    stats_bump("alt_sub_miss", int(_as.get("miss") or 0))
                if r.get("mode") not in ("offline_fallback",):
                    stats_bump("plan_llm")
                    if cache_key:  # 只缓存真实 LLM 结果（离线兜底不缓存）
                        try:
                            _llm_cache_put(cache_key, r)
                        except OSError:
                            pass
                # 缺口台账：本次规划未落地的提案点持久化（扩城 SOP 数据源）
                if gap_log.append_gap_record(cname, None, query, days, mode,
                                             r.get("grounding")):
                    stats_bump("plan_with_gaps")
                if _days_notice:  # 天数被上限截断须显式告知
                    r["notices"] = _days_notice + _resolve_notice + (r.get("notices") or [])
                return self._json(_shape_mcp(
                    {"ok": True, "city_meta": city_meta(cname), "result": r,
                     "days_source": days_src,
                     "parsed": {"city": cname, "date0": date0,
                                "hotel_text": hotel_text,
                                "from_query": bool(query and (extract_days(query) or date0 or hotel_text))}},
                    cname, _resolve_notice))
            except Exception as e:  # noqa: 单请求异常不挂服务
                stats_bump("plan_error")
                import traceback
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-900:]}, code=500)
            finally:
                _stage_pop(sid)  # 进度条目用毕即清（无论成功/失败/缓存命中）
        return self._json({"error": "not found"}, code=404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # M5.x：云端 MCP 端点（Streamable HTTP）——实现全在 webui/mcp_endpoint.py
        if u.path == "/mcp":
            return self._mcp()
        # Agent-Proposal（Phase 1）：/api/plan 的 proposal 可能比 URL 长得多
        # （nginx 头默认 8K，5 天 × 多点的提案 JSON 会超）⇒ 支持 POST body 传参：
        # 把 JSON body 合并进 query 后**复用 do_GET 的同一段处理器**（零逻辑分叉）。
        if u.path == "/api/plan":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = (json.loads(self.rfile.read(n).decode("utf-8"))
                        if 0 < n <= 262144 else None)
            except Exception:  # noqa: BLE001 —— 脏 body 如实按 400 报，不静默
                body = None
            if not isinstance(body, dict):
                return self._json({"ok": False,
                                   "error": "POST /api/plan 需要 JSON 对象 body"}, code=400)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            q.update({k: v for k, v in body.items() if v is not None})
            self.path = "/api/plan?" + urllib.parse.urlencode(
                {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                     else v) for k, v in q.items()})
            return self.do_GET()
        # 文案懒生成（Phase 3）：对会话里的 lazy 行程补跑文案（每天一次 LLM 并行）。
        # open_plan（定稿）时由瘦客户端自动调用 —— 模型/用户都无感。
        if u.path == "/api/copy":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = (json.loads(self.rfile.read(n).decode("utf-8"))
                        if 0 < n <= 262144 else None)
            except Exception:  # noqa: BLE001
                body = None
            sess = (body or {}).get("session") if isinstance(body, dict) else None
            sess = str(sess or "").strip()[:64]
            if not sess:
                return self._json({"ok": False, "error": "需要 session（会话键）"}, code=400)
            plan = _session_get(sess)
            if not plan or not plan.get("dayList"):
                return self._json({"ok": False,
                                   "error": "会话已过期或无行程，请重新生成行程"}, code=404)
            cname = str(plan.get("city") or "")
            if plan.get("copyMode") != "lazy":
                # 幂等：full 行程（或已补过）原样返回，不重复消耗 LLM
                return self._json({"ok": True, "plan": plan, "copyMode": plan.get("copyMode"),
                                   "regenerated": False})
            # 逐天归城：单城直接用 cname；多城（city="A+B"）用该天 POI 的 id 前缀反查
            # 所属城市（_day_city + _ID_PREFIX_CITY，与 /api/photo、换点/挪时段**同一份**
            # 注册表）。补文案必须**逐城重建 all_pois**再送 _regen_reasons —— 单一
            # load_city("A+B") 必失败，这正是 lazy 在多城下此前被拒的根因。
            groups = {}
            for d in plan["dayList"]:
                if "+" in cname:
                    _ids = [s.get("id") for s in (d.get("timeline") or [])
                            if s.get("type") == "poi"]
                    c = _day_city(cname, _ids) or ""
                else:
                    c = cname
                groups.setdefault(c, []).append(d)
            regen, unfilled = {}, []
            try:
                from src import m2_planner as _m2
                for c, days_of_c in groups.items():
                    if not c:  # 定位不出城的那些天：绝不静默假装补过
                        unfilled.extend(d.get("day") for d in days_of_c)
                        continue
                    cc = poi_db.load_city(c)
                    all_pois = {p["id"]: p for p in
                                (poi_db.parse_poi(p, cc) for p in cc["pois"])}
                    # 会话里的 plan 是归一化形状；dayList 的 day/weekday/finish/timeline
                    # （type/name/start/end/arrive/id）正是 _regen_reasons 消费的键。
                    _rg, okr = _m2._regen_reasons(
                        cc, plan.get("query") or "", {"days": days_of_c}, all_pois)
                    if not okr:
                        return self._json({"ok": False,
                                           "error": "文案生成本轮不可用（LLM 异常），可重试"},
                                          code=503)
                    regen.update(_rg)
            except Exception as e:  # noqa: BLE001 —— 补文案失败不能拖垮 open_plan
                stats_bump("copy_error")
                return self._json({"ok": False,
                                   "error": f"文案生成失败：{type(e).__name__}: {e}"}, code=500)
            for d in plan["dayList"]:
                info = regen.get(d.get("day")) or {}
                if info.get("theme"):
                    d["theme"] = info["theme"]
                if info.get("reason"):
                    d["reason"] = info["reason"]
                if info.get("tips"):
                    d["tips"] = info["tips"]
            if unfilled:
                # 有天定位不出城 → 文案补不齐：**保持 lazy 供重试**，不置 full 掩盖缺口
                stats_bump("copy_partial")
                plan["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _session_put(sess, plan)
                return self._json({"ok": True, "plan": plan, "copyMode": "lazy",
                                   "regenerated": False, "unfilled": sorted(unfilled)})
            plan["copyMode"] = "full"          # 补完置 full ⇒ 天然幂等
            plan["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _session_put(sess, plan)           # 会话里的当前行程跟着更新
            stats_bump("copy_finalized")
            return self._json({"ok": True, "plan": plan, "copyMode": "full",
                               "regenerated": True,
                               "days": sorted(regen.keys())})
        if u.path == "/api/log":  # M5：面板动作轨迹（面板改走线上后，落点搬到云端）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = (json.loads(self.rfile.read(n).decode("utf-8"))
                           if 0 < n <= 65536 else None)
            except Exception:  # noqa: BLE001 —— 观测入口永不因脏输入报错
                payload = None
            if isinstance(payload, dict):
                _panel_log(payload)
            return self._json({"ok": True})
        if u.path == "/api/share":  # P1-2：保存行程快照，返回短 id
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
                 self.client_address[0]
            if not rate_allow(ip):
                return self._json({"ok": False, "error": "请求过于频繁，请稍后再试"}, code=429)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 3_000_000 else None
            except (ValueError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                return self._json({"ok": False, "error": "分享内容格式错误"}, code=400)
            os.makedirs(SHARES_DIR, exist_ok=True)
            sid = uuid.uuid4().hex[:8]
            try:
                with open(os.path.join(SHARES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"city_meta": payload.get("city_meta") or {},
                               "result": payload["result"]}, f, ensure_ascii=False)
            except OSError as e:
                return self._json({"ok": False, "error": f"保存失败: {e}"}, code=500)
            return self._json({"ok": True, "id": sid})
        if u.path == "/api/replace":  # P2-1 反馈闭环：不喜欢某点 → 同类目邻近换点并重排
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 1_000_000 else None
            except (ValueError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "请求体格式错误"}, code=400)
            cname = payload.get("city")
            poi_id, day = payload.get("poi_id"), payload.get("day")
            day_ids = payload.get("day_ids") or []
            # 多城联游：`city` 是拼接串 ⇒ 按被换点的 id 前缀定位到「那一天所在的那个城」
            cname = _day_city(cname, [poi_id] + list(day_ids))
            if not cname or not poi_id or not day or not day_ids:
                return self._json({"ok": False, "error": "参数缺失（city/day/poi_id/day_ids）"}, code=400)
            # 逻辑已抽到 src/poi_swap.py —— MCP App 面板的「换一个」按钮共用同一实现，
            # 避免两处各写一遍后漂移（行为与原内联实现逐字段一致）。
            code, data = poi_swap.replace_poi(
                cname, poi_id, day, day_ids,
                used_ids=payload.get("used_ids") or [],
                date0=payload.get("date0") or None,
                query=payload.get("query") or "",
                hotel_text=payload.get("hotel_text") or None)
            if data.get("ok"):
                # M5：归一化 —— 换点回的是内核形状的 day（无 photo / 坐标），
                # 而瘦客户端没有 `data/photo_cache.json` 与 POI 库可补，**只能在云端补**。
                # 不补的后果实测：面板「换一个」之后缩略图与地图点一起消失。
                # 与 /api/move 保持同一处理（两处必须对称）。
                data["day"] = plan_view.normalize_day(
                    data["day"], plan_view.poi_coords(cname))
                # 换点后 reason/tips 已被刻意清空（keep_day_meta 只保 theme，旧文案
                # 逐点点名已过期）⇒ 按新时间轴补写当天文案。lazy 行程跳过
                # （open_plan 定稿统一补）；失败 fail-open = 空文案现状。
                _plan_in = payload.get("plan")
                if not (isinstance(_plan_in, dict) and _plan_in.get("copyMode") == "lazy"):
                    entry = _regen_one_day_copy(
                        cname, payload.get("query") or "", data["day"])
                    if entry:
                        data["day"].update(
                            {k: v for k, v in entry.items() if v})
                # M5：会话表同步（面板带了整份 plan 才能复现 apply_day）
                _session_apply(payload.get("session") or "", payload.get("plan"),
                               data["day"])
            return self._json(data, code=code)

        # ------------- M3：面板交互的远端兜底端点 -------------
        # 面板侧「本地优先、线上兜底」——本地常驻进程被宿主回收时，交互自动切到这里。
        # 入参**自包含**（day_ids / ordered_ids / plan 全由面板带上）⇒ 本服务无需保存
        # 任何会话状态，天然支持多用户 / 多面板并发。
        if u.path in ("/api/move", "/api/export", "/api/remove"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 3_000_000 else None
            except (ValueError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "请求体格式错误"}, code=400)

            if u.path == "/api/remove":  # 2026-10-13：主动减点（腾时间预算）
                # 与 /api/move 同一实现（reorder_day 天然支持点集变小），
                # 唯一差别：**不做「点集必须一致」的守门** —— 那正是本端点的目的。
                day = payload.get("day")
                ordered = [str(x) for x in (payload.get("ordered_ids") or [])]
                day_ids = [str(x) for x in (payload.get("day_ids") or [])]
                cname = _day_city(payload.get("city"), ordered or day_ids)
                if not cname or not day or not ordered:
                    return self._json({"ok": False,
                                       "error": "参数缺失（city/day/ordered_ids）"}, code=400)
                # 安全约束（比 move 松、但仍有底线）：新点集必须是原点集的**真子集**，
                # 且至少少 1 个 —— 防止误用本端点**增点**或换点。
                if day_ids and not (set(ordered) < set(day_ids)):
                    return self._json({"ok": False, "error":
                                       "减点要求新点集是当天点集的真子集（不做增点/换点）"})
                code, data = poi_swap.reorder_day(
                    cname, day, ordered,
                    date0=payload.get("date0") or None,
                    query=payload.get("query") or "",
                    hotel_text=payload.get("hotel_text") or None)
                if data.get("ok"):
                    data["day"] = plan_view.normalize_day(
                        data["day"], plan_view.poi_coords(cname))
                    _plan_in = payload.get("plan")
                    if not (isinstance(_plan_in, dict)
                            and _plan_in.get("copyMode") == "lazy"):
                        entry = _regen_one_day_copy(
                            cname, payload.get("query") or "", data["day"])
                        if entry:
                            data["day"].update(
                                {k: v for k, v in entry.items() if v})
                    _session_apply(payload.get("session") or "", payload.get("plan"),
                                   data["day"])
                return self._json(data, code=code)

            if u.path == "/api/move":  # 挪时段：按给定新顺序重排某一天
                day = payload.get("day")
                ordered = [str(x) for x in (payload.get("ordered_ids") or [])]
                day_ids = [str(x) for x in (payload.get("day_ids") or [])]
                # 多城联游：同 /api/replace —— 用当天点的 id 前缀定位生效城市
                cname = _day_city(payload.get("city"), ordered or day_ids)
                if not cname or not day or not ordered:
                    return self._json({"ok": False, "error": "参数缺失（city/day/ordered_ids）"}, code=400)
                # 守门：拖拽只改顺序，**不允许**悄悄丢点或加点（与本地 MCP 侧同一条规则）
                if day_ids and set(ordered) != set(day_ids):
                    return self._json({"ok": False, "error":
                                       f"新顺序的点集与当天不一致（原 {len(day_ids)} 个，"
                                       f"收到 {len(ordered)} 个）"})
                code, data = poi_swap.reorder_day(
                    cname, day, ordered,
                    date0=payload.get("date0") or None,
                    query=payload.get("query") or "",
                    hotel_text=payload.get("hotel_text") or None)
                if data.get("ok"):  # 归一化：补照片 / 坐标，面板无需再自己拼
                    data["day"] = plan_view.normalize_day(
                        data["day"], plan_view.poi_coords(cname))
                    # 与 /api/replace 对称：挪时段后顺序变了，旧 reason 的「依次
                    # 游览 A→B→C」同样过期 ⇒ 按新顺序补写当天文案（lazy 跳过）。
                    _plan_in = payload.get("plan")
                    if not (isinstance(_plan_in, dict)
                            and _plan_in.get("copyMode") == "lazy"):
                        entry = _regen_one_day_copy(
                            cname, payload.get("query") or "", data["day"])
                        if entry:
                            data["day"].update(
                                {k: v for k, v in entry.items() if v})
                    # M5：会话表同步（面板带了整份 plan 才能复现 apply_day）
                    _session_apply(payload.get("session") or "", payload.get("plan"),
                                   data["day"])
                return self._json(data, code=code)

            # /api/export：把面板传来的行程导出为 Markdown
            plan = payload.get("plan")
            if not isinstance(plan, dict):
                return self._json({"ok": False, "error": "缺少 plan（要导出的行程内容）"}, code=400)
            fmt = str(payload.get("format") or "markdown").lower()
            if fmt not in ("markdown", "md"):
                return self._json({"ok": False, "error": f"暂不支持的导出格式：{fmt}"}, code=400)
            text = md_export.plan_to_markdown(plan)
            return self._json({"ok": True, "filename": md_export.export_filename(plan),
                               "mimeType": "text/markdown", "text": text})

        return self._json({"error": "not found"}, code=404)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


class QuickBindServer(ThreadingHTTPServer):
    def server_bind(self):
        # 跳过 HTTPServer 默认的 socket.getfqdn() 反向解析——
        # DNS 异常环境下它会阻塞启动 1~2 分钟（本服务用不到 server_name）
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def main():
    global CFG
    cfg = load_config()
    CFG = cfg
    # 云端部署：PORT 环境变量注入 + 绑 0.0.0.0；本地：argv[1] 或默认 8765
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    srv = QuickBindServer(("0.0.0.0", port), Handler)
    print(f"TripAgent Web UI → http://127.0.0.1:{port} "
          f"(LLM {'可用' if llm_client.llm_available() else '不可用，离线兜底'}"
          f"{'，config.json 已加载' if cfg else '，未找到 config.json（可用环境变量或页面自带 Key）'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
