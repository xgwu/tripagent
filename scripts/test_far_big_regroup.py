# -*- coding: utf-8 -*-
"""远郊大点同天片区守门（_far_big_point_regroup）单测。

背景：LLM 提案偶发把距市中心 >12km 的半日型大点与 10km 外片区混排同天，
修复链按「剔最远点」会剔掉大点导致需求主题丢失。守门须在求解前把错配点
移到最近的天，保住大点。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import poi_db
from src.proposal_planner import _far_big_point_regroup

city = poi_db.load_city("杭州")
ALL = {p["id"]: poi_db.parse_poi(p, city) for p in city["pois"]}
BIG = "HZ047"    # 杭州极地海洋公园（萧山，dist_center 18.8km，dur 5h）
NORTH = ["HZ048", "HZ034", "HZ033", "HZ035"]  # 刀剪剑/桥西/拱宸桥/小河直街（城北，~5km）
WEST = ["HZ001", "HZ005"]  # 断桥/苏堤（西湖，~2-5km）


def test_misplaced_points_regrouped():
    """远郊大点与城北片区混排同天 → 城北点被移走，大点保留原天。"""
    day_map = {1: list(WEST), 2: [BIG] + list(NORTH)}
    moves = _far_big_point_regroup(day_map, ALL)
    assert BIG in day_map[2], "远郊大点应保留在其天"
    assert all(i not in day_map[2] for i in NORTH), "城北错配点应被移出大点天"
    assert all(i in day_map[1] for i in NORTH), "城北点应移到最近的西湖天"
    assert len(moves) == len(NORTH), f"移动记录应完整: {moves}"
    print("✅ 错配点重排：", [(m["name"], m["from"], "->", m["to"]) for m in moves])


def test_compliant_days_untouched():
    """远郊大点已与同片区点组合（湘湖距海洋公园 ~4km）→ 不触发移动。"""
    day_map = {1: list(WEST) + list(NORTH), 2: [BIG, "HZ046"]}
    moves = _far_big_point_regroup(day_map, ALL)
    assert moves == [], f"合规天不应移动: {moves}"
    assert day_map == {1: list(WEST) + list(NORTH), 2: [BIG, "HZ046"]}
    print("✅ 合规天保持不动")


def test_single_day_noop():
    """单天行程无从重排 → 原样返回。"""
    day_map = {1: [BIG] + list(NORTH)}
    moves = _far_big_point_regroup(day_map, ALL)
    assert moves == [] and day_map == {1: [BIG] + list(NORTH)}
    print("✅ 单天行程不处理")


if __name__ == "__main__":
    test_misplaced_points_regrouped()
    test_compliant_days_untouched()
    test_single_day_noop()
    print("3/3 PASS")
