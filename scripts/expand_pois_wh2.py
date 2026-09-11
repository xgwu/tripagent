# -*- coding: utf-8 -*-
"""武汉 POI 第二轮扩容（WH035-050，16 个）：补齐辛亥文脉、亲子乐园、江滩里份、远郊木兰。

- 坐标：高德 place/text 定位，偏移 >3km 回退人工兜底坐标
- 校验：离市中心 >60km 拒绝；与库内近邻 <200m 视为重复跳过
- 幂等：按 name 已存在跳过；保存时保留城市文件全部顶层键（center/meal_slots 等）
- 合入后需跑：scripts/expand_travel_cache.py（NEW_IDS 已含新 ID）→ scripts/fetch_poi_photos.py

用法：python scripts/expand_pois_wh2.py [--dry]
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
CITY = "武汉"
CENTER = (30.5433, 114.298)

NEW_POIS = [
    dict(id="WH035", name="辛亥革命博物院（红楼）", kw="辛亥革命博物院", category="history",
         tags=["红色", "历史", "室内"], fb=(30.5427, 114.3085), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=5,
         area="wuchang", family_ok=True, closed_days=["周一"],
         note="武昌阅马场，红楼主楼+辛亥革命史陈列，与首义广场/黄鹤楼南门串联，免费预约"),
    dict(id="WH036", name="江汉关博物馆", kw="江汉关博物馆", category="history",
         tags=["历史", "建筑", "室内"], fb=(30.5817, 114.2957), duration_h=1,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="hankou", family_ok=True, closed_days=["周一"],
         note="沿江大道江汉路口，1924年海关大楼，汉口开埠史；江汉路步行街端头，免费预约"),
    dict(id="WH037", name="古琴台·月湖风景区", kw="古琴台", category="culture",
         tags=["历史", "知音文化", "公园"], fb=(30.5574, 114.2573), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=15, rating=4,
         area="hanyang", family_ok=True, closed_days=[],
         note="俞伯牙钟子期高山流水遇知音处，月湖畔；与琴台大剧院/美术馆同片区"),
    dict(id="WH038", name="湖北美术馆", kw="湖北美术馆", category="art",
         tags=["美术馆", "艺术", "室内"], fb=(30.5597, 114.3660), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="wuchang", family_ok=True, closed_days=["周一"],
         note="武昌东湖路，省博对面可串联，常设湖北漆艺/雕塑展，免费预约"),
    dict(id="WH039", name="武汉欢乐谷", kw="武汉欢乐谷", category="family",
         tags=["游乐园", "亲子", "刺激"], fb=(30.5935, 114.4270), duration_h=6,
         open="09:30", close="18:00", best_time="morning", price=230, rating=5,
         area="wuchang", family_ok=True, closed_days=[],
         note="欢乐大道196号，华中旗舰主题乐园，木翼双龙/极速飞车；夜场常开到21:30，须独占大半天"),
    dict(id="WH040", name="武汉海昌极地海洋公园", kw="武汉海昌极地海洋公园", category="family",
         tags=["海洋馆", "亲子", "动物"], fb=(30.6510, 114.2810), duration_h=5,
         open="09:00", close="17:30", best_time="morning", price=150, rating=4,
         area="hankou", family_ok=True, closed_days=[],
         note="东西湖区金银潭大道，极地动物+海洋剧场，地铁2/8号线可达，亲子全天"),
    dict(id="WH041", name="中科院武汉植物园", kw="中国科学院武汉植物园", category="nature",
         tags=["植物", "亲子", "科普"], fb=(30.5350, 114.4170), duration_h=3,
         open="08:00", close="17:30", best_time="morning", price=35, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="磨山南侧，温室+水生植物区，四季花展；与东湖落雁/磨山同侧可串联"),
    dict(id="WH042", name="龟山风景区", kw="龟山风景区 武汉", category="outdoor",
         tags=["山体", "徒步", "江景"], fb=(30.5550, 114.2760), duration_h=2,
         open="08:00", close="18:00", best_time="any", price=0, rating=3,
         area="hanyang", family_ok=True, closed_days=[],
         note="汉阳龟山，山体栈道+电视塔+长江汉江双江景；紧邻晴川阁/古琴台可串联，免费"),
    dict(id="WH043", name="凌波门·东湖日出", kw="武汉大学凌波门", category="photo",
         tags=["日出", "东湖", "网红"], fb=(30.5370, 114.3580), duration_h=1,
         open="00:00", close="23:59", best_time="morning", price=0, rating=4,
         area="wuchang", family_ok=True, closed_days=[],
         note="武大凌波门东湖栈桥，武汉看日出首选，清晨人少光线最好；近武大/东湖听涛"),
    dict(id="WH044", name="咸安坊·同兴里老里份", kw="咸安坊 武汉", category="photo",
         tags=["里份", "建筑", "文艺"], fb=(30.5845, 114.2980), duration_h=1,
         open="00:00", close="23:59", best_time="any", price=0, rating=3,
         area="hankou", family_ok=True, closed_days=[],
         note="汉口老里份代表，石库门+天井民居，咖啡馆小店聚集；与黎黄陂路/江汉路同片区"),
    dict(id="WH045", name="武汉园博园·长江文明馆", kw="武汉园博园", category="family",
         tags=["园林", "亲子", "展馆"], fb=(30.6110, 114.2480), duration_h=3,
         open="08:30", close="17:30", best_time="any", price=60, rating=4,
         area="hankou", family_ok=True, closed_days=[],
         note="园博园东路，全国园博会址，长江文明馆+汉口小镇；园区大建议乘观光车"),
    dict(id="WH046", name="张之洞与武汉博物馆", kw="张之洞与武汉博物馆", category="culture",
         tags=["历史", "工业", "建筑"], fb=(30.5610, 114.2440), duration_h=1.5,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="hanyang", family_ok=True, closed_days=["周一"],
         note="琴台大道汉阳铁厂遗址旁，悬浮造型建筑，'武汉城市之父'工业史；免费预约"),
    dict(id="WH047", name="光谷空轨·光谷光子号", kw="光谷空轨", category="family",
         tags=["空轨", "亲子", "科技"], fb=(30.5110, 114.4530), duration_h=1.5,
         open="09:00", close="18:00", best_time="any", price=30, rating=3,
         area="wuchang", family_ok=True, closed_days=[],
         note="光谷中心城悬挂式空轨，全国首条商业空轨，车厢地板透明观景；光谷四路站"),
    dict(id="WH048", name="盘龙城国家考古遗址公园", kw="盘龙城国家考古遗址公园", category="history",
         tags=["考古", "商代", "遗址"], fb=(30.7010, 114.2870), duration_h=2,
         open="09:00", close="17:00", best_time="any", price=0, rating=4,
         area="hankou", family_ok=True, closed_days=["周一"],
         note="黄陂盘龙城，长江流域最完整商代城址，'武汉城市之根'；博物院+遗址区，免费预约"),
    dict(id="WH049", name="木兰天池", kw="木兰天池", category="outdoor",
         tags=["山水", "徒步", "远郊"], fb=(31.0880, 114.4180), duration_h=5,
         open="08:00", close="17:00", best_time="morning", price=70, rating=4,
         area="suburb", family_ok=False, closed_days=[],
         note="黄陂木兰文化生态区，峡谷瀑布+天池，花木兰传说地；市区车程约1.5小时，须独占一天"),
    dict(id="WH050", name="大成路夜市", kw="武昌大成路夜市", category="food",
         tags=["夜市", "小吃", "夜宵"], fb=(30.5435, 114.3090), duration_h=1.5,
         open="17:00", close="23:00", best_time="evening", price=40, rating=3,
         area="wuchang", family_ok=True, closed_days=[],
         note="武昌老牌夜市，烧烤/炸炸/汤包烟火气十足；近户部巷/黄鹤楼，晚饭后遛弯首选"),
]


def amap_locate(key: str, kw: str, city: str):
    qs = urllib.parse.urlencode({"keywords": kw, "city": city, "citylimit": "true", "key": key})
    url = f"https://restapi.amap.com/v3/place/text?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "1" and data.get("pois"):
            p = data["pois"][0]
            lng, lat = p["location"].split(",")
            return float(lat), float(lng), p.get("address", "") or p.get("pname", ""), p.get("name")
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
        cfg = os.path.join(ROOT, "config.json")
        if os.path.exists(cfg):
            key = json.load(io.open(cfg, encoding="utf-8")).get("amap_key", "")
    path = os.path.join(DATA, f"{CITY}_pois.json")
    city = json.load(io.open(path, encoding="utf-8"))
    pois = city["pois"]
    added = skipped = 0
    for spec in NEW_POIS:
        if any(p["name"] == spec["name"] for p in pois):
            print(f"  跳过（已存在）: {spec['name']}")
            skipped += 1
            continue
        lat, lng = spec["fb"]
        src = "fallback"
        addr = ""
        if key:
            hit = amap_locate(key, spec["kw"], CITY)
            if hit:
                alat, alng, addr, aname = hit
                d = haversine_km((alat, alng), spec["fb"])
                if d <= 3.0:
                    lat, lng, src = alat, alng, "amap"
                else:
                    print(f"    ⚠️ amap 偏移 {d:.1f}km（{aname}），用兜底坐标")
        # 校验：离群（木兰天池等黄陂远郊特批至 65km，与库内木兰草原同级）/ 近邻重复
        dc = haversine_km((lat, lng), CENTER)
        if dc > 65:
            print(f"  ✗ {spec['id']} {spec['name']} 离市中心 {dc:.0f}km 超限，拒绝入库")
            skipped += 1
            continue
        near = [(p["name"], haversine_km((lat, lng), (p["lat"], p["lng"]))) for p in pois]
        near = sorted(near, key=lambda x: x[1])[:1]
        if near and near[0][1] < 0.2:
            print(f"  ✗ {spec['id']} {spec['name']} 与「{near[0][0]}」仅 {near[0][1]*1000:.0f}m，疑似重复，跳过")
            skipped += 1
            continue
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
        print(f"  + {spec['id']} {spec['name']} @({lat:.4f},{lng:.4f}) [{src}] 距中心{dc:.1f}km 近邻:{near[0][0] if near else '-'}{near[0][1]:.1f}km" if near else f"  + {spec['id']} {spec['name']}")
        if not dry:
            pois.append(poi)
        added += 1
    if not dry and added:
        json.dump(city, io.open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"完成：新增 {added} 个、跳过 {skipped} 个{'（dry-run 未落盘）' if dry else ''}；武汉现有 {len(pois)} 个")


if __name__ == "__main__":
    main()
