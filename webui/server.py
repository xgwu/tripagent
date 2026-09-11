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
            "pois": {p["id"]: {"name": p["name"], "lat": p["lat"], "lng": p["lng"]}
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
                               "amap_key": CFG.get("amap_js_key") or CFG.get("amap_key", "")})
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
