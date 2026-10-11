# -*- coding: utf-8 -*-
"""多城联游（跨城）时的坐标与交互城市解析守卫。

报障（2026-10-10，「贵州5日自驾游」）：面板地图不渲染，提示
「行程数据里没有坐标（lat/lng），地图无法绘制 —— 请反馈：server 未下发坐标」。

根因：多城路径 `webui/server.py::plan_multi` 把行程的 `city` 写成 `"+".join(cities)`
（如 `贵阳+安顺+黄果树+西江千户苗寨`），而下游全都把它当**单个城名**用：
  ① `plan_view.poi_coords(city)` → `poi_db.load_city("贵阳+安顺+…")` 抛错 → 返回 `{}`
     ⇒ `normalize_plan` 一个坐标都补不上（照片不受影响：`photo_of` 查的是全局缓存，
     所以症状**只有地图**，与报障完全一致）；
  ② `plan_view.apply_day` 同源 ⇒ 换点/挪时段后那天的坐标也会消失；
  ③ `/api/replace` / `/api/move` 的 `if cname not in CITIES` 直接 400
     ⇒ 多城行程里「🔁 换一个」与拖拽**全废**（同一根因，尚未被报出来）。

修法：坐标按 `+` 拆开逐城合并（`poi_coords`）；换点/挪时段真正作用于**某一天**，
用该天 POI 的 id 前缀（`_ID_PREFIX_CITY`，与 /api/photo 同一份注册表）定位生效城市。

用法：python scripts/test_multicity_coords.py
"""
import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import intercity, m1_planner, plan_view, poi_db  # noqa: E402
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


# 用户原话里的目的地（4 城，跨黔中/黔西南/黔东南）
USE = ["贵阳", "安顺", "黄果树", "西江千户苗寨"]
QUERY = "贵州5日自驾游，喜欢自然风光和苗族文化"
CITY_FIELD = "+".join(USE)

# ==================== 用例 0：前提（判据失败先问「数据到位了吗」）====================
print("== 用例 0：前提")
_counts = {}
for c in USE:
    try:
        _counts[c] = len(poi_db.load_city(c)["pois"])
    except Exception as e:  # noqa: BLE001
        _counts[c] = f"错误:{type(e).__name__}"
case("0a 四城 POI 库都可加载且非空",
     all(isinstance(v, int) and v > 0 for v in _counts.values()), str(_counts))
case("0b 贵阳 POI id 前缀为 GY（前缀→城市 注册表的前提）",
     poi_db.load_city("贵阳")["pois"][0]["id"].startswith("GY"),
     poi_db.load_city("贵阳")["pois"][0]["id"])

# ==================== 用例 1：poi_coords 支持多城 ====================
print("\n== 用例 1：poi_coords 多城")
_single = plan_view.poi_coords("贵阳")
case("1a 单城坐标非空", len(_single) > 0, f"贵阳 {len(_single)} 点")
case("1b 城名带空格/未知城不炸（返回 dict）",
     isinstance(plan_view.poi_coords(""), dict)
     and isinstance(plan_view.poi_coords("贵阳 + 安顺"), dict))
_multi = plan_view.poi_coords("贵阳+安顺")
want_multi = dict(plan_view.poi_coords("贵阳"))
want_multi.update(plan_view.poi_coords("安顺"))
case("1c 多城＝各城合并（这才是报障的直接原因）",
     len(_multi) == len(want_multi) and all(_multi.get(k) == v for k, v in want_multi.items()),
     f"实测 {len(_multi)} 点，期望 {len(want_multi)} 点")

# ==================== 用例 2：真实多城行程归一化后每个 POI 都有坐标 ====================
print("\n== 用例 2：真实多城行程（离线 m1 链路）+ normalize_plan")
# 城际车程走缓存/高德有网络依赖 ⇒ 打桩保证确定性（口径与 test_intercity_transfer 同）
_real_transfer = intercity.transfer
intercity.transfer = lambda a, ca, b, cb, **kw: {          # noqa: ARG005
    "minutes": 150.0, "km": 150.0, "source": "stub"}
try:
    raw = sv.plan_multi(USE, QUERY, 5, None, use_llm=False, planner=m1_planner,
                        hotel_text=None)
finally:
    intercity.transfer = _real_transfer

