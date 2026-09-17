# -*- coding: utf-8 -*-
"""城际转移段 + driving 主题画像 的守卫。

背景（2026-09-17）：跨城联游此前把「从 A 城开到 B 城」当作不存在——
`plan_multi` 只按天均分逐城规划再拼接，城际驾驶段既不占时间也不出现在时间轴。
实测「赤水 → 兴义」真实车程 6.7h/580km，而系统给出的是两城无缝衔接。

两处根因与修复：
  A. 复用 `poi_db.travel_hours` 算城际段会得出荒谬值——它的兜底是**市内**速度模型
     （km × 1.4 ÷ 18km/h），苏州→杭州直线 130km 会算成 ≈10 小时。
     修复：新增 `src/intercity.py` 三级模型（缓存 / 高德驾车 / 高速直线兜底）。
  B. 长途段必须**先扣天再规划**：抵达城拿到的天数已扣掉转移日，该城才会按更少的
     天数排点；事后插入转移日会让景点总量超出真实可用时间。
     修复：`plan_multi` 对 ≥240min 的段预扣 1 天并插入 type=transfer 的整天。

driving 主题：`THEME_PROFILES` 新增 ("driving", …, 400km/天)。400km 对同城恒不触发
（市内单日 30~80km），其作用域是「含城际转移段的天」。

用法：python scripts/test_intercity_transfer.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import poi_db, sequencer, intercity, m1_planner, m2_planner  # noqa: E402
from webui import server as sv  # noqa: E402

# 编码须在 import 之后（server.py 导入期也会调整 stdout；先自建 wrapper 会被 GC 关闭 buffer）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# ==================== 用例 1：driving 主题画像 ====================
print("== 用例 1：driving 主题画像")
case("1a 自驾命中 driving 主题",
     sequencer.detect_theme("自驾贵州 5 天") == "driving",
     f"detect_theme={sequencer.detect_theme('自驾贵州 5 天')}")
case("1b 租车/驾车同样命中",
     sequencer.detect_theme("租车环黔东南") == "driving"
     and sequencer.detect_theme("driving through Guizhou") == "driving")
case("1c 每日里程预算 400km",
     sequencer.theme_km_cap("自驾贵州 5 天") == 400.0,
     f"cap={sequencer.theme_km_cap('自驾贵州 5 天')}")
case("1d 骑行/徒步优先于自驾（更严格者胜）",
     sequencer.detect_theme("骑行自驾都行") == "cycling"
     and sequencer.theme_km_cap("骑行自驾都行") == 15.0)
case("1e travel_mode 透出 driving",
     sequencer.travel_mode("自驾贵州") == "driving")
case("1f hop 标签写「自驾」而非「车程」",
     "自驾" in sequencer._hop_label(30.0, 0.6, "driving")
     and "车程" in sequencer._hop_label(30.0, 0.6, None))
case("1g 短腿仍是步行",
     "步行" in sequencer._hop_label(0.8, 0.1, "driving"))
case("1h 单日主题「其中一天自驾」被识别为单日范围",
     sequencer.theme_scoped("苏州3天，其中一天自驾去周边古镇") is True)
case("1i 回归对照：无主题查询不受影响",
     sequencer.detect_theme("上海2天经典深度游，喜欢历史文化、寺庙和博物馆") is None
     and sequencer.theme_km_cap("上海2天经典深度游") is None)

# driving 不得改变同城通行时长（travel_cache 存的就是驾车时长）
_c = poi_db.load_city("苏州")
_all = {p["id"]: p for p in (poi_db.parse_poi(p, _c) for p in _c["pois"])}
_ids = sorted(_all)[:6]
_same = all(
    abs(poi_db.travel_hours(_all[a], _all[b], "driving")
        - poi_db.travel_hours(_all[a], _all[b], None)) < 1e-12
    for a in _ids for b in _ids if a != b)
case("1j driving 与默认口径的同城时长完全一致（无时长回归）", _same)

# 剔除原因标签不再错写成「骑行」
import inspect  # noqa: E402
_src_cap = inspect.getsource(sequencer._cap_km_repair)
case("1k _cap_km_repair 标签表登记了 driving",
     '"driving": "自驾"' in _src_cap or "'driving': '自驾'" in _src_cap)

# ==================== 用例 2：城际时长模型 ====================
print("\n== 用例 2：城际时长模型（src/intercity.py）")
_sz, _hz = poi_db.load_city("苏州")["center"], poi_db.load_city("杭州")["center"]
case("2a 同城转移为 0",
     intercity.transfer("苏州", _sz, "苏州", _sz)["minutes"] == 0.0)

_t = intercity.transfer("苏州", _sz, "杭州", _hz, amap_key=None)
case("2b L3 兜底给出合理车程（60~180min）",
     60 <= _t["minutes"] <= 180,
     f"{_t['minutes']:.0f}min / {_t['km']:.0f}km / {_t['source']}")

# 复现旧缺陷：复用市内速度模型会荒谬
_fake_a = {"id": "X1", "lat": _sz["lat"], "lng": _sz["lng"]}
_fake_b = {"id": "X2", "lat": _hz["lat"], "lng": _hz["lng"]}
_old_h = poi_db.travel_hours(_fake_a, _fake_b, None)
case("2c 对照：复用 poi_db.travel_hours 会夸大到 >5h（故必须独立模型）",
     _old_h > 5.0 and _t["minutes"] / 60.0 < _old_h / 2,
     f"travel_hours={_old_h:.1f}h vs intercity={_t['minutes'] / 60:.1f}h")

case("2d 长途阈值边界 240min",
     intercity.is_long_transfer(239) is False
     and intercity.is_long_transfer(240) is True)
case("2e 直线兜底不写缓存（避免把估算值钉死）",
     intercity._key("苏州", "杭州") not in (intercity._load_cache() or {})
     or (intercity._load_cache()[intercity._key("苏州", "杭州")].get("source")
         == "amap-driving"))
case("2f 缓存键与方向无关（A|B 与 B|A 同键）",
     intercity._key("苏州", "杭州") == intercity._key("杭州", "苏州"))
case("2g duration_text 人读",
     intercity.duration_text(90) == "1 小时 30 分钟"
     and intercity.duration_text(45) == "45 分钟"
     and intercity.duration_text(120) == "2 小时")


# ==================== 用例 3：plan_multi 长途转移日 ====================
print("\n== 用例 3：plan_multi 长途转移日（≥240min 预扣 1 天）")


class _StubPlanner:
    """记录每段拿到的天数，并产出最简可用 itinerary。"""

    def __init__(self):
        self.calls = []

    def plan(self, city, query, di, use_llm=True, date0=None, hotel_text=None):
        self.calls.append((city["city"], di))
        days = []
        for k in range(1, di + 1):
            # 桩必须尊重 day_start_by_day（真实 sequencer/toptw 经 day_start_of 读它）：
            # 否则桩恒返回 10:00，「抵达日顺延」这条守卫会变成假通过
            _ds = sequencer.day_start_of(city, k)
            _sh = poi_db.hhmm_to_h(_ds)
            _st = f"{int(max(_sh, 10.0)):02d}:{int(round((max(_sh, 10.0) % 1) * 60)):02d}"
            days.append({
                "day": k, "theme": f"{city['city']}第{k}天",
                "timeline": [
                    {"type": "poi", "id": f"P{k}", "name": f"{city['city']}景点{k}",
                     "start": _st, "end": "13:00", "arrive": _st},
                    {"type": "meal", "name": "午餐", "start": "13:00", "end": "14:00"},
                ],
                "travel_km": 5.0, "travel_h": 0.4, "violations": [], "repairs": [],
                "finish": "13:00", "gaps": [], "weekday": "周一",
            })
        return {"mode": "stub", "days": di, "hotel": None, "notices": [],
                "itinerary": {"days": days, "total_violations": 0,
                              "total_travel_km": 5.0 * di, "dropped_pois": []},
                "grounding": {"n_proposed": 2 * di, "unmatched": 0,
                              "grounding_rate": 1.0, "gaps": []},
                "n_dup_across_days": 0, "candidates": 10, "latency_s": 0.1}


_orig_transfer = intercity.transfer
_orig_probe = sv._hotel_city_probe
try:
    sv._hotel_city_probe = lambda cname, text: False
    # 固定为长途（360min / 500km），与网络无关
    intercity.transfer = lambda a, ca, b, cb, amap_key=None: (
        {"minutes": 0.0, "km": 0.0, "source": "same-city"} if a == b else
        {"minutes": 360.0, "km": 500.0, "source": "stub-long"})
    st = _StubPlanner()
    r = sv.plan_multi(["苏州", "杭州"], "苏杭4天自驾联游", 4, "2026-10-01",
                      use_llm=False, planner=st)
    days = r["itinerary"]["days"]
    tds = [d for d in days if d.get("transfer_day")]
    case("3a 总天数守恒（4 天）", len(days) == 4, f"实际 {len(days)} 天")
    case("3b 插入了 1 个转移日", len(tds) == 1, f"transfer_day 数 ={len(tds)}")
    case("3c 抵达城少排 1 天景点（2/2 → 2/1）",
         st.calls == [("苏州", 2), ("杭州", 1)], f"各段天数={st.calls}")
    if tds:
        td = tds[0]
        tl = td["timeline"]
        case("3d 转移日只有一条 transfer 行、无 poi",
             len(tl) == 1 and tl[0]["type"] == "transfer"
             and not [s for s in tl if s.get("type") == "poi"],
             f"timeline={[s['type'] for s in tl]}")
        case("3e transfer 行带 min/km/from/to",
             tl[0].get("min") == 360 and tl[0].get("km") == 500.0
             and tl[0].get("from") == "苏州" and tl[0].get("to") == "杭州")
        case("3f 转移日 day 号连续且无违规",
             isinstance(td["day"], int) and td["violations"] == [])
        case("3g 转移日 theme 标明城际",
             "城际转移" in td["theme"], td["theme"])
    case("3h Day 号全局连续 1..4",
         [d["day"] for d in days] == [1, 2, 3, 4], f"{[d['day'] for d in days]}")
    case("3i 城际里程计入 total_travel_km",
         r["itinerary"]["total_travel_km"] >= 500.0,
         f"total={r['itinerary']['total_travel_km']}")
    _n = [m for m in (r.get("notices") or []) if m.get("kind") == "intercity_transfer"]
    case("3j 有城际转移 notice 且 Day 号为全局号",
         len(_n) == 1 and _n[0].get("day") == tds[0]["day"] if tds else False,
         str(_n))

    # ---- 短途：不占天，只给出发时刻 ----
    print("\n== 用例 4：plan_multi 短途转移（<240min 不占天）")
    intercity.transfer = lambda a, ca, b, cb, amap_key=None: (
        {"minutes": 0.0, "km": 0.0, "source": "same-city"} if a == b else
        {"minutes": 120.0, "km": 150.0, "source": "stub-short"})
    st2 = _StubPlanner()
    r2 = sv.plan_multi(["苏州", "杭州"], "苏杭4天联游", 4, "2026-10-01",
                       use_llm=False, planner=st2)
    days2 = r2["itinerary"]["days"]
    case("4a 短途不插转移日",
         not [d for d in days2 if d.get("transfer_day")] and len(days2) == 4)
    case("4b 两城天数不变（2/2）",
         st2.calls == [("苏州", 2), ("杭州", 2)], f"{st2.calls}")
    _n2 = [m for m in (r2.get("notices") or []) if m.get("kind") == "intercity_transfer"]
    case("4c 有短途 notice", len(_n2) == 1, str(_n2))
    if _n2:
        case("4d notice 给出出发时刻与车程",
             "出发" in _n2[0]["message"] and "2 小时" in _n2[0]["message"],
             _n2[0]["message"])
        case("4e notice 的 Day 号指向抵达城首日（Day3）",
             _n2[0].get("day") == 3, f"day={_n2[0].get('day')}")

    # ---- 短途转移必须「看得见」：时间轴行 + 抵达日顺延 ----
    # 2026-09-17 报障：「杭州苏州3日自驾游」没有转移日、地图也没有转移路径。
    # 首版设计只给一条 notice（「需 06:57 前出发」），车程完全不进时间轴，
    # 抵达日仍按整天塞 6 个点 —— 用户在行程上看不到跨城这件事。
    print("\n== 用例 7：短途转移在时间轴上可见且抵达日顺延")
    _first_day = next((d for d in days2 if d.get("arrive_transfer")), None)
    case("7a 抵达日被标记 arrive_transfer", _first_day is not None)
    if _first_day:
        _tl = _first_day["timeline"]
        case("7b 时间轴首行就是 transfer 行",
             _tl and _tl[0]["type"] == "transfer", f"首行={_tl[0]['type']}")
        case("7c transfer 行带出发/抵达时刻与里程",
             _tl[0]["start"] == "09:00" and _tl[0]["end"] == "11:00"
             and _tl[0]["km"] == 150.0,
             f"{_tl[0]['start']}→{_tl[0]['end']} {_tl[0]['km']}km")
        _fp = next((s for s in _tl if s.get("type") == "poi"), None)
        case("7d 抵达日景点从抵达后开始（不再 09:00 起排）",
             _fp and _fp["start"] >= _tl[0]["end"],
             f"首个景点 {_fp['start'] if _fp else '-'} vs 抵达 {_tl[0]['end']}")
        case("7e 城际里程计入该天 travel_km",
             float(_first_day["travel_km"]) >= 150.0, f"{_first_day['travel_km']}")
    _n2b = [m for m in (r2.get("notices") or []) if m.get("kind") == "intercity_transfer"]
    case("7f notice 改为「出发/抵达」口径，不再让用户自己倒推出发时刻",
         _n2b and "抵达" in _n2b[0]["message"] and "前出发" not in _n2b[0]["message"],
         _n2b[0]["message"] if _n2b else "")

    # ---- 超长车程：一天开不完，须拆多日 + 时刻不得溢出 24h ----
    # 复现 2026-09-17 实测缺陷：上海→成都 1242min，首版实现产出
    # 「09:00-29:42」的非法时刻，且把 20.7h 车程当成一天开完。
    print("\n== 用例 6：超长车程（>10h/天 开不完）")
    case("6a days_needed 按单日 10h 上限向上取整",
         intercity.days_needed(1242) == 3 and intercity.days_needed(360) == 1
         and intercity.days_needed(120) == 0,
         f"1242→{intercity.days_needed(1242)} 360→{intercity.days_needed(360)} "
         f"120→{intercity.days_needed(120)}")
    case("6b 超 12h 触发改乘公共交通提示",
         intercity.needs_rail_advice(1242) is True
         and intercity.needs_rail_advice(360) is False)

    intercity.transfer = lambda a, ca, b, cb, amap_key=None: (
        {"minutes": 0.0, "km": 0.0, "source": "same-city"} if a == b else
        {"minutes": 1242.0, "km": 1936.0, "source": "stub-verylong"})
    st3 = _StubPlanner()
    r3 = sv.plan_multi(["上海", "成都"], "上海成都5天自驾", 5, "2026-10-01",
                       use_llm=False, planner=st3)
    days3 = r3["itinerary"]["days"]
    tds3 = [d for d in days3 if d.get("transfer_day")]
    # 5 天 2 城 → alloc=[3,2]；抵达段只有 2 天，最多让出 1 天（须给成都留 1 天游玩）
    case("6c 转移日占用受 alloc 约束（5天2城只能挤出 1 天）",
         len(tds3) == 1 and st3.calls == [("上海", 3), ("成都", 1)],
         f"转移日 {len(tds3)} 天，各段={st3.calls}")
    case("6d 抵达城仍保留至少 1 天景点",
         st3.calls and st3.calls[-1][1] >= 1, f"{st3.calls}")
    _bad = [s for d in tds3 for s in d["timeline"]
            if int((s.get("end") or "00:00").split(":")[0]) > 23]
    case("6e 转移行时刻钳在 24h 内（不再出现 29:42）", not _bad,
         str([(d["day"], d["timeline"][0]["start"], d["timeline"][0]["end"])
              for d in tds3]))
    case("6f 总天数仍守恒（5 天）", len(days3) == 5, f"{len(days3)}")
    _n3 = [m for m in (r3.get("notices") or []) if m.get("kind") == "intercity_transfer"]
    if _n3:
        case("6g notice 说清「实际需要 N 天、只挤出 M 天」",
             "实际需要 3 天" in _n3[0]["message"], _n3[0]["message"])
        case("6h notice 给出改乘高铁/飞机建议",
             "高铁" in _n3[0]["message"])
        case("6i notice 的 days 覆盖全部转移日",
             _n3[0].get("days") == [d["day"] for d in tds3],
             f"{_n3[0].get('days')} vs {[d['day'] for d in tds3]}")

    # 天数充足时应真的拆成多天：8 天 2 城 → alloc=[4,4]，抵达段可让出 3 天
    st4 = _StubPlanner()
    r4 = sv.plan_multi(["上海", "成都"], "上海成都8天自驾", 8, "2026-10-01",
                       use_llm=False, planner=st4)
    tds4 = [d for d in r4["itinerary"]["days"] if d.get("transfer_day")]
    case("6j 天数充足时按需拆成 3 个转移日",
         len(tds4) == 3 and st4.calls == [("上海", 4), ("成都", 1)],
         f"转移日 {len(tds4)} 天，各段={st4.calls}")
    case("6k 多段转移行标注第 N/M 段且里程均分回总数",
         all(s["n_legs"] == 3 for d in tds4 for s in d["timeline"])
         and abs(sum(s["km"] for d in tds4 for s in d["timeline"]) - 1936.0) < 1.0,
         str([(d["day"], d["timeline"][0]["km"]) for d in tds4]))
    case("6l 多段转移日总天数仍守恒（8 天）",
         len(r4["itinerary"]["days"]) == 8, f"{len(r4['itinerary']['days'])}")
finally:
    intercity.transfer = _orig_transfer
    sv._hotel_city_probe = _orig_probe

# ==================== 用例 5：契约登记 ====================
print("\n== 用例 5：transfer 行的下游契约")
_tl = [
    {"type": "transfer", "name": "自驾 苏州 → 杭州", "start": "09:00", "end": "15:00",
     "min": 360, "km": 500.0},
    {"type": "poi", "id": "P1", "name": "景点", "start": "15:30", "end": "17:00"},
]
_gaps = sequencer.scan_gaps(_tl, {})
case("5a scan_gaps 把 transfer 当「在路上」，不报成空档",
     _gaps == [], f"gaps={_gaps}")

_html = open(os.path.join(ROOT, "webui", "index.html"), encoding="utf-8").read()
case("5b 前端 ICON 注册了 transfer", "transfer:" in _html and "🚗" in _html)

# 地图必须能画城际腿（报障：地图上没有转移路径）
_srv = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("5c /api/route 支持 driving（此前只有 walking/riding，城际腿画不出来）",
     '"walking", "riding", "driving"' in _srv)
case("5d _amap_route 有 driving 分支（v3 direction/driving）",
     'mode == "driving"' in _srv and "direction/driving" in _srv)
case("5e 前端为 transfer 行画城际腿",
     'x.type==="transfer"' in _html and 'mode:"driving"' in _html)
case("5f 城际腿拿不到路网时退化为直线（不能什么都不画）",
     "intercity?[pts[i],pts[i+1]]" in _html)

# ==================== 用例 8：每城天数按用户诉求分配 ====================
# 2026-09-17 报障：「苏杭3日自驾游，第一天在杭州，晚上9点开车去苏州，然后苏州玩2天」
# 被排成「杭州2天+苏州1天」—— plan_multi 用 divmod 盲分、余数给靠前的城市，
# 完全不读用户明说的每城天数。
print("\n== 用例 8：每城天数尊重用户诉求")
_Q = "苏杭3日自驾游，第一天在杭州，晚上9点开车去苏州，然后苏州玩2天"
_w = sv.parse_city_days(_Q, ["杭州", "苏州"])
case("8a 解析出「杭州1天 / 苏州2天」", _w.get("杭州") == 1 and _w.get("苏州") == 2, str(_w))
_a = sv.allocate_city_days(["杭州", "苏州"], 3, _w)
case("8b 分配结果 = 杭州1 + 苏州2（旧实现是 2+1）", _a == [1, 2], str(_a))
case("8c 总天数守恒", sum(_a) == 3)
_w2 = sv.parse_city_days("苏杭4天联游，苏州玩3天", ["苏州", "杭州"])
case("8d 只指定一城时另一城分得剩余",
     sv.allocate_city_days(["苏州", "杭州"], 4, _w2) == [3, 1], str(_w2))
case("8e 「杭州2天苏州2天」两城都解析",
     sv.parse_city_days("杭州2天苏州2天", ["杭州", "苏州"]) == {"杭州": 2, "苏州": 2})
# 回归对照：总天数不得被误当成每城天数
case("8f 对照：「上海成都5天自驾」的 5 天是总数，不是每城天数",
     sv.parse_city_days("上海成都5天自驾", ["上海", "成都"]) == {},
     str(sv.parse_city_days("上海成都5天自驾", ["上海", "成都"])))
case("8g 对照：无每城诉求时仍按均分（保持旧行为）",
     sv.parse_city_days("苏杭3日联游", ["杭州", "苏州"]) == {}
     and sv.allocate_city_days(["杭州", "苏州"], 3, {}) == [2, 1])

# ==================== 用例 9：夜间转移 ====================
print("\n== 用例 9：夜间转移（「晚上9点开车去苏州」）")
case("9a 解析出 21:00 出发", sv._night_transfer_hour("晚上9点开车去苏州", "苏州") == 21.0)
case("9b 中文数字时刻也支持",
     sv._night_transfer_hour("晚上九点开车去苏州", "苏州") == 21.0
     and sv._night_transfer_hour("晚上十一点自驾去苏州", "苏州") == 23.0)
case("9c 白天时刻不按夜间转移处理（仍走抵达日顺延）",
     sv._night_transfer_hour("上午9点开车去苏州", "苏州") is None)
case("9d 无时间表述返回 None", sv._night_transfer_hour("苏杭3日自驾游", "苏州") is None)
case("9e 跨零点时刻回绕并标注次日",
     sv._hm(24.5, wrap=True).startswith("次日") and sv._hm(21.0, wrap=True) == "21:00",
     f"24.5→{sv._hm(24.5, wrap=True)}")

_orig_tr2 = intercity.transfer
_orig_pr2 = sv._hotel_city_probe
try:
    sv._hotel_city_probe = lambda cname, text: False
    intercity.transfer = lambda a, ca, b, cb, amap_key=None: (
        {"minutes": 0.0, "km": 0.0, "source": "same-city"} if a == b else
        {"minutes": 123.0, "km": 153.0, "source": "stub"})
    st5 = _StubPlanner()
    r5 = sv.plan_multi(["杭州", "苏州"], _Q, 3, "2026-10-01", use_llm=False, planner=st5)
    d5 = r5["itinerary"]["days"]
    case("9f 各城天数 = 杭州1 + 苏州2", st5.calls == [("杭州", 1), ("苏州", 2)],
         str(st5.calls))
    _nd = [d for d in d5 if d.get("night_transfer")]
    case("9g 夜间 transfer 行挂在出发城当天（Day1）末尾",
         len(_nd) == 1 and _nd[0]["day"] == 1, str([d["day"] for d in _nd]))
    if _nd:
        _row = [s for s in _nd[0]["timeline"] if s.get("type") == "transfer"]
        case("9h transfer 行是当天最后一行且标记 night",
             _row and _nd[0]["timeline"][-1]["type"] == "transfer"
             and _row[0].get("night") is True)
        case("9i 出发时刻 = 21:00", _row and _row[0]["start"] == "21:00",
             _row[0]["start"] if _row else "")
    case("9j 抵达日不再被顺延（夜间已到，次日整天可用）",
         not any(d.get("arrive_transfer") for d in d5))
    _n5 = [m for m in (r5.get("notices") or []) if m.get("kind") == "intercity_transfer"]
    case("9k notice 说明夜间转移与次日全天游玩",
         _n5 and "夜间" in _n5[0]["message"] and "全天" in _n5[0]["message"],
         _n5[0]["message"] if _n5 else "")

    # ---- 夜间转移日必须提前收尾：末个安排不得越过出发时刻 ----
    # 2026-09-17 实测缺陷：夜转行是当天求解完之后追加的，求解器不知道要为
    # 21:00 出发留时间，于是排出「苏堤春晓 20:00-21:30」撞上 21:00 出发。
    # 修复：出发城最后一天设 day_end_by_day（提前到出发时刻），
    # sequencer 与 toptw 共用 day_end_of() 读取。
    print("\n== 用例 10：夜间转移日提前收尾（day_end_by_day）")
    case("10a day_end_of 支持按天覆盖",
         sequencer.day_end_of({"day_end": "21:30",
                               "day_end_by_day": {2: "21:00"}}, 2) == "21:00"
         and sequencer.day_end_of({"day_end": "21:30",
                                   "day_end_by_day": {2: "21:00"}}, 1) == "21:30")
    case("10b 无覆盖时回落 city.day_end",
         sequencer.day_end_of({"day_end": "21:30"}, 1) == "21:30")
    _dep_day = next((d for d in d5 if d.get("night_transfer")), None)
    if _dep_day:
        _rows = [s for s in _dep_day["timeline"]
                 if s.get("type") in ("poi", "meal")]
        _tf = [s for s in _dep_day["timeline"] if s.get("type") == "transfer"][0]
        _late = [s for s in _rows if s.get("end", "") > _tf["start"]]
        case("10c 出发城当天没有安排越过出发时刻", not _late,
             f"出发 {_tf['start']}，越界项={[(s['name'], s['start'], s['end']) for s in _late]}")
        case("10d 求解器与排序器同口径（_day_horizon 认按天覆盖）",
             m2_planner._day_horizon({"day_start": "09:00", "day_end": "21:30",
                                      "day_end_by_day": {1: "21:00"}}, 1)
             < m2_planner._day_horizon({"day_start": "09:00", "day_end": "21:30"}, 1))
finally:
    intercity.transfer = _orig_tr2
    sv._hotel_city_probe = _orig_pr2

print(f"\n{'❌ 失败 ' + str(len(FAIL)) + ' 项：' + '；'.join(FAIL) if FAIL else '🟢 全部通过'}")
sys.exit(1 if FAIL else 0)
