# -*- coding: utf-8 -*-
"""M5-1：从 POI note 提取闭馆日 → closed_days 字段回填（全城市）。

规则：note 中的「周X闭馆/闭门」模式（X ∈ 一~日/天）→ closed_days = ["周一", ...]
- 所有 POI 统一补上 closed_days 字段（无闭馆为 []），schema 对齐
- 幂等：已有 closed_days 的 POI 重新解析覆盖（以 note 为准）
用法：python scripts/backfill_closed_days.py
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PATTERN = re.compile(r"周([一二三四五六日天])闭[馆门]")
NAME_MAP = {"一": "周一", "二": "周二", "三": "周三", "四": "周四",
            "五": "周五", "六": "周六", "日": "周日", "天": "周日"}


def backfill_file(path: str) -> tuple[int, int]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    n_closed = 0
    for p in data["pois"]:
        found = sorted({NAME_MAP[m] for m in PATTERN.findall(p.get("note", ""))})
        p["closed_days"] = found
        if found:
            n_closed += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data["pois"]), n_closed


def main():
    total = n_closed = 0
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*_pois.json"))):
        city = os.path.basename(path).replace("_pois.json", "")
        n, c = backfill_file(path)
        total += n
        n_closed += c
        print(f"{city}: {n} POI，{c} 个有闭馆日")
    print(f"完成：{total} POI 回填 closed_days，{n_closed} 个闭馆")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
