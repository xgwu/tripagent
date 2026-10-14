# -*- coding: utf-8 -*-
"""飞机段时长模型（跨城联游的「飞到下一座城」这一段）。

为什么需要：`src/intercity.py` 的驱动模型对疆内长线完全失效 —— 乌鲁木齐→喀什
直线 1065km，驾车 18 小时（按 DRIVE_DAY_MAX_MIN=10h 要拆 2 个转移日），而这段
真实形态是「飞 1 小时 25 分」。用户写「坐飞机去喀什」时，行程必须按飞机段排。

**门到门口径**（不是飞行时长）：市区→机场 + 候机安检 + 起降滑行 + 巡航
+ 落地提取 + 机场→市区。国内航线单看飞行时长会严重低估，实测上海→乌鲁木齐
「飞行 5h」对应「门到门 8h+」。故本模型给的是**门到门**，与 intercity 的
驾车门到门同口径，两者可直接比较。

三级降级（与 intercity 同构）：
  L1 磁盘缓存   data/intercity_cache.json（键带 `flight:` 前缀，与驾车记录并存）
  L2 直线估算   本模块（机场表 + 巡航速度 + 固定地面耗时项）
  （机场缺失 → 返回 None，由调用方回落驾车模型并出 notice）

⚠️ 机场表是**静态数据**（坐标来自公开机场资料），不依赖任何在线服务：
    飞行时长本就受航路/机型影响大，本模型的目标是「量级正确 + 不荒谬」，
    不是分钟级精确（精确值以购票为准，行程里已如实披露）。
"""
import json
import math
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERCITY_CACHE_PATH = os.path.join(ROOT, "data", "intercity_cache.json")

# ---- 时长参数（门到门口径）----
FLIGHT_TAXI_MIN = 30.0        # 起降 + 滑行（离港 + 进港合计）
FLIGHT_CRUISE_KMH = 750.0     # 巡航均速（含爬升/下降的等效值）
AIRPORT_DWELL_MIN = 90.0      # 建议提前到机场（国内航班值机/安检/登机）
AIRPORT_LAND_MIN = 30.0       # 落地 → 出站（滑行/下机/提取行李）
AIRPORT_GROUND_KMH = 55.0     # 市区 ↔ 机场地面交通均速（含红绿灯/高速混合）
AIRPORT_GROUND_CIRCUITY = 1.30  # 地面绕行系数（机场多在郊区，比市内顺直但高于直线）
MIN_FLIGHT_KM = 300.0         # 空中距离短于此不建议乘机（高铁/自驾更快）

