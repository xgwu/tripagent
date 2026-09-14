# -*- coding: utf-8 -*-
"""TripAgent Web UI 原型：纯 stdlib HTTP 服务（零第三方依赖）。

路由：
  GET /                 → webui/index.html（前端单页）
  GET /api/cities       → 5 城元信息（POI 数、闭馆数、坐标）+ LLM 可用性
  GET /api/plan         → 规划（city/query/days/date/hotel/mode/llm），包装 m1/m2 链路

启动：python webui/server.py [port]   （默认 8765，绑定 127.0.0.1）
"""
import hashlib, io, json, os, re, sys, threading, time, urllib.parse, uuid
from datetime import date as _date, timedelta as _td
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src import poi_db, m1_planner, llm_client  # m2_planner 懒加载：云端 ortools 缺失也不阻塞启动
from src import gap_log  # POI 库缺口台账：规划缺口持久化（旁路，失败不拖垮主链路）

# ---- 直连 opener：本机代理会拦截外网 API，urllib 需显式绕过 ----
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
_ID_PREFIX_CITY = {"SH": "上海", "HZ": "杭州", "NJ": "南京", "SZ": "苏州", "WH": "武汉", "BJ": "北京", "CD": "成都"}
_CITYCODE = {"上海": "021", "杭州": "0571", "南京": "025", "苏州": "0512", "武汉": "027", "北京": "010", "成都": "028"}


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
    """高德路径规划：walking(v3) / riding(v4)。返回 {distance_m, duration_s, points}。"""
    if mode == "riding":
        url = (f"https://restapi.amap.com/v4/direction/bicycling"
               f"?origin={o}&destination={d}&key={key}")
    else:
        url = (f"https://restapi.amap.com/v3/direction/walking"
               f"?origin={o}&destination={d}&key={key}")
    with _NO_PROXY_OPENER.open(url, timeout=10) as resp:
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

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}


