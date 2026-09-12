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


def test_family_solo_day():
    """family 查询：5h+ 远郊点独占日——同天所有点（含近邻湘湖）都让位。"""
    day_map = {1: list(WEST), 2: [BIG, "HZ046"]}
    moves = _far_big_point_regroup(day_map, ALL, family=True)
    assert day_map[2] == [BIG], f"独占日应清空其他点: {day_map[2]}"
    assert "HZ046" in day_map[1], "湘湖应移到 Day1"
    assert len(moves) == 1
    print("✅ family 独占日：", [(m["name"], m["from"], "->", m["to"]) for m in moves])


def test_family_two_strict_days_no_swap():
    """family 查询两天各有 5h+ 远郊点 → 不互踢不震荡（各自保留，点无处可移则不动）。"""
    SAFARI = "HZ057"  # 杭州野生动物世界（富阳，21.2km，5.5h）
    day_map = {1: [BIG, "HZ001"], 2: [SAFARI, "HZ048"]}
    moves = _far_big_point_regroup(day_map, ALL, family=True)
    assert BIG in day_map[1] and SAFARI in day_map[2], "两个独占日大点应原地保留"
    assert moves == [], f"互为独占日时不应移动: {moves}"
    print("✅ 双独占日不震荡")


def test_family_nonfamily_diff():
    """同一混排：非 family 只移 10km 外错配点，family 把近邻也让位。"""
    day_map = {1: list(WEST), 2: [BIG, "HZ046"]}
    moves_normal = _far_big_point_regroup(dict(day_map), ALL, family=False)
    assert moves_normal == [], "非 family：湘湖距大点 ~4km 合规不移动"
    moves_family = _far_big_point_regroup({1: list(WEST), 2: [BIG, "HZ046"]}, ALL, family=True)
    assert len(moves_family) == 1, "family：近邻也让位独占"
    print("✅ family/非 family 行为差异符合预期")


def test_demand_notices():
    """主题保真：动物点被剔且行程无同主题点 → 通知；有动物园保留 → 无通知。"""
    from src.m2_planner import _demand_notices
    q = "带5岁孩子去杭州玩2天，不要太累，最好有动物"
    dropped = [{"id": BIG, "name": ALL[BIG]["name"], "day": 2}]
    # 行程只剩西湖点（无动物标签）→ 应报「动物需求未满足」
    itin = {"days": [{"day": 1, "timeline": [{"type": "poi", "id": i} for i in WEST]}]}
    notices = _demand_notices(q, dropped, itin, ALL)
    assert any(n["tag"] == "动物" for n in notices), f"应生成动物需求通知: {notices}"
    # 行程含杭州动物园（HZ056 有动物标签）→ 不应报
    itin2 = {"days": [{"day": 1, "timeline": [{"type": "poi", "id": "HZ056"}]}]}
    notices2 = _demand_notices(q, dropped, itin2, ALL)
    assert not any(n["tag"] == "动物" for n in notices2), f"有同主题点不应通知: {notices2}"
    print("✅ 需求满足检测：剔除报通知 / 同主题保留不报")


if __name__ == "__main__":
    test_misplaced_points_regrouped()
    test_compliant_days_untouched()
    test_single_day_noop()
    test_family_solo_day()
    test_family_two_strict_days_no_swap()
    test_family_nonfamily_diff()
    test_demand_notices()
    print("7/7 PASS")
