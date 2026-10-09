# -*- coding: utf-8 -*-
"""行程微调内核：换点 / 挪时段。

**为什么单独成模块**：这段逻辑原先内联在 `webui/server.py` 的 `/api/replace` 处理函数里，
MCP App 也要用同一套能力（面板内「换一个」按钮）。若复制一份，两处实现必然漂移
（本项目的既有教训：多副本注册表必须有机守测试）。故抽出为单一实现，Web UI 与 MCP 共用。

共用内核：`poi_db`（POI 库 / 球面距离）+ `sequencer.build_itinerary`（时序编排 + 修复链）。

两条守门规则（都来自原实现，别弱化）：
  - **零剔点**：`build_itinerary` 遇到闭馆 / 餐窗 / 里程违规会走修复链**剔点**（含前置修剪）。
    换点不得改变当天景点数，因此只接受 `dropped` 为空且景点一一对应的候选。
  - **点集不变**：新方案的点集必须与请求的点集完全一致（换点：原顺序里的点除被换点外不变；
    挪时段：同一批点换顺序）。否则说明内核改动了行程内容，不是用户要的。

返回约定：两个函数都返回 `(http_code, payload)`，`payload` 形状与原 `/api/replace`
的响应体一致，Web UI 可直接 `self._json(payload, code=code)`；MCP 侧只看 `ok` / `error`。
"""
from __future__ import annotations

from . import poi_db


def _load(city_name: str):
    """载入城市 POI 库。返回 (city, all_pois)。"""
    city = poi_db.load_city(city_name)
    all_pois = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}
    return city, all_pois


def _poi_ids_of_day(day_obj: dict) -> list:
    """从一天的编排结果里取出景点 id（按时间轴顺序，不含 hop / meal / hotel）。"""
    return [s["id"] for s in (day_obj.get("timeline") or []) if s.get("type") == "poi"]


def _too_bad(d0: dict, expect_ids: list) -> bool:
    """守门判据：出现剔点，或景点数与点集对不上。"""
    tl_ids = _poi_ids_of_day(d0)
    if d0.get("dropped"):
        return True
    if len(tl_ids) != len(expect_ids):
        return True
    return set(tl_ids) != set(expect_ids)


def replace_poi(city_name: str, poi_id: str, day, day_ids: list,
                used_ids: list | None = None, date0: str | None = None,
                query: str = "", hotel_text: str | None = None,
                max_try: int = 25) -> tuple:
    """找一个「同类目、就近」的替代点替换当天某个点，并重排当天。

    候选策略：同类目优先（没有同类才放宽到全部）→ 按「距原点的球面距离升序、评分降序」排 →
    **逐个试排**，取第一个「零剔点且景点一一对应」的候选。试到 `max_try` 个为止，
    全都不行就明确拒绝（而不是硬塞一个会挤掉别处的点）。

    返回 (code, payload)：
      (200, {"ok": True, "swap": {from,to,same_category}, "new_id": …,
             "day": {timeline,travel_km,finish,violations,dropped,weekday,…}})
      (200, {"ok": False, "error": …})   候选池空 / 全部候选都会挤掉行程
      (404, {"ok": False, "error": …})   起点不存在
      (500, {"ok": False, "error": …, "trace": …})
    """
    from . import hotel as hotel_mod, sequencer
    try:
        city, all_pois = _load(city_name)
        replaced = all_pois.get(poi_id)
        if not replaced:
            return 404, {"ok": False, "error": f"POI {poi_id} 不存在"}

        # 排除「当天已用 + 全程已用」的点，含被换点本身
        exclude = set(used_ids or []) | set(day_ids)
        cands = [p for p in all_pois.values() if p["id"] not in exclude]
        if not cands:
            return 200, {"ok": False, "error": "候选池已空，无点可换"}

        same_cat = [p for p in cands if p["category"] == replaced["category"]]
        pool = same_cat or cands            # 同类目优先；没有则放宽到全部
        pool.sort(key=lambda p: (
            poi_db.haversine_km(p["lat"], p["lng"], replaced["lat"], replaced["lng"]),
            -p["rating"]))

        hotel = hotel_mod.resolve_hotel(city, hotel_text or None)
        date0 = date0 or None
        query = query or ""

        pick = it = None
        for cand in pool[:max_try]:
            trial_ids = [cand["id"] if i == poi_id else i for i in day_ids]
            trial = sequencer.build_itinerary({int(day): trial_ids}, city, all_pois,
                                              order_given=False, date0=date0,
                                              hotel=hotel, query=query)
            d0 = trial["days"][0]
            if not _too_bad(d0, trial_ids):
                pick, it = cand, trial
                break
        if pick is None:
            return 200, {"ok": False,
                         "error": "附近的候选点都会挤掉当天其他行程，已保留原行程；可换一天或稍后再试"}

        return 200, {
            "ok": True,
            "swap": {"from": replaced["name"], "to": pick["name"],
                     "same_category": bool(same_cat)},
            "new_id": pick["id"],
            "day": it["days"][0],
        }
    except Exception as e:  # noqa: BLE001 —— 与原实现一致：任何异常都回 500 + 末段 trace
        import traceback
        return 500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                     "trace": traceback.format_exc()[-600:]}


def reorder_day(city_name: str, day, ordered_ids: list,
                date0: str | None = None, query: str = "",
                hotel_text: str | None = None) -> tuple:
    """按用户给定的**新顺序**重排某一天（「挪时段」）。

    `build_itinerary(order_given=True)` 会尊重给定顺序，但仍可能因硬约束
    （餐窗 / 闭馆 / 里程）微调 —— 此时 `day.reordered=True`，用 `order_respected`
    如实回报，让面板能提示「顺序已按硬约束微调」，而不是假装完全按用户排的。

    守门同换点：出现剔点或点集变化即拒绝（保留原顺序），避免用户的拖动把行程搞坏。

    返回 (code, payload)：
      (200, {"ok": True, "day": {...}, "order_respected": bool})
      (200, {"ok": False, "error": …})
      (404, {"ok": False, "error": …})   有点 id 不在库里
      (500, …)
    """
    from . import hotel as hotel_mod, sequencer
    try:
        city, all_pois = _load(city_name)
        ids = [str(i) for i in (ordered_ids or [])]
        if not ids:
            return 200, {"ok": False, "error": "新顺序为空"}

        missing = [i for i in ids if i not in all_pois]
        if missing:
            return 404, {"ok": False, "error": f"POI 不存在：{'、'.join(missing)}"}

        hotel = hotel_mod.resolve_hotel(city, hotel_text or None)
        trial = sequencer.build_itinerary({int(day): ids}, city, all_pois,
                                          order_given=True, date0=date0 or None,
                                          hotel=hotel, query=query or "")
        d0 = trial["days"][0]
        if _too_bad(d0, ids):
            return 200, {"ok": False,
                         "error": "这个顺序会让当天行程丢点或被约束改得更多，已保留原顺序"}

        return 200, {"ok": True, "day": d0,
                     "order_respected": _poi_ids_of_day(d0) == ids}
    except Exception as e:  # noqa: BLE001
        import traceback
        return 500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                     "trace": traceback.format_exc()[-600:]}
