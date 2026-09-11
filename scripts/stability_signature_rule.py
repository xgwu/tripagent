# -*- coding: utf-8 -*-
"""P0-3 招牌体验规则稳定性验证：多种子 × 多主题，验证「该出现的出现、不该出现的不乱插」。

用法（需 DEEPSEEK_API_KEY）：
    python scripts/stability_signature_rule.py
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import llm_client, poi_db, proposal_planner  # noqa: E402

# (城市, 查询, 天数, 期望种子列表, 必含名称片段 or None, 禁止名称片段 or None)
CASES = [
    # 正向：亲子需求 → 招牌大点应稳定出现（多种子）
    ("上海", "上海3日亲子游", 3, [42, 7, 123, 2026, 99], "迪士尼", None),
    # 负向：非亲子主题 → 不应乱插全天大点
    ("上海", "上海2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, [42, 123], None, "迪士尼"),
    ("上海", "上海2天骑行，喜欢咖啡和美食", 2, [42, 123], None, "迪士尼"),
    # 亲子但城市无主题乐园 → 不应硬凑
    ("成都", "成都2日亲子游，想看熊猫", 2, [42, 123], "熊猫", None),
]

CITY_KEYS = {"上海": "上海迪士尼度假区", "成都": ""}


def main():
    if not llm_client.llm_available():
        print("DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        sys.exit(2)
    _orig_chat = llm_client.chat
    n_fail = 0
    print(f"{'城市':<4} {'查询':<26} {'seed':>5}  结果")
    print("-" * 80)
    for city_name, query, days, seeds, must, forbid in CASES:
        city = poi_db.load_city(city_name)
        for seed in seeds:
            # 强制当前种子（不改生产代码，运行时猴子补丁）
            def chat_with_seed(messages, **kw):
                kw["seed"] = seed
                return _orig_chat(messages, **kw)
            llm_client.chat = chat_with_seed
            try:
                r = proposal_planner.plan(city, query, days, use_llm=True)
            finally:
                llm_client.chat = _orig_chat
            names = []
            for d in (r.get("itinerary") or {}).get("days", []):
                names += [s["name"] for s in d.get("timeline", []) if s.get("type") == "poi"]
            joined = " ".join(names)
            problems = []
            if must and must not in joined:
                problems.append(f"缺少「{must}」")
            if forbid and forbid in joined:
                problems.append(f"误插「{forbid}」")
            ok = "✅" if not problems else "❌ " + "；".join(problems)
            if problems:
                n_fail += 1
            print(f"{city_name:<4} {query[:24]:<26} {seed:>5}  {ok}  （{len(names)} 点）")
            with open(f"output/stability_{city_name}_{seed}_{abs(hash(query)) % 10000}.json",
                      "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, default=str)
    print("-" * 80)
    print(f"合计失败 {n_fail} 例")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
