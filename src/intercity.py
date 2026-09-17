# -*- coding: utf-8 -*-
"""城际转移时长模型（跨城联游的「开到下一座城」这一段）。

为什么需要独立模型：`data/travel_cache.json` 只建**同城** POI 对
（`scripts/build_travel_cache.py` 的注释写明「跨城对不建（行程按城规划）」），
而 `poi_db.travel_hours` 的兜底是**市内**速度模型（km × 1.4 ÷ 18km/h）——
拿它算苏州→杭州（直线约 130km）会得出 ≈10 小时，比真实车程夸大 5 倍。
故城际段必须走本模块，不能复用 travel_hours。

三级降级（与 travel_cache 的 L1/L2/L3 分层同构）：
  L1 磁盘缓存   data/intercity_cache.json（城市对 → 分钟/公里/来源）
  L2 高德驾车   /v3/direction/driving 真实路网（需 amap_key）
  L3 直线兜底   haversine × 1.25 ÷ 75km/h（高速路网比市内路网顺直，
                系数取 1.25 而非市内的 1.4；75km/h 是含收费站/服务区的实测均速）

缓存键按城市名排序（A|B 与 B|A 共用一条记录）：往返车程的差异远小于
本模型的精度目标，换取一半的 API 调用与更小的缓存。
"""
import json
import math
import os
import threading
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERCITY_CACHE_PATH = os.path.join(ROOT, "data", "intercity_cache.json")

HIGHWAY_KMH = 75.0          # 高速实测均速（含收费站/服务区停顿）
INTERCITY_CIRCUITY = 1.25   # 高速路网绕行系数（市内为 poi_db.CIRCUITY=1.4）
MIN_TRANSFER_MIN = 20       # 城际段时长下限（相邻城市也不会低于此）

# 长途转移日阈值：单段车程 ≥ 该分钟数时，这一天判定为「转移日」，
# 不再安排景点（4 小时车程 + 取还车 + 午餐，实际已无有效游玩时间）。
LONG_TRANSFER_MIN = 240

# 单日最大驾驶时长：超过则一天开不完，须拆成多个转移日。
# 10 小时是自驾安全上限口径（含服务区休息），再长应换高铁/飞机而非硬开。
DRIVE_DAY_MAX_MIN = 600

# 建议改乘公共交通的阈值：车程超过该值时，纯自驾已不合理
# （项目 Roadmap P2「跨城交通衔接（高铁段作为日间转移）」尚未实现，
#  故此处只出提示，不擅自把行程改成高铁段）。
RAIL_ADVICE_MIN = 720

_lock = threading.Lock()
_cache: dict | None = None
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(INTERCITY_CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    """原子写（同 proposal_planner 的 .tmp + os.replace 口径）。"""
    try:
        tmp = INTERCITY_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache or {}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, INTERCITY_CACHE_PATH)
    except OSError:  # noqa: 缓存写失败不影响主流程
        pass


def _key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def _amap_driving(o: dict, d: dict, key: str) -> tuple:
    """L2：高德驾车路径规划。返回 (分钟, 公里) 或 (None, None)。"""
    url = ("https://restapi.amap.com/v3/direction/driving?"
           + urllib.parse.urlencode({
               "origin": f"{o['lng']},{o['lat']}",
               "destination": f"{d['lng']},{d['lat']}",
               "extensions": "base", "strategy": "0", "key": key}))
    try:
        with _OPENER.open(url, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "1":
            return None, None
        p = (data.get("route") or {}).get("paths") or []
        if not p:
            return None, None
        return int(p[0]["duration"]) / 60.0, int(p[0]["distance"]) / 1000.0
    except Exception:  # noqa: 网络/解析失败 → 交由 L3 兜底
        return None, None


def transfer(city_a: str, center_a: dict, city_b: str, center_b: dict,
             amap_key: str | None = None) -> dict:
    """城际转移段。返回 {"minutes": float, "km": float, "source": str}。

    center_*: {"lat","lng"}，取各城 `data/{city}_pois.json` 的 center 字段。
    amap_key 缺省或调用失败时自动降到 L3 直线兜底，**永不抛异常**（与全链路
    降级原则一致：城际段算不准也不能让整次规划失败）。
    """
    if not city_a or not city_b or city_a == city_b:
        return {"minutes": 0.0, "km": 0.0, "source": "same-city"}
    k = _key(city_a, city_b)
    with _lock:
        hit = _load_cache().get(k)
    if hit and hit.get("minutes") is not None:
        return {"minutes": float(hit["minutes"]), "km": float(hit.get("km") or 0.0),
                "source": "cache"}

    minutes = km = None
    if amap_key:
        minutes, km = _amap_driving(center_a, center_b, amap_key)
    source = "amap-driving"
    if minutes is None:
        km = haversine_km(center_a["lat"], center_a["lng"],
                          center_b["lat"], center_b["lng"]) * INTERCITY_CIRCUITY
        minutes = km / HIGHWAY_KMH * 60.0
        source = "haversine-highway"
    minutes = max(MIN_TRANSFER_MIN, round(minutes, 1))
    km = round(km or 0.0, 1)

    # 只缓存真实路网结果：直线兜底是「拿不到数据时的估算」，缓存它会让
    # 后续即使 key 可用也永远读到估算值（travel_cache 踩过的同类坑）。
    if source == "amap-driving":
        with _lock:
            _load_cache()[k] = {"minutes": minutes, "km": km, "source": source,
                                "pair": f"{city_a}→{city_b}"}
            _save_cache()
    return {"minutes": minutes, "km": km, "source": source}


def is_long_transfer(minutes: float) -> bool:
    """是否长途转移（该天不再安排景点）。"""
    return float(minutes or 0) >= LONG_TRANSFER_MIN


def days_needed(minutes: float) -> int:
    """该段车程需要占用几个转移日（按单日最大驾驶时长向上取整）。

    非长途返回 0（不占天）。上海→成都实测 1242min → 3 天，
    这类距离本应改乘高铁/飞机，见 needs_rail_advice。
    """
    m = float(minutes or 0)
    if m < LONG_TRANSFER_MIN:
        return 0
    return int(math.ceil(m / DRIVE_DAY_MAX_MIN))


def needs_rail_advice(minutes: float) -> bool:
    """车程过长，纯自驾不合理，应提示改乘高铁/飞机。"""
    return float(minutes or 0) >= RAIL_ADVICE_MIN


def duration_text(minutes: float) -> str:
    """人读时长：90 →「1 小时 30 分钟」；45 → 「45 分钟」。"""
    m = int(round(float(minutes or 0)))
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} 小时 {mm} 分钟"
    if h:
        return f"{h} 小时"
    return f"{mm} 分钟"
