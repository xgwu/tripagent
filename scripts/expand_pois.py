# -*- coding: utf-8 -*-
"""M8：按 M7 缺口清单扩容 POI 库（22 个新增，5 城）。

- 坐标：高德 place/text API 定位（首个结果），失败/偏移>3km 时退回人工坐标兜底
- 闭馆日/开放时间：2026-09 经 WebSearch 核实（官网/腾讯地图/百科交叉）
- 合入 data/{city}_pois.json，ID 续号（SH029+ / NJ029+ / HZ051+ / WH029+ / SZ029+）
- 幂等：按 name 已存在则跳过

用法：python -X utf8 scripts/expand_pois.py [--dry]
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

# 人工兜底坐标（amap 失败或结果偏移 >3km 时使用）
NEW_POIS = [
    # ---- 上海 SH029-032 ----
    dict(city="上海", name="上海动物园", kw="上海动物园", id="SH029", category="family",
         tags=["动物", "亲子", "公园"], fb=(31.1877, 121.3520), duration_h=3.5,
         open="07:30", close="17:30", best_time="morning", price=40, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="百年老园，动物种类多、绿化好；虹桥路2381号，适合亲子半天"),
    dict(city="上海", name="上海城市规划展示馆", kw="上海城市规划展示馆", id="SH030", category="culture",
         tags=["博物馆", "亲子", "室内"], fb=(31.2313, 121.4755), duration_h=2,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="central", family_ok=True, closed_days=["周三"],
         note="人民大道100号；巨型城市沙盘+8K环幕，免费预约；注意周三闭馆（非常规周一）"),
    dict(city="上海", name="中共一大会址纪念馆", kw="中共一大会址纪念馆", id="SH031", category="history",
         tags=["红色", "历史", "室内"], fb=(31.2194, 121.4689), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=5,
         area="central", family_ok=True, closed_days=["周一"],
         note="兴业路76号（新天地旁），免费实名预约，石库门建筑群"),
    dict(city="上海", name="龙华寺", kw="龙华寺", id="SH032", category="culture",
         tags=["寺庙", "历史", "素食"], fb=(31.1799, 121.4486), duration_h=1.5,
         open="07:00", close="16:30", best_time="morning", price=10, rating=4,
         area="south", family_ok=True, closed_days=[],
         note="江南名刹+龙华塔，素斋有名；龙华路2853号"),
    # ---- 南京 NJ029-033 ----
    dict(city="南京", name="红山森林动物园", kw="红山森林动物园", id="NJ029", category="family",
         tags=["动物", "亲子", "公园"], fb=(32.0900, 118.8110), duration_h=3.5,
         open="08:30", close="16:30", best_time="morning", price=40, rating=5,
         area="north", family_ok=True, closed_days=[],
         note="全国口碑第一的城市动物园（拒绝动物表演），亲子必去；和燕路168号"),
    dict(city="南京", name="灵谷寺", kw="灵谷寺", id="NJ030", category="culture",
         tags=["寺庙", "历史", "森林"], fb=(32.0572, 118.8464), duration_h=1.5,
         open="06:30", close="18:00", best_time="any", price=35, rating=4,
         area="east", family_ok=True, closed_days=[],
         note="钟山风景区东片，无梁殿+灵谷塔，与中山陵同区可串联"),
    dict(city="南京", name="六朝博物馆", kw="六朝博物馆", id="NJ031", category="culture",
         tags=["博物馆", "历史", "室内"], fb=(32.0435, 118.7957), duration_h=1.5,
         open="09:00", close="18:00", best_time="any", price=25, rating=4,
         area="central", family_ok=True, closed_days=["周一"],
         note="长江路302号，贝聿铭团队设计，与总统府/江宁织造博物馆同街区"),
    dict(city="南京", name="南京科技馆", kw="南京科技馆", id="NJ032", category="family",
         tags=["科技馆", "亲子", "室内"], fb=(31.9860, 118.7834), duration_h=2.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="south", family_ok=True, closed_days=["周一", "周二"],
         note="雨花台区紫荆花路9号，免费登记入园；注意周一、周二双闭馆"),
    dict(city="南京", name="先锋书店（老门东店）", kw="先锋书店 老门东", id="NJ033", category="culture",
         tags=["书店", "文艺", "室内"], fb=(32.0090, 118.7883), duration_h=1,
         open="10:00", close="22:00", best_time="evening", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="骏惠书屋，古建里的书店，与老门东街区串联；边营2号"),
    # ---- 杭州 HZ051-055 ----
    dict(city="杭州", name="南宋官窑博物馆", kw="南宋官窑博物馆", id="HZ051", category="culture",
         tags=["博物馆", "历史", "陶瓷"], fb=(30.2158, 120.1638), duration_h=1.5,
         open="08:30", close="16:30", best_time="any", price=0, rating=4,
         area="south", family_ok=True, closed_days=["周一"],
         note="南复路60号，杭州四大专题博物馆之一，免费；近玉皇山/八卦田"),
    dict(city="杭州", name="浙江省科技馆", kw="浙江省科技馆", id="HZ052", category="family",
         tags=["科技馆", "亲子", "室内"], fb=(30.2763, 120.1647), duration_h=2.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="north", family_ok=True, closed_days=["周一", "周二"],
         note="西湖文化广场2号，免费免预约；注意周一、周二双闭馆"),
    dict(city="杭州", name="胡庆余堂中药博物馆", kw="胡庆余堂中药博物馆", id="HZ053", category="culture",
         tags=["博物馆", "中药", "历史"], fb=(30.2437, 120.1692), duration_h=1,
         open="08:30", close="17:00", best_time="any", price=10, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="大井巷95号，全国唯一国家级中药专业博物馆；全年无休（已核实365天开放），近河坊街"),
    dict(city="杭州", name="楼外楼（孤山店）", kw="楼外楼", id="HZ054", category="food",
         tags=["杭帮菜", "西湖醋鱼", "老字号"], fb=(30.2516, 120.1404), duration_h=1.5,
         open="10:30", close="19:30", best_time="lunch", price=150, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="孤山路30号，西湖边150年老字号（西湖醋鱼/东坡肉/龙井虾仁），建议正餐时段"),
    dict(city="杭州", name="武林门码头（京杭大运河夜游）", kw="武林门码头", id="HZ055", category="nightlife",
         tags=["夜游", "运河", "游船"], fb=(30.2790, 120.1655), duration_h=1.5,
         open="19:00", close="21:30", best_time="evening", price=100, rating=4,
         area="north", family_ok=True, closed_days=[],
         note="环城北路运河畔，乘漕舫夜游京杭大运河（拱宸桥方向），灯光夜景最佳"),
    # ---- 武汉 WH029-034 ----
    dict(city="武汉", name="武汉科技馆", kw="武汉科技馆新馆", id="WH029", category="family",
         tags=["科技馆", "亲子", "室内"], fb=(30.5794, 114.2998), duration_h=2.5,
         open="09:00", close="16:30", best_time="any", price=0, rating=4,
         area="hankou", family_ok=True, closed_days=["周一", "周二"],
         note="沿江大道91号（原武汉港客运楼改造），免费预约；注意周一、周二双闭馆"),
    dict(city="武汉", name="宝通寺", kw="宝通寺", id="WH030", category="culture",
         tags=["寺庙", "历史", "银杏"], fb=(30.5360, 114.3400), duration_h=1,
         open="07:00", close="17:00", best_time="morning", price=10, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="武珞路549号，洪山宝塔+皇家寺院，武汉现存最古老寺院"),
    dict(city="武汉", name="长春观", kw="长春观", id="WH031", category="culture",
         tags=["道观", "历史"], fb=(30.5395, 114.3170), duration_h=1,
         open="08:00", close="17:00", best_time="morning", price=10, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="武珞路269号，全真道著名丛林，藏式道教建筑罕见"),
    dict(city="武汉", name="武昌起义门", kw="武昌起义门", id="WH032", category="history",
         tags=["红色", "历史", "城门"], fb=(30.5352, 114.2975), duration_h=0.75,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="武昌古城中和门遗址，辛亥革命武昌起义起点，免费"),
    dict(city="武汉", name="首义广场", kw="首义广场", id="WH033", category="history",
         tags=["红色", "广场", "历史"], fb=(30.5426, 114.3050), duration_h=0.75,
         open="00:00", close="23:59", best_time="any", price=0, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="阅马场，紧邻辛亥革命博物院（红楼）与黄鹤楼南门，可串联"),
    dict(city="武汉", name="武汉动物园", kw="武汉动物园", id="WH034", category="family",
         tags=["动物", "亲子", "公园"], fb=(30.5470, 114.2350), duration_h=3,
         open="08:00", close="17:30", best_time="morning", price=20, rating=4,
         area="hanyang", family_ok=True, closed_days=[],
         note="墨水湖畔，改造后重开的半岛式动物园；汉阳动物园路"),
    # ---- 苏州 SZ029-030 ----
    dict(city="苏州", name="苏州科技馆", kw="苏州科技馆", id="SZ029", category="family",
         tags=["科技馆", "亲子", "室内"], fb=(31.2944, 120.5453), duration_h=2.5,
         open="09:00", close="17:00", best_time="any", price=50, rating=4,
         area="west", family_ok=True, closed_days=["周一", "周二"],
         note="高新区金山东路85号狮山文化广场（玉如意造型，2026新开），门票50元；注意周一、周二双闭馆"),
    dict(city="苏州", name="上方山森林动物世界", kw="上方山森林动物世界", id="SZ030", category="family",
         tags=["动物", "亲子", "森林"], fb=(31.2570, 120.5810), duration_h=3.5,
         open="08:30", close="16:30", best_time="morning", price=60, rating=4,
         area="south", family_ok=True, closed_days=[],
         note="苏州动物园新址（上方山/石湖畔），华东前列的森林动物世界"),
]


def amap_locate(key: str, kw: str, city: str):
    """高德 place/text 首个结果 → (lat,lng) 或 None。"""
    qs = urllib.parse.urlencode({"keywords": kw, "city": city, "citylimit": "true", "key": key})
    url = f"https://restapi.amap.com/v3/place/text?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "1" and data.get("pois"):
            p = data["pois"][0]
            lng, lat = p["location"].split(",")
            addr = p.get("address", "") or p.get("pname", "")
            return float(lat), float(lng), addr, p.get("name")
    except Exception as e:  # noqa
        print(f"    amap 失败 {kw}: {e}")
    return None


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def main():
    dry = "--dry" in sys.argv
    key = os.environ.get("AMAP_KEY", "")
    if not key:
        cfg_path = os.path.join(ROOT, "config.json")
        if os.path.exists(cfg_path):
            key = json.load(io.open(cfg_path, encoding="utf-8")).get("amap_key", "")
    added = 0
    for spec in NEW_POIS:
        path = os.path.join(DATA, f"{spec['city']}_pois.json")
        pois = json.load(io.open(path, encoding="utf-8"))["pois"]
        if any(p["name"] == spec["name"] for p in pois):
            print(f"  跳过（已存在）: {spec['city']}｜{spec['name']}")
            continue
        lat, lng = spec["fb"]
        src = "fallback"
        addr = ""
        if key:
            hit = amap_locate(key, spec["kw"], spec["city"])
            if hit:
                alat, alng, addr, aname = hit
                d = haversine_km((alat, alng), spec["fb"])
                if d <= 3.0:
                    lat, lng, src = alat, alng, "amap"
                else:
                    print(f"    ⚠️ amap 结果偏移 {d:.1f}km（{aname}），用兜底坐标")
        poi = {
            "id": spec["id"], "name": spec["name"], "category": spec["category"],
            "tags": spec["tags"], "lat": round(lat, 6), "lng": round(lng, 6),
            "duration_h": spec["duration_h"], "open": spec["open"], "close": spec["close"],
            "best_time": spec["best_time"], "price": spec["price"], "rating": spec["rating"],
            "area": spec["area"], "family_ok": spec["family_ok"], "note": spec["note"],
            "closed_days": spec["closed_days"],
        }
        if src == "amap" and addr:
            poi["note"] = f"{spec['note']}（@{addr}）"
        print(f"  + {spec['id']} {spec['name']} @({lat:.4f},{lng:.4f}) [{src}]{' ' + addr if addr else ''}")
        if not dry:
            pois.append(poi)
            json.dump({"pois": pois}, io.open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        added += 1
    print(f"完成：新增 {added} 个 POI{'（dry-run 未落盘）' if dry else ''}")


if __name__ == "__main__":
    main()
