# -*- coding: utf-8 -*-
"""行程视图层：把内核输出压成「面板 + 模型」好消化的形状，并把局部改动写回整份行程。

**为什么单独成模块**：M3（面板交互远端化）后，同一天的处理既要在**本地** MCP server
里跑，也要在**线上** webui 后端里跑。若各写一份，两处必然漂移（本项目既有教训：
多副本实现必须有守测试）。故抽出为单一实现，`mcp/tripplanner_mcp_server.py` 与
`webui/server.py` **共用同一份**。

对外几件事：
  - `photo_of(poi_id)`         → 高德直链缩略图（读 `data/photo_cache.json`）
  - `split_cities(city_field)` → `plan.city` 的拆分口径（多城联游时是 `"A+B"`，只此一份）
  - `poi_coords(city)`         → `{poi_id: (lat, lng)}`（GCJ-02，可直接给高德；支持 `"A+B"`）
  - `normalize_day(d, coords)` → 一天的内核 timeline → 面板形状（补照片 / 坐标）
  - `apply_day(plan, day)`     → 把改动后的一天写回整份行程，刷新总里程
  - `keep_day_meta(new, old)`  → 换点 / 挪时段只改时间轴，日内文案（theme）不因缺失被清空

⚠️ `normalize_day` 必须**幂等**：已归一化的一天再走一遍要与原来逐字段一致
（否则换点 / 重排后的那天会掉字段）。自检专门守这条。
"""
from __future__ import annotations

import datetime
import json
import os

from . import poi_db

# 项目根（本文件位于 <root>/src/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PHOTO_CACHE = None


def photo_of(poi_id: str) -> str:
    """POI 照片（高德直链）。查不到返回空串 —— 面板侧据此隐藏 img，不留占位框。"""
    global _PHOTO_CACHE
    if not poi_id:
        return ""
    if _PHOTO_CACHE is None:
        path = os.path.join(ROOT, "data", "photo_cache.json")
        try:
            with open(path, encoding="utf-8") as f:
                _PHOTO_CACHE = json.load(f)
        except Exception:  # noqa: BLE001 —— 照片缺失不该影响行程本身
            _PHOTO_CACHE = {}
    url = _PHOTO_CACHE.get(poi_id) or ""
    return url if isinstance(url, str) and url.startswith("http") else ""


_COORD_CACHE: dict = {}


def split_cities(city_field: str) -> list:
    """把 `plan.city` 拆成城名列表。

    多城联游时它是 `"+".join(cities)`（见 `webui/server.py::plan_multi`：
    `_shape_mcp(..., "+".join(multi), ...)` ⇒ `normalize_plan` 把整串写进 `plan.city`）。
    下游若把它当**单个城名**用就会静默失效 —— 2026-10-10 报障「贵州5日自驾游地图不渲染」
    正是 `poi_db.load_city("贵阳+安顺+…")` 抛错后返回空坐标表。**拆分口径只有这一份**。
    """
    return [x.strip() for x in str(city_field or "").split("+") if x.strip()]


def poi_coords(city: str) -> dict:
    """{poi_id: (lat, lng)} —— 面板地图需要坐标，而内核 timeline 不提供。

    坐标是 **GCJ-02**（POI 库来源即高德），可直接交给高德 JS API，无需转换。
    按城市缓存：一次规划后的多次换点 / 重排都复用，不必反复解析整个库。
    `city` 可以是多城拼接串（`A+B`）—— 逐城合并（见 `split_cities`）。
    """
    if not city:
        return {}
    if city in _COORD_CACHE:
        return _COORD_CACHE[city]
    m: dict = {}
    for name in split_cities(city):
        if name not in _COORD_CACHE:
            one: dict = {}
            try:
                city_data = poi_db.load_city(name)
                for p in city_data.get("pois", []):
                    pp = poi_db.parse_poi(p, city_data)
                    if pp.get("id") is not None:
                        one[pp["id"]] = (pp.get("lat"), pp.get("lng"))
            except Exception:  # noqa: BLE001 —— 未知城名不该炸掉整份行程
                pass
            _COORD_CACHE[name] = one
        m.update(_COORD_CACHE[name])
    _COORD_CACHE[city] = m
    return m


def normalize_day(d: dict, coords: dict | None = None) -> dict:
    """把内核一天的原始编排压成面板 / 模型好消化的形状。

    `coords`：{poi_id: (lat, lng)}，用来给 poi 项补坐标（面板地图需要，
    而内核 timeline 只有 id/name/时刻）。
    """
    tl = []
    for s in d.get("timeline", []):
        item = {"type": s.get("type"), "name": s.get("name"),
                "start": s.get("start"), "end": s.get("end")}
        if s.get("type") == "poi":
            item["id"] = s.get("id")
            # 文案懒生成（Phase 3）：补跑 `_regen_reasons` 需要「早到等待」事实
            if s.get("arrive"):
                item["arrive"] = s.get("arrive")
            item["photo"] = photo_of(s.get("id") or "")
            c = (coords or {}).get(s.get("id"))
            if c and c[0] is not None and c[1] is not None:
                item["lat"], item["lng"] = c[0], c[1]
        if s.get("type") == "hop":
            item["min"] = s.get("min")
            item["km"] = s.get("km")
        tl.append(item)
    return {
        "day": d.get("day"),
        "theme": d.get("theme") or "",
        "reason": d.get("reason") or "",
        "weekday": d.get("weekday"),
        "travel_km": round(float(d.get("travel_km") or 0), 1),
        "travel_h": round(float(d.get("travel_h") or 0), 1),
        "finish": d.get("finish"),
        "violations": d.get("violations") or [],
        "dropped": d.get("dropped") or [],
        "reordered": bool(d.get("reordered")),
        "timeline": tl,
    }


