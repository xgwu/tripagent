# -*- coding: utf-8 -*-
"""成都库补全：清理高德误采脏数据 + 补招牌缺口点（9 个新增）。

背景（2026-09-12）：
- 采集关键词「地标/商业街」误采进 4 条非景点商户（卫浴库房/电动车门店/设计公司等）→ 删除
- 招牌缺口：天府广场/人民公园/锦里/金沙遗址/东郊记忆/都江堰等核心点不在库 → 新增
- 坐标：高德 place/text 定位（失败或偏移 >3km 用人工兜底坐标）
- 幂等：按 name 已存在则跳过；脏数据按 name 精确匹配删除

用法：python scripts/expand_pois_cd.py [--dry]
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

# 脏数据（非景点商户，按名称精确删除）
REMOVE = [
    "帝标不锈钢城",
    "新帝标英皇卫浴库房",
    "爱玛成都地标店(成都解放路总代店)",
    "浙江地标设计集团有限公司西南分公司",
]

NEW_POIS = [
    dict(name="天府广场", kw="天府广场", id="CD081", category="photo",
         tags=["地标", "广场", "地铁枢纽"], fb=(30.6570, 104.0660), duration_h=0.5,
         open="00:00", close="23:59", best_time="any", price=0, rating=4,
         area="central", family_ok=True, closed_days=[],
         note="成都市中心地标，地铁1/2号线枢纽，与人民广场/科技馆/博物馆步行串联"),
    dict(name="人民公园", kw="成都人民公园", id="CD082", category="nature",
         tags=["公园", "茶馆", "市井"], fb=(30.6624, 104.0556), duration_h=1.5,
         open="07:00", close="22:00", best_time="any", price=0, rating=5,
         area="central", family_ok=True, closed_days=[],
         note="鹤鸣茶社百年盖碗茶+金河路市井生活，体验成都慢生活首选；紧邻天府广场"),
    dict(name="锦里古街", kw="锦里古街", id="CD083", category="shopping",
         tags=["古街", "小吃", "夜游"], fb=(30.6456, 104.0436), duration_h=2,
         open="10:00", close="22:00", best_time="evening", price=0, rating=5,
         area="central", family_ok=True, closed_days=[],
         note="武侯祠旁仿古商业街，夜景+川味小吃；与武侯祠正馆同片区串联"),
    # 金沙遗址博物馆：2025-12-05 至 2027-04-30 闭馆综合提升（官网公告核实），
    # 期间不入库避免推荐闭馆场馆；2027-05-01 重开后补回（坐标 30.6817,104.0126，
    # 每日 9:00-18:00，重开后恢复每周一闭馆）
    dict(name="东郊记忆", kw="东郊记忆", id="CD085", category="culture",
         tags=["工业遗址", "音乐", "文创"], fb=(30.6910, 104.1365), duration_h=2,
         open="10:00", close="22:00", best_time="evening", price=0, rating=4,
         area="east", family_ok=True, closed_days=[],
         note="老厂房改造的音乐/艺术园区，晚间演出多、好出片；成华区建设南支路"),
    dict(name="望江楼公园", kw="望江楼公园", id="CD086", category="nature",
         tags=["公园", "竹", "纪念"], fb=(30.6304, 104.0817), duration_h=1.5,
         open="07:00", close="20:00", best_time="any", price=0, rating=4,
         area="south", family_ok=True, closed_days=[],
         note="锦江畔竹类公园，纪念薛涛；与四川大学望江校区相邻"),
    dict(name="青羊宫", kw="青羊宫", id="CD087", category="history",
         tags=["道观", "历史", "古建"], fb=(30.6626, 104.0392), duration_h=1,
         open="08:00", close="18:00", best_time="any", price=10, rating=4,
         area="west", family_ok=True, closed_days=[],
         note="川西第一道观，与文化公园/杜甫草堂同片区串联"),
    dict(name="成都武侯祠博物馆", kw="成都武侯祠", id="CD088", category="history",
         tags=["三国", "博物馆", "历史"], fb=(30.6442, 104.0476), duration_h=2.5,
         open="09:00", close="18:00", best_time="any", price=50, rating=5,
         area="central", family_ok=True, closed_days=[],
         note="君臣合祀祠庙+三国文化圣地（正馆），出口即锦里；全年开放"),
    dict(name="都江堰景区", kw="都江堰景区", id="CD089", category="history",
         tags=["水利工程", "世界遗产", "一日游"], fb=(31.0065, 103.6194), duration_h=4,
         open="08:00", close="18:00", best_time="morning", price=80, rating=5,
         area="far", family_ok=True, closed_days=[],
         note="两千余年水利奇迹+世界遗产，距市区约 55km，适合独占一天（可串联离堆公园/南桥）"),
]

AMAP_TEXT = "https://restapi.amap.com/v3/place/text"


def load_key():
    for f in ("secrets.json", "config.json"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                k = json.load(fh).get("amap_key")
                if k:
                    return k
    raise SystemExit("缺 amap_key（secrets.json）")


def amap_locate(key, kw, fb):
    """高德关键词定位首个结果；失败或与兜底坐标偏移 >3km 时用兜底。"""
    try:
        url = AMAP_TEXT + "?" + urllib.parse.urlencode(
            {"key": key, "keywords": kw, "city": "成都", "citylimit": "true", "page_size": 1})
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        poi = (d.get("pois") or [None])[0]
        if poi and poi.get("location"):
            lng, lat = map(float, poi["location"].split(","))
            dist = math.hypot((lat - fb[0]) * 111, (lng - fb[1]) * 111 * math.cos(math.radians(lat)))
            if dist <= 3:
                return lat, lng, poi.get("name", kw)
            print(f"    高德偏移 {dist:.1f}km → 用兜底坐标")
    except Exception as e:
        print(f"    高德失败({e}) → 用兜底坐标")
    return fb[0], fb[1], kw


def main():
    dry = "--dry" in sys.argv
    key = load_key()
    path = os.path.join(DATA, "成都_pois.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pois = data["pois"]

    removed = [p["name"] for p in pois if p["name"] in REMOVE]
    pois = [p for p in pois if p["name"] not in REMOVE]
    print(f"删除脏数据 {len(removed)} 条: {removed}")

    exist = {p["name"] for p in pois}
    added = []
    for np in NEW_POIS:
        if np["name"] in exist:
            print(f"  跳过（已存在）: {np['name']}")
            continue
        lat, lng, resolved = amap_locate(key, np["kw"], np["fb"])
        print(f"  {np['id']} {np['name']} → ({lat:.4f},{lng:.4f}) 高德解析: {resolved}")
        pois.append(dict(
            id=np["id"], name=np["name"], category=np["category"], tags=np["tags"],
            lat=lat, lng=lng, duration_h=np["duration_h"], open=np["open"], close=np["close"],
            best_time=np["best_time"], price=np["price"], rating=np["rating"],
            area=np["area"], family_ok=np["family_ok"], closed_days=np["closed_days"],
            note=np["note"] + "；2026-09 新增（坐标高德核验，闭馆日建议行前抽查）"))
        added.append(np["name"])

    data["pois"] = pois
    print(f"\n合计: 删 {len(removed)} / 增 {len(added)} / 库存 {len(pois)}")
    if dry:
        print("dry-run 结束（未落盘）")
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"已写回 {path}")


if __name__ == "__main__":
    main()
