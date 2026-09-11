# -*- coding: utf-8 -*-
"""M8 事故修复：恢复 data/*_pois.json 丢失的顶层城市元数据。

事故：scripts/expand_pois.py 落盘时用 {"pois":...} 覆盖了原文件，丢失
city/center/day_start/day_end/meal_slots/meal_cost。本脚本按 schema 反推重建：
- day_end "21:30"：由 M6 报告最晚活动结束时间证实
- day_start "09:00"、午餐 12:00/晚餐 18:00 各 1h：与 toptw MEAL_BUFFER_MIN=120 假设一致
- meal_cost：est_cost 估算口径（仅影响成本指标展示）
- center：市中心代表点（仅影响 retrieval 距离启发与酒店兜底，低敏感）

幂等：已有 city 字段则跳过。
"""
import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

META = {
    "上海": {"center": {"lat": 31.2304, "lng": 121.4737}},
    "南京": {"center": {"lat": 32.0415, "lng": 118.7860}},
    "杭州": {"center": {"lat": 30.2794, "lng": 120.1616}},
    "武汉": {"center": {"lat": 30.5433, "lng": 114.2980}},
    "苏州": {"center": {"lat": 31.3130, "lng": 120.6240}},
}
COMMON = {
    "day_start": "09:00",
    "day_end": "21:30",
    "meal_slots": {"lunch": ["12:00", "13:00"], "dinner": ["18:00", "19:00"]},
    "meal_cost": {"lunch": 50, "dinner": 80},
}

for city, extra in META.items():
    path = os.path.join(DATA, f"{city}_pois.json")
    d = json.load(io.open(path, encoding="utf-8"))
    if "city" in d:
        print(f"{city}: 元数据已存在，跳过")
        continue
    restored = {"city": city, **extra, **COMMON, "pois": d["pois"]}
    json.dump(restored, io.open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{city}: 恢复 {len(restored['pois'])} POI + 元数据 {list(restored.keys())}")