def extract_days(query: str):
    """从自然语言需求提取行程天数（「3天」「玩 4 天」「两日」…），提不到返回 None。

    只认 1-5（前端历史上限），「带5岁孩子」这类不会误匹配（数字后须跟 天/日）。
    先剥离日期表达式，避免「10月1日」的「1日」被误读成 1 天。
    """
    q = (query or "")
    # 日期区间「10月1日到3日」→ 天数 = 3-1+1（同月内；跨月/超上限不处理，走默认天数）
    mr = re.search(r"(\d{1,2})月(\d{1,2})[日号]\s*[到至]\s*(\d{1,2})[日号]", q)
    if mr:
        b, c = int(mr.group(2)), int(mr.group(3))
        if b <= c and 1 <= c - b + 1 <= 5:
            return c - b + 1
    q = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?", "", q)  # 2026-10-01 / 2026年10月1日
    q = re.sub(r"\d{1,2}月\d{1,2}[日号]", "", q)                   # 10月1日 / 9月30号
    # 日期残片清理（不伤「3日亲子游」这类天数表达）：
    # 两位数「11日」「30号」必是日期（行程天数上限 5）；「到3日」这类连接词后残片同理
    q = re.sub(r"\d{2}[日号]", "", q)
    q = re.sub(r"(?<=[到至,—-])\d{1,2}[日号]", "", q)
    m = re.search(r"([1-5一二两三四五])\s*[天日]", q)
    if not m:
        return None
    c = m.group(1)
    return int(c) if c.isdigit() else _CN_NUM[c]


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
    qkey = "P:" + hashlib.sha1(query.encode("utf-8")).hexdigest()
    hit = _nl_cache_get(qkey)
    if hit is not None:
        return hit or None
    today = _date.today().isoformat()
    sys_p = (
        "你是旅行行程助手的前置分析器，对用户需求一次完成「参数抽取」和「澄清判断」，"
        '只输出 JSON：{"city": "目的地城市名或null", "days": 行程天数整数或null, '
        '"date0": "出发日期YYYY-MM-DD或null", "hotel": "住宿酒店名或区域名或null", '
        '"multi_cities": ["多城联游时的城市列表或null"], '
        '"need": 是否需要追问布尔值, "question": "一句话追问或null", '
        '"options": ["选项1", "选项2"]}。\n'
        "抽取规则：相对日期（明天/下周六/月底/国庆等）以今天为基准换算；"
        "「住的地方离西湖近点」这类模糊住宿描述抽出区域名（如 西湖）；"
        "「玩一周」=7天但上限按5算；没有明确信息就填 null，不要猜。\n"
        "澄清判断规则（保守，大多数需求应 need=false 直接生成）：\n"
        "- 行程天数完全未提及（如只说「去杭州玩」）→ 可以问；已写「3天」「周末」等则不问\n"
        "- 同行人员完全未提及且明显影响节奏（亲子/老人/情侣/团队）→ 可以问；已提及则不问\n"
        "- 用户表达了强偏好但存在明显歧义（如「热闹的地方」不知指夜市还是商圈）→ 可以问\n"
        "不问的：目的地城市（未提及会用默认城市）、预算、交通方式、住宿（未提及就不排酒店）、"
        "具体日期（未提及就按近期规划）。只问最关键的一个问题，选项 2~4 个、每个不超过 12 字；"
        "不追问时 question/options 填 null。\n"
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
        d = obj.get("days")
        if isinstance(d, int) and 1 <= d <= 5:
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


def plan_multi(cities: list, query: str, days: int, date0: str | None,
               use_llm: bool, planner) -> dict:
    """跨城规划：按天均分逐城走完整规划链，合并时间轴/统计/city_meta。

    planner 为单城规划模块（m7/m1）。酒店锚点是城市专属概念，跨城模式忽略。
    """
    from datetime import date as _date
    n = min(len(cities), max(days, 1))
    use = cities[:n]
    base, rem = divmod(days, n)
    alloc = [base + (1 if i < rem else 0) for i in range(n)]

    merged_days, merged_pois, merged_gaps = [], {}, []
    tot = {"violations": 0, "km": 0.0, "dup": 0, "cands": 0, "lat": 0.0,
           "proposed": 0, "unmatched": 0, "rate_w": 0.0}
    seg_date0 = _date.fromisoformat(date0) if date0 else None
    day_no = 0
    for i, cname in enumerate(use):
        di = alloc[i]
        if di < 1:
            continue
        city = poi_db.load_city(cname)
        d0 = seg_date0.isoformat() if seg_date0 else None
        r = planner.plan(city, query, di, use_llm=use_llm, date0=d0)
        it = r["itinerary"]
        for d in it["days"]:
            day_no += 1
            d["day"] = day_no
            d["theme"] = f"{cname}｜{d.get('theme') or cname}"
            merged_days.append(d)
        meta = city_meta(cname)
        merged_pois.update(meta["pois"])
        g = r.get("grounding") or {}
        merged_gaps.extend(g.get("gaps") or [])
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
    return {
        "mode": "m7_multi",
        "days": days,
        "query": query,
        "date0": date0,
        "cities": use,
        "itinerary": {"days": merged_days, "total_violations": tot["violations"],
                      "total_travel_km": round(tot["km"], 1), "dropped_pois": []},
        "grounding": grounding,
        "n_dup_across_days": tot["dup"],
        "candidates": tot["cands"] or None,
        "latency_s": round(tot["lat"], 1),
    }

WEBUI_DIR = os.path.dirname(os.path.abspath(__file__))
CITIES = ["杭州", "南京", "上海", "苏州", "武汉", "成都", "北京"]
CITY_META = {}  # 懒加载缓存
PLAN_LOCK = threading.Lock()  # 访客自带 Key 时临时注入环境变量，加锁防并发串包

# ---- 护栏：公开链接防滥用 ----
RATE_LIMIT_N = 8          # 每 IP 每 RATE_LIMIT_WINDOW_S 秒内最多 /api/plan 次数
RATE_LIMIT_WINDOW_S = 60.0
_rate_lock = threading.Lock()
_rate = {}                # ip -> [timestamp,...]（滑动窗口，启动起累计，进程生命周期内）
_stats_lock = threading.Lock()
_stats = {"plan_total": 0, "plan_llm": 0, "plan_rate_limited": 0, "plan_error": 0,
          "export_total": 0, "start_ts": time.time(), "latency_sum": 0.0, "latency_max": 0.0}

# ---- P2 分阶段进度：前端携带 sid 发起 /api/plan，期间轮询 /api/progress 拿实时阶段 ----
# 结构 {sid: {"stage": str, "ts": float}}；规划结束即删除条目（进程生命周期内自清理）
_plan_stages: dict = {}
_plan_stages_lock = threading.Lock()


def _stage_put(sid: str, stage: str) -> None:
    with _plan_stages_lock:
        _plan_stages[sid] = {"stage": stage, "ts": time.time()}


def _stage_pop(sid: str) -> None:
    with _plan_stages_lock:
        _plan_stages.pop(sid, None)


def _plan_extra_kw(planner, progress_cb) -> dict:
    """规划器签名兼容：仅当其 plan() 支持 progress 参数时才透传（m1 无此参数）。"""
    if not progress_cb:
        return {}
    import inspect
    try:
        if "progress" in inspect.signature(planner.plan).parameters:
            return {"progress": progress_cb}
    except (TypeError, ValueError):  # noqa: 内置/异常签名 → 不透传
        pass
    return {}


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
    return cfg


def city_meta(name: str) -> dict:
    if name not in CITY_META:
        city = poi_db.load_city(name)
        CITY_META[name] = {
            "name": name,
            "n_pois": len(city["pois"]),
            "n_closed": sum(1 for p in city["pois"] if p.get("closed_days")),
            "pois": {p["id"]: {"id": p["id"], "name": p["name"], "lat": p["lat"], "lng": p["lng"]}
                     for p in city["pois"]},
        }
    return CITY_META[name]


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "index.html missing"}, code=500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/" or u.path == "/index.html":
            return self._html(os.path.join(WEBUI_DIR, "index.html"))
        if u.path == "/api/cities":
            return self._json({"cities": [city_meta(c) for c in CITIES],
                               "llm": llm_client.llm_available(),
                               "amap_key": CFG.get("amap_js_key") or ""})
            # 安全：前端只拿 JSAPI key（本就设计为公开）；Web 服务 key（amap_key）
            # 不出服务端——原 staticmap_key 字段为死代码已移除，js_key 缺失时
            # 也不再 fallback 到 Web 服务 key。
        if u.path == "/api/route":  # P1-1：实际路网路径（骑行/步行），带磁盘缓存
            mode = q.get("mode", "walking")
            o, d = _norm_ll(q.get("o") or ""), _norm_ll(q.get("d") or "")
            key = CFG.get("amap_key", "")
            if mode not in ("walking", "riding") or not o or not d:
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
            return self._json({"ok": True, "stage": ent["stage"],
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
            if not rate_allow(ip):
                stats_bump("plan_rate_limited")
                return self._json({"ok": False,
                                   "error": f"请求过于频繁（每 {int(RATE_LIMIT_WINDOW_S)} 秒最多 {RATE_LIMIT_N} 次规划），请稍后再试"}, code=429)
            stats_bump("plan_total")
            # P2 分阶段进度：前端带 sid 发起，规划期间可轮询 /api/progress?sid=
            sid = (q.get("sid") or "")[:40]
            if sid:
                _stage_put(sid, "parse")
            progress_cb = (lambda s: _stage_put(sid, s)) if sid else None
            try:
                query = q.get("query") or ""
                # ---- P5 NL 提取健壮化：正则未命中时 LLM 结构化抽取兜底（llm=0 离线路径不触发） ----
                nl = llm_extract(query) if (q.get("llm", "1") == "1" and query) else None
                # ---- P4 纯自然语言：城市可从需求文字识别，未提及用默认城市 ----
                multi = detect_multi_city(query)
                if not multi and nl and nl.get("multi_cities"):
                    multi = nl["multi_cities"]
                cname = (q.get("city") or "").strip()
                found = detect_cities(query) if not cname else []
                if not cname:
                    cname = found[0] if found else (nl or {}).get("city") or DEFAULT_CITY
                    if not found and cname == DEFAULT_CITY and (nl or {}).get("city_raw") \
                            and nl["city_raw"] not in CITIES:
                        return self._json({"ok": False,
                                           "error": f"暂不支持目的地「{nl['city_raw']}」"
                                                    f"（当前支持：{'、'.join(CITIES)}），"
                                                    "可换支持城市或直接说「上海/杭州…3天」"},
                                          code=400)
                if cname not in CITIES:
                    return self._json({"ok": False,
                                       "error": f"未能识别目的地城市（当前支持：{'、'.join(CITIES)}）。试试在需求里写明，如「杭州3天…」"},
                                      code=400)
                if not query:
                    query = f"{cname}2天经典深度游"
                # ---- P2-2 多城联游：查询出现 ≥2 城（或「苏杭」别名）→ 跨城合并规划 ----
                if len(multi) >= 2:
                    d = extract_days(query) or (nl or {}).get("days") or 2
                    days = max(1, min(5, d))
                    use_llm_m = q.get("llm", "1") == "1" and llm_client.llm_available()
                    try:
                        from src import proposal_planner as _pp
                        planner_m = _pp if use_llm_m else m1_planner
                    except ImportError:
                        planner_m = m1_planner
                    stats_bump("plan_total")
                    try:
                        m_date0 = q.get("date") or extract_date(query) or (nl or {}).get("date0")
                        r = plan_multi(multi, query, days, m_date0,
                                       use_llm=use_llm_m, planner=planner_m)
                        merged_meta = {"name": "+".join(multi), "n_pois": 0, "n_closed": 0,
                                       "pois": {}}
                        for c in multi:
                            m = city_meta(c)
                            merged_meta["pois"].update(m["pois"])
                            merged_meta["n_pois"] += m["n_pois"]
                            merged_meta["n_closed"] += m["n_closed"]
                        stats_latency(r.get("latency_s", 0))
                        # 缺口台账：跨城合并后的各城缺口一并落账
                        if gap_log.append_gap_record(None, multi, query, days,
                                                     "m7_multi", r.get("grounding")):
                            stats_bump("plan_with_gaps")
                        return self._json({"ok": True, "city_meta": merged_meta, "result": r,
                                           "days_source": "query" if extract_days(query) else "default",
                                           "parsed": {"city": "+".join(multi), "date0": m_date0,
                                                      "hotel_text": None}})
                    except Exception as e:  # noqa
                        stats_bump("plan_error")
                        import traceback
                        return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                           "trace": traceback.format_exc()[-900:]}, code=500)
                city = poi_db.load_city(cname)
                # 天数优先从需求文字里识别（「3天」「两日」…）；显式 days 参数仅作兼容保留；都没有则默认 2 天
                days_param = (q.get("days") or "").strip()
                if days_param:
                    days, days_src = max(1, min(5, int(days_param))), "param"
                else:
                    d = extract_days(query)
                    if d:
                        days, days_src = d, "query"
                    elif (nl or {}).get("days"):
                        days, days_src = nl["days"], "llm"
                    else:
                        days, days_src = 2, "default"
                # P4：日期/住宿优先显式参数，缺省时从需求文字提取（正则 miss 再用 LLM 兜底）
                date0 = q.get("date") or extract_date(query) or (nl or {}).get("date0")
                hotel_text = q.get("hotel") or extract_hotel(query) or (nl or {}).get("hotel")
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
                # P0-3 LLM 结果缓存：m7+LLM 路径且非访客 Key 时，7 天内同参数直接秒回（?fresh=1 跳过）
                cache_key = None
                if use_llm and mode == "m7" and not llm_key and q.get("fresh", "0") != "1":
                    cache_key = _llm_cache_key(cname, query, days, date0, hotel_text, mode)
                    cached = _llm_cache_get(cache_key)
                    if cached is not None:
                        stats_bump("plan_cache_hit")
                        stats_latency(cached.get("latency_s", 0))
                        # 缺口台账：缓存回放同样代表真实用户需求缺口
                        if gap_log.append_gap_record(cname, None, query, days, mode,
                                                     cached.get("grounding")):
                            stats_bump("plan_with_gaps")
                        return self._json({"ok": True, "city_meta": city_meta(cname), "result": cached,
                                           "days_source": days_src,
                                           "parsed": {"city": cname, "date0": date0,
                                                      "hotel_text": hotel_text,
                                                      "from_query": bool(query and (extract_days(query) or date0 or hotel_text))}})
                if llm_key:
                    # 临时注入访客 Key → 规划 → 恢复环境（锁内串行，防并发互相覆盖）
                    with PLAN_LOCK:
                        old = os.environ.get("DEEPSEEK_API_KEY")
                        os.environ["DEEPSEEK_API_KEY"] = llm_key
                        try:
                            r = planner.plan(city, query, days, use_llm=use_llm,
                                             date0=date0, hotel_text=hotel_text,
                                             **_plan_extra_kw(planner, progress_cb))
                        finally:
                            if old is None:
                                os.environ.pop("DEEPSEEK_API_KEY", None)
                            else:
                                os.environ["DEEPSEEK_API_KEY"] = old
                    cache_key = None  # 访客 Key 结果不落缓存
                else:
                    r = planner.plan(city, query, days, use_llm=use_llm,
                                     date0=date0, hotel_text=hotel_text,
                                     **_plan_extra_kw(planner, progress_cb))
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
                return self._json({"ok": True, "city_meta": city_meta(cname), "result": r,
                                   "days_source": days_src,
                                   "parsed": {"city": cname, "date0": date0,
                                              "hotel_text": hotel_text,
                                              "from_query": bool(query and (extract_days(query) or date0 or hotel_text))}})
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
            if cname not in CITIES or not poi_id or not day or not day_ids:
                return self._json({"ok": False, "error": "参数缺失（city/day/poi_id/day_ids）"}, code=400)
            try:
                from src import hotel as hotel_mod, sequencer
                city = poi_db.load_city(cname)
                all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
                replaced = all_pois.get(poi_id)
                if not replaced:
                    return self._json({"ok": False, "error": f"POI {poi_id} 不存在"}, code=404)
                exclude = set(payload.get("used_ids") or []) | set(day_ids)  # 含被换点本身
                cands = [p for p in all_pois.values() if p["id"] not in exclude]
                if not cands:
                    return self._json({"ok": False, "error": "候选池已空，无点可换"})
                same_cat = [p for p in cands if p["category"] == replaced["category"]]
                pool = same_cat or cands  # 同类目优先；没有则放宽到全部
                pool.sort(key=lambda p: (
                    poi_db.haversine_km(p["lat"], p["lng"], replaced["lat"], replaced["lng"]),
                    -p["rating"]))
                hotel = hotel_mod.resolve_hotel(city, payload.get("hotel_text") or None)
                date0, query = payload.get("date0") or None, payload.get("query") or ""
                # 逐候选试排：换点不得改变当天景点数——
                # 新点若引发闭馆/餐窗/里程违规，build_itinerary 修复链会剔点（含前置修剪），
                # 因此只接受「零剔点且景点一一对应」的候选，按距离从近到远试到成功为止
                pick = it = new_ids = None
                for cand in pool[:25]:
                    trial_ids = [cand["id"] if i == poi_id else i for i in day_ids]
                    trial = sequencer.build_itinerary({int(day): trial_ids}, city, all_pois,
                                                      order_given=False, date0=date0,
                                                      hotel=hotel, query=query)
                    d0 = trial["days"][0]
                    tl_ids = [s["id"] for s in d0["timeline"] if s.get("type") == "poi"]
                    if not d0.get("dropped") and len(tl_ids) == len(trial_ids) \
                            and set(tl_ids) == set(trial_ids):
                        pick, it, new_ids = cand, trial, trial_ids
                        break
                if pick is None:
                    return self._json({"ok": False,
                                       "error": "附近的候选点都会挤掉当天其他行程，已保留原行程；可换一天或稍后再试"})
                return self._json({"ok": True,
                                   "swap": {"from": replaced["name"], "to": pick["name"],
                                            "same_category": bool(same_cat)},
                                   "new_id": pick["id"], "day": it["days"][0]})
            except Exception as e:  # noqa
                import traceback
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-600:]}, code=500)
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
