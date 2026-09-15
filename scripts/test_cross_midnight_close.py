# -*- coding: utf-8 -*-
"""跨零点闭店归一化确定性单测（纯离线）。

背景：parse_poi 原先直接 close_h = hhmm_to_h(close)。闭店时刻跨过午夜的点
（18:00-02:00 宵夜街、10:00-06:00 KTV）close_h 落在 open_h 之前，被三重判死：
  1. toptw 预剔除 to_min(close)-dur < 0
  2. toptw 时间窗 lo = to_min(open) > hi = to_min(close)-dur
  3. sequencer t + dur > close_h → 恒报「超出营业时间」
实测 GZ069 宝业路宵夜街 / NJ021 1912 街区两个 nightlife 点自入库起从未落地。

用法：python scripts/test_cross_midnight_close.py
"""
import io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, sequencer, toptw  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


city_gz = poi_db.load_city("广州")
pois_gz = {p["id"]: p for p in (poi_db.parse_poi(p, city_gz) for p in city_gz["pois"])}
city_nj = poi_db.load_city("南京")
pois_nj = {p["id"]: p for p in (poi_db.parse_poi(p, city_nj) for p in city_nj["pois"])}

day_start_gz = poi_db.hhmm_to_h(city_gz["day_start"])
horizon_gz = max(240, int((poi_db.hhmm_to_h(city_gz["day_end"]) - day_start_gz) * 60)
                 - toptw.MEAL_BUFFER_MIN)
print(f"广州 day_start={day_start_gz} day_end={city_gz['day_end']} horizon={horizon_gz}min")
print(f"南京 day_start={poi_db.hhmm_to_h(city_nj['day_start'])}\n")

# ---- 用例 1：跨零点点被正确归一化 ----
print("== 用例 1：跨零点闭店 → close_h 归一化到次日（+24）")
for pid, pois, cname in (("GZ069", pois_gz, "广州"), ("NJ021", pois_nj, "南京")):
    p = pois[pid]
    raw_close = poi_db.hhmm_to_h(p["close"])
    print(f"  {pid} {p['name']}  {p['open']}-{p['close']}  dur={p['dur']}h")
    print(f"     raw close_h={raw_close}  parsed close_h={p['close_h']}")
    case(f"1 {pid} 原始闭店时刻早于开门（确属跨零点）",
         raw_close <= poi_db.hhmm_to_h(p["open"]), f"{raw_close} <= {poi_db.hhmm_to_h(p['open'])}")
    case(f"1 {pid} 归一化为次日时刻",
         abs(p["close_h"] - (raw_close + 24.0)) < 1e-9,
         f"close_h={p['close_h']} 期望={raw_close + 24.0}")

# ---- 用例 2：回归保护 —— 非跨零点与全天点的 close_h 不变 ----
print("\n== 用例 2（回归保护）：不跨零点的点 close_h 必须与解析值一致")
for pid, exp in (("GZ001", 22.5), ("GZ070", poi_db.hhmm_to_h("23:59")),
                 ("GZ068", poi_db.hhmm_to_h("23:59")), ("GZ003", poi_db.hhmm_to_h("23:59"))):
    p = pois_gz[pid]
    case(f"2 {pid} {p['name'][:12]} close_h 未变",
         abs(p["close_h"] - exp) < 1e-9, f"{p['close_h']} vs {exp}")
allp = [p for p in pois_gz.values()]
bad = [p["id"] for p in allp if p["close_h"] <= p["open_h"]]
case("2 全库无残留 close_h<=open_h 的点（归一化完备）", not bad, str(bad))

# ---- 用例 3：修复前后的可排性数学对比 ----
print("\n== 用例 3：修复前死点 / 修复后可行（toptw 预剔除判据）")
p69 = pois_gz["GZ069"]
raw_close = poi_db.hhmm_to_h(p69["close"])
dur_min = int(round(p69["dur"] * 60))
pre_before = int(round((raw_close - day_start_gz) * 60)) - dur_min
pre_after = int(round((p69["close_h"] - day_start_gz) * 60)) - dur_min
lo = max(0, int(round((p69["open_h"] - day_start_gz) * 60)))
hi_after = min(horizon_gz, pre_after)
print(f"  to_min(close) 修复前={int(round((raw_close - day_start_gz) * 60))} 修复后={pre_after} dur={dur_min}min")
print(f"  预剔除量 修复前={pre_before}  修复后={pre_after}")
print(f"  时间窗 lo={lo}  hi 修复后={hi_after}")
case("3 修复前被判不可达（预剔除量 < 0）", pre_before < 0, f"{pre_before} < 0")
case("3 修复后通过预剔除", pre_after >= 0, f"{pre_after} >= 0")
case("3 修复后时间窗非空（lo <= hi）", lo <= hi_after, f"lo={lo} hi={hi_after}")

