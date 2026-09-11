# -*- coding: utf-8 -*-
"""离线启发式规划器 —— LLM 不可用时的同接口兜底（也是 pipeline 测试工具）。

模拟「本地经验」：按地理分区聚天 + 评分优先 + 标签加权，给出模板化理由。
注意：这只是让管线跑通的替代品，不代表 M1 的真实体验假设。
"""
from . import sequencer, poi_db

BUDGET_H_PER_DAY = 8.0
CLUSTER_RADIUS_KM = 9.0  # 同日 POI 必须与锚点在半径内（地理聚集优先）


def plan_days(city: dict, cands: list, query: str, days: int):
    # 标签得分（复用检索的关键词映射）
    from .retrieval import extract_query_tags
    qtags = extract_query_tags(query)

    def score(p):
        s = p["rating"] * 2 + sum(2 for t in qtags if t in p["tags"] or t == p["category"])
        if qtags and any(k in ("亲子", "family") for k in qtags) and not p["family_ok"]:
            s -= 100  # 亲子硬排除
        return s

    pool = sorted(cands, key=score, reverse=True)
    used = set()
    day_map, themes = {}, {}
    for d in range(1, days + 1):
        # 锚点：未用池中得分最高者；同日只选锚点半径内的点（真实距离，非 area 标签）
        anchor = next((p for p in pool if p["id"] not in used), None)
        if anchor is None:
            break
        chosen, t = [], 0.0
        for p in pool:
            if p["id"] in used:
                continue
            km = poi_db.haversine_km(anchor["lat"], anchor["lng"], p["lat"], p["lng"])
            if p is not anchor and km > CLUSTER_RADIUS_KM:
                continue
            if t + p["dur"] > BUDGET_H_PER_DAY:
                continue
            chosen.append(p)
            used.add(p["id"])
            t += p["dur"]
        if not chosen:
            chosen.append(anchor)
            used.add(anchor["id"])
        day_map[d] = [p["id"] for p in chosen]
        names = "/".join(p["name"] for p in chosen[:3])
        themes[d] = {"theme": f"{anchor['name']}周边线",
                     "reason": f"（离线兜底：以{anchor['name']}为锚点{CLUSTER_RADIUS_KM:.0f}km内地理聚集+评分排序，非 LLM 经验编排）"}
    return day_map, themes
