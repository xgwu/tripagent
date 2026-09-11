# -*- coding: utf-8 -*-
"""TripAgent Web UI 原型：纯 stdlib HTTP 服务（零第三方依赖）。

路由：
  GET /                 → webui/index.html（前端单页）
  GET /api/cities       → 5 城元信息（POI 数、闭馆数、坐标）+ LLM 可用性
  GET /api/plan         → 规划（city/query/days/date/hotel/mode/llm），包装 m1/m2 链路

启动：python webui/server.py [port]   （默认 8765，绑定 127.0.0.1）
"""
import io, json, os, re, sys, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src import poi_db, m1_planner, llm_client  # m2_planner 懒加载：云端 ortools 缺失也不阻塞启动

# ---- 直连 opener：本机代理会拦截外网 API，urllib 需显式绕过 ----
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---- 路径规划缓存（P1-1：高德骑行/步行实际路网） ----
ROUTE_CACHE_PATH = os.path.join(ROOT, "data", "route_cache.json")
_route_lock = threading.Lock()
_route_cache = None
SHARES_DIR = os.path.join(ROOT, "data", "shares")

# ---- POI 实拍照片（P3：高德 place/text photos 字段，磁盘缓存，302 跳转图库直链） ----
PHOTO_CACHE_PATH = os.path.join(ROOT, "data", "photo_cache.json")
_photo_lock = threading.Lock()
_photo_cache = None
_ID_PREFIX_CITY = {"SH": "上海", "HZ": "杭州", "NJ": "南京", "SZ": "苏州", "WH": "武汉"}
_CITYCODE = {"上海": "021", "杭州": "0571", "南京": "025", "苏州": "0512", "武汉": "027"}


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
    """
    m = re.search(r"([1-5一二两三四五])\s*[天日]", query or "")
    if not m:
        return None
    c = m.group(1)
    return int(c) if c.isdigit() else _CN_NUM[c]


# P2-2 多城联游：查询中出现 ≥2 个城市（或「苏杭」类别名）→ 跨城规划
CITY_ALIAS = {"苏杭": ["苏州", "杭州"], "杭苏": ["杭州", "苏州"]}


def detect_multi_city(query: str) -> list:
    found = [c for c in CITIES if c in (query or "")]
    if len(found) >= 2:
        return found
    for alias, pair in CITY_ALIAS.items():
        if alias in (query or "") and not any(c in query for c in pair):
            return pair
    return []


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
CITIES = ["杭州", "南京", "上海", "苏州", "武汉"]
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
    """从项目根目录 config.json 读取服务端密钥（仅注入缺失的环境变量，不覆盖已有值）。"""
    cfg = {}
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            cfg = {k: str(v).strip() for k, v in raw.items()
                   if not k.startswith("_") and isinstance(v, str) and v.strip()}
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠ config.json 解析失败（忽略）: {e}", flush=True)
    for env_key, cfg_key in (("DEEPSEEK_API_KEY", "deepseek_api_key"),
                             ("AMAP_KEY", "amap_key"),
                             ("M1_MODEL", "m1_model")):
        if cfg.get(cfg_key) and not os.environ.get(env_key):
            os.environ[env_key] = cfg[cfg_key]
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
                               "amap_key": CFG.get("amap_js_key") or CFG.get("amap_key", ""),
                               "staticmap_key": CFG.get("amap_key", "")})  # P2-3 静态图缩略
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
        if u.path == "/api/plan":
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
                 self.client_address[0]
            if not rate_allow(ip):
                stats_bump("plan_rate_limited")
                return self._json({"ok": False,
                                   "error": f"请求过于频繁（每 {int(RATE_LIMIT_WINDOW_S)} 秒最多 {RATE_LIMIT_N} 次规划），请稍后再试"}, code=429)
            stats_bump("plan_total")
            try:
                cname = q.get("city", "杭州")
                if cname not in CITIES:
                    return self._json({"ok": False, "error": f"未知城市 {cname}"}, code=400)
                query = q.get("query") or f"{cname}2天经典深度游"
                # ---- P2-2 多城联游：查询出现 ≥2 城（或「苏杭」别名）→ 跨城合并规划 ----
                multi = detect_multi_city(query)
                if len(multi) >= 2:
                    d = extract_days(query) or 2
                    days = max(1, min(5, d))
                    use_llm_m = q.get("llm", "1") == "1" and llm_client.llm_available()
                    try:
                        from src import proposal_planner as _pp
                        planner_m = _pp if use_llm_m else m1_planner
                    except ImportError:
                        planner_m = m1_planner
                    stats_bump("plan_total")
                    try:
                        r = plan_multi(multi, query, days, q.get("date") or None,
                                       use_llm=use_llm_m, planner=planner_m)
                        merged_meta = {"name": "+".join(multi), "n_pois": 0, "n_closed": 0,
                                       "pois": {}}
                        for c in multi:
                            m = city_meta(c)
                            merged_meta["pois"].update(m["pois"])
                            merged_meta["n_pois"] += m["n_pois"]
                            merged_meta["n_closed"] += m["n_closed"]
                        stats_latency(r.get("latency_s", 0))
                        return self._json({"ok": True, "city_meta": merged_meta, "result": r,
                                           "days_source": "query" if extract_days(query) else "default"})
                    except Exception as e:  # noqa
                        stats_bump("plan_error")
                        import traceback
                        return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                           "trace": traceback.format_exc()[-900:]}, code=500)
                city = poi_db.load_city(cname)
                query = q.get("query") or f"{cname}2天经典深度游"
                # 天数优先从需求文字里识别（「3天」「两日」…）；显式 days 参数仅作兼容保留；都没有则默认 2 天
                days_param = (q.get("days") or "").strip()
                if days_param:
                    days, days_src = max(1, min(5, int(days_param))), "param"
                else:
                    d = extract_days(query)
                    if d:
                        days, days_src = d, "query"
                    else:
                        days, days_src = 2, "default"
                date0 = q.get("date") or None
                hotel_text = q.get("hotel") or None
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
                if llm_key:
                    # 临时注入访客 Key → 规划 → 恢复环境（锁内串行，防并发互相覆盖）
                    with PLAN_LOCK:
                        old = os.environ.get("DEEPSEEK_API_KEY")
                        os.environ["DEEPSEEK_API_KEY"] = llm_key
                        try:
                            r = planner.plan(city, query, days, use_llm=use_llm,
                                             date0=date0, hotel_text=hotel_text)
                        finally:
                            if old is None:
                                os.environ.pop("DEEPSEEK_API_KEY", None)
                            else:
                                os.environ["DEEPSEEK_API_KEY"] = old
                else:
                    r = planner.plan(city, query, days, use_llm=use_llm,
                                     date0=date0, hotel_text=hotel_text)
                stats_latency(r.get("latency_s", 0))
                if r.get("mode") not in ("offline_fallback",):
                    stats_bump("plan_llm")
                return self._json({"ok": True, "city_meta": city_meta(cname), "result": r,
                                   "days_source": days_src})
            except Exception as e:  # noqa: 单请求异常不挂服务
                stats_bump("plan_error")
                import traceback
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-900:]}, code=500)
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
                pick = min(pool, key=lambda p: (
                    poi_db.haversine_km(p["lat"], p["lng"], replaced["lat"], replaced["lng"]),
                    -p["rating"]))
                new_ids = [pick["id"] if i == poi_id else i for i in day_ids]
                hotel = hotel_mod.resolve_hotel(city, payload.get("hotel_text") or None)
                it = sequencer.build_itinerary({int(day): new_ids}, city, all_pois,
                                               order_given=False, date0=payload.get("date0") or None,
                                               hotel=hotel, query=payload.get("query") or "")
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
