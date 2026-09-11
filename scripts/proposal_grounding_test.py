#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提案落地率测试：多城 × 多轮统计「LLM 世界知识提案 → 库内落地」的存活情况。

用法：
    python3 scripts/proposal_grounding_test.py            # 默认 3 城 × 2 轮
    python3 scripts/proposal_grounding_test.py --rounds 3 # 指定轮数

指标：提案点数、四级落地分布、未落地缺口（世界知识被丢弃的部分）、
锚点地标命中（上海迪士尼 SH006）——世界知识主导后是否仍稳定。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import poi_db, proposal_planner  # noqa: E402

CASES = [
    ("上海", "上海3日亲子游，住迪士尼附近酒店", 3, "SH006"),
    ("武汉", "带6岁孩子去武汉玩3天", 3, None),
    ("成都", "成都3天经典深度游，喜欢历史文化、美食", 3, None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        from src.config import load_config
        os.environ.setdefault("DEEPSEEK_API_KEY", load_config().get("deepseek_api_key", ""))

    rows = []
    for city_name, query, days, anchor_id in CASES:
        city = poi_db.load_city(city_name)
        all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
        for rnd in range(1, args.rounds + 1):
            raw = proposal_planner.llm_client.chat(
                [{"role": "system", "content": proposal_planner.PROPOSE_SYSTEM},
                 {"role": "user", "content": proposal_planner.PROPOSE_PROMPT.format(
                     city=city_name, days=days, query=query,
                     date_line="", weather_line="",
                     library_hint=proposal_planner._library_hint(all_pois))}],
                temperature=0.2, seed=42 + rnd)
            proposal = proposal_planner.llm_client.parse_json_safe(raw)
            day_map, _themes, st = proposal_planner._ground(proposal, city, all_pois, days)
            anchor_hit = "n/a"
            if anchor_id:
                anchor_hit = "✅" if any(anchor_id in ids for ids in day_map.values()) else "❌"
            gap_names = [g["name"] for g in st["gaps"]]
            rows.append((city_name, rnd, st["n_proposed"],
                         st["exact"], st["contain"], st["fuzzy"], st["llm"],
                         st["unmatched"], st["grounding_rate"], anchor_hit, gap_names))
            print(f"{city_name} r{rnd} | 提案{st['n_proposed']:>2} | 落地 e{st['exact']} c{st['contain']} "
                  f"f{st['fuzzy']} l{st['llm']} | 缺口{st['unmatched']:>2} | 率{st['grounding_rate']:.0%} "
                  f"| 锚点{anchor_hit}")
            if gap_names:
                print(f"    缺口: {gap_names}")

    n = len(rows)
    if n:
        avg_rate = sum(r[8] for r in rows) / n
        avg_gap = sum(r[7] for r in rows) / n
        print("-" * 60)
        print(f"合计 {n} 轮 | 平均落地率 {avg_rate:.0%} | 平均缺口 {avg_gap:.1f} 点/轮")


if __name__ == "__main__":
    main()
