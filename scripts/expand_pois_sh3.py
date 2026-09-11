# -*- coding: utf-8 -*-
"""上海 POI 二期扩容（SH033-044，12 个）。

- 坐标：高德 place/text API 定位（首个结果），失败/偏移>3km 时退回人工坐标兜底
- 闭馆日/开放时间：2026-09-11 经 WebSearch 核实（政府官网/场馆官网/百科交叉）：
  * 上海天文馆：周一闭馆（国定假日除外），9:30-17:00，30 元
  * 上海汽车博物馆：周一闭馆（节假日除外），9:30-16:30，成人 55 元
  * 上海邮政博物馆：仅周三/四/六/日开放（周一/二/五闭馆），免费
- 幂等：按 name 已存在则跳过

用法：AMAP_KEY 环境变量可选；python -X utf8 scripts/expand_pois_sh3.py [--dry]
"""
import io
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
    dict(name="世博文化公园（双子山）", kw="世博文化公园", id="SH033", category="nature",
         tags=["自然", "滨江", "亲子"], fb=(31.1825, 121.4890), duration_h=3.0,
         open="06:00", close="21:00", best_time="afternoon", price=0, rating=4,
         area="pudong", family_ok=True, closed_days=[],
         note="浦东后滩；双子山登高看浦江、温室花园，登双子山需小程序免费预约"),
    dict(name="共青森林公园", kw="共青森林公园", id="SH034", category="nature",
         tags=["自然", "森林", "野趣"], fb=(31.3049, 121.5435), duration_h=3.0,
         open="06:00", close="18:00", best_time="morning", price=0, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="市区难得的森林型公园，烧烤/游船/亲子野趣；军工路2000号，2021年起免费"),
    dict(name="张园", kw="张园", id="SH035", category="photo",
         tags=["石库门", "街区", "逛街"], fb=(31.2362, 121.4648), duration_h=2.0,
         open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="茂名北路百年石库门建筑群改造街区，展览+品牌+弄堂打卡，连南京西路商圈"),
    dict(name="上海邮政博物馆", kw="上海邮政博物馆", id="SH036", category="culture",
         tags=["博物馆", "历史", "室内", "免费"], fb=(31.2444, 121.4848), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="north", family_ok=True, closed_days=["周一", "周二", "周五"],
         note="天潼路395号百年邮政大楼，免费免预约；仅周三/四/六/日开放（周三四走北苏州路入口）"),
    dict(name="广富林文化遗址", kw="广富林文化遗址", id="SH037", category="history",
         tags=["遗址", "公园", "历史"], fb=(31.0805, 121.1990), duration_h=4.0,
         open="09:00", close="17:00", best_time="morning", price=30, rating=4,
         area="suburb", family_ok=True, closed_days=[],
         note="松江'上海之根'；水下博物馆+遗址公园（公园免费、展示馆收费）"),
    dict(name="上海天文馆", kw="上海天文馆", id="SH038", category="family",
         tags=["科普", "亲子", "室内"], fb=(30.9165, 121.8870), duration_h=4.0,
         open="09:30", close="17:00", best_time="morning", price=30, rating=5,
         area="suburb", family_ok=True, closed_days=["周一"],
         note="临港大道380号全球最大天文馆；周一闭馆（国定假日除外），门票需提前约；可与海昌同日"),
    dict(name="上海汽车博物馆", kw="上海汽车博物馆", id="SH039", category="family",
         tags=["博物馆", "亲子", "室内"], fb=(31.2870, 121.1490), duration_h=2.5,
         open="09:30", close="16:30", best_time="any", price=55, rating=5,
         area="west", family_ok=True, closed_days=["周一"],
         note="安亭博园路7565号古董车珍藏+四楼摩都儿童乐园；周一闭馆（节假日除外）"),
    dict(name="朵云书院·旗舰店", kw="朵云书院旗舰店", id="SH040", category="art",
         tags=["书店", "高空景观", "室内"], fb=(31.2354, 121.5055), duration_h=1.5,
         open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="pudong", family_ok=True, closed_days=[],
         note="上海中心大厦52层239米高空书店（需预约），可俯瞰浦江两岸"),
    dict(name="前滩太古里", kw="前滩太古里", id="SH041", category="shopping",
         tags=["商圈", "滨江", "逛街"], fb=(31.1600, 121.4780), duration_h=2.5,
         open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="pudong", family_ok=True, closed_days=[],
         note="前滩滨江新商圈，屋顶天空环路人少景好，适合傍晚"),
    dict(name="今潮8弄", kw="今潮8弄", id="SH042", category="nightlife",
         tags=["街区", "展览", "夜生活"], fb=(31.2528, 121.4808), duration_h=2.0,
         open="10:00", close="22:00", best_time="evening", price=0, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="四川北路石库门弄堂艺术街区，展览+市集+夜景灯光"),
    dict(name="光明邨大酒家（淮海中路店）", kw="光明邨大酒家", id="SH043", category="food",
         tags=["美食", "本帮菜", "点心", "老字号"], fb=(31.2208, 121.4635), duration_h=0.8,
         open="07:00", close="20:00", best_time="lunch", price=90, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="淮海中路588号，鲜肉月饼/蟹粉小笼常年排队，熟菜窗口与堂食分开"),
    dict(name="鲜得来排骨年糕（云南南路店）", kw="鲜得来排骨年糕", id="SH044", category="food",
         tags=["美食", "小吃", "老字号"], fb=(31.2245, 121.4755), duration_h=0.8,
         open="10:00", close="21:00", best_time="lunch", price=35, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="云南南路46号'排骨年糕'鼻祖，人均约35"),
]


def amap_geocode(kw, key):
    if not key:
        return None
    qs = urllib.parse.urlencode({"keywords": f"{CITY}{kw}", "city": CITY,
                                 "key": key, "citylimit": "true"})
    url = f"https://restapi.amap.com/v3/place/text?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
        pois = d.get("pois") or []
        if pois:
            lng, lat = pois[0]["location"].split(",")
            return float(lat), float(lng)
    except Exception as e:
        print(f"  amap 失败({kw}): {e}")
    return None


def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


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
        if coord is None or haversine_km(coord[0], coord[1], *np["fb"]) > 3.0:
            if coord is not None:
                print(f"  amap 结果偏移>3km，用人工坐标: {np['name']}")
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
