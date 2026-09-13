# -*- coding: utf-8 -*-
"""单日主题（"其中一天骑行"）口径单测。

背景：查询「国庆带8岁孩子苏州玩3天，其中一天亲近自然骑行」中骑行关键词
命中主题画像后，整单 3 天全部切到骑行口径——非主题日的 1.2~6km 接驳被
标成「骑行」、15km 里程预算也全局生效。修复：主题识别为单日粒度时，
通行模型与里程预算只作用于自然锚点所在天，其余天回退车驾口径。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sequencer
from src.sequencer import build_itinerary, scoped_theme_day, theme_scoped

Q_SCOPED = "国庆带8岁孩子苏州玩3天，其中一天亲近自然骑行"
Q_GLOBAL = "苏州骑行2天，怎么安排"
city = sequencer.poi_db.load_city("苏州")
ALL = {p["id"]: sequencer.poi_db.parse_poi(p, city) for p in city["pois"]}
TIANPING = "SZ018"  # 天平山（自然/徒步/户外，suburb）——苏州库的自然锚点


def test_theme_scoped_detection():
    """「其中一天骑行」→ 单日主题；「苏州骑行2天」→ 全局主题。"""
    assert theme_scoped(Q_SCOPED) is True, "一天+骑行同句应识别为单日主题"
    assert theme_scoped(Q_GLOBAL) is False, "无「一天」限定的骑行应保持全局主题"
    assert theme_scoped("苏州3天亲子游") is False, "无主题不判单日"
    print("✅ 单日主题识别")


def test_scoped_theme_day_picked():
    """单日主题 → 承载天 = 自然锚点所在天。"""
    day_map = {1: ["SZ001", "SZ009"], 2: [TIANPING, "SZ006"], 3: ["SZ026"]}
    d = scoped_theme_day(day_map, ALL, Q_SCOPED)
    assert d == 2, f"自然锚点天平山在 Day2，承载天应为 2，实际 {d}"
    # 全局主题/无主题 → None（不启用单日口径）
    assert scoped_theme_day(day_map, ALL, Q_GLOBAL) is None
    assert scoped_theme_day(day_map, ALL, "苏州3天") is None
    print("✅ 承载天挑选：Day", d)


def test_no_nature_anchor_no_theme_day():
    """行程中无任何自然信号（tag/名称均无）→ 不伪造承载天。"""
    pois = {"A": {"id": "A", "name": "市博物馆", "tags": ["博物馆", "历史"],
                  "lat": 31.3, "lng": 120.6},
            "B": {"id": "B", "name": "观前街", "tags": ["购物", "美食"],
                  "lat": 31.31, "lng": 120.62}}
    day_map = {1: ["A"], 2: ["B"]}
    assert scoped_theme_day(day_map, pois, Q_SCOPED) is None
    print("✅ 无自然锚点不启用单日口径")


def test_hop_labels_non_theme_days():
    """非主题天的时间轴 hop 标签不得出现「骑行」；承载天可以。"""
    day_map = {1: ["SZ001", "SZ009"], 2: [TIANPING, "SZ006"], 3: ["SZ026"]}
    itin = build_itinerary(day_map, city, ALL, date0="2026-10-02", query=Q_SCOPED)
    for d in itin["days"]:
        hops = [s["name"] for s in d["timeline"] if s["type"] == "hop"]
        if d["day"] == 2:
            continue
        bad = [h for h in hops if "骑行" in h]
        assert not bad, f"Day{d['day']} 非主题天出现骑行 hop: {bad}"
    print("✅ 非主题天 hop 无骑行标签")


def test_hiking_scoped_same_rule():
    """徒步同理：「其中一天徒步」只作用于承载天。"""
    q = "苏州3天，其中一天徒步"
    day_map = {1: ["SZ001", "SZ009"], 2: [TIANPING, "SZ006"]}
    assert theme_scoped(q) is True
    assert scoped_theme_day(day_map, ALL, q) == 2
    itin = build_itinerary(day_map, city, ALL, date0="2026-10-02", query=q)
    for d in itin["days"]:
        if d["day"] == 1:
            bad = [s["name"] for s in d["timeline"]
                   if s["type"] == "hop" and "骑行" in s["name"]]
            assert not bad, f"Day1 出现骑行 hop: {bad}"
    print("✅ 徒步单日口径同理")


def test_global_theme_regression():
    """全局主题回归：非单日表述仍整单生效（不改变既有行为）。"""
    day_map = {1: ["SZ001", "SZ009"], 2: [TIANPING, "SZ006"]}
    itin = build_itinerary(day_map, city, ALL, date0="2026-10-02", query=Q_GLOBAL)
    n_cycle_hops = sum(1 for d in itin["days"] for s in d["timeline"]
                       if s["type"] == "hop" and "骑行" in s["name"])
    # 全局骑行口径下，1.2~6km 的接驳段应标骑行（苏州库点位间距多在此区间）
    assert n_cycle_hops >= 1, "全局骑行主题下应存在骑行 hop，回归了？"
    print("✅ 全局主题行为不变（骑行 hop 数:", n_cycle_hops, "）")


if __name__ == "__main__":
    test_theme_scoped_detection()
    test_scoped_theme_day_picked()
    test_no_nature_anchor_no_theme_day()
    test_hop_labels_non_theme_days()
    test_hiking_scoped_same_rule()
    test_global_theme_regression()
    print("🎉 全部通过")
