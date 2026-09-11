# -*- coding: utf-8 -*-
"""跨天重平衡单元测试（纯离线，无需 LLM/ortools）：

1) 地理错位 POI 应被移动到更近的日簇，且 0 违规 0 剔除；
2) 全天大点（SH006 迪士尼）永不被移动；
3) 单点日（无点可匀出）不产生移动。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db, m2_planner  # noqa: E402

city = poi_db.load_city("上海")
all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}

# 用例1：SH009 武康路（31.207,121.437 西南）错放进陆家嘴日簇（31.24,121.50 东北），
# Day2 是徐家汇/田子坊西南日簇 → 应移到 Day2，总里程显著下降
day_map = {
    1: ["SH001", "SH003", "SH025", "SH009"],   # 外滩/南京路/滨江大道 + 错位的武康路
    2: ["SH023", "SH011", "SH010"],            # 徐家汇/田子坊/新天地
}
before = m2_planner._crossday_rebalance(dict(day_map), city, all_pois, None, None, "")
rebalanced, n_moves = before
assert n_moves >= 1, f"用例1失败：地理错位未被修正（moves={n_moves}）"
assert "SH009" not in rebalanced[1] and "SH009" in rebalanced[2], f"用例1失败：移动目标错误 {rebalanced}"
print(f"用例1 ✅ 武康路移至 Day2（moves={n_moves}，日序 {rebalanced}）")

# 用例2：全天大点不可移动 —— Day1 放迪士尼，其余点错位也不应动迪士尼
day_map2 = {
    1: ["SH006", "SH001", "SH003"],
    2: ["SH023", "SH011", "SH010", "SH009"],
}
rebalanced2, n_moves2 = m2_planner._crossday_rebalance(dict(day_map2), city, all_pois, None, None, "")
assert "SH006" in rebalanced2[1], "用例2失败：迪士尼被移动"
print(f"用例2 ✅ 全天大点未被移动（moves={n_moves2}）")

# 用例3：供出日仅 1 点 → 不动
day_map3 = {
    1: ["SH009"],
    2: ["SH001", "SH003", "SH025"],
}
rebalanced3, n_moves3 = m2_planner._crossday_rebalance(dict(day_map3), city, all_pois, None, None, "")
assert n_moves3 == 0 and rebalanced3[1] == ["SH009"], f"用例3失败 {rebalanced3} moves={n_moves3}"
print(f"用例3 ✅ 单点日保持不动（moves={n_moves3}）")

print("\n全部 3 例通过")