# 机场表：城市 → {code, name, lat, lng}。坐标为机场基准点（非航站楼）。
# 覆盖 12 主城 + 贵州 19 目的地 + 新疆 12 片区（本项目全部在库城市）。
AIRPORTS = {
    # ---- 12 主城 ----
    "上海": {"code": "SHA", "name": "上海虹桥国际机场", "lat": 31.1979, "lng": 121.3363},
    "杭州": {"code": "HGH", "name": "杭州萧山国际机场", "lat": 30.2295, "lng": 120.4344},
    "南京": {"code": "NKG", "name": "南京禄口国际机场", "lat": 31.7420, "lng": 118.8620},
    # 苏州无民航机场，最近为无锡硕放（约 30km，跨市）——如实标注
    "苏州": {"code": "WUX", "name": "苏南硕放国际机场", "lat": 31.4944, "lng": 120.4292},
    "武汉": {"code": "WUH", "name": "武汉天河国际机场", "lat": 30.7838, "lng": 114.2081},
    "成都": {"code": "CTU", "name": "成都双流国际机场", "lat": 30.5785, "lng": 103.9471},
    "北京": {"code": "PEK", "name": "北京首都国际机场", "lat": 40.0799, "lng": 116.6031},
    "广州": {"code": "CAN", "name": "广州白云国际机场", "lat": 23.3924, "lng": 113.2988},
    "盐城": {"code": "YNZ", "name": "盐城南洋国际机场", "lat": 33.4258, "lng": 120.2031},
    "西安": {"code": "XIY", "name": "西安咸阳国际机场", "lat": 34.4471, "lng": 108.7516},
    "重庆": {"code": "CKG", "name": "重庆江北国际机场", "lat": 29.7192, "lng": 106.6417},
    "长沙": {"code": "CSX", "name": "长沙黄花国际机场", "lat": 28.1892, "lng": 113.2196},
    # ---- 贵州 19 目的地 ----
    "贵阳": {"code": "KWE", "name": "贵阳龙洞堡国际机场", "lat": 26.5385, "lng": 106.8007},
    "黄果树": {"code": "AVA", "name": "安顺黄果树机场", "lat": 26.2606, "lng": 105.8733},
    "兴义": {"code": "ACX", "name": "兴义万峰林机场", "lat": 25.0833, "lng": 104.9583},
    "万峰林": {"code": "ACX", "name": "兴义万峰林机场", "lat": 25.0833, "lng": 104.9583},
    "小七孔": {"code": "LLB", "name": "黔南荔波机场", "lat": 25.4525, "lng": 107.9617},
    "六盘水": {"code": "LPF", "name": "六盘水月照机场", "lat": 26.6056, "lng": 104.9794},
    "凯里": {"code": "KJH", "name": "凯里黄平机场", "lat": 26.9722, "lng": 107.9833},
    "镇远古城": {"code": "KJH", "name": "凯里黄平机场", "lat": 26.9722, "lng": 107.9833},
    "遵义": {"code": "ZYI", "name": "遵义新舟机场", "lat": 27.5892, "lng": 106.9997},
    "西江千户苗寨": {"code": "KJH", "name": "凯里黄平机场", "lat": 26.9722, "lng": 107.9833},
    "赤水": {"code": "LZO", "name": "泸州云龙机场", "lat": 28.8522, "lng": 105.3928},
    "安顺": {"code": "AVA", "name": "安顺黄果树机场", "lat": 26.2606, "lng": 105.8733},
    "铜仁": {"code": "TEN", "name": "铜仁凤凰机场", "lat": 27.8833, "lng": 109.3167},
    "梵净山": {"code": "TEN", "name": "铜仁凤凰机场", "lat": 27.8833, "lng": 109.3167},
    "中国天眼": {"code": "KWE", "name": "贵阳龙洞堡国际机场", "lat": 26.5385, "lng": 106.8007},
    "织金洞": {"code": "BFJ", "name": "毕节飞雄机场", "lat": 27.2667, "lng": 105.4667},
    "都匀": {"code": "KWE", "name": "贵阳龙洞堡国际机场", "lat": 26.5385, "lng": 106.8007},
    "肇兴侗寨": {"code": "HZH", "name": "黎平机场", "lat": 26.2497, "lng": 109.1900},
    "百里杜鹃": {"code": "BFJ", "name": "毕节飞雄机场", "lat": 27.2667, "lng": 105.4667},
    # ---- 新疆 12 片区 ----
    "乌鲁木齐": {"code": "URC", "name": "乌鲁木齐天山国际机场", "lat": 43.9071, "lng": 87.4742},
    "喀纳斯": {"code": "KJI", "name": "布尔津喀纳斯机场", "lat": 48.2222, "lng": 86.9958},
    "伊犁": {"code": "YIN", "name": "伊宁机场", "lat": 43.9558, "lng": 81.3303},
    "那拉提": {"code": "NLT", "name": "新源那拉提机场", "lat": 43.4317, "lng": 83.3783},
    "吐鲁番": {"code": "TLQ", "name": "吐鲁番交河机场", "lat": 43.0306, "lng": 89.0986},
    "克拉玛依": {"code": "KRY", "name": "克拉玛依机场", "lat": 45.4664, "lng": 84.9525},
    "喀什": {"code": "KHG", "name": "喀什徕宁国际机场", "lat": 39.5428, "lng": 76.0197},
    "叶尔羌": {"code": "QSZ", "name": "莎车叶尔羌机场", "lat": 38.2453, "lng": 77.0561},
    "帕米尔": {"code": "HQL", "name": "塔什库尔干红其拉甫机场", "lat": 37.6628, "lng": 75.2916},
    "库车": {"code": "KCA", "name": "库车龟兹机场", "lat": 41.7181, "lng": 82.9867},
    "阿克苏": {"code": "AKU", "name": "阿克苏红旗坡机场", "lat": 41.2625, "lng": 80.2917},
    # 独山子无民航机场，最近为克拉玛依机场（约 100km，跨市）
    "独山子": {"code": "KRY", "name": "克拉玛依机场", "lat": 45.4664, "lng": 84.9525},
}

