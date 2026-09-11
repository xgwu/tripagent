# -*- coding: utf-8 -*-
"""多路召回：标签 + 关键词 + 地理 + 热度，产出 LLM 的候选卡片集。

M1 核心思想：不让 LLM 凭世界知识自由生成 POI 名称（幻觉源头），
而是先从自建库检索候选，LLM 只能在候选 ID 里做选择与排序（结构性零幻觉）。
"""
import re
from . import poi_db

# 查询关键词 → 检索标签映射（多路召回的「语义」一路，无向量化版）
KEYWORD_TAGS = {
    "亲子": ["亲子", "family"], "孩子": ["亲子", "family"], "娃": ["亲子", "family"],
    "儿童": ["亲子", "family"], "小朋友": ["亲子", "family"], "5岁": ["亲子", "family"],
    "历史": ["历史", "history"], "文化": ["文化", "culture"], "博物馆": ["博物馆"],
    "寺庙": ["寺庙"], "茶": ["茶文化"],
    "美食": ["美食", "小吃", "夜市"], "吃": ["美食", "小吃", "夜市"],
    "杭帮菜": ["美食"], "本帮菜": ["美食"], "苏帮菜": ["美食"], "汉味": ["美食"], "过早": ["美食"],
    "夜景": ["夜景", "夜生活"], "拍照": ["photo", "文艺"], "打卡": ["photo"],
    "徒步": ["徒步", "outdoor"], "自然": ["自然", "nature"], "轻松": ["轻松", "relax"],
    "不要太累": ["轻松"], "购物": ["购物", "shopping"], "小众": ["小众"],
    "经典": ["经典"], "骑行": ["骑行"], "雨天": ["室内"],
}


def extract_query_tags(query: str) -> list:
    tags = []
    for kw, tlist in KEYWORD_TAGS.items():
        if kw in query:
            tags.extend(tlist)
    return list(dict.fromkeys(tags))


def recall(city: dict, query: str, max_candidates: int = 45) -> list:
    """多路召回，返回候选 POI（已 parse）。四路合并去重。"""
    pois = [poi_db.parse_poi(p, city) for p in city["pois"]]
    qtags = extract_query_tags(query)
    scores = {p["id"]: 0.0 for p in pois}
    hit = lambda pid, w: scores.__setitem__(pid, scores.get(pid, 0) + w)

    # 路1：标签/关键词匹配
    for p in pois:
        overlap = len(set(p["tags"]) | {p["category"]}) and len(
            [t for t in qtags if t in p["tags"] or t == p["category"]])
        if overlap:
            hit(p["id"], 3.0 * overlap)
    # 路2：名称直命中（如"西湖""灵隐"）
    for p in pois:
        for frag in re.findall(r"[\u4e00-\u9fa5]{2,4}", query):
            if frag in p["name"] or p["name"] in frag:
                hit(p["id"], 5.0)
    # 路3：热度/质量先验（rating）
    for p in pois:
        hit(p["id"], p["rating"] * 0.4)
    # 路4：地理多样性 —— 离市中心太远的轻微降权（除非被前面正命中）
    for p in pois:
        if scores[p["id"]] <= 1.6 and p["dist_center_km"] > 12:
            hit(p["id"], -1.5)

    ranked = sorted(pois, key=lambda p: scores[p["id"]], reverse=True)
    return ranked[:max_candidates]


def candidate_cards(cands: list, detail_top: int | None = None) -> str:
    """两级卡片（M4-3 token 优化）：

    - Top `detail_top`（召回序，即与需求最相关）：完整卡片（标签/建议时段/小贴士）
    - 其余：单行压缩卡（ID/名称/分类/时长/营业时间/价格/建议时段）——备选只需
      「知道有什么」，组线叙事由 Top 卡承担；默认自适应：池的 55%，8~20 之间
    """
    if detail_top is None:
        detail_top = max(8, min(20, int(len(cands) * 0.55)))
    lines = []
    for i, p in enumerate(cands):
        closed = ("｜闭馆:" + "/".join(p["closed_days"])) if p.get("closed_days") else ""
        if i < detail_top:
            tags = "/".join(p["tags"][:4])
            lines.append(
                f'{p["id"]} {p["name"]}｜{p["category"]}｜标签:{tags}｜时长{p["duration_h"]}h｜'
                f'{p["open"]}-{p["close"]}｜¥{p["price"]}｜评分{p["rating"]}｜'
                f'建议时段:{p["best_time"]}｜{p["note"]}')
        else:
            lines.append(
                f'{p["id"]} {p["name"]}｜{p["category"]}｜{p["duration_h"]}h｜'
                f'{p["open"]}-{p["close"]}｜¥{p["price"]}｜{p["best_time"]}{closed}')
    return "\n".join(lines)
