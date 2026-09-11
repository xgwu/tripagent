# -*- coding: utf-8 -*-
"""M6 酒店锚点：把用户输入解析为虚拟 POI 节点（每日出发/返回的固定起终点）。

三级解析：
- L1 Amap 地点搜索（AMAP_KEY）：「酒店名/关键词」→ GCJ-02 坐标（限定城市）
- L2 显式坐标：「名称@lng,lat」直接构造
- L3 兜底：城市中心坐标 + 通用名「酒店」

产出的 hotel dict 形似 POI（id=HOTEL，duration_h=0，全天开放），
可无缝进入 poi_db.travel_hours（缓存 miss 自动走 L3 haversine）与求解器/排序器。
"""
import json
import os
import urllib.parse
import urllib.request

AMAP_PLACE = ("https://restapi.amap.com/v3/place/text?keywords={kw}&city={city}"
              "&citylimit=true&offset=1&page=1&key={key}")


def _base_hotel(name: str, lat: float, lng: float, resolved: str) -> dict:
    return {"id": "HOTEL", "name": name, "category": "hotel", "tags": [],
            "lat": lat, "lng": lng, "duration_h": 0,
            "open": "00:00", "close": "23:59", "best_time": "any",
            "price": 0, "rating": 0, "area": "", "family_ok": True,
            "note": f"住宿锚点（{resolved}）", "closed_days": []}


def hard_guarantee_enabled() -> bool:
    """住宿锚点硬保障开关（env ANCHOR_HARD_GUARANTEE，由 config.json anchor_hard_guarantee 注入）。

    默认关闭：世界知识主导提案后，兜底仅作显式锚点违约的保险，按需开启。
    """
    return os.environ.get("ANCHOR_HARD_GUARANTEE", "").strip().lower() in ("1", "true", "yes", "on")


def match_landmark_poi(city: dict, text: str | None) -> dict | None:
    """L0：库内地标匹配。text 与 POI 名互相包含即命中，返回原始 POI dict 或 None。

    评分门槛 rating≥4 防止「西湖」误配「西湖船宴(江桥店)」这类同名小店。
    供 resolve_hotel 取坐标、以及排程链做「锚点地标必进行程」硬保障。
    """
    t = (text or "").strip()
    if not t or len(t) < 2:
        return None
    hits = [p for p in city["pois"]
            if p.get("rating", 0) >= 4 and (t in p["name"] or p["name"] in t)]
    if not hits:
        return None
    return max(hits, key=lambda p: (p.get("rating", 0), -len(p["name"])))


def ensure_landmark_in_day_map(day_map: dict, days: int, poi: dict | None) -> str | None:
    """住宿锚点硬保障：锚点地标不在任何一天时注入点最少的一天队首（返回注入说明或 None）。

    LLM 提案对「住迪士尼附近→必排迪士尼」的遵守不稳定，落地后必须兜底；
    注入后由时间约束/全天大点豁免自然收敛为该地标独占一天。
    """
    if not poi:
        return None
    used = {pid for ids in day_map.values() for pid in ids}
    if poi["id"] in used:
        return None
    tgt = min(range(1, days + 1), key=lambda d: (len(day_map.get(d, [])), d))
    day_map[tgt] = [poi["id"]] + day_map.get(tgt, [])
    return f"{poi['name']}（住宿锚点地标，注入 Day {tgt}）"


def resolve_hotel(city: dict, text: str | None) -> dict | None:
    """解析酒店锚点。text 为空返回 None；「名称@lng,lat」直取坐标。

    L0 库内地标：「迪士尼附近」这类描述先匹配 POI 库（「迪士尼」⊂「上海迪士尼度假区」），
    命中即以该地标坐标为锚——高德裸搜「迪士尼」会命中市区授权店铺，锚点错到十万八千里。
    """
    if not text:
        return None
    if "@" in text:  # 显式坐标：--hotel "西湖国宾馆@120.13,30.24"
        name, _, coords = text.partition("@")
        lng_s, lat_s = coords.split(",", 1)
        return _base_hotel(name or "酒店", float(lat_s), float(lng_s), "显式坐标")

    key = os.environ.get("AMAP_KEY", "")

    best = match_landmark_poi(city, text)
    if best:
        return _base_hotel(f"{best['name']}（住宿锚点）", best["lat"], best["lng"],
                           "库内地标")

    if key:  # L1：高德地点搜索（POI 库无酒店类，酒店是外部锚点）
        try:
            url = AMAP_PLACE.format(kw=urllib.parse.quote(text),
                                    city=urllib.parse.quote(city["city"]), key=key)
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            pois = data.get("pois") or []
            if data.get("status") == "1" and pois:
                p = pois[0]
                lng, lat = (float(x) for x in p["location"].split(","))
                return _base_hotel(p.get("name", text), lat, lng,
                                   f"高德POI {p.get('type', '')[:24]}")
        except Exception:  # noqa：Key 失效/网络问题自动降级
            pass

    c = city["center"]
    return _base_hotel(f"{text}（市中心附近）", c["lat"], c["lng"], "市中心兜底")