# ---- 用例 4：排序层 —— 含跨零点点的天不再报「超出营业时间」 ----
print("\n== 用例 4：sequencer.build_itinerary 含 GZ069 的构型")
it = sequencer.build_itinerary({1: ["GZ070", "GZ069"]}, city_gz, pois_gz, order_given=True)
tl = it["days"][0]
names = [(s.get("start"), s.get("name")) for s in tl["timeline"] if s.get("type") == "poi"]
print("  timeline:", names)
print("  dropped :", [d["name"] for d in tl.get("dropped", [])])
print("  viol    :", [v["reason"][:40] for v in tl["violations"]])
landed = [s for s in tl["timeline"] if s.get("type") == "poi" and s.get("name") == p69["name"]]
case("4a GZ069 落地未被剔除", bool(landed), f"landed={bool(landed)}")
overtime = [v for v in tl["violations"] if "超出营业时间" in v.get("reason", "")]
case("4b 无「超出营业时间」违规", not overtime, str([v["reason"] for v in overtime]))
if landed:
    st = poi_db.hhmm_to_h(landed[0]["start"])
    case("4c 收尾时段到达（>=17:00）", st >= 17.0, f"start={landed[0]['start']}")

# ---- 用例 5：求解层 —— 预剔除解除（单点池，剔除＝空池直接返回） ----
# 注：GZ069 18:00 才开门，horizon 10.5h 决定了"等到 18:00 + 90min + 回程"必须
# 恰好卡满，故单点池是最干净的判据；混池能否入选取决于当日其余点的填充分布。
print("\n== 用例 5：toptw.solve_day 单点池（归一化 vs 还原旧语义）")
ordered, dropped, solved = toptw.solve_day(
    [pois_gz["GZ069"]], ["GZ069"], city_gz, pois_gz, lock_mains=True)
print(f"  [归一化] solved={solved} ordered={ordered} dropped={dropped}")
case("5a 归一化后 GZ069 通过预剔除并入选", solved and "GZ069" in ordered,
     f"solved={solved} ordered={ordered} dropped={dropped}")

pool_old = [dict(pois_gz["GZ069"])]
pool_old[0]["close_h"] = raw_close          # 还原修复前语义（02:00 → 2.0）
ordered2, dropped2, solved2 = toptw.solve_day(
    pool_old, ["GZ069"], city_gz, pois_gz, lock_mains=True)
print(f"  [还原] solved={solved2} ordered={ordered2} dropped={dropped2}")
case("5b 对照：未归一化时被预剔除（复现原缺陷）", not ordered2,
     f"ordered={ordered2} dropped={dropped2}")

# ---- 用例 6：混池下归一化点进入模型（不再被预剔除静默抹除） ----
print("\n== 用例 6：混池（白天点 + 跨零点点）求解器可见性")
mixed = [pois_gz[i] for i in ("GZ070", "GZ069")]
o3, d3, s3 = toptw.solve_day(mixed, ["GZ069"], city_gz, pois_gz, lock_mains=True)
print(f"  混池 solved={s3} ordered={o3} dropped={d3}")
case("6a 混池求解成功（跨零点点参与模型而非被预剔除）",
     s3 and (("GZ069" in o3) or ("GZ069" in d3)),
     f"ordered={o3} dropped={d3}")
mixed_old = [dict(pois_gz["GZ070"]), dict(pois_gz["GZ069"], close_h=raw_close)]
o4, d4, s4 = toptw.solve_day(mixed_old, ["GZ069"], city_gz, pois_gz, lock_mains=True)
print(f"  [还原] 混池 solved={s4} ordered={o4} dropped={d4}")
case("6b 对照：还原后该点从模型完全消失（既不入选也不在 dropped）",
     "GZ069" not in o4 and "GZ069" not in d4,
     f"ordered={o4} dropped={d4}")

print("")
if FAIL:
    print(f"❌ {len(FAIL)} 组用例失败：{FAIL}")
    sys.exit(1)
print("全部用例通过 ✅")
