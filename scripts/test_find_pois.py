# -*- coding: utf-8 -*-
"""Phase 0 守卫：POI 发现 —— 云端 `GET /api/pois` + MCP 工具 `tripplanner_find_pois`。

为什么要有这个能力（见 docs/MCP-Agent提案外包-方案探讨.md §5）：
调用方 agent 若想自己出提案，**必须**先能查到「库内真实有哪些点」。否则它只能凭
世界知识编点名字 ⇒ 落地匹配失败 ⇒ 触发修正轮 ⇒ 反而比「服务端自己提案」更贵。
发现工具是这个方向所有后续动作的地基，而它自身**不改** plan_trip、零风险。

本文件钉住四条：
  A. 云端 `search_pois`：单城 / 多城（`+`）/ 全库 / keyword / category / limit / 未收录城；
  B. `/api/pois` 端点已接线到 `do_GET`（MCP 侧依赖它，漏接＝工具永远拿不到数据）；
  C. MCP 工具面：`tripplanner_find_pois` 存在、**不带** `_meta`（不开面板）、
     description 讲清「查库内真实 POI」的用途（契约只走工具定义）；
  D. MCP 行为：正常调用的文本/结构化返回、缺参数报错、后端报错（含 supported 清单）、
     无匹配的空结果。

用法：python scripts/test_find_pois.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import poi_db  # noqa: E402
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


def _load_mcp():
    """按路径加载真 MCP server（与 scripts/test_panel_single_open.py 同一手法）。"""
    server = os.environ.get("TRIPPLANNER_SERVER") or os.path.join(
        ROOT, "mcp", "tripplanner_mcp_server.py")
    spec = importlib.util.spec_from_file_location("_tp_server_find_pois", server)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 隔离副作用：不落盘、不写诊断日志、不打网络。
    mod._plan_state_read = lambda *a, **k: None
    mod._plan_state_write = lambda *a, **k: None
    mod.dbg = lambda *a, **k: None
    mod.log = lambda *a, **k: None
    return mod


# ==================== 用例 0：前提（判据失败先问「数据到位了吗」）====================
print("== 用例 0：前提")
try:
    _hz = poi_db.load_city("杭州")["pois"]
except Exception as e:  # noqa: BLE001
    case("0a 杭州 POI 库可加载", False, f"{type(e).__name__}: {e}")
    _hz = []
else:
    case("0a 杭州 POI 库可加载且非空", len(_hz) > 0, f"{len(_hz)} 点")
_hz_xihu = [p for p in _hz
            if "西湖" in (p.get("name") or "")
            or any("西湖" in str(t) for t in (p.get("tags") or []))]
case("0b 杭州库里存在含「西湖」的点（keyword 用例的前提）",
     len(_hz_xihu) > 0, f"{len(_hz_xihu)} 个")

# ==================== 用例 1：云端 search_pois ====================
print("\n== 用例 1：云端 search_pois")
d = sv.search_pois(city="杭州", limit=100)
case("1a 单城查询：ok + 全部落在该城",
     d.get("ok") and d.get("count", 0) > 0
     and all(p["city"] == "杭州" for p in d["pois"]),
     f"count={d.get('count')} total={d.get('total')}")
case("1b total == 该城 POI 总数（limit 不改变 total）",
     d.get("total") == len(_hz), f"total={d.get('total')} 库={len(_hz)}")

dk = sv.search_pois(city="杭州", keyword="西湖", limit=100)
_matched = all(
    ("西湖" in poi_db.norm_name(p["name"]))
    or any("西湖" in poi_db.norm_name(t) for t in p["tags"])
    or ("西湖" in poi_db.norm_name(p["note"]))
    for p in dk["pois"]) if dk["pois"] else False
case("1c keyword 命中：每条都真的含关键词（name/tags/note 之一）",
     dk.get("ok") and dk.get("count", 0) > 0 and _matched,
     f"count={dk.get('count')} 例={[p['name'] for p in dk['pois'][:3]]}")

dc = sv.search_pois(city="杭州", category="nature", limit=100)
case("1d category 过滤：全部该类目",
     dc.get("total", 0) > 0 and all(p["category"] == "nature" for p in dc["pois"]),
     f"total={dc.get('total')} 类目={sorted({p['category'] for p in dc['pois']})}")

dm = sv.search_pois(city="杭州+苏州", limit=100)
case("1e 多城（`+`）：cities 正确、结果不越界",
     dm.get("ok") and dm.get("cities") == ["杭州", "苏州"]
     and all(p["city"] in ("杭州", "苏州") for p in dm["pois"]),
     f"cities={dm.get('cities')} count={dm.get('count')}")

da = sv.search_pois(city="", keyword="西湖", limit=100)
case("1f 全库搜（city 留空）：cities == 全部已收录城",
     da.get("ok") and da.get("cities") == list(sv.CITIES),
     f"{len(da.get('cities') or [])} 城")

du = sv.search_pois(city="火星", limit=10)
case("1g 未收录城市：ok=False + 给出 supported 清单（不静默排空）",
     du.get("ok") is False and "火星" in str(du.get("error"))
     and isinstance(du.get("supported"), list) and len(du["supported"]) > 0,
     str(du.get("error"))[:60])

dl = sv.search_pois(city="杭州", limit=999)
case("1h limit 上限被夹到 100（防一次性拉爆上下文）",
     dl.get("count", 0) <= 100, f"count={dl.get('count')}")

dn = sv.search_pois(city="杭州", keyword="这个词库里一定没有xyzzy")
case("1i 无匹配：ok=True 且 count=0（不是报错）",
     dn.get("ok") is True and dn.get("count") == 0, str(dn.get("count")))

case("1j 结果不含坐标（省上下文；坐标走 poi_coords）",
     d["pois"] and all("lat" not in p and "lng" not in p for p in d["pois"]))
case("1k 结果带选点必需字段",
     all(set(("id", "name", "city", "category", "duration_h", "family_ok",
              "closed_days", "note")) <= set(p) for p in d["pois"][:5]))

# ==================== 用例 2：端点接线 ====================
print("\n== 用例 2：`/api/pois` 端点接线")
with open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8") as f:
    _srv_src = f.read()
case("2a do_GET 里存在 `/api/pois` 分支（精确锚点）",
     'if u.path == "/api/pois"' in _srv_src)
case("2b 端点调用的正是 search_pois",
     "search_pois(city=q.get(\"city\")" in _srv_src.replace("'", '"'))

# ==================== 用例 3：MCP 工具面 ====================
print("\n== 用例 3：MCP 工具面")
srv = _load_mcp()
by = {t["name"]: t for t in srv.TOOLS}
case("3a 工具清单含 tripplanner_find_pois",
     "tripplanner_find_pois" in by, str(sorted(by)))
case("3b 不是面板入口（不带 _meta）—— 否则每次查库都弹一扇面板",
     not by.get("tripplanner_find_pois", {}).get("_meta"),
     repr(by.get("tripplanner_find_pois", {}).get("_meta")))
_desc = (by.get("tripplanner_find_pois") or {}).get("description") or ""
case("3c description 讲清用途（查库内真实 POI / 别凭世界知识编名字）",
     ("库内真实" in _desc) and ("世界知识" in _desc) and ("开面板" in _desc), _desc[:80])
_props = ((by.get("tripplanner_find_pois") or {}).get("inputSchema") or {}).get("properties") or {}
case("3d schema 有 city / keyword / category / limit",
     {"city", "keyword", "category", "limit"} <= set(_props), str(sorted(_props)))
case("3e 无必填参数（全留空由 handler 给出指引，而不是 schema 层硬拦）",
     not (((by.get("tripplanner_find_pois") or {}).get("inputSchema") or {}).get("required")))

# ==================== 用例 4：MCP 行为（打桩远端）====================
print("\n== 用例 4：MCP 行为")


def _call(args):
    r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": "tripplanner_find_pois", "arguments": args}})
    assert r and "result" in r, r
    return r["result"]


fake = {"ok": True, "count": 2, "total": 7, "cities": ["杭州"], "unknown": [],
        "keyword": "西湖", "category": "",
        "pois": [
            {"id": "HZ001", "name": "断桥残雪·白堤", "city": "杭州", "category": "nature",
             "tags": ["西湖", "步行"], "duration_h": 1.5, "best_time": "morning",
             "open": "00:00", "close": "23:59", "price": 0, "rating": 5,
             "family_ok": True, "closed_days": [], "note": "西湖必打卡起点"},
            {"id": "HZ020", "name": "浙江省博物馆", "city": "杭州", "category": "culture",
             "tags": ["文物"], "duration_h": 2.0, "best_time": "afternoon",
             "open": "09:00", "close": "17:00", "price": 0, "rating": 4,
             "family_ok": True, "closed_days": ["周一"], "note": "周一闭馆"},
        ]}

srv.remote_request = lambda path, **kw: fake
res = _call({"city": "杭州", "keyword": "西湖"})
sc = res.get("structuredContent") or {}
txt = "".join(c.get("text", "") for c in (res.get("content") or []))
case("4a 正常调用：回结构化 POI 清单 + 计数",
     sc.get("ok") and sc.get("count") == 2 and len(sc.get("pois") or []) == 2
     and sc.get("total") == 7, str({k: sc.get(k) for k in ("ok", "count", "total")}))
case("4b 文本回执：含 id / 名称 / 时长 / 闭馆日 / 亲子 / 标签",
     all(k in txt for k in ("HZ001", "断桥残雪", "1.5h", "闭馆", "亲子OK", "西湖")), txt[:120])
case("4c 显示已截断时说明总数（total > count）",
     ("命中 7 个" in txt) and ("只显示前 2 个" in txt), txt.splitlines()[0] if txt else "")

srv.remote_request = lambda path, **kw: {"ok": False, "error": "未收录的城市：火星",
                                        "supported": ["杭州", "苏州"]}
res = _call({"city": "火星"})
sc = res.get("structuredContent") or {}
txt = "".join(c.get("text", "") for c in (res.get("content") or []))
case("4d 后端报错：ok=False 且把 supported 清单带给模型",
     sc.get("ok") is False and "杭州" in txt and "火星" in txt, txt[:100])

srv.remote_request = lambda path, **kw: {"ok": True, "count": 0, "total": 0,
                                        "cities": ["杭州"], "unknown": [], "pois": []}
res = _call({"city": "杭州", "keyword": "xyzzy"})
sc = res.get("structuredContent") or {}
txt = "".join(c.get("text", "") for c in (res.get("content") or []))
case("4e 无匹配：ok=True、count=0，并给出下一步指引",
     sc.get("ok") is True and sc.get("count") == 0 and "关键词" in txt, txt[:80])

res = _call({})
sc = res.get("structuredContent") or {}
case("4f 全空参数：报错 + 指引（不是空跑一次全库）",
     sc.get("ok") is False and "city" in str(sc.get("error")), str(sc.get("error"))[:80])


def _raise(path, **kw):
    raise RuntimeError("无法连接后端 http://x（URLError）")


srv.remote_request = _raise
res = _call({"city": "杭州"})
sc = res.get("structuredContent") or {}
case("4g 连不上后端：ok=False（绝不静默回空清单）",
     sc.get("ok") is False and "无法连接" in str(sc.get("error")), str(sc.get("error"))[:60])

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：")
    for f in FAIL:
        print("   -", f)
    raise SystemExit(1)
print("✅ 全部通过（Phase 0：POI 发现 —— 云端 /api/pois + MCP find_pois）")
