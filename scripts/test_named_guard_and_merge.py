# -*- coding: utf-8 -*-
"""2026-09-21 两个缺陷的确定性复现/回归。

缺陷 1（跨城重复游玩）：方案B 开城时景区与市区各自独立成城、且**允许 POI 重叠**
（万峰林的招牌点同时也是兴义的招牌点，id 前缀不同 XY/WF）。用户同时点名
「兴义万峰林」会走多城路径，逐城规划时同一片景区被走两遍
（实测 Day1/Day3 重复万峰林景区、八卦田、上下纳灰村）。
修＝`plan_multi` 里近邻目的地合并（阈值 25km，只覆盖「其实是一地」的两对）。

缺陷 2（点名点被修复链丢弃）：query「织金洞1日游」用户点名织金洞，最终行程里没有它。
根因不在求解器（`forced` 给 +1e6，实测求解器**保留**了它），而在排序器的修复链：

  1. `order_day._greedy` 选点只看 `rating − 3×时段分`，**完全不看闭园时间**；
  2. 于是「一级修复」（`build_itinerary` 里 `tl["violations"]` 触发的贪婪重排）
     把织金洞抛到第三位 → 14:17 到达 + 2.5h 超出 16:30 闭园 → 判违规；
  3. 「二级修复」`_repair_by_drop` 直接剔掉肇事点，**不问它是不是用户点名的**。
     口径应当是「要丢就丢 LLM 提的点，不是丢它」。
  修＝`_repair_by_drop` 接 `required`，肇事者是必选点时改丢别的；`build_itinerary`
  透传 `required`，`compose` 传 `forced_ids`。

用法：python scripts/test_named_guard_and_merge.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, sequencer as sq  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _city_pois(name):
    c = poi_db.load_city(name)
    return c, {p["id"]: p for p in (poi_db.parse_poi(x, c) for x in c["pois"])}


def _kept(tl):
    return [s["id"] for s in tl.get("timeline", []) if s.get("type") == "poi"]


# ============ 缺陷 1：近邻目的地合并 ============
print("== 缺陷 1：plan_multi 近邻目的地合并 ==")
from webui import server as S  # noqa: E402  （须在 stdout 包装之后导入）

kept, merged = S.merge_near_destinations(["兴义", "万峰林"])
case("1a 兴义+万峰林（9.6km）合并为一次规划",
     kept == ["兴义"] and merged == [("兴义", "万峰林")], f"kept={kept} merged={merged}")

kept, merged = S.merge_near_destinations(["万峰林", "兴义"])
case("1b 反序同样合并，且保留景点更多的一方（兴义）",
     kept == ["兴义"], f"kept={kept}")

kept, merged = S.merge_near_destinations(["凯里", "西江千户苗寨"])
case("1c 凯里+西江千户苗寨（20.7km）合并", kept == ["凯里"], f"kept={kept}")

kept, merged = S.merge_near_destinations(["铜仁", "梵净山"])
case("1d 对照：铜仁+梵净山（49km）不合并——真两地",
     kept == ["铜仁", "梵净山"] and not merged, f"kept={kept}")

kept, merged = S.merge_near_destinations(["安顺", "黄果树"])
case("1e 对照：安顺+黄果树（40km）不合并", kept == ["安顺", "黄果树"], f"kept={kept}")

kept, merged = S.merge_near_destinations(["杭州", "苏州"])
case("1f 对照：既有城市（杭州+苏州）不受影响",
     kept == ["杭州", "苏州"] and not merged, f"kept={kept}")

kept, merged = S.merge_near_destinations(["不存在的地方"])
case("1g 缺库的城市一律保留（宁可多跑一城，不可静默丢城）",
     kept == ["不存在的地方"], f"kept={kept}")

_src = io.open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("1h 分配期通知确实并入 merged_notices（此前 _alloc_notice 只有构建无消费点，"
     "用户永远收不到「你要求苏州2天实际安排1天」这类提示）",
     "merged_notices = list(_alloc_notice)" in _src)

# ============ 缺陷 2：点名点不得被修复链丢弃 ============
print("\n== 缺陷 2：修复链不得丢点名点 ==")
CITY, POIS = _city_pois("织金洞")
DAY3 = ["ZJ001", "ZJ007", "ZJ006"]          # 织金洞(09:00-16:30, 2.5h) + 博物馆 + 古城
pois = [POIS[i] for i in DAY3]

# 先确认这个组合确实会触发「点名点超窗」——否则下面的对照是假的
tl0 = sq._build_timeline(sq.order_day(pois, city=CITY), CITY, 1, None)
viol0 = [v.get("poi") for v in tl0["violations"]]
case("2a 前提：贪婪重排确实把织金洞排到超窗（复现旧缺陷的触发条件）",
     "织金洞" in viol0, f"违规={viol0}")

r_old = sq._repair_by_drop(pois, CITY, 1)
case("2b 对照：不传 required 时，点名点被直接剔除（旧行为）",
     [d["id"] for d in r_old.get("dropped", [])] == ["ZJ001"],
     f"剔除={[d['id'] for d in r_old.get('dropped', [])]}")

r_new = sq._repair_by_drop(pois, CITY, 1, required={"ZJ001"})
case("2c 传 required 后改丢别的，点名点不再被剔",
     "ZJ001" not in [d["id"] for d in r_new.get("dropped", [])],
     f"剔除={[d['id'] for d in r_new.get('dropped', [])]}")

case("2d 修复后点名点真的在时间轴里且 0 违规",
     "ZJ001" in _kept(r_new) and not r_new["violations"],
     f"kept={_kept(r_new)} 违规={[v.get('poi') for v in r_new['violations']]}")

# build_itinerary 必须真的把 required 透传到修复链（否则上面只是单测通过、链路没接上）。
# 给定顺序故意把点名点排在**最后**（餐块叠加后必然超窗），才会真正走到修复链。
dm = {1: ["ZJ006", "ZJ007", "ZJ001"]}
it_off = sq.build_itinerary(dm, CITY, POIS, date0=None, query="织金洞1日游")
it_on = sq.build_itinerary(dm, CITY, POIS, date0=None, query="织金洞1日游",
                           required={"ZJ001"})

def _ids(it):
    return [s["id"] for d in it["days"] for s in d["timeline"] if s["type"] == "poi"]

case("2e 链路透传：build_itinerary(required=...) 能保住点名点",
     "ZJ001" in _ids(it_on) and "ZJ001" not in _ids(it_off),
     f"off={_ids(it_off)} on={_ids(it_on)}")

case("2f 全部点都是必选时不崩（仍收敛，无法两全时如实剔除）",
     sq._repair_by_drop(pois, CITY, 1, required=set(DAY3)) is not None)

# compose 必须把 forced_ids 传下去，否则 required 永远是空
_m2 = io.open(os.path.join(ROOT, "src", "m2_planner.py"), encoding="utf-8").read()
_re_pat = r"build_itinerary\([^)]*required=forced_ids"
case("2g compose 的 build_itinerary 调用带上 required=forced_ids",
     len(re.findall(_re_pat, _m2, re.S)) >= 3,
     f"命中 {len(re.findall(_re_pat, _m2, re.S))} 处")

# ============ 缺陷 2 前置：注入位置按窗口分流 ============
print("\n== 缺陷 2 前置：点名注入位置 ==")
from src import proposal_planner as pp  # noqa: E402

# 非夜间点（织金洞 09:00-16:30）必须排队首，否则被后续点位挤到闭窗前
dm2 = {1: ["ZJ007", "ZJ006"]}
g2 = {}
pp._inject_named_pois(dm2, POIS, "织金洞1日游", g2)
case("2h 非夜间点名点注入到当天**队首**",
     dm2[1][0] == "ZJ001", f"day_map[1]={dm2[1]}")

# 夜间点仍排队尾（收尾更自然）
CY, CYPOIS = _city_pois("贵阳")
_night = next((p for p in CYPOIS.values()
               if p.get("category") == "nightlife" and p["name"]), None)
if _night is not None:
    dm3 = {1: ["GY001"]}
    g3 = {}
    pp._inject_named_pois(dm3, CYPOIS, f"贵阳2日游 {_night['name']}", g3)
    case("2i 对照：真夜间点仍注入到队尾",
         dm3[1] and dm3[1][-1] == _night["id"],
         f"{_night['id']} day_map[1]={dm3[1]}")
else:
    print("· 跳过 2i：贵阳库内无 nightlife 点")

print()
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：")
for f in FAIL:
    print("   -", f)
sys.exit(1 if FAIL else 0)
