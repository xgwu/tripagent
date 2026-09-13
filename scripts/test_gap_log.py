# -*- coding: utf-8 -*-
"""POI 库缺口台账（gap_log）单测：追加写/无缺口跳过/损坏行容忍/汇总口径。"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gap_log


def _tmp_log():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.unlink(path)  # 追加模式从空文件开始
    return path


def test_no_gaps_skipped():
    """无缺口（grounding 为空/gaps 空）→ 不写任何行。"""
    p = _tmp_log()
    assert gap_log.append_gap_record("苏州", None, "苏州3天", 3, "m7", None, path=p) is False
    assert gap_log.append_gap_record("苏州", None, "苏州3天", 3, "m7", {"gaps": []}, path=p) is False
    assert not os.path.exists(p)
    print("✅ 无缺口不写行")


def test_append_and_record_shape():
    """有缺口 → 写一行合法 JSON，字段齐全、超长截断。"""
    p = _tmp_log()
    g = {"grounding_rate": 0.92, "n_proposed": 12,
         "gaps": [{"name": "阳澄湖半岛旅游度假区", "note": "亲子骑行" * 30, "day": 2}]}
    assert gap_log.append_gap_record("苏州", None, "带8岁孩子苏州玩3天，其中一天亲近自然骑行" + "很" * 200,
                                     3, "m7", g, path=p) is True
    rec = json.loads(open(p, encoding="utf-8").read().strip())
    assert rec["city"] == "苏州" and rec["days"] == 3 and rec["mode"] == "m7"
    assert len(rec["query"]) <= gap_log._MAX_QUERY_LEN
    assert len(rec["gaps"][0]["note"]) <= gap_log._MAX_NOTE_LEN
    assert rec["gaps"][0]["name"] == "阳澄湖半岛旅游度假区" and rec["gaps"][0]["day"] == 2
    assert rec["ts"] and rec["grounding_rate"] == 0.92
    os.unlink(p)
    print("✅ 追加写与字段口径")


def test_multi_city_record():
    """跨城：city 为 None，cities 合并展示。"""
    p = _tmp_log()
    g = {"gaps": [{"name": "迪士尼乐园", "note": "", "day": 1}]}
    assert gap_log.append_gap_record(None, ["上海", "苏州"], "苏杭4天", 4,
                                     "m7_multi", g, path=p) is True
    rec = json.loads(open(p, encoding="utf-8").read().strip())
    assert rec["city"] is None and rec["cities"] == ["上海", "苏州"]
    os.unlink(p)
    print("✅ 跨城记录")


def test_summarize_counts_and_corrupt_lines():
    """汇总：按 城市×点名 计数；损坏行跳过不炸。"""
    p = _tmp_log()
    g1 = {"grounding_rate": 0.8, "n_proposed": 10,
          "gaps": [{"name": "天平山索道", "note": "", "day": 2},
                   {"name": "阳澄湖半岛", "note": "", "day": 2}]}
    g2 = {"grounding_rate": 0.9, "n_proposed": 9,
          "gaps": [{"name": "天平山索道", "note": "", "day": 1}]}
    assert gap_log.append_gap_record("苏州", None, "苏州3天骑行", 3, "m7", g1, path=p)
    assert gap_log.append_gap_record("苏州", None, "苏州亲子3天", 3, "m7", g2, path=p)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"city": "杭州", "broken"' + "\n")  # 模拟半截写
    s = gap_log.summarize(p)
    assert s["records"] == 2 and s["lines"] == 3, f"损坏行应跳过: {s}"
    assert s["by_city"] == {"苏州": 2}
    top = {e["name"]: e["count"] for e in s["top_gaps"]}
    assert top == {"天平山索道": 2, "阳澄湖半岛": 1}, f"计数错: {top}"
    assert s["top_gaps"][0]["name"] == "天平山索道"  # 按频次降序
    assert any("苏州3天骑行" in q for q in s["top_gaps"][0]["queries"])
    os.unlink(p)
    print("✅ 汇总计数与损坏行容忍")


def test_missing_file():
    """台账不存在 → 空汇总，不抛异常。"""
    s = gap_log.summarize("/nonexistent/gap_log.jsonl")
    assert s == {"records": 0, "lines": 0, "by_city": {}, "top_gaps": []}
    print("✅ 台账缺失返回空汇总")


if __name__ == "__main__":
    test_no_gaps_skipped()
    test_append_and_record_shape()
    test_multi_city_record()
    test_summarize_counts_and_corrupt_lines()
    test_missing_file()
    print("🎉 全部通过")
