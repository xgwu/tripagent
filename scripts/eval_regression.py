# -*- coding: utf-8 -*-
"""P2-5 基准回归：固化跨城基准查询，一键验证「0 违规 + 天数正确」不退化。

默认离线确定性路径（use_llm=False → M7 降级 M1 链路，不受 LLM 随机性影响），
适合每次改动后快速回归；--llm 可选走真实 M7 全链路（结果有随机性，仅观察）。

用法：
    python scripts/eval_regression.py            # 离线回归（CI 友好，非 0 退出码）
    python scripts/eval_regression.py --llm      # 真实 LLM 全链路（观察用）
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db, m1_planner, proposal_planner  # noqa: E402

# (城市, 查询, 期望天数, 期望落地率下限[仅 --llm 生效])
CASES = [
    ("上海", "上海2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, 0.8),
    ("上海", "上海2天骑行，喜欢咖啡和美食", 2, 0.8),
    ("上海", "带5岁孩子去上海玩2天，不要太累，最好有动物或者博物馆", 2, 0.8),
    ("上海", "上海下雨天玩2天，想多安排室内场馆", 2, 0.7),
    ("杭州", "杭州2天骑行，喜欢咖啡和美食", 2, 0.8),
    ("杭州", "杭州2天历史文化深度游", 2, 0.8),
    ("南京", "带5岁孩子去南京玩2天，不要太累", 2, 0.8),
    ("苏州", "苏州2天园林和美食漫游", 2, 0.8),
    ("武汉", "武汉2天早餐美食和长江桥梁打卡", 2, 0.8),
]


def main():
    use_llm = "--llm" in sys.argv
    n_fail = 0
    print(f"{'城市':<4} {'查询':<28} {'天数':>4} {'实际':>4} {'违规':>4} {'落地率':>7} {'耗时':>6}  结果")
    print("-" * 88)
    t_all = time.time()
    for city_name, query, want_days, min_rate in CASES:
        city = poi_db.load_city(city_name)
        planner = proposal_planner if use_llm else m1_planner
        t0 = time.time()
        try:
            r = planner.plan(city, query, want_days, use_llm=use_llm)
        except Exception as e:  # noqa
            print(f"{city_name:<4} {query:<28}    --    --    --       --     ❌ 异常 {type(e).__name__}: {e}")
            n_fail += 1
            continue
        sec = time.time() - t0
        it = r["itinerary"]
        n_days = len(it["days"])
        viol = it["total_violations"]
        g = r.get("grounding") or {}
        rate = g.get("grounding_rate", 1.0)
        problems = []
        if n_days != want_days:
            problems.append(f"天数 {n_days}≠{want_days}")
        if viol != 0:
            problems.append(f"违规 {viol}")
        if use_llm and rate < min_rate:
            problems.append(f"落地率 {rate:.0%}<{min_rate:.0%}")
        ok = "✅" if not problems else "❌ " + "；".join(problems)
        n_fail += 0 if not problems else 1
        print(f"{city_name:<4} {query[:26]:<28} {want_days:>4} {n_days:>4} {viol:>4} {rate:>6.0%} {sec:>5.1f}s  {ok}")
    print("-" * 88)
    print(f"合计 {len(CASES)} 例，失败 {n_fail}，总耗时 {time.time()-t_all:.1f}s（{'LLM 全链路' if use_llm else '离线确定性'}）")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
