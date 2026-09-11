# -*- coding: utf-8 -*-
"""基线方案（旧架构）：关键词→标签硬过滤 + 贪婪补足 + 最近邻排序。

模拟文档中「之前的方案」：LLM 只做标签提取，POI 完全靠标签匹配召回，
路线由排序器决定，没有任何经验先验。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, retrieval, sequencer

# 查询关键词 → 硬过滤标签（基线的世界只有这些标签）
HARD_FILTER = {
    "亲子": ["亲子", "family"], "孩子": ["亲子", "family"], "娃": ["亲子", "family"], "5岁": ["亲子", "family"],
    "历史": ["历史"], "文化": ["文化", "博物馆"], "博物馆": ["博物馆"],
    "美食": ["美食", "夜市"], "吃": ["美食"], "夜市": ["夜市"], "夜景": ["夜景"],
    "拍照": ["photo"], "徒步": ["徒步"], "自然": ["自然"], "小众": ["小众"], "经典": ["经典"],
    "轻松": ["轻松"], "购物": ["购物"],
}


def plan(city: dict, query: str, days: int = 2) -> dict:
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    tags = []
    for kw, tl in HARD_FILTER.items():
        if kw in query:
            tags.extend(tl)
    tags = list(dict.fromkeys(tags))

    # 硬过滤：命中任一标签才进候选池（旧方案的召回瓶颈所在）
    if tags:
        pool = [p for p in all_pois.values()
                if any(t in p["tags"] or t == p["category"] for t in tags)]
    else:
        pool = sorted(all_pois.values(), key=lambda p: -p["rating"])[:20]
    recalled = len(pool)

    day_map, t0 = {}, __import__("time").time()
    per_day = 3 if any(k in query for k in ("亲子", "孩子", "轻松")) else 4
    pool = sorted(pool, key=lambda p: -p["rating"])
    idx = 0
    for d in range(1, days + 1):
        ids = []
        budget = per_day * 2.0  # 时长预算（小时）
        while idx < len(pool) and len(ids) < per_day:
            p = pool[idx]
            if p["dur"] <= budget:
                ids.append(p["id"])
                budget -= p["dur"]
            idx += 1
        day_map[d] = ids
    # 基线没有经验先验：乱序丢给排序器（order_given=False，纯 NN 排序）
    itin = sequencer.build_itinerary(day_map, city, all_pois, order_given=False)
    return {"mode": "baseline_tag_filter", "query": query, "days": days,
            "candidates": recalled, "invalid_poi_ids": [],
            "llm_raw_violations": 0, "latency_s": round(__import__("time").time() - t0, 1),
            "itinerary": itin, "filter_tags": tags}
