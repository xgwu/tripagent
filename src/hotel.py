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


def resolve_hotel(city: dict, text: str | None) -> dict | None:
    """解析酒店锚点。text 为空返回 None；「名称@lng,lat」直取坐标。"""
    if not text:
        return None
    if "@" in text:  # 显式坐标：--hotel "西湖国宾馆@120.13,30.24"
        name, _, coords = text.partition("@")
        lng_s, lat_s = coords.split(",", 1)
        return _base_hotel(name or "酒店", float(lat_s), float(lng_s), "显式坐标")

    key = os.environ.get("AMAP_KEY", "")
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
