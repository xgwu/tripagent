# -*- coding: utf-8 -*-
"""行程视图层：把内核输出压成「面板 + 模型」好消化的形状，并把局部改动写回整份行程。

**为什么单独成模块**：M3（面板交互远端化）后，同一天的处理既要在**本地** MCP server
里跑，也要在**线上** webui 后端里跑。若各写一份，两处必然漂移（本项目既有教训：
多副本实现必须有守测试）。故抽出为单一实现，`mcp/tripplanner_mcp_server.py` 与
`webui/server.py` **共用同一份**。

对外四件事：
  - `photo_of(poi_id)`         → 高德直链缩略图（读 `data/photo_cache.json`）
  - `poi_coords(city)`         → `{poi_id: (lat, lng)}`（GCJ-02，可直接给高德）
  - `normalize_day(d, coords)` → 一天的内核 timeline → 面板形状（补照片 / 坐标）
  - `apply_day(plan, day)`     → 把改动后的一天写回整份行程，刷新总里程

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


def poi_coords(city: str) -> dict:
    """{poi_id: (lat, lng)} —— 面板地图需要坐标，而内核 timeline 不提供。

    坐标是 **GCJ-02**（POI 库来源即高德），可直接交给高德 JS API，无需转换。
    按城市缓存：一次规划后的多次换点 / 重排都复用，不必反复解析整个库。
    """
    if not city:
        return {}
    if city in _COORD_CACHE:
        return _COORD_CACHE[city]
    m: dict = {}
    try:
        city_data = poi_db.load_city(city)
        for p in city_data.get("pois", []):
            pp = poi_db.parse_poi(p, city_data)
            if pp.get("id") is not None:
                m[pp["id"]] = (pp.get("lat"), pp.get("lng"))
    except Exception:  # noqa: BLE001
        pass
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


def apply_day(plan: dict, day_obj: dict) -> dict:
    """把换点 / 挪时段后的一天写回整个行程，并刷新总里程。

    返回**新的** plan（不改原对象），面板拿到后整体重渲染即可。
    """
    if not isinstance(plan, dict):
        return plan
    nd = normalize_day(day_obj, poi_coords(plan.get("city") or ""))
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
