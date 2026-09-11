# -*- coding: utf-8 -*-
"""上海 POI 四期扩容（SH063-068，6 个）——按骑行/徒步/咖啡主题落地缺口定向补库。

缺口来源：2026-09-11 M7 实测「上海玩3天，骑车，徒步，喝咖啡」落地率 53%-75%，
高频缺口：永康路（咖啡街）、苏州河步道、四行仓库、1862船厂、八号桥创意园；
另补前滩滨江骑行道，与陆家嘴滨江大道/绿之丘形成浦江两岸骑行带。

- 永康路用 category=relax（咖啡街区≠正餐，避免触发美食餐窗硬约束）
- 坐标：高德 place/text API 定位（首个结果），失败/偏移>2km 时退回人工坐标兜底
- 幂等：按 name/id 已存在则跳过

用法：AMAP_KEY 可选；python scripts/expand_pois_sh5.py [--dry]
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CITY = "上海"

NEW_POIS = [
    # ---- 骑行/徒步 滨江带（浦西苏州河 + 浦东前滩）----
    dict(name="四行仓库抗战纪念馆", kw="四行仓库抗战纪念馆", id="SH063", category="history",
         tags=["历史", "纪念馆", "苏州河", "免费", "徒步"], fb=(31.2452, 121.4607),
         duration_h=1.0, open="09:00", close="16:30", best_time="morning", price=0, rating=4,
         area="north", family_ok=True, closed_days=["周一"],
         note="光复路181号，苏州河北岸，'八佰'原型地，西墙弹孔遗迹，免费参观"),
    dict(name="苏州河步道", kw="苏州河步道 南苏州路", id="SH064", category="nature",
         tags=["骑行", "徒步", "步道", "滨江", "历史桥梁"], fb=(31.2418, 121.4680),
         duration_h=1.0, open="06:00", close="22:00", best_time="any", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="南苏州路沿河步道，串联外白渡桥/邮政博物馆等多座历史桥梁，骑行步行皆宜"),
    dict(name="前滩滨江骑行道", kw="前滩休闲公园", id="SH065", category="nature",
         tags=["骑行", "滨江", "步道", "草坪", "亲子"], fb=(31.1789, 121.4871),
         duration_h=1.5, open="06:00", close="22:00", best_time="any", price=0, rating=3,
         area="pudong", family_ok=True, closed_days=[],
         note="前滩滨江骑行道，与前滩太古里相邻，浦东骑行带上新选择"),
    # ---- 咖啡/创意街区 ----
    dict(name="永康路咖啡街", kw="永康路 咖啡", id="SH066", category="relax",
         tags=["咖啡", "街拍", "城市漫步", "小马路"], fb=(31.2115, 121.4520),
         duration_h=1.0, open="10:00", close="22:00", best_time="afternoon", price=50, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="徐汇永康路，百米小街聚集精品咖啡与咖啡馆，街头生活气息浓，可与安福路武康路串联"),
    dict(name="船厂1862", kw="船厂1862 MIFA", id="SH067", category="art",
         tags=["艺术", "工业遗迹", "咖啡", "滨江"], fb=(31.2398, 121.5030),
         duration_h=1.0, open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="pudong", family_ok=True, closed_days=[],
         note="陆家嘴滨江旧船厂改造的艺术空间，工业遗迹+咖啡馆，骑行滨江大道顺路歇脚"),
    dict(name="八号桥创意园", kw="八号桥创意园 建国中路", id="SH068", category="art",
         tags=["艺术", "创意园", "咖啡", "城市漫步"], fb=(31.2113, 121.4658),
         duration_h=1.0, open="09:00", close="21:00", best_time="afternoon", price=0, rating=3,
         area="central", family_ok=True, closed_days=[],
         note="建国中路8-10号，旧厂房改造设计园区，安静适合小憩，近田子坊"),
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def amap_geocode(kw: str, key: str):
    if not key:
        return None
    url = ("https://restapi.amap.com/v3/place/text?keywords=" + urllib.parse.quote(kw)
           + f"&city={CITY}&key={key}&offset=1")
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        pois = d.get("pois") or []
        if not pois:
            return None
        loc = pois[0]["location"].split(",")
        return float(loc[1]), float(loc[0])  # (lat, lng)
    except Exception:
        return None


def main():
    dry = "--dry" in sys.argv
    key = os.environ.get("AMAP_KEY", "")
    path = os.path.join(DATA, f"{CITY}_pois.json")
    doc = json.load(open(path, encoding="utf-8"))
    pois = doc["pois"]
    existing_names = {p["name"] for p in pois}
    existing_ids = {p["id"] for p in pois}

    added = 0
    for np in NEW_POIS:
        if np["name"] in existing_names or np["id"] in existing_ids:
            print(f"跳过（已存在）: {np['name']}")
            continue
        coord = amap_geocode(np["kw"], key)
        src = "amap"
        if coord is None or haversine_km(coord[0], coord[1], *np["fb"]) > 2.0:
            if coord is not None:
                print(f"  amap 结果偏移>2km，用人工坐标: {np['name']}")
            coord, src = np["fb"], "manual"
        poi = dict(id=np["id"], name=np["name"], category=np["category"],
                   tags=np["tags"], lat=coord[0], lng=coord[1],
                   duration_h=np["duration_h"], open=np["open"], close=np["close"],
                   best_time=np["best_time"], price=np["price"], rating=np["rating"],
                   area=np["area"], family_ok=np["family_ok"],
                   note=np["note"], closed_days=np["closed_days"])
        pois.append(poi)
        added += 1
        print(f"+ {np['id']} {np['name']} ({src}) @ ({coord[0]:.4f}, {coord[1]:.4f})")

    if added and not dry:
        pois.sort(key=lambda p: p["id"])
        json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"共新增 {added} 个（{CITY} 现有 {len(pois)} 个）" + ("  [dry 未写盘]" if dry else ""))


if __name__ == "__main__":
    main()
