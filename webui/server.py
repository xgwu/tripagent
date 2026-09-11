# -*- coding: utf-8 -*-
"""TripAgent Web UI 原型：纯 stdlib HTTP 服务（零第三方依赖）。

路由：
  GET /                 → webui/index.html（前端单页）
  GET /api/cities       → 5 城元信息（POI 数、闭馆数、坐标）+ LLM 可用性
  GET /api/plan         → 规划（city/query/days/date/hotel/mode/llm），包装 m1/m2 链路

启动：python webui/server.py [port]   （默认 8765，绑定 127.0.0.1）
"""
import io, json, os, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src import poi_db, m1_planner, llm_client  # m2_planner 懒加载：云端 ortools 缺失也不阻塞启动

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
                             ("AMAP_KEY", "amap_key")):
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
                               "amap_key": CFG.get("amap_key", "")})
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
                days = max(1, min(4, int(q.get("days", "2"))))
                date0 = q.get("date") or None
                hotel_text = q.get("hotel") or None
                llm_key = self.headers.get("X-LLM-Key", "").strip()  # 访客自带 Key（不落盘）
                use_llm = q.get("llm", "1") == "1" and bool(llm_key or llm_client.llm_available())
                mode = q.get("mode", "m2")
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
                return self._json({"ok": True, "city_meta": city_meta(cname), "result": r})
            except Exception as e:  # noqa: 单请求异常不挂服务
                stats_bump("plan_error")
                import traceback
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-900:]}, code=500)
        return self._json({"error": "not found"}, code=404)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


def main():
    global CFG
    cfg = load_config()
    CFG = cfg
    # 云端部署：PORT 环境变量注入 + 绑 0.0.0.0；本地：argv[1] 或默认 8765
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"TripAgent Web UI → http://127.0.0.1:{port} "
          f"(LLM {'可用' if llm_client.llm_available() else '不可用，离线兜底'}"
          f"{'，config.json 已加载' if cfg else '，未找到 config.json（可用环境变量或页面自带 Key）'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
