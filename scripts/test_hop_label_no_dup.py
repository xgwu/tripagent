# -*- coding: utf-8 -*-
"""hop（通行段）展示口径单测：里程 / 时长**不得重复显示**。

背景（2026-10-10 报障）：面板上出现
    「→ 车程 15 分钟 · 3.7km　3.7km · 15分钟」
根因：hop 的 `name` 已经是 `src/sequencer.py::_hop_label()` 产出的
    「车程 15 分钟 · 3.7km」（**自带**方式 + 分钟 + 公里），而渲染层又拿
    结构化 `s.km` / `s.min` 拼了一遍。同一坑有两个消费方：
      ① `mcp/panel/plan_panel.html`（面板）—— 行为守卫在 `mcp/panel_guard.py`
      ② `src/md_export.py`（导出 Markdown）—— 由本文件守
⇒ 钉两条不变式：
    A. `_hop_label()` 的输出**必须同时含** km 与分钟
       （否则渲染层「不再自己拼」会直接丢信息）；
    B. `plan_to_markdown()` 的每个 hop 行里，该段里程数值**只出现一次**。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import md_export  # noqa: E402
from src import sequencer  # noqa: E402


def _plan_with_hop():
    """一天两点的最小行程，中间夹一个 3.7km / 15 分钟的车程段。"""
    return {
        "city": "杭州", "days": 1, "totalTravelKm": 7.7, "totalViolations": 0,
        "candidates": 12, "mode": "m7_proposal", "query": "杭州1天，带8岁孩子",
        "generatedAt": "2026-10-10 18:00:00",
        "dayList": [{
            "day": 1, "theme": "西湖亲子日", "travel_km": 7.7, "travel_h": 0.5,
            "finish": "19:00",
            "timeline": [
                {"type": "poi", "id": "HZ01", "name": "断桥残雪",
                 "start": "09:00", "end": "10:00"},
                {"type": "hop", "name": "车程 15 分钟 · 3.7km",
                 "start": "10:00", "end": "10:15", "km": 3.7, "min": 15},
                {"type": "poi", "id": "HZ02", "name": "灵隐寺",
                 "start": "10:15", "end": "11:15"},
            ],
        }],
    }


def test_hop_label_contains_km_and_min():
    """不变式 A：`_hop_label()` 自带「方式 + 分钟 + 公里」，渲染层据此不再补。"""
    s = sequencer._hop_label(3.7, 0.25, None)
    assert "车程" in s and "15 分钟" in s and "3.7km" in s, s
    # 骑行 / 徒步 / 自驾三种口径同样自带（防空口；取值都落在各自「非步行」区间）
    for mode, km, must in (("cycling", 6.0, "骑行"), ("hiking", 2.0, "步行"),
                           ("driving", 6.0, "自驾")):
        s2 = sequencer._hop_label(km, 0.5, mode)
        assert must in s2 and "30 分钟" in s2 and str(km) + "km" in s2, s2
    print("✅ _hop_label 自带里程与分钟")


def test_markdown_hop_line_no_duplicate():
    """不变式 B：导出的 hop 行里，里程数值只出现一次（旧实现会出现两次）。"""
    md = md_export.plan_to_markdown(_plan_with_hop())
    lines = [ln for ln in md.splitlines() if "→" in ln]
    assert lines, "导出里没找到 hop 行：\n" + md
    hop_line = lines[0]
    n = len(re.findall(r"3\.7\s*km", hop_line))
    assert n == 1, f"hop 行里程重复显示（{n} 次）：{hop_line!r}"
    assert "车程 15 分钟" in hop_line, hop_line
    # 更宽的兜底：整段行程里 "3.7" 只应出现在这一处
    assert md.count("3.7") == 1, f"里程 3.7 出现 {md.count('3.7')} 次：\n{md}"
    print("✅ 导出 hop 行不重复：", hop_line.strip())


if __name__ == "__main__":
    test_hop_label_contains_km_and_min()
    test_markdown_hop_line_no_duplicate()
    print("🎉 全部通过")
