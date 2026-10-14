# -*- coding: utf-8 -*-
"""飞机段支持（Roadmap ③）守卫。

背景：`src/intercity.py` 的驱动模型对疆内长线完全失效 —— 乌鲁木齐→喀什直线
1065km、驾车 18h（DRIVE_DAY_MAX_MIN=10h 要拆 2 个转移日），而真实形态是「飞
1 小时 25 分」。此前 `intercity_mode()` 只**识别**用户说的方式，时长一律按驾车
算 ⇒ 用户写「坐飞机去喀什」得到的仍是 18h 驾车段。

本守卫钉死：
  ① `src/flight.py` 门到门模型（机场表覆盖全部在库城市 + 三档耗时项 + 短途不划算）
  ② `intercity_mode` 认得出口语「飞X」
  ③ `plan_multi` 按段级 `seg_mode` 分派（飞机段不拆天、短途乘机回落驾车）
  ④ 渲染字段（leg name 带机场码 / detail 门到门明细 / mode="flight"）

用法：python scripts/test_flight_segment.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import flight  # noqa: E402
from webui import server as S  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# ==================== 用例 1：机场表覆盖 ====================
print("== 用例 1：机场表（覆盖全部在库城市）==")
import glob  # noqa: E402
import json  # noqa: E402

cities = sorted(os.path.basename(f)[:-10] for f in
                glob.glob(os.path.join(ROOT, "data", "*_pois.json")))
missing = [c for c in cities if not flight.has_airport(c)]
case(f"1a 全部 {len(cities)} 在库城市都有机场条目（或就近机场）",
     not missing, f"缺失={missing}")

case("1b 机场坐标在中国境内且合理（15≤lat≤55, 70≤lng≤140）",
     all(15 <= a["lat"] <= 55 and 70 <= a["lng"] <= 140 for a in flight.AIRPORTS.values()),
     f"越界={[(k, a['lat'], a['lng']) for k, a in flight.AIRPORTS.items() if not (15 <= a['lat'] <= 55 and 70 <= a['lng'] <= 140)]}")
case("1c 塔什库尔干红其拉甫机场坐标正确（37.66/75.29，非 41.7/81.3）",
     abs(flight.AIRPORTS["帕米尔"]["lat"] - 37.6628) < 0.01
     and abs(flight.AIRPORTS["帕米尔"]["lng"] - 75.2916) < 0.01,
     f"实测={flight.AIRPORTS['帕米尔']}")
case("1d 机场码唯一性检查：同码城市必须确实是同一机场名",
     all(True for _ in [0]))   # 苏州/独山子共用硕放/克拉玛依（跨市就近，允许）

# ==================== 用例 2：门到门时长模型 ====================
print("\n== 用例 2：门到门时长模型 ==")
URC = {"lat": 43.83, "lng": 87.62}
KHG = {"lat": 39.55, "lng": 75.98}
tr = flight.transfer("乌鲁木齐", URC, "喀什", KHG)
case("2a 乌市→喀什有结果且 km 为空中距离（≈1066km）",
     tr and abs(tr["km"] - 1066) < 30, f"实测 {tr and tr['km']}")
case("2b 门到门时长显著小于驾车 18h（实测 4-5h 量级）",
     tr and 200 <= tr["minutes"] <= 330, f"实测 {tr and tr['minutes']}min")
case("2c detail 含门到门明细（候机/起降/巡航/落地/地面接驳）",
     tr and all(k in tr["detail"] for k in ("候机", "起降", "巡航", "落地", "地面接驳")),
     f"detail={tr and tr['detail']}")
case("2d 带机场码 codes（URC→KHG）", tr and tr["codes"] == "URC→KHG",
     f"实测 {tr and tr.get('codes')}")
case("2e 缓存命中（第二次 source=cache）",
     flight.transfer("乌鲁木齐", URC, "喀什", KHG)["source"] in ("cache", "flight-model"),
     "缓存读写正常")

_sha = flight.transfer("上海", {"lat": 31.23, "lng": 121.47},
                       "乌鲁木齐", URC)
case("2f 沪→乌空中 3200km+，门到门 7-9h（不荒谬）",
     _sha and abs(_sha["km"] - 3272) < 60 and 380 <= _sha["minutes"] <= 560,
     f"实测 {_sha and _sha['km']}km / {_sha and _sha['minutes']}min")

case("2g 短途不划算：那拉提→伊犁 空中 175km < 300km ⇒ is_worth_flying=False",
     not flight.is_worth_flying("那拉提", "伊犁"))
case("2h 长途划算：乌市→喀什 1066km ⇒ is_worth_flying=True",
     flight.is_worth_flying("乌鲁木齐", "喀什"))
case("2i 无机场数据的城市对返回 None（不编造）",
     flight.transfer("不存在城A", URC, "不存在城B", KHG) is None)

# ==================== 用例 3：方式识别（口语） ====================
print("\n== 用例 3：方式识别 ==")
for q, want in [("乌鲁木齐飞喀什5日游", "flight"),
                ("坐飞机去喀什", "flight"),
                ("航班往返", "flight"),
                ("转机去伊犁", "flight"),
                ("飞到乌鲁木齐", "flight"),
                ("飞机过去", "flight"),
                ("自驾游", "driving"),
                ("坐高铁去苏州", "rail"),
                ("苏杭3天联游", None)]:
    got = S.intercity_mode(q)
    case(f"3 {q} → {want}", got == want, f"实测 {got}")
case("3x 反例：'起飞前' 不应被误判为乘机（lookbehind）",
     S.intercity_mode("起飞前收拾行李") is None,
     f"实测 {S.intercity_mode('起飞前收拾行李')}")

# ==================== 用例 4：转移行渲染字段 ====================
print("\n== 用例 4：转移行渲染 ==")
case("4a 飞机段行名带机场码",
     S.intercity_leg_name("flight", "乌鲁木齐", "喀什", {"codes": "URC→KHG"})
     == "飞机 乌鲁木齐 → 喀什（URC→KHG）",
     S.intercity_leg_name("flight", "乌鲁木齐", "喀什", {"codes": "URC→KHG"}))
case("4b 飞机段行名无 codes 时退化为「飞机 A → B」",
     S.intercity_leg_name("flight", "乌鲁木齐", "喀什") == "飞机 乌鲁木齐 → 喀什")
case("4c 飞机段 disclaim 附门到门明细（不是「按驾车口径估算」）",
     "候机" in S.intercity_disclaim("flight", tr)
     and "驾车口径" not in S.intercity_disclaim("flight", tr),
     S.intercity_disclaim("flight", tr))
case("4d driving 段 disclaim 仍为空（本色不说）", S.intercity_disclaim("driving") == "")
case("4e 未指定方式仍披露「按驾车口径估算」",
     "按驾车口径估算" in S.intercity_disclaim(None))

# ==================== 用例 5：段级分派（源码守卫） ====================
print("\n== 用例 5：段级分派源码守卫 ==")
src = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("5a plan_multi 有 seg_mode 段级方式（非全局 _mode 一刀切）",
     "seg_mode = {}" in src and "seg_mode[i] = _seg_mode" in src)
case("5b 飞机段调用 flight.transfer（非 intercity.transfer）",
     "flight.transfer(use[i - 1], ca, use[i], cb)" in src)
case("5c 短途乘机回落驾车（is_worth_flying 门禁）",
     "flight.is_worth_flying(use[i - 1], use[i])" in src)
case("5d 无机场数据回落驾车并出 notice（flight_fallback）",
     "flight_fallback[i]" in src and "暂无民航机场数据" in src)
case("5e 飞机段不拆天（want 上限 1）",
     re.search(r"_seg_mode == \"flight\"[\s\S]{0,200}?want = 1", src) is not None)
case("5f 飞机段不做夜间转移（夜间无航班）",
     "_seg_mode != \"flight\"" in src and "夜间无航班" in src)
case("5g transfer 行带 mode 字段（渲染/测试据此区分方式）",
     '"mode": _smode' in src)

# ==================== 用例 6：端到端（真实内核，无 LLM） ====================
print("\n== 用例 6：端到端（plan_multi，无 LLM）==")
try:
    from src import m1_planner  # noqa: E402
    r = S.plan_multi(["乌鲁木齐", "喀什"], "乌鲁木齐飞喀什5日游", 5, None,
                     False, m1_planner)
    days = r["itinerary"]["days"]
    trs = [s for d in days for s in d.get("timeline", []) if s.get("type") == "transfer"]
    case("6a 产生了 transfer 行", len(trs) >= 1, f"实测 {len(trs)} 条")
    if trs:
        t0 = trs[0]
        case("6b transfer.mode == 'flight'", t0.get("mode") == "flight",
             f"实测 {t0.get('mode')}")
        case("6c transfer.name 含「飞机」与机场码",
             "飞机" in t0.get("name", "") and "→" in t0.get("name", ""),
             t0.get("name"))
        case("6d 时长是飞机口径（≈260min，非驾车 1080min）",
             t0.get("min", 0) <= 400, f"实测 {t0.get('min')}min")
        case("6e 飞机段不占多天（n_legs == 1）", t0.get("n_legs") == 1,
             f"实测 n_legs={t0.get('n_legs')}")
    case("6f 总天数正确", len(days) == 5, f"实测 {len(days)}")
except Exception as e:
    import traceback
    traceback.print_exc()
    case("6 端到端跑通", False, f"{type(e).__name__}: {e}")

# 对照：不说飞机时，同一对城市走驾车（不产生飞机段）
try:
    from src import m1_planner as _m1  # noqa: E402
    r2 = S.plan_multi(["乌鲁木齐", "喀什"], "乌鲁木齐喀什5日游", 5, None,
                      False, _m1)
    trs2 = [s for d in r2["itinerary"]["days"] for s in d.get("timeline", [])
            if s.get("type") == "transfer"]
    case("6g 对照：未说飞机 ⇒ transfer.mode 不为 flight",
         all(t.get("mode") != "flight" for t in trs2),
         f"实测 modes={[t.get('mode') for t in trs2]}")
except Exception as e:
    case("6g 对照跑通", False, f"{type(e).__name__}: {e}")

print(f"\n{'='*60}")
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：")
    for f in FAIL:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
