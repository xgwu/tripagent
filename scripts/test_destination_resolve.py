# -*- coding: utf-8 -*-
"""目的地解析层守卫（2026-09-21）。

缺陷现象（用户报障）：「国庆贵州5日自驾游」→ 400 暂不支持「贵州」。
排查后发现同一段逻辑还有一个**更严重且静默**的失败模式：

    query「川西4日游」→ 静默按 **上海** 规划，且响应里标 parsed.from_query=true。

根因：目的地解析只有两个出口——精确命中白名单，否则回落 `DEFAULT_CITY`；
既无「区域」概念，也无「我不知道」状态。而 400 护栏只在 LLM 填了 `city_raw`
时才生效，LLM 返回空时默认值静默胜出。

修＝新增解析层 `resolve_destinations`：显式城市 → LLM 从**受支持清单**里挑子集
→ 省份确定性降级（带 notice）→ 明确报错。**解析不出绝不回落默认城市**（仅当需求
里完全未提地名时才用默认城）。

用法：python scripts/test_destination_resolve.py
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from webui import server as S  # noqa: E402

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def R(q, nl=None, days=2):
    return S.resolve_destinations(q, nl, days)


print("== 1. 显式城市（既有快路径不得被新解析层影响）=")
case("1a 单城：贵阳3日游 → explicit 贵阳",
     R("贵阳3日游", None, 3)["dests"] == ["贵阳"],
     str(R("贵阳3日游", None, 3)))
case("1b 多城按提及顺序：苏州杭州 → ['苏州','杭州']",
     R("苏州杭州5日游", None, 5)["dests"] == ["苏州", "杭州"])
case("1c 省份+城市同现时以显式城市为准（贵州贵阳 → 只贵阳）",
     R("贵州贵阳5日自驾游", None, 5)["dests"] == ["贵阳"])

print("\n== 2. LLM 路径（从受支持清单挑子集）=")
nl_gz = {"destinations": ["贵阳", "安顺", "黄果树", "西江千户苗寨"]}
case("2a LLM 挑出的目的地被采用且顺序保持",
     R("贵州5日自驾游", nl_gz, 5)["dests"] == nl_gz["destinations"]
     and R("贵州5日自驾游", nl_gz, 5)["method"] == "llm")
case("2b 超出天数的部分被截断（3 天不该排 4 城）",
     len(R("贵州3日游", nl_gz, 3)["dests"]) == 3, str(R("贵州3日游", nl_gz, 3)["dests"]))
case("2c 不在受支持清单里的名字被丢弃（不得排没库的城市）",
     R("贵州5日游", {"destinations": ["贵阳", "不存在的城市", "凯里"]}, 5)["dests"]
     == ["贵阳", "凯里"])
case("2d 兼容既有字段 multi_cities（P5 起就有，不能丢）",
     R("贵州5日游", {"multi_cities": ["贵阳", "凯里"]}, 5)["dests"] == ["贵阳", "凯里"])
case("2e destinations 为空时回退 multi_cities",
     R("贵州5日游", {"destinations": [], "multi_cities": ["贵阳", "凯里"]}, 5)["dests"]
     == ["贵阳", "凯里"])

print("\n== 3. 省份降级路径（LLM 不可用）=")
r = R("国庆贵州5日自驾游", None, 5)
case("3a 贵州展开为受支持目的地、按库容取前 3",
     r["method"] == "province" and r["dests"][0] == "贵阳" and len(r["dests"]) == 3,
     f"{r['method']} {r['dests']}")
case("3b 降级结果仍全部在受支持清单内",
     all(c in S.CITIES for c in r["dests"]), str(r["dests"]))

print("\n== 4. 绝不静默回落默认城市（本次报障的核心）=")
r = R("川西4日游", None, 4)
case("4a 川西：解析不出 → dests 为空、unsupported=川西（而不是上海）",
     r["dests"] == [] and r["unsupported"] == "川西", f"{r}")
case("4b 川西：S.default 不得出现在 dests 里",
     S.DEFAULT_CITY not in r["dests"], f"DEFAULT_CITY={S.DEFAULT_CITY}")
case("4c 川西：报错建议里给出同省受支持目的地（成都）",
     S.suggest_for_region("川西") == ["成都"], str(S.suggest_for_region("川西")))
case("4d LLM 说 not supported 时同样不回落（unsupported_region 生效）",
     R("某地5日游", {"unsupported_region": "赣南"}, 5)["dests"] == [])
r = R("3日游", None, 3)
case("4e 对照：需求完全未提地名 → 保持既有默认城市行为（不误伤）",
     r["dests"] == [] and r["unsupported"] == "", f"{r}")
case("4f 离线地名判据能认出「XX省/自治州/地区」",
     S.look_place_like("青海省5日游") == "青海省"
     and S.look_place_like("湘西4日游") == "湘西"
     and S.look_place_like("3日游") == "")

print("\n== 5. 数据侧：province 是单一口径 ==")
miss = []
for c in S.CITIES:
    p = os.path.join(ROOT, "data", f"{c}_pois.json")
    d = json.load(io.open(p, encoding="utf-8"))
    if not (d.get("province") or "").strip():
        miss.append(c)
case("5a 全部城市都有 province", not miss, f"缺：{miss}")
case("5b 贵州有 19 个受支持目的地（区域解析必须有可选项）",
     sum(1 for _c, p, _n in S.dest_meta() if p == "贵州") == 19)

print("\n== 6. 源码守卫（防改动被摘掉）=")
src = io.open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("6a 旧「静默回落默认城市」那行已不存在",
     'cname = found[0] if found else (nl or {}).get("city") or DEFAULT_CITY' not in src)
case("6b /api/plan 走 plan_route_decision（决策不再散在 handler 里）",
     "plan_route_decision(query, nl, _days_r" in src)
case("6b2 plan_route_decision 内部调 resolve_destinations",
     "res = resolve_destinations(query, nl, days)" in src)
case("6c 前置解析 prompt 要求了 destinations 与受支持清单",
     '"destinations": ["从下方受支持清单里挑出的目的地' in src
     and "受支持目的地清单（destinations 只能从这里选，不得编造）" in src)
case("6d 降级路径会出 notice（不静默降级）",
     '"kind": "destinations_degraded"' in src)
case("6e 前置解析缓存键带 schema 版本（改输出结构必须升版本，否则旧结构被复用）",
     '"P2:" + hashlib.sha1' in src)

print("\n== 7. plan_route_decision（接线层：单城/多城的判定不得互相干扰）=")
# 7a 是本次真实踩到的回归：解析层返回 ≥2 城时 cname 为空，被单城校验误判成 400
D = S.plan_route_decision
case("7a 别名多城：苏杭3日游 → multi=['苏州','杭州'] 且无 error",
     D("苏杭3日游", None, 3) == {"multi": ["苏州", "杭州"], "cname": "",
                                 "notice": [], "error": None},
     str(D("苏杭3日游", None, 3)))
case("7b 显式多城：苏州杭州5日游 → multi 两城、无 error",
     D("苏州杭州5日游", None, 5)["multi"] == ["苏州", "杭州"]
     and not D("苏州杭州5日游", None, 5)["error"])
d7 = D("贵州5日自驾游", {"destinations": ["贵阳", "安顺", "黄果树", "西江千户苗寨"]}, 5)
case("7c LLM 多城：贵州 → multi 4 城、cname 留空（不得报错）",
     d7["multi"] == ["贵阳", "安顺", "黄果树", "西江千户苗寨"] and not d7["error"],
     str(d7))
case("7d 单城：贵阳3日游 → cname=贵阳、multi 为空",
     D("贵阳3日游", None, 3)["cname"] == "贵阳"
     and D("贵阳3日游", None, 3)["multi"] == [])
case("7e 川西4日游 → error 非空且 cname 不是上海（本次报障核心）",
     bool(D("川西4日游", None, 4)["error"])
     and D("川西4日游", None, 4)["cname"] != S.DEFAULT_CITY,
     str(D("川西4日游", None, 4)))
case("7f 对照：3日游（未提地名）→ 保持默认城市",
     D("3日游", None, 3)["cname"] == S.DEFAULT_CITY
     and not D("3日游", None, 3)["error"])
case("7g ?city= 显式参数优先（无效城名报错）",
     D("3日游", None, 3, "苏州")["cname"] == "苏州"
     and bool(D("3日游", None, 3, "不存在的城市")["error"]))
case("7h ?city= 与多城查询共存时仍走城际（既有行为不丢）",
     D("苏杭3日游", None, 3, "苏州")["multi"] == ["苏州", "杭州"])
case("7i 省份降级会带 destinations_degraded notice（不静默降级）",
     any(n.get("kind") == "destinations_degraded"
         for n in D("国庆贵州5日自驾游", None, 5)["notice"]))

print()
print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：")
for f in FAIL:
    print("   -", f)
sys.exit(1 if FAIL else 0)
