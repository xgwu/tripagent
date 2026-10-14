# -*- coding: utf-8 -*-
"""行程导出：plan → Markdown。

**为什么单独成模块**：M3 起「导出」在两条通路上都要用 —— 本地 MCP server 的
`tripplanner_export` 工具，与线上 webui 后端的 `POST /api/export`（面板远端兜底）。
两处必须**同一份实现**（本项目既有教训：多副本必然漂移）。

纯函数、零依赖：只吃 plan dict，吐 Markdown 字符串。
"""
from __future__ import annotations

TYPE_ICON = {"poi": "📍", "hop": "→", "meal": "🍽", "hotel": "🏨"}
# 城际转移按方式换图标（与 webui / MCP 面板同口径）：飞机✈️ / 高铁🚄 / 自驾🚗 / 待定🚌
TRANSFER_ICON = {"driving": "🚗", "rail": "🚄", "flight": "✈️"}

# 降级关键词：只要 mode 里出现这些，就说明**没走到**该模式的原生路径
_DEGRADED_KEYS = ("degrad", "fallback", "offline")


def mode_label(mode) -> str:
    """行程 mode → 人类可读标签。**导出与面板、Web UI 必须同一口径。**

    ⚠️ 为什么不能只判 `startswith("llm")`（2026-10-10 实测暴露的真缺陷）：
    线上**主路径**是 M7 经验提案，成功时 mode == `"m7_proposal"` —— 它不以 `llm`
    开头，于是被错标成「离线兜底」，**每一条 M7 行程导出后都显示错标签**
    （面板徽标 `plan_panel.html` 犯的是同一个错，导致用户看到「离线兜底规划」）。

    真实取值（`src/` 全量枚举）：
        `m7_proposal`                   M7 提案成功（LLM）      → M7 经验提案
        `m7_proposal → m7_degraded`     M7 提案被降级           → M7 经验提案（降级）
        `m7_proposal_failed → m2_fallback`  M7 失败退 M2        → M7 经验提案（降级）
        `m2_toptw` / `m2_...`           M2 库内直选 + TOPTW      → M2 库内直选 + TOPTW
        `llm`                           M1 直接走 LLM           → LLM 规划
        `offline_fallback(...)`         真兜底                  → 离线兜底（…）
    """
    m = str(mode or "").strip()
    if not m:
        return "未知"
    low = m.lower()
    degraded = any(k in low for k in _DEGRADED_KEYS)
    for prefix, name in (("m7", "M7 经验提案"),
                         ("m2", "M2 库内直选 + TOPTW"),
                         ("llm", "LLM 规划")):
        if m.startswith(prefix):
            return name + ("（降级）" if degraded else "")
    # 认不出的：真兜底就按兜底写（保持历史文案），否则原样回显（诚实 > 好看）
    return f"离线兜底（{m}）" if degraded else m


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
    mode_txt = mode_label(mode)
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
            if t == "transfer":
                icon = TRANSFER_ICON.get(s.get("mode") or "", "🚌")
            else:
                icon = TYPE_ICON.get(t, "·")
            span = f"`{s.get('start') or ''}`"
            if s.get("end"):
                span = f"`{s.get('start')}–{s.get('end')}`"
            name = s.get("name") or ""
            if t == "hop":
                # ⚠️ hop 的 name 已含「车程 15 分钟 · 3.7km」（src/sequencer.py::_hop_label），
                #    不要再拼 s.km / s.min —— 否则会导出成
                #    「→ 前往 车程 15 分钟 · 3.7km（3.7 km · 15 分钟）」重复显示。
                #    与 MCP 面板是同一个坑（2026-10-10 报障，两处一起修）。
                md.append(f"- {span} {icon} {name}")
            elif t == "transfer":
                # 城际段：附时长 + 门到门明细（飞机段 detail 有候机/起降/巡航/接驳拆解，
                # 不加会在行程单里被误以为「飞行 4 小时」）
                dur = _dur_text(s.get("start"), s.get("end"))
                det = s.get("detail") or ""
                md.append(f"- {span} {icon} **{name}**"
                          + (f"（{dur}）" if dur else "")
                          + (f"\n  - <sub>{det}</sub>" if det else ""))
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
