# -*- coding: utf-8 -*-
"""行程导出：plan → Markdown。

**为什么单独成模块**：M3 起「导出」在两条通路上都要用 —— 本地 MCP server 的
`tripplanner_export` 工具，与线上 webui 后端的 `POST /api/export`（面板远端兜底）。
两处必须**同一份实现**（本项目既有教训：多副本必然漂移）。

纯函数、零依赖：只吃 plan dict，吐 Markdown 字符串。
"""
from __future__ import annotations

TYPE_ICON = {"poi": "📍", "hop": "→", "meal": "🍽", "hotel": "🏨"}


def _to_min(t):
    """HH:MM → 分钟数；非法返回 None。"""
    p = str(t or "").split(":")
    if len(p) != 2:
        return None
    try:
        return int(p[0]) * 60 + int(p[1])
    except ValueError:
        return None


def _dur_text(a, b) -> str:
    """由 start/end 反推停留时长文本（内核 timeline 只给时刻，不给时长）。"""
    x, y = _to_min(a), _to_min(b)
    if x is None or y is None:
        return ""
    d = y - x
    if d <= 0:
        d += 24 * 60            # 跨零点兜底
    if d <= 0:
        return ""
    hh, mm = divmod(d, 60)
    return (f"{hh} 小时" if hh else "") + (f"{mm} 分" if mm else "")


def plan_to_markdown(plan: dict) -> str:
    """把行程导出成 Markdown（面板「导出」用；也可直接粘进文档 / 微信 / 笔记）。"""
    md = []
    md.append(f"# 【{plan.get('city', '')}】{plan.get('days', '')} 天行程")
    md.append("")
    mode = str(plan.get("mode") or "")
    mode_txt = "LLM 规划" if mode.startswith("llm") else f"离线兜底（{mode or '未知'}）"
    md.append(f"**需求**：{plan.get('query') or '—'}  ")
    md.append(f"**模式**：{mode_txt} ｜ **总里程**：{plan.get('totalTravelKm')} km ｜ "
              f"**硬约束违规**：{plan.get('totalViolations')} ｜ "
              f"**候选池**：{plan.get('candidates')} 个点  ")
    stamp = str(plan.get("generatedAt") or "")
    if plan.get("updatedAt"):
        stamp += f"（更新于 {plan['updatedAt']}）"
    md.append(f"**生成时间**：{stamp}")
    if plan.get("hotel"):
        h = plan["hotel"]
        md.append(f"**住宿锚点**：{h.get('name')}（{h.get('resolved')}）")
    md.append("")
    md.append("---")

    for d in plan.get("dayList") or []:
        md.append("")
        md.append(f"## Day {d.get('day')} · {d.get('theme') or '未命名主题'}")
        meta = [f"日交通 {d.get('travel_km')} km / {d.get('travel_h')} h",
                f"收工 {d.get('finish') or '—'}"]
        if d.get("weekday"):
            meta.append(str(d["weekday"]))
        md.append("")
        md.append("> " + " ｜ ".join(meta))
        md.append("")
        for s in d.get("timeline") or []:
            t = s.get("type")
            icon = TYPE_ICON.get(t, "·")
            span = f"`{s.get('start') or ''}`"
            if s.get("end"):
                span = f"`{s.get('start')}–{s.get('end')}`"
            name = s.get("name") or ""
            if t == "hop":
                extra = []
                if s.get("km") is not None:
                    extra.append(f"{s['km']} km")
                if s.get("min") is not None:
                    extra.append(f"{s['min']} 分钟")
                md.append(f"- {span} {icon} 前往 {name}"
                          + (f"（{' · '.join(extra)}）" if extra else ""))
            else:
                dur = _dur_text(s.get("start"), s.get("end")) if t in ("poi", "meal") else ""
                md.append(f"- {span} {icon} **{name}**" + (f"（{dur}）" if dur else ""))
        if d.get("reason"):
            md.append("")
            md.append(f"*{d['reason']}*")
        if d.get("violations"):
            md.append("")
            md.append("> ⚠️ 违规：" + "；".join(str(v) for v in d["violations"]))
        if d.get("dropped"):
            md.append("")
            md.append("> ✕ 当天剔除：" + "；".join(
                f"{p.get('name')}（{p.get('reason')}）" for p in d["dropped"]))

    if plan.get("droppedPois"):
        md.append("")
        md.append("---")
        md.append("")
        md.append("## 被剔除的点")
        md.append("")
        for p in plan["droppedPois"]:
            md.append(f"- {p.get('name')} —— {p.get('reason')}")

    md.append("")
    md.append("---")
    md.append("")
    md.append("*由 TripPlanner 生成*")
    return "\n".join(md)


def export_filename(plan: dict) -> str:
    """导出文件名（与面板显示一致）。"""
    return f"TripPlanner-{plan.get('city') or 'trip'}-{plan.get('days') or ''}天.md"
