# -*- coding: utf-8 -*-
"""POI 库加载与地理计算。"""
import datetime
import json, math, os

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def trip_weekday(date_str: str, day_no: int) -> str:
    """行程第 day_no 天（从 date_str 起算）的星期名，如 trip_weekday('2026-09-14', 1)='周一'。"""
    d = datetime.date.fromisoformat(date_str)
    return WEEKDAY_NAMES[(d + datetime.timedelta(days=day_no - 1)).weekday()]

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 市内混合交通（地铁+打车）有效速度假设：直线 ×1.4 绕路系数 / 18 km/h
CIRCUITY = 1.4
SPEED_KMH = 18.0
MIN_TRAVEL_H = 0.25


def load_city(name: str = "杭州") -> dict:
    path = os.path.join(_DATA_DIR, f"{name}_pois.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_travel_cache = None


def _load_travel_cache():
    """L1 交通矩阵（OSRM 真实路网，scripts/build_travel_cache.py 构建）。"""
    global _travel_cache
    if _travel_cache is None:
        path = os.path.join(_DATA_DIR, "travel_cache.json")
        _travel_cache = json.load(open(path, encoding="utf-8"))["minutes"] \
            if os.path.exists(path) else {}
    return _travel_cache


def travel_hours(p1: dict, p2: dict) -> float:
    """两 POI 间通行时间（小时）。L1 缓存优先（OSRM 路网），L3 直线兜底。"""
    if p1.get("id") and p2.get("id"):
        m = _load_travel_cache().get(p1["id"], {}).get(p2["id"])
        if m is not None:
            return max(MIN_TRAVEL_H, m / 60.0)
    km = haversine_km(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
    return max(MIN_TRAVEL_H, km * CIRCUITY / SPEED_KMH)


def hhmm_to_h(s: str) -> float:
    h, m = s.split(":")
    return int(h) + int(m) / 60.0


def parse_poi(p: dict, city: dict) -> dict:
    """给 POI 补充数值化字段。"""
    q = dict(p)
    q["open_h"] = hhmm_to_h(p["open"])
    q["close_h"] = hhmm_to_h(p["close"])
    q["dur"] = float(p["duration_h"])
    q["dist_center_km"] = haversine_km(p["lat"], p["lng"], city["center"]["lat"], city["center"]["lng"])
    return q
