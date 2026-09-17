# -*- coding: utf-8 -*-
"""2026-09-15 报障 18：多城联游时住宿锚点在前端完全不可见。

现象：query「苏杭3天联游，喜欢园林和美食，住西湖国宾馆附近」——杭州段明明以酒店为
起点（首行是无源 hop、末尾「返回酒店」），前端却**看不到任何酒店 POI**。

两个独立缺陷叠加：
  A. `webui/server.py:plan_multi` 的返回字典**没有 hotel 键**（单城 `plan()` 有）→
     前端 `r.hotel` 取不到 → 顶部 🏨 徽标、高德/SVG 地图酒店标记、导出文档与 ICS 的
     「住宿：…」全部丢失。
  B. `sequencer._build_timeline` 只在末尾补「返回酒店」（通用名），**没有出发行**——
     「起点是酒店」只体现为一个无源 hop，酒店名全程不出现。

修复：A 透出 hotel；B 首部插入 type=hotel 的出发行（用酒店名）+ 返回行带名，并剥掉
resolve_hotel 附加的「（住宿锚点）」括注。前端对 0 时长行只显示单个时刻。

用法：python scripts/test_multi_hotel.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import poi_db, sequencer, m1_planner  # noqa: E402
from webui import server as sv  # noqa: E402

# 编码须在 import 之后设置：webui/server.py 在导入期也会调整 stdout，
# 若先自建 TextIOWrapper 再导入，前一个 wrapper 被 GC 会关闭底层 buffer
# （→「I/O operation on closed file」）。reconfigure 原地改编码，无此问题。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


CITY = poi_db.load_city("杭州")   # 借 day_start/day_end/meal_slots 口径（酒店在杭州）


def _poi(pid, name, cat, o, c, dur, lat=30.2500, lng=120.1450, **kw):
    d = {"id": pid, "name": name, "category": cat, "lat": lat, "lng": lng,
         "open": o, "close": c, "open_h": poi_db.hhmm_to_h(o), "close_h": poi_db.hhmm_to_h(c),
         "dur": dur, "open_h_raw": o, "closed_days": [], "tags": [], "best_time": None,
         "rating": 4.5}
    d.update(kw)
    return d


HOTEL = {"id": "HOTEL", "name": "西湖国宾馆", "category": "hotel", "tags": [],
         "lat": 30.2350, "lng": 120.1337, "duration_h": 0, "open": "00:00", "close": "23:59",
         "best_time": "any", "price": 0, "rating": 0, "area": "", "family_ok": True,
         "note": "住宿锚点", "closed_days": []}

# ==================== 用例 1：时间轴显式含「出发行」与「返回行」 ====================
print("== 用例 1：_build_timeline 酒店行的可见性")
# 夹具须与酒店同片区（酒店 30.2350,120.1337 = 西湖）：否则「返程腿」变成跨城长途，
# 一天被撑爆 → 违规与超长收尾全是夹具失真，不是被测行为（首版即栽在此）。
POIS = [
    _poi("T1", "断桥残雪", "culture", "07:30", "17:30", 1.0, lat=30.2580, lng=120.1480),
    _poi("T2", "平湖秋月", "culture", "00:00", "23:59", 0.5, lat=30.2520, lng=120.1430),
    _poi("T3", "曲院风荷", "nature", "00:00", "23:59", 1.0, lat=30.2460, lng=120.1360),
]
tlr = sequencer._build_timeline(POIS, CITY, 1, hotel=HOTEL)
tl = tlr["timeline"]
hotel_rows = [e for e in tl if e.get("type") == "hotel"]
case("1a 含且仅含 2 行酒店（出发 + 返回）", len(hotel_rows) == 2,
     str([(e["name"], e["start"], e["end"]) for e in hotel_rows]))
case("1b 首行即出发行（type=hotel）", tl and tl[0].get("type") == "hotel",
     f"首行={tl[0].get('type') if tl else None}")
case("1c 出发行在 day_start 且 0 时长（前端据此只显示单时刻）",
     bool(tl) and tl[0]["start"] == CITY["day_start"] and tl[0]["start"] == tl[0]["end"],
     f"{tl[0]['start']}–{tl[0]['end']}" if tl else "")
case("1d 出发行展示酒店名（不是通用『酒店』）",
     bool(tl) and "西湖国宾馆" in tl[0]["name"], tl[0]["name"] if tl else "")
case("1e 返回行带酒店名", bool(hotel_rows) and "西湖国宾馆" in hotel_rows[-1]["name"],
     hotel_rows[-1]["name"] if hotel_rows else "")
case("1f 新增行不引入违规", not tlr["violations"], str(tlr["violations"])[:80])
case("1g 新增行不制造假空档", not tlr["gaps"], str(tlr["gaps"])[:120])

# 对照：无酒店锚点 → 一行酒店都没有（老行为不被污染）
tlr_no = sequencer._build_timeline(POIS, CITY, 1)
case("1h 对照：无酒店 → 零酒店行",
     not [e for e in tlr_no["timeline"] if e.get("type") == "hotel"],
     str([e.get("type") for e in tlr_no["timeline"]]))

# ==================== 用例 2：展示名剥掉解析器附加括注 ====================
print("\n== 用例 2：括注剥离（L0/L3 解析会给名字加「（住宿锚点）」）")
for raw, want in (("西湖国宾馆（住宿锚点）", "西湖国宾馆"),
                  ("上海迪士尼度假区（住宿锚点）", "上海迪士尼度假区"),
                  ("西湖国宾馆（市中心附近）", "西湖国宾馆"),
                  ("西湖国宾馆", "西湖国宾馆")):
    h = dict(HOTEL, name=raw)
    out = sequencer._build_timeline(POIS, CITY, 1, hotel=h)["timeline"]
    dep = [e for e in out if e.get("type") == "hotel"][0]["name"]
    case(f"2 {raw} → {want}", dep == want, dep)


# ==================== 用例 3：plan_multi 透出 hotel（缺陷 A） ====================
print("\n== 用例 3：plan_multi 的 hotel 透出（多城链路）")
# 离线锚定归属：真实 _hotel_city_probe 要走高德（网络不可用于单测）
_probe = sv._hotel_city_probe
sv._hotel_city_probe = lambda cname, text: cname == "杭州"
try:
    r = sv.plan_multi(["苏州", "杭州"], "苏杭3天联游，喜欢园林和美食", 3, None,
                      use_llm=False, planner=m1_planner,
                      hotel_text="西湖国宾馆@120.1337,30.2350")
    case("3a plan_multi 返回 hotel 键（含名称与经纬度）",
         bool(r.get("hotel")) and r["hotel"].get("name") and r["hotel"].get("lat"),
         str(r.get("hotel")))
    days = r["itinerary"]["days"]
    hz = [d for d in days if (d.get("theme") or "").startswith("杭州")]
    sz = [d for d in days if (d.get("theme") or "").startswith("苏州")]
    case("3b 确有杭州段", bool(hz), f"days={[d.get('theme') for d in days]}")
    case("3c 杭州段每天都含出发行+返回行",
         bool(hz) and all(sum(1 for e in d["timeline"] if e.get("type") == "hotel") == 2
                          for d in hz),
         str([sum(1 for e in d["timeline"] if e.get("type") == "hotel") for d in hz]))
    case("3d 非住宿城（苏州）段不注入酒店行",
         all(not [e for e in d["timeline"] if e.get("type") == "hotel"] for d in sz),
         f"苏州天数={len(sz)}")

    # 对照：不传 hotel_text 时恒为 None（不误报住宿）
    r0 = sv.plan_multi(["苏州", "杭州"], "苏杭3天联游", 3, None,
                       use_llm=False, planner=m1_planner, hotel_text=None)
    case("3e 对照：无住宿文本 → hotel=None", r0.get("hotel") is None, str(r0.get("hotel")))
finally:
    sv._hotel_city_probe = _probe

# ==================== 用例 4：前端源码守卫 ====================
print("\n== 用例 4：前端源码守卫（改渲染层时防回退）")
_h = open(os.path.join(ROOT, "webui", "index.html"), encoding="utf-8").read()
case("4a 徽标/地图仍读 r.hotel（依赖后端透出）", _h.count("r.hotel") >= 4, str(_h.count("r.hotel")))
case("4b dayCard 对 0 时长行只显示单时刻",
     "s.start===s.end)?s.start" in _h, "dayCard")
case("4c buildDoc / exportPNG 同步处理", _h.count("s.start===s.end)?s.start") >= 3,
     f"命中 {_h.count('s.start===s.end)?s.start')} 处")
case("4d 酒店行图标已注册", 'hotel:"🏨"' in _h or "🏨" in _h, "ICON")
# 恢复 r.hotel 后，drawRealRoutes 若对「所有天」补酒店腿，多城苏州天会被画出
# 「苏州→杭州西湖→苏州」假长途 → 必须按「该天是否以酒店为锚」判定（时间轴 hotel 行为准）
case("4e 仅对以酒店为锚的天画酒店腿（time 轴 hotel 行为判据）",
     _h.count("hasHotel") >= 2 and 'some(x=>x.type==="hotel")' in _h,
     f"hasHotel x{_h.count('hasHotel')}")
case("4f ICS 导出跳过 0 时长行（防 DTSTART==DTEND 零长事件）",
     "s.start===s.end)return" in _h, "exportICS")

# ==================== 用例 5：分段 notices 上浮 + day 重映射（同族缺陷） ====================
print("\n== 用例 5：多城分段 notices 不再静默丢弃")
_lift = sv._lift_seg_notices("杭州", 1, [
    {"type": "day_gap", "day": 2, "min": 194,
     "message": "Day2 有 1 段共 3.2h 空档：14:46-18:00（自由休整）"},
    {"type": "named_lost", "dropped": ["莲花岛"], "message": "你点名的 莲花岛 未能排入"},
    {"type": "rain", "days": [1, 2], "message": "第 1、2 天有雨"},
])
case("5a day 字段按偏移重写（段内 Day2 → 全局 Day3）", _lift[0]["day"] == 3, str(_lift[0].get("day")))
case("5b 文案里的 Day 号同步重写", _lift[0]["message"].startswith("Day3"), _lift[0]["message"])
case("5c 每条 notice 带城市标签", all(x.get("city") == "杭州" for x in _lift),
     str([x.get("city") for x in _lift]))
case("5d 无 day 字段的 notice 不受影响（且不凭空加 day）",
     _lift[1]["type"] == "named_lost" and "day" not in _lift[1], str(sorted(_lift[1].keys())))
case("5e 列表型 days 逐项重映射", _lift[2]["days"] == [2, 3], str(_lift[2]["days"]))
case("5f 偏移 0 时文案保持原样",
     sv._lift_seg_notices("苏州", 0, [{"type": "day_gap", "day": 1,
                                       "message": "Day1 x"}])[0]["message"] == "Day1 x")


class _StubPlanner:
    """桩 planner：精确控制各段返回，验证 plan_multi 的 notices 上浮与 day 重映射。"""

    def plan(self, city, query, di, use_llm=True, date0=None, hotel_text=None):
        cname = city.get("city") or "?"
        return {"itinerary": {"days": [{"day": k + 1, "timeline": [], "theme": cname}
                                      for k in range(di)],
                              "total_violations": 0, "total_travel_km": 0.0},
                "notices": [{"type": "day_gap", "day": 1, "min": 90,
                             "message": f"Day1 来自{cname}的空档"}],
                "grounding": {"gaps": [{"day": 1, "min": 90, "start": "10:00",
                                        "end": "11:30", "reason": "free"}]}}


_probe2 = sv._hotel_city_probe
sv._hotel_city_probe = lambda cname, text: False   # 本用例不涉住宿，避免网络
try:
    rs = sv.plan_multi(["苏州", "杭州"], "苏杭3天联游", 3, None, use_llm=False,
                       planner=_StubPlanner())
    _ns = rs.get("notices") or []
    # 断言「两段的段内 notice 都被透出」而不是 notices 总数——2026-09-17 起
    # plan_multi 还会追加 kind=intercity_transfer 的城际转移提示，按总数断言会误报。
    _seg = [x for x in _ns if x.get("type") == "day_gap"]
    case("5g plan_multi 透出两段的段内 notices（此前整块缺失）", len(_seg) == 2,
         str([x.get("message") for x in _ns]))
    case("5h 后段（杭州）的段内 Day1 被重写为 Day3",
         any((x.get("message") or "").startswith("Day3 来自杭州") for x in _ns),
         str([x.get("message") for x in _ns]))
    case("5i grounding.gaps 的 day 也已重映射为全局号",
         sorted(g["day"] for g in rs["grounding"]["gaps"]) == [1, 3],
         str([g["day"] for g in rs["grounding"]["gaps"]]))
    case("5j 各段 theme 带城市前缀",
         all(any((d.get("theme") or "").startswith(c) for c in ("苏州", "杭州"))
             for d in rs["itinerary"]["days"]),
         str([d.get("theme") for d in rs["itinerary"]["days"]]))
finally:
    sv._hotel_city_probe = _probe2

print("\n" + ("全部用例通过" if not FAIL else f"失败用例：{FAIL}"))
sys.exit(1 if FAIL else 0)