_lock = threading.Lock()
_cache: dict | None = None


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
    try:
        tmp = INTERCITY_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache or {}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, INTERCITY_CACHE_PATH)
    except OSError:
        pass


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def airport_of(city: str) -> dict | None:
    """该城最近的民航机场；无则 None（调用方回落驾车模型）。"""
    return AIRPORTS.get(city)


def has_airport(city: str) -> bool:
    return city in AIRPORTS


def ground_minutes(city: str, center: dict) -> float:
    """市区中心 → 该城机场的地面交通分钟数（无机场/无中心返回 0）。"""
    ap = AIRPORTS.get(city)
    if not ap or not center:
        return 0.0
    km = haversine_km(center["lat"], center["lng"], ap["lat"], ap["lng"])
    return km * AIRPORT_GROUND_CIRCUITY / AIRPORT_GROUND_KMH * 60.0


def _air_km(city_a: str, city_b: str) -> float | None:
    a, b = AIRPORTS.get(city_a), AIRPORTS.get(city_b)
    if not a or not b:
        return None
    return haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])


def is_worth_flying(city_a: str, city_b: str) -> bool:
    """该城市对是否值得乘机（空中距离 ≥ MIN_FLIGHT_KM；无机场一律 False）。"""
    km = _air_km(city_a, city_b)
    return km is not None and km >= MIN_FLIGHT_KM


def transfer(city_a: str, center_a: dict, city_b: str, center_b: dict) -> dict | None:
    """飞机段（**门到门**）。返回 {"minutes","km","source","detail"} 或 None。

    None 的语义＝「这两座城之间无机场数据，飞不了」⇒ 调用方回落驾车模型并出 notice。
    km 为**空中距离**（地图画线用；地面接驳里程另计在 minutes 里，不混入 km，
    避免把「市区→机场 30km」算进「航程」造成里程口径混乱）。
    """
    if not city_a or not city_b or city_a == city_b:
        return {"minutes": 0.0, "km": 0.0, "source": "same-city", "detail": ""}
    ap_a, ap_b = AIRPORTS.get(city_a), AIRPORTS.get(city_b)
    if not ap_a or not ap_b:
        return None
    k = "flight:" + "|".join(sorted([city_a, city_b]))
    with _lock:
        hit = _load_cache().get(k)
    if hit and hit.get("minutes") is not None:
        codes = hit.get("codes") or f"{ap_a['code']}→{ap_b['code']}"
        return {"minutes": float(hit["minutes"]), "km": float(hit.get("km") or 0.0),
                "source": "cache", "detail": hit.get("detail", ""), "codes": codes}

    air_km = _air_km(city_a, city_b) or 0.0
    taxi = FLIGHT_TAXI_MIN
    cruise = air_km / FLIGHT_CRUISE_KMH * 60.0
    g_a = ground_minutes(city_a, center_a)
    g_b = ground_minutes(city_b, center_b)
    total = g_a + AIRPORT_DWELL_MIN + taxi + cruise + AIRPORT_LAND_MIN + g_b
    total = max(60.0, round(total, 1))
    codes = f"{ap_a['code']}→{ap_b['code']}"
    detail = (f"{codes}｜空中 {air_km:.0f}km；"
              f"候机 {AIRPORT_DWELL_MIN:.0f}min + 起降 {taxi:.0f}min + "
              f"巡航 {cruise:.0f}min + 落地 {AIRPORT_LAND_MIN:.0f}min + "
              f"地面接驳 {g_a + g_b:.0f}min")
    rec = {"minutes": total, "km": round(air_km, 1), "source": "flight-model",
           "detail": detail, "codes": codes, "pair": f"{city_a}→{city_b}"}
    with _lock:
        _load_cache()[k] = rec
        _save_cache()
    return {"minutes": total, "km": round(air_km, 1), "source": "flight-model",
            "detail": detail, "codes": codes}


def drive_to_airport_text(city: str) -> str:
    """机场名（用于 notice/行程行）。"""
    ap = AIRPORTS.get(city)
    return f"{ap['name']}（{ap['code']}）" if ap else ""