days = (raw.get("itinerary") or {}).get("days") or []
_pois = [s for d in days for s in (d.get("timeline") or []) if s.get("type") == "poi"]
_prefixes = sorted({str(s.get("id"))[:2] for s in _pois})
case("2a 行程确为跨城（≥2 个城市前缀）", len(_prefixes) >= 2,
     f"前缀={_prefixes} 天数={len(days)} 点位数={len(_pois)}")
plan = plan_view.normalize_plan(raw, CITY_FIELD)
case("2b 归一化后 plan.city 是多城拼接串（下游都读它）",
     plan.get("city") == CITY_FIELD, str(plan.get("city")))
_items = [s for d in plan["dayList"] for s in (d.get("timeline") or [])
          if s.get("type") == "poi"]
_with = [s for s in _items if s.get("lat") is not None and s.get("lng") is not None]
case("2c 归一化后**每个** POI 都带 lat/lng（地图的前提）",
     len(_items) > 0 and len(_with) == len(_items),
     f"{len(_with)}/{len(_items)} 有坐标"
     + (f"；缺坐标示例={[s.get('id') for s in _items if s.get('lat') is None][:5]}"
        if len(_with) != len(_items) else ""))
case("2d 坐标落在景区范围内（不是 0,0 / 不是别的城）",
     all(20.0 < s["lat"] < 32.0 and 103.0 < s["lng"] < 110.0 for s in _with)
     if _with else False,
     str(_with[0].get("lat") if _with else None))

# ==================== 用例 3：换点 / 挪时段的生效城市解析 ====================
print("\n== 用例 3：生效城市解析（_day_city）")
_day_city = getattr(sv, "_day_city", None)
if _day_city is None:
    case("3a 存在 _day_city 助手（本轮修复）", False, "未定义 ⇒ 多城换点必 400")
else:
    case("3a 单城照旧", _day_city("安顺", ["AS003"]) == "安顺")
    case("3b 多城：按 poi id 前缀定位到「那天所在的那个城」",
         _day_city(CITY_FIELD, ["GY001", "GY002"]) == "贵阳"
         and _day_city(CITY_FIELD, ["XJ007"]) == "西江千户苗寨"
         and _day_city(CITY_FIELD, ["HG004"]) == "黄果树")
    case("3c 混装 id 取**第一个**能解析的（换点传的是被换的那个点）",
         _day_city(CITY_FIELD, ["HG004", "GY001"]) == "黄果树")
    case("3d 🔴 POI id 优先于传入的城名（多城行程里 agent 只说了其中一城）",
         _day_city("贵阳", ["AS003"]) == "安顺"
         and _day_city("", ["GY001"]) == "贵阳")
    case("3e 解析不出返回 None（不猜、不静默用错城）",
         _day_city(CITY_FIELD, ["ZZ999", "QQ001"]) is None
         and _day_city(CITY_FIELD, []) is None
         and _day_city("", []) is None)
    case("3f 拼不出单城又没 id ⇒ None（宁可 400）",
         _day_city("贵阳+不存在城", []) is None
         and _day_city("贵阳+不存在城", ["QQ001"]) is None)

# ==================== 用例 4：写回路径不掉坐标（换点/挪时段同源）====================
print("\n== 用例 4：apply_day（换点/挪时段写回）在多城 plan 下仍带坐标")
_d0 = copy.deepcopy(plan["dayList"][0])
new_plan = plan_view.apply_day(plan, dict(_d0, theme=""))
_n0 = new_plan["dayList"][0]
_n_pois = [s for s in (_n0.get("timeline") or []) if s.get("type") == "poi"]
_n_with = [s for s in _n_pois if s.get("lat") is not None and s.get("lng") is not None]
case("4a 写回后当天每个 POI 仍有 lat/lng",
     len(_n_pois) > 0 and len(_n_with) == len(_n_pois),
     f"{len(_n_with)}/{len(_n_pois)}")
case("4b 写回沿用旧主题（与换点保主题同一条语义）",
     _n0.get("theme") == plan["dayList"][0].get("theme"), str(_n0.get("theme")))

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：")
    for f in FAIL:
        print("   -", f)
    raise SystemExit(1)
print("✅ 全部通过（多城联游的坐标与交互城市解析）")
