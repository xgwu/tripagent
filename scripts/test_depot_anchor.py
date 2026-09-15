# -*- coding: utf-8 -*-
"""toptw depot 锚点换位修复的确定性单测（纯离线）。

背景：无酒店时，设计意图是「depot 取主选第一点」——以首点为出发/收尾锚点，
减少首跳空驶。旧实现有两处叠加缺陷：

  1. **元组赋值陷阱**
     `nodes[0], nodes[nodes.index(first)] = nodes[nodes.index(first)], nodes[0]`
     Python 先求值 RHS、再从**左到右**落位。RHS 求值时 first 位于位置 k，
     故 RHS = (first, old_nodes[0])；LHS 第一项把 first 写入 nodes[0]，
     第二项**重算** `nodes.index(first)`——此时 first 已在 nodes[0]，
     index() 返回 0 → `nodes[0] = old_nodes[0]`，把刚写入的东西撤销。
     **整个交换等于没做**，所以「depot 取主选第一点」从未生效。

  2. **位置错位隐患**
     该语句位于 RoutingIndexManager 构造、时间窗 SetRange、AddDisjunction
     **之后**。索引映射 / 时间窗 / disjunction 罚分全部按 nodes 的「位置」建立；
     换位一旦真的生效，位置 idx 的 POI 就会与按旧点算出的窗口、罚分错位。

修复：换位提前到 RoutingIndexManager 构造之前，定位改用「基于 id 的下标」
（不用 list.index(obj)——POI 是 dict，相等性判断可能命中非目标位置），
交换用显式下标。

本单测锁定「depot == 主选第一点」的行为，并用源码守卫防止换位被挪回构造之后、
或复位为元组赋值写法。

用法：python scripts/test_depot_anchor.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import poi_db, toptw  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


city = poi_db.load_city("广州")
POIS = {p["id"]: p for p in (poi_db.parse_poi(p, city) for p in city["pois"])}


def pool(*ids):
    """构造候选池（dict 拷贝，避免与 POIS 共享引用）。"""
    return [dict(POIS[i]) for i in ids]


# 构型说明：pool[0] 故意放「纯备选」（不在主选里），主选首点放位置 2。
# 修复后 depot 应换成主选首点；修复前 depot 会停在 pool[0]（备选）上。
CAND = pool("GZ011", "GZ008", "GZ020")   # GZ011 镇海楼 / GZ008 王墓 / GZ020 中山纪念堂
MAINS = ["GZ020", "GZ008"]               # 主选首点 GZ020 位于 pool 位置 2
print(f"候选池顺序：{[p['id'] for p in CAND]}")
print(f"主选顺序  ：{MAINS}   → 主选首点 {MAINS[0]} 在池中位置 "
      f"{[p['id'] for p in CAND].index(MAINS[0])}\n")

# ---- 用例 1：depot 必须换到主选第一点 ----
print("== 用例 1：候选池首元素 ≠ 主选首点时，depot 换到主选首点")
ordered, dropped, solved = toptw.solve_day(CAND, MAINS, city, POIS)
print(f"   solved={solved}\n   ordered={ordered}\n   dropped={dropped}")
case("1a 序列首项 = 主选第一点（depot 已换位）",
     bool(solved and ordered) and ordered[0] == MAINS[0],
     f"ordered[0]={ordered[0] if ordered else None} 期望={MAINS[0]}")
print(f"   ℹ️  原 pool[0] GZ011 是否仍入选：{'GZ011' in ordered}"
      f"（软观察——它现在只是普通备选，入选与否由利润决定，不作断言）")
case("1c 换位不丢点、不重复（ordered ∪ dropped == 候选集）",
     (set(ordered) | set(dropped)) == {p["id"] for p in CAND}
     and len(ordered) == len(set(ordered)),
     f"并集={sorted(set(ordered) | set(dropped))}")

# ---- 用例 2：对照组 —— 等价复现旧写法，证明「交换被撤销」 ----
print("\n== 用例 2（对照）：等价复现旧元组赋值写法 → 交换被整体撤销")
old_nodes = pool("GZ011", "GZ008", "GZ020")
first = next((p for p in old_nodes if p["id"] == MAINS[0]), old_nodes[0])
before = old_nodes[0]["id"]
old_nodes[0], old_nodes[old_nodes.index(first)] = \
    old_nodes[old_nodes.index(first)], old_nodes[0]
print(f"   旧写法执行前 nodes[0]={before}；执行后 nodes[0]={old_nodes[0]['id']}")
case("2a 旧写法等价于「不换位」（depot 仍是 pool[0]）",
     old_nodes[0]["id"] == before,
     f"执行后={old_nodes[0]['id']} 期望={before}（未变即为复现缺陷）")
case("2b 修复后行为与旧写法不同（证明修复确实改变了 depot）",
     bool(ordered) and ordered[0] != old_nodes[0]["id"],
     f"新 ordered[0]={ordered[0] if ordered else None} vs 旧 nodes[0]={old_nodes[0]['id']}")

# ---- 用例 3：主选首点已在位置 0 → 幂等，不做交换 ----
print("\n== 用例 3：主选首点已在位置 0 → 幂等")
cand3 = pool("GZ020", "GZ008", "GZ011")
ordered3, dropped3, solved3 = toptw.solve_day(cand3, MAINS, city, POIS)
print(f"   solved={solved3}\n   ordered={ordered3}")
case("3a 序列首项仍是主选第一点", bool(solved3 and ordered3) and ordered3[0] == MAINS[0],
     f"ordered[0]={ordered3[0] if ordered3 else None}")

# ---- 用例 4：day_ids 为空 → 不换位、不崩 ----
print("\n== 用例 4：day_ids 为空 → 不换位（depot 保持 pool[0]）")
ordered4, dropped4, solved4 = toptw.solve_day(CAND, [], city, POIS)
print(f"   solved={solved4}\n   ordered={ordered4}")
case("4a 求解正常返回且序列非空", bool(solved4) and bool(ordered4), f"ordered={ordered4}")
case("4b 无主选 → depot 保持 pool[0]",
     bool(ordered4) and ordered4[0] == CAND[0]["id"],
     f"ordered[0]={ordered4[0] if ordered4 else None} 期望={CAND[0]['id']}")

# ---- 用例 5：有酒店时不走主选换位，depot 是酒店 ----
print("\n== 用例 5：有酒店锚点 → depot 即酒店，不做主选换位")
hotel = {"id": "HOTEL", "name": "测试酒店", "lat": 23.129, "lng": 113.264}
ordered5, dropped5, solved5 = toptw.solve_day(CAND, MAINS, city, POIS, hotel=hotel)
print(f"   solved={solved5}\n   ordered={ordered5}")
case("5a HOTEL 不出现在访问序列（depot 不计入行程）",
     bool(solved5) and "HOTEL" not in ordered5, f"ordered={ordered5}")
case("5b HOTEL 不计入丢弃列表", "HOTEL" not in dropped5, f"dropped={dropped5}")

# ---- 用例 6：源码守卫（防换位被挪回构造之后 / 复位为元组赋值） ----
print("\n== 用例 6：源码守卫")
src = open(os.path.join(ROOT, "src", "toptw.py"), encoding="utf-8").read()
i_swap = src.find("nodes[0], nodes[k] = nodes[k], nodes[0]")
i_mgr = src.find("RoutingIndexManager(n, 1, [0], [0])")
print(f"   换位语句 @{i_swap}  RoutingIndexManager @{i_mgr}")
case("6a 显式下标交换语句存在", i_swap > 0, f"pos={i_swap}")
case("6b 换位发生在 RoutingIndexManager 构造之前（防位置错位）",
     i_swap > 0 and i_mgr > 0 and i_swap < i_mgr,
     f"swap@{i_swap} < manager@{i_mgr}")
case("6c 源码中已不存在元组赋值式换位（旧缺陷写法）",
     "nodes[nodes.index(" not in src)

print()
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
