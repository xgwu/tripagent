# -*- coding: utf-8 -*-
"""亲子出行「不适合儿童的点」全链过滤 —— 确定性单测（纯离线）。

背景：POI 字段 family_ok=false 标记不适合带小孩去的点（全库 11 个：KTV/酒吧街区/
题材沉重纪念馆/高强度登山徒步/温泉泡汤）。但该字段**此前只有 offline_planner 读**，
主链路（proposal_planner 提案 → m2_planner 选点 → TOPTW 求解）完全不看，
LLM 若给亲子 query 提案 KTV，排程链不会拦——实测 LLM 会自觉避开，但那是概率
不是保证。

修复：亲子判据（FAMILY_RE / is_family_query）下沉到 sequencer，m2_planner
re-export；确定性 gate 挂到选点链**每个入口**（教训：漏一个入口等于没挂）：
  1. 提案层主选移除（proposal_planner._post_ground_fixups）
  2. 候选池构造（m2_planner._build_day_pool）
  3. LLM 备选并入池（m2_planner._solve_all_days）
  4. 求解器主选过滤（m2_planner._solve_all_days）
  5. 剔点补位（m2_planner._alt_substitute）
  6. 薄天补强池（proposal_planner）
  7. 提案 prompt 规则（PROPOSE_PROMPT 亲子规则，引入于 VER v8，此后只增不减）

本单测覆盖判据、纯函数 gate、求解层主选过滤，并用源码守卫锁住挂载点。

用法：python scripts/test_family_gate.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import glob  # noqa: E402

from src import m2_planner, poi_db, toptw  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# ---- 用例 1：亲子判据（正例 / 负例 / 边界） ----
print("== 用例 1：is_family_query 判据")
POS = ["广州3天，带2个小孩", "上海2日亲子游", "带孩子去北京玩", "周末遛娃",
       "儿童乐园为主", "带娃去成都", "亲子", "带小朋友逛逛"]
NEG = ["广州2天经典深度游", "上海2天骑行，喜欢咖啡和美食", "带老人去苏州玩2天",
       "苏州2天园林和美食漫游", "北京2天历史文化深度游"]
for q in POS:
    case(f"1+ 命中亲子：「{q}」", m2_planner.is_family_query(q))
for q in NEG:
    case(f"1- 不误判：「{q}」", not m2_planner.is_family_query(q))
case("1e 空查询 / None 不崩、判否",
     not m2_planner.is_family_query("") and not m2_planner.is_family_query(None))
case("1f 慢节奏判据与亲子判据互不干扰（带老人 ≠ 亲子）",
     bool(m2_planner.SLOW_PACE_RE.search("带老人去苏州")) and not m2_planner.is_family_query("带老人去苏州"))

# ---- 用例 2：poi_db.is_family_ok ----
print("\n== 用例 2：poi_db.is_family_ok 字段语义")
case("2a 显式 false → 不适合", not poi_db.is_family_ok({"family_ok": False}))
case("2b 显式 true → 适合", poi_db.is_family_ok({"family_ok": True}))
case("2c 字段缺省 → 视为适合（向后兼容）", poi_db.is_family_ok({}))
case("2d None → 视为适合", poi_db.is_family_ok({"family_ok": None}))
case("2e 全链路判据一致：is_family_query 与 FAMILY_RE 同源",
     m2_planner.is_family_query("亲子游") == bool(m2_planner.FAMILY_RE.search("亲子游")))

# ---- 用例 3：全库数值守卫 ----
print("\n== 用例 3：全库 family_ok=false 点位台账")
blocked = []
n_total = 0
for f in sorted(glob.glob(os.path.join(ROOT, "data", "*_pois.json"))):
    cname = os.path.basename(f).replace("_pois.json", "")
    city = poi_db.load_city(cname)
    n_total += len(city["pois"])
    blocked += [(cname, p["id"], p["name"], p.get("category"))
                for p in city["pois"] if p.get("family_ok") is False]
print(f"   全库 {n_total} 点，family_ok=false 共 {len(blocked)} 个：")
for b in blocked:
    print(f"     {b[0]} {b[1]} {b[2]}（{b[3]}）")
case("3a 全库点位数与口径一致（722 = 573 + 西安 54 + 重庆 48 + 长沙 47）", n_total == 722, f"实测 {n_total}")
case("3b family_ok=false 恰为 11 个（新增点位应显式评估该字段）",
     len(blocked) == 11, f"实测 {len(blocked)}")
# 白名单＝可能承载成人/高强度内容的类目：夜生活、高强度户外、题材沉重纪念馆、
# 山岳徒步（nature）、温泉泡汤（relax）。断言「无标记点落入 food/购物/亲子等日常类目」，
# 防止误标把普通点从亲子链里静默剔除（曾误标 XA006 西安碑林博物馆）。
RISKY_CATS = ("nightlife", "outdoor", "culture", "nature", "relax")
case("3c 全部标记点均为夜间/高强度/沉重题材类目（nature 登山、relax 泡汤也属此列）",
     all(b[3] in RISKY_CATS for b in blocked),
     f"类目={sorted({b[3] for b in blocked})}｜白名单={RISKY_CATS}")

# ---- 用例 4：候选池构造 gate ----
print("\n== 用例 4：_build_day_pool 过滤（含对照）")
city_gz = poi_db.load_city("广州")
POIS = {p["id"]: p for p in (poi_db.parse_poi(p, city_gz) for p in city_gz["pois"])}
CANDS = list(POIS.values())
mains4 = [POIS["GZ008"]]
pool_f = m2_planner._build_day_pool(mains4, CANDS, set(), set(),
                                    radius_km=100.0, n_backups=500, family=True)
pool_n = m2_planner._build_day_pool(mains4, CANDS, set(), set(),
                                    radius_km=100.0, n_backups=500, family=False)
bad_in_pool = [p["id"] for p in pool_f if not poi_db.is_family_ok(p)]
print(f"   亲子池 {len(pool_f)} 个 / 普通池 {len(pool_n)} 个")
case("4a 亲子池不含任何 family_ok=false 的点", not bad_in_pool, f"越界点={bad_in_pool}")
case("4b 对照：普通池含 GZ076（KTV）",
     any(p["id"] == "GZ076" for p in pool_n))
case("4c 亲子池仍保留普通点（不是把池清空）",
     len(pool_f) >= 20 and all(poi_db.is_family_ok(p) for p in pool_f),
     f"池大小={len(pool_f)}")

# ---- 用例 5：剔点补位 gate ----
print("\n== 用例 5：_alt_substitute 补位过滤（含对照）")
day_map5 = {1: ["GZ008"]}
dropped5 = [{"id": "GZ020", "name": "中山纪念堂", "day": 1}]
alt_map5 = {1: ["GZ076"]}   # 唯一备选是 KTV（family_ok=false）
res_f, subs_f, rest_f = m2_planner._alt_substitute(
    day_map5, dropped5, alt_map5, POIS, None, slow=False, family=True)
res_n, subs_n, rest_n = m2_planner._alt_substitute(
    day_map5, dropped5, alt_map5, POIS, None, slow=False, family=False)
print(f"   亲子：subs={subs_f} rest={[r['id'] for r in rest_f]}")
print(f"   对照：subs={[s['poi_id'] for s in subs_n]}")
case("5a 亲子下不补 KTV（subs 为空、该剔点留在 rest）",
     not subs_f and [r["id"] for r in rest_f] == ["GZ020"])
case("5b 对照：非亲子下同一备选会被补进去（GZ076）",
     [s["poi_id"] for s in subs_n] == ["GZ076"])

# ---- 用例 6：求解层主选过滤（含对照） ----
print("\n== 用例 6：_solve_all_days 亲子主选过滤（含对照）")


def solve_with(query, day_map):
    return m2_planner._solve_all_days(
        city_gz, query, day_map, POIS, CANDS, None, None, None, None,
        toptw.TIME_LIMIT_S, toptw.MAIN_BONUS, toptw.SOFT_W)


dm = {1: ["GZ076", "GZ008", "GZ020"]}   # GZ076 = 纯K（family_ok=false）
res_fam = solve_with("广州2天亲子游，带小孩", dm)
res_norm = solve_with("广州2天经典深度游", dm)
fam_day1 = res_fam["final_day_map"].get(1, [])
fam_reasons = [r for r in res_fam["solver_dropped"] if "亲子" in r.get("reason", "")]
print(f"   亲子：Day1={fam_day1}")
print(f"         亲子剔除记录={[(r['id'], r['reason']) for r in fam_reasons]}")
print(f"   对照：Day1={res_norm['final_day_map'].get(1, [])}")
case("6a 亲子下 KTV 主选被剔除且给出亲子 reason",
     any(r["id"] == "GZ076" for r in fam_reasons),
     f"reason 记录={[r['id'] for r in fam_reasons]}")
case("6b 亲子下最终行程不含 GZ076", "GZ076" not in fam_day1)
case("6c 对照：非亲子 query 下不存在任何「亲子」剔除记录",
     not any("亲子" in r.get("reason", "") for r in res_norm["solver_dropped"]))
case("6d 亲子下其余主选正常保留（不是把当天清空）",
     len(fam_day1) >= 1, f"Day1={fam_day1}")

# ---- 用例 7：源码守卫（防 gate 被摘掉） ----
print("\n== 用例 7：源码守卫（防 gate 被摘掉）")
src_m2 = open(os.path.join(ROOT, "src", "m2_planner.py"), encoding="utf-8").read()
src_pp = open(os.path.join(ROOT, "src", "proposal_planner.py"), encoding="utf-8").read()
src_sq = open(os.path.join(ROOT, "src", "sequencer.py"), encoding="utf-8").read()
case("7a sequencer 定义 FAMILY_RE 与 is_family_query",
     "FAMILY_RE = re.compile" in src_sq and "def is_family_query" in src_sq)
case("7b m2_planner re-export 亲子判据",
     "from .sequencer import SLOW_PACE_RE, FAMILY_RE, is_family_query" in src_m2)
case("7c m2_planner 的 is_family_ok gate ≥3 处（主选/池/备选）",
     src_m2.count("is_family_ok") >= 3, f"实测 {src_m2.count('is_family_ok')} 处")
case("7d proposal_planner 有主选亲子移除 + 补强池过滤",
     "family_removed" in src_pp and src_pp.count("is_family_ok") >= 2,
     f"is_family_ok {src_pp.count('is_family_ok')} 处")
import re
_ver_m = re.search(r'PROPOSE_PROMPT_VER = "v(\d+)"', src_pp)
case("7e 提案 prompt 含亲子规则且版本号已递增",
     "亲子规则（针对带娃" in src_pp and _ver_m is not None and int(_ver_m.group(1)) >= 8,
     f"VER=v{_ver_m.group(1) if _ver_m else '?'}（亲子规则引入于 v8，此后只许增）")
case("7f proposal_planner 的 _is_family_query 已委托统一判据",
     "return m2_planner.is_family_query(query)" in src_pp)

print()
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
