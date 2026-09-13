# -*- coding: utf-8 -*-
"""POI 库缺口台账汇总：按 城市×缺口点名 统计频次，对接扩城 SOP。

用法：
  python3 scripts/gap_report.py            # 汇总 data/poi_gap_log.jsonl
  python3 scripts/gap_report.py --top 30   # 展示前 30 个缺口
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gap_log

if __name__ == "__main__":
    s = gap_log.summarize()
    print(f"📚 POI 库缺口台账：{s['records']} 条记录 / {s['lines']} 行")
    if s["by_city"]:
        print("\n按城市（出现缺口的规划次数）：")
        for c, n in sorted(s["by_city"].items(), key=lambda kv: -kv[1]):
            print(f"  {c}: {n}")
    if s["top_gaps"]:
        print("\n高频缺口（城市 × 提案点名）：")
        for ent in s["top_gaps"]:
            print(f"  [{ent['city']}] {ent['name']} ×{ent['count']}"
                  + (f"  如: {ent['queries'][0]}" if ent["queries"] else ""))
        print("\n→ 高频缺口即扩城 SOP 的入库候选：用 scripts/add_city.py / expand_pois*.py 补点后重新校验。")
    else:
        print("（暂无缺口记录）")
