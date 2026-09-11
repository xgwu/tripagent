# -*- coding: utf-8 -*-
"""上海 POI 三期扩容（SH045-062，18 个）——按 M7 落地缺口清单定向扩容。

缺口来源：2026-09-11 四轮 M7 实测（亲子骑行咖啡/历史文化/美食老字号/文艺逛街），
高频缺口集中在美食老字号、滨江骑行+咖啡、艺术场馆。

- 坐标：高德 place/text API 定位（首个结果），失败/偏移>2km 时退回人工坐标兜底
- 老字号门店闭馆日风险低（全年营业），均置 closed_days=[]
- 泛路线（浦东/杨浦滨江骑行道、西岸步道、五原路、云南南路美食街）不建点，
  由既有 POI 的 tags 增强（见 scripts/patch_sh_tags.py 或 expand_pois_sh3 后续）
- 幂等：按 name 已存在则跳过

用法：AMAP_KEY 可选；python -X utf8 scripts/expand_pois_sh4.py [--dry]
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
    # ---- 豫园·老城厢 美食集群 ----
    dict(name="南翔馒头店（豫园店）", kw="南翔馒头店 豫园", id="SH045", category="food",
         tags=["美食", "小笼", "老字号"], fb=(31.2270, 121.4920), duration_h=0.8,
         open="08:30", close="20:30", best_time="lunch", price=80, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="豫园路85号，'小笼包鼻祖'，一楼外带常年排队"),
    dict(name="绿波廊（豫园店）", kw="绿波廊", id="SH046", category="food",
         tags=["美食", "本帮菜", "老字号"], fb=(31.2272, 121.4922), duration_h=1.0,
         open="10:00", close="21:00", best_time="lunch", price=150, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="豫园老路137号，国宴本帮菜，桂花拉糕/蟹粉小笼"),
    dict(name="宁波汤团店（豫园店）", kw="宁波汤团店", id="SH047", category="food",
         tags=["美食", "汤团", "老字号"], fb=(31.2264, 121.4915), duration_h=0.6,
         open="08:00", close="20:00", best_time="any", price=25, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="豫园商城内，黑洋酥汤团百年老店，人均25"),
    dict(name="松月楼素菜馆", kw="松月楼素菜馆", id="SH048", category="food",
         tags=["美食", "素食", "老字号"], fb=(31.2260, 121.4931), duration_h=0.8,
         open="10:30", close="20:00", best_time="lunch", price=60, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="城隍庙旁百年素菜馆，素菜包出名"),
    # ---- 云南南路/南京路 一带 老字号 ----
    dict(name="小绍兴鸡粥店（云南南路店）", kw="小绍兴鸡粥店", id="SH049", category="food",
         tags=["美食", "鸡粥", "老字号"], fb=(31.2241, 121.4753), duration_h=0.8,
         open="07:00", close="21:00", best_time="lunch", price=45, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="云南南路69号，白斩鸡+鸡粥，与鲜得来同街可串"),
    dict(name="小杨生煎（吴江路店）", kw="小杨生煎 吴江路", id="SH050", category="food",
         tags=["美食", "生煎", "老字号"], fb=(31.2349, 121.4596), duration_h=0.6,
         open="10:00", close="21:00", best_time="any", price=30, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="吴江路美食街，'大满贯'全发面生煎鼻祖"),
    dict(name="王家沙点心店（南京西路总店）", kw="王家沙点心店", id="SH051", category="food",
         tags=["美食", "点心", "老字号"], fb=(31.2336, 121.4647), duration_h=0.6,
         open="07:30", close="20:00", best_time="any", price=40, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="南京西路805号，蟹粉锅贴/鲜肉月饼，紧邻张园"),
    dict(name="泰康食品（南京东路店）", kw="泰康食品 南京东路", id="SH052", category="food",
         tags=["美食", "点心", "老字号"], fb=(31.2356, 121.4792), duration_h=0.5,
         open="08:00", close="21:00", best_time="any", price=35, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="南京东路766号，鲜肉月饼/蝴蝶酥伴手礼"),
    dict(name="国际饭店西饼屋", kw="国际饭店西饼屋", id="SH053", category="food",
         tags=["美食", "蝴蝶酥", "老字号"], fb=(31.2347, 121.4681), duration_h=0.4,
         open="08:00", close="20:00", best_time="any", price=45, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="黄河路28号，'蝴蝶酥天花板'，排队伴手礼地标"),
    dict(name="阿娘面馆（思南路店）", kw="阿娘面馆 思南路", id="SH054", category="food",
         tags=["美食", "面馆", "老字号"], fb=(31.2178, 121.4642), duration_h=0.7,
         open="07:30", close="19:30", best_time="lunch", price=40, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="思南路36号，黄鱼面传奇小店，与思南公馆同街区"),
    # ---- 咖啡（骑行+咖啡主题高频缺口） ----
    dict(name="% Arabica（武康路店）", kw="%Arabica 武康路", id="SH055", category="food",
         tags=["咖啡", "网红店", "休闲"], fb=(31.2073, 121.4378), duration_h=0.6,
         open="09:00", close="20:00", best_time="afternoon", price=40, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="武康大楼旁全球网红咖啡，配武康路漫步"),
    dict(name="Seesaw Coffee（愚园路店）", kw="Seesaw咖啡 愚园路", id="SH056", category="food",
         tags=["咖啡", "网红店", "休闲"], fb=(31.2237, 121.4270), duration_h=0.6,
         open="09:00", close="21:00", best_time="afternoon", price=40, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="愚园路创意园区内本土精品咖啡代表"),
    dict(name="O.P.S. CAFE", kw="O.P.S. CAFE 徐汇", id="SH057", category="food",
         tags=["咖啡", "网红店", "休闲"], fb=(31.2050, 121.4360), duration_h=0.6,
         open="10:00", close="19:00", best_time="afternoon", price=50, rating=5,
         area="west", family_ok=True, closed_days=["周二"],
         note="沪上精品咖啡口碑榜首（季节限定菜单），近衡复街区"),
    # ---- 公园/滨江/艺术 ----
    dict(name="静安雕塑公园", kw="静安雕塑公园", id="SH058", category="nature",
         tags=["公园", "亲子", "休闲"], fb=(31.2282, 121.4530), duration_h=1.5,
         open="05:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="自然博物馆旁开放式雕塑公园，樱花季热门，可两点串联"),
    dict(name="绿之丘（杨浦滨江）", kw="绿之丘 杨浦滨江", id="SH059", category="photo",
         tags=["滨江", "骑行", "建筑", "网红店"], fb=(31.2577, 121.5292), duration_h=1.0,
         open="09:00", close="20:00", best_time="afternoon", price=0, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="杨树浦路1500号烟草仓库改造'空中花园'，杨浦骑行线网红打卡点"),
    dict(name="上海国际时尚中心", kw="上海国际时尚中心", id="SH060", category="relax",
         tags=["滨江", "骑行", "工业遗存", "休闲"], fb=(31.2631, 121.5330), duration_h=1.5,
         open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="杨树浦路2866号原裕丰纱厂改造，杨浦滨江骑行线配套（餐饮/ outlet）"),
    dict(name="油罐艺术中心", kw="油罐艺术中心", id="SH061", category="art",
         tags=["艺术", "展览", "滨江"], fb=(31.1758, 121.4562), duration_h=2.0,
         open="10:00", close="21:00", best_time="afternoon", price=80, rating=4,
         area="west", family_ok=True, closed_days=["周一"],
         note="西岸龙腾大道2380号航油罐改造美术馆，特展售票；西岸骑行线上"),
    dict(name="K11购物艺术中心", kw="K11购物艺术中心", id="SH062", category="shopping",
         tags=["商场", "艺术", "逛街"], fb=(31.2241, 121.4703), duration_h=2.0,
         open="10:00", close="22:00", best_time="afternoon", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="淮海中路300号，'购物中心+美术馆'混合业态，常设艺术展"),
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
