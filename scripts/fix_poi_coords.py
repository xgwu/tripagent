# -*- coding: utf-8 -*-
"""M8 补丁：对 expand_pois 中 fallback 坐标的高德重定位（带 QPS 节流 + 结果过滤）。

只更新坐标（差值 ≤3km 才采纳），不改其它属性；按 NEW_IDS 清单处理，幂等。
用法：python -X utf8 scripts/fix_poi_coords.py
"""
import io
import json
import math
import os
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SLEEP = 0.5  # 2 QPS，避开高德频控
BAD_NAME_TOKENS = ("保管部", "停车场", "出入口", "售票处", "游客中心")

NEW_IDS = ["SH031", "SH032", "NJ029", "NJ030", "NJ031", "NJ032", "NJ033",
           "HZ051", "HZ052", "HZ053", "WH029", "WH030", "WH031", "WH032",
           "WH033", "WH034", "SZ029", "SZ030"]
# 名称 → 搜索关键词/城市（SH031 搜主馆名而非全称，避免命中保管部）
KW = {
    "SH031": ("中共一大", "上海"), "SH032": ("龙华寺", "上海"),
    "NJ029": ("红山森林动物园", "南京"), "NJ030": ("灵谷寺", "南京"),
    "NJ031": ("六朝博物馆", "南京"), "NJ032": ("南京科技馆", "南京"),
    "NJ033": ("先锋书店 骏惠书屋", "南京"),
    "HZ051": ("南宋官窑博物馆", "杭州"), "HZ052": ("浙江省科技馆", "杭州"),
    "HZ053": ("胡庆余堂中药博物馆", "杭州"),
    "WH029": ("武汉科技馆新馆", "武汉"), "WH030": ("宝通寺", "武汉"),
    "WH031": ("长春观", "武汉"), "WH032": ("武昌起义门", "武汉"),
    "WH033": ("首义广场", "武汉"), "WH034": ("武汉动物园", "武汉"),
    "SZ029": ("苏州科技馆", "苏州"), "SZ030": ("上方山森林动物世界", "苏州"),
}


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def main():
    key = json.load(io.open(os.path.join(ROOT, "config.json"), encoding="utf-8"))["amap_key"]
    by_id = {}
    for city in ["上海", "南京", "杭州", "武汉", "苏州"]:
        path = os.path.join(DATA, f"{city}_pois.json")
        for p in json.load(io.open(path, encoding="utf-8"))["pois"]:
            if p["id"] in NEW_IDS:
                by_id[p["id"]] = (city, path, p)
    fixed = 0
    for pid in NEW_IDS:
        city, path, p = by_id[pid]
        kw, _ = KW[pid]
        qs = urllib.parse.urlencode({"keywords": kw, "city": city, "citylimit": "true", "key": key})
        url = f"https://restapi.amap.com/v3/place/text?{qs}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa
            print(f"  {pid} 请求失败: {e}")
            time.sleep(SLEEP)
            continue
        hit = None
        if d.get("status") == "1":
            for cand in d.get("pois", []):
                nm = cand.get("name", "")
                if any(t in nm for t in BAD_NAME_TOKENS):
                    continue
                hit = cand
                break
        old = (p["lat"], p["lng"])
        if hit:
            lng, lat = hit["location"].split(",")
            lat, lng = float(lat), float(lng)
            dist = haversine_km(old, (lat, lng))
            if dist <= 3.0:
                p["lat"], p["lng"] = round(lat, 6), round(lng, 6)
                json.dump({"pois": [x for x in json.load(io.open(path, encoding='utf-8'))['pois']]},
                          io.open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                fixed += 1
                print(f"  {pid} {p['name']}: ({old[0]:.4f},{old[1]:.4f}) -> ({lat:.4f},{lng:.4f}) [{hit['name']}] Δ{dist*1000:.0f}m")
            else:
                print(f"  ⚠️ {pid} {p['name']}: 候选 {hit.get('name')} 偏移 {dist:.1f}km，保留原坐标")
        else:
            print(f"  ✗ {pid} {p['name']}: status={d.get('status')} infocode={d.get('infocode')}，保留原坐标")
        time.sleep(SLEEP)
    print(f"完成：修正 {fixed}/{len(NEW_IDS)} 个坐标")


if __name__ == "__main__":
    main()