# 换点 / 挪时段要沿用旧值的日内文案。**只列 theme**：`reason` 与 tips 逐点点名
# （"沿湖依次游览苏堤春晓…"），换掉其中一个点后就是过期信息，宁可没有。
_META_KEEP = ("theme",)


def keep_day_meta(new_day: dict, old_day: dict | None) -> dict:
    """换点 / 挪时段只改**时间轴**：新结果没给出的日内文案，沿用旧值。

    为什么必须有这一步（2026-10-10 报障「换点会导致当日主题丢失」）：
      ① 内核 `sequencer.build_itinerary` 产不出 `theme` —— 主题是 compose 阶段
         （`m2_planner.compose`）按**最终时间轴**由 LLM 生成的；
      ② `normalize_day` 为保证形状稳定，把缺失字段一律补成**空串**；
      ③ 写回端（本函数 / 面板 `applyDayToPlan` / 瘦客户端 `_merge_day_into_plan`）
         做的是**整日替换** ⇒ 空串成了「明确的空」，当天标题退化成「未命名主题」。

    新结果**明确给了** theme 时以新值为准（将来云端若按新时间轴回填主题，不许被旧值盖掉）。
    四处写回点必须同一套语义，守测试：`scripts/test_swap_keeps_theme.py` +
    `mcp/panel_guard.py`（面板侧 node 真跑）。
    """
    if not isinstance(new_day, dict) or not isinstance(old_day, dict):
        return new_day
    for k in _META_KEEP:
        if not new_day.get(k) and old_day.get(k):
            new_day[k] = old_day[k]
    return new_day


def apply_day(plan: dict, day_obj: dict) -> dict:
    """把换点 / 挪时段后的一天写回整个行程，并刷新总里程。

    返回**新的** plan（不改原对象），面板拿到后整体重渲染即可。
    """
    if not isinstance(plan, dict):
        return plan
    nd = normalize_day(day_obj, poi_coords(plan.get("city") or ""))
    old = next((d for d in (plan.get("dayList") or [])
                if d.get("day") == nd.get("day")), None)
    keep_day_meta(nd, old)
    new = dict(plan)
    days = [(nd if d.get("day") == nd.get("day") else d)
            for d in (plan.get("dayList") or [])]
    new["dayList"] = days
    new["totalTravelKm"] = round(sum(float(d.get("travel_km") or 0) for d in days), 1)
    new["updatedAt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return new


def normalize_plan(raw: dict, city: str, city_note: str = "") -> dict:
    """把规划内核的原始输出压成「面板 + 模型」都好消化的形状（整份行程）。

    只做两件事：① 补齐面板要展示但内核没给的字段（照片、坐标）；
    ② 裁掉面板用不上的诊断噪音，保证 structuredContent 不臃肿。

    **M5 从 `mcp/tripplanner_mcp_server.py` 移到这里**：面板改为「后端全走线上」后，
    归一化必须由**云端**做 —— 瘦客户端没有 `data/`（2.6M POI 库 + 照片缓存）可用，
    而 `photo_of` / `poi_coords` 都要读它。放在这里，Web UI 与 MCP 依旧**共用一份**。
    """
    itin = raw.get("itinerary") or {}
    coords = poi_coords(city)
    days = [normalize_day(d, coords) for d in itin.get("days", [])]

    dropped = []
    for p in itin.get("dropped_pois", []) or []:
        dropped.append({"name": p.get("name"), "reason": p.get("reason")})

    return {
        "city": city,
        "cityFrom": city_note,
        "query": raw.get("query"),
        "days": raw.get("days"),
        "date0": raw.get("date0"),
        "mode": raw.get("mode"),
        # Agent-Proposal（Phase 1）："agent"（调用方提案）| "server"（服务端提案，缺省路径）
        "planSource": raw.get("plan_source"),
        # 文案懒生成（Phase 3）："lazy"（reason/tips 待定稿补跑）| "full"（随规划生成）
        "copyMode": raw.get("copy_mode") or "full",
        "candidates": raw.get("candidates"),
        "hotel": raw.get("hotel"),
        "invalidPoiIds": raw.get("invalid_poi_ids") or [],
        "llmRawViolations": raw.get("llm_raw_violations"),
        "substitutes": itin.get("substitutes") or [],
        "totalViolations": itin.get("total_violations"),
        "totalTravelKm": round(float(itin.get("total_travel_km") or 0), 1),
        "droppedPois": dropped,
        "dayList": days,
        # 耗时由内核给出（M5：瘦客户端也要显示它 ⇒ 必须随归一化一起下发，
        # 否则 plan_summary 会在缺字段时崩 —— 探针实测踩过 KeyError('latency_s')）。
        "latency_s": round(float(raw.get("latency_s") or 0), 1),
        "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
