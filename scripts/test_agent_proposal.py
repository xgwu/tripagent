# -*- coding: utf-8 -*-
"""Phase 1 守卫：Agent-Proposal —— `plan_trip` 可选 `proposal` / `dry_run` 校验回执。

见 docs/MCP-Agent提案外包-方案探讨.md §6：调用方 agent 可自己拟逐日点位清单，
云端跳过自己的提案 LLM（省「重的那一次」），直接进入落地+修正闭环+求解。
**共存不替换**：不传 proposal ⇒ 一切照旧（服务端自己提案）。

本文件钉住四条：
  A. `proposal_planner.validate_proposal`：dry_run 校验回执（确定性口径与正式规划
     同一套 `_ground_one`）—— 命中 / 未命中+候选 / 跨天重复 / 天号越界 / 超员 / 空天；
  B. `plan()` 注入点：`agent_proposal` 参数存在、A 阶段跳过、`plan_source` 全路径落账；
  C. webui `/api/plan` 接线：POST body 合并（提案 JSON 可能远超 URL 长度）、
     dry_run 短路、`use_prop` 绕过 LLM 结果缓存、`planSource` 随归一化下发；
  D. MCP 工具面：schema 有 proposal/dry_run、契约写在 description、
     `plan_trip` 仍**不带** `_meta`（面板入口唯一，test_panel_single_open 的领地）、
     proposal 走 POST payload、dry_run 回执文本可读。

用法：python scripts/test_agent_proposal.py
"""
import importlib.util
import inspect
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src import poi_db, proposal_planner  # noqa: E402
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
    spec = importlib.util.spec_from_file_location("_tp_server_agent_proposal", server)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 隔离副作用：不落盘、不写诊断日志、不打网络。
    mod._plan_state_read = lambda: None
    mod._plan_state_write = lambda value: None
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
_real = [p for p in _hz if p.get("name")][:2]
case("0b 杭州库里有可用点名（用例素材）", len(_real) == 2,
     "、".join(p["name"] for p in _real))

# ==================== 用例 A：validate_proposal（dry_run 回执）====================
print("\n== 用例 A：validate_proposal")
city = poi_db.load_city("杭州")
n1, n2 = _real[0]["name"], _real[1]["name"]

# -- A 组 1：全命中 + 跨天重复（口径：命名点位才计数；跨天重复仍算命中但告警）
v = proposal_planner.validate_proposal(city, {"days": [
    {"day": 1, "stops": [{"name": n1}, {"name": n2}]},
    {"day": 2, "stops": [{"name": n1}]},          # 跨天重复
]}, 4)
case("A1 命中数正确（3 个命名点位全部命中）",
     v["n_matched"] == 3 and v["n_stops"] == 3,
     f"matched={v['n_matched']} stops={v['n_stops']}")
case("A2 落地率口径 = 命中/命名点位总数", v["grounding_rate"] == 1.0,
     str(v["grounding_rate"]))
case("A3 跨天重复有告警", any("跨天重复" in w for w in v["warnings"]))
case("A3b 全命中 ⇒ ok=True 且 unmatched 空",
     v["ok"] and v["unmatched"] == [])

# -- A 组 2：结构性告警 + 未命中
v = proposal_planner.validate_proposal(city, {"days": [
    {"day": 9, "stops": []},                       # 天号越界 + 空天
    {"day": 3, "stops": [{"name": "不存在的假景点XYZ"},
                         {},                       # 无名点位（不计数）
                         {"name": ""}]},           # 空名（不计数）
    {"day": 4, "stops": [{"name": f"点{i}"} for i in range(7)]},  # 超员 + 全未命中
]}, 4)
case("A4 未命中进 unmatched 且带 candidates 字段",
     len(v["unmatched"]) == 8
     and all(isinstance(u.get("candidates"), list) for u in v["unmatched"]),
     f"unmatched={len(v['unmatched'])}")
case("A5 天号越界有告警", any("超出范围" in w for w in v["warnings"]))
case("A6 空天有告警", any("没有任何点位" in w for w in v["warnings"]))
case("A7 无名/空名点位有告警且不计数",
     sum("的点位" in w for w in v["warnings"]) == 2 and v["n_stops"] == 8,
     f"stops={v['n_stops']}")
case("A8 单天超员有告警", any("个点" in w and "≤5" in w for w in v["warnings"]))
case("A9 全未命中 ⇒ 落地率 0、ok=False",
     v["grounding_rate"] == 0.0 and v["ok"] is False)
v2 = proposal_planner.validate_proposal(city, {"days": []}, 2)
case("A10 days 为空 → ok 且有指引告警", v2["days"] == 0 and len(v2["warnings"]) == 1)

# ==================== 用例 B：plan() 注入点 ====================
print("\n== 用例 B：plan() 注入点")
_sig = inspect.signature(proposal_planner.plan)
case("B1 plan() 有 agent_proposal 参数", "agent_proposal" in _sig.parameters)
_src = open(os.path.join(ROOT, "src", "proposal_planner.py"), encoding="utf-8").read()
case("B2 agent 提案 → 跳过 A 阶段 LLM（agent_days 分支）",
     _src.count('agent_days = (agent_proposal or {}).get("days") or []') == 1)
case("B3 plan_source 全路径落账（agent/server/兜底）",
     _src.count('r["plan_source"]') == 4, f"count={_src.count(chr(114)+'[\"plan_source\"]')}")
case("B4 成功路径 plan_source = agent|server",
     'r["plan_source"] = source' in _src)
_pv = open(os.path.join(ROOT, "src", "plan_view.py"), encoding="utf-8").read()
case("B5 normalize_plan 透传 planSource", '"planSource": raw.get("plan_source")' in _pv)

# ==================== 用例 C：webui /api/plan 接线 ====================
print("\n== 用例 C：webui 接线")
case("C1 WEBUI_VERSION = m12（换点补文案）", sv.WEBUI_VERSION == "m12", sv.WEBUI_VERSION)
_sv = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("C2 do_POST 合并 /api/plan 的 JSON body（提案走 POST，不受 URL 长度限制）",
     'if u.path == "/api/plan":' in _sv and "return self.do_GET()" in _sv)
case("C3 proposal 参数解析 + 坏 JSON 报 400 + 数组宽容归一化",
     _sv.count('proposal 不是合法 JSON') == 1
     and 'proposal 需要是数组（逐天对象）或含 days 数组的对象' in _sv
     and '_p = {"days": _p}' in _sv)
case("C4 proposal_mode 白名单 + agent 必带 proposal",
     'proposal_mode 只支持 auto/server/agent' in _sv
     and 'proposal_mode=agent 需要同时传 proposal' in _sv)
case("C5 dry_run 短路返回 validate_proposal（不规划）",
     'proposal_planner.validate_proposal(' in _sv and 'dry_run=1 需要同时传 proposal' in _sv)
case("C6 use_prop 绕过 LLM 结果缓存（提案每次都可能不同）",
     "not llm_key and not use_prop" in _sv)
case("C7 agent_proposal 经 kw 透传（不用全局变量传请求态）",
     '_plan_args["agent_proposal"] = proposal_data' in _sv)
case("C8 多城联游显式拒绝 proposal（不静默忽略）",
     "多城联游暂不支持 proposal" in _sv)
case("C9 mode!=m7 / 内核缺失时显式报错（不静默丢提案）",
     "proposal 仅支持 mode=m7" in _sv and "提案规划内核不可用" in _sv)

# ==================== 用例 D：MCP 工具面 ====================
print("\n== 用例 D：MCP 工具面")
srv = _load_mcp()
case("D1 SERVER_VERSION = 0.5.7-m5（面板导出按钮摘除）",
     srv.SERVER_VERSION == "0.5.7-m5", srv.SERVER_VERSION)
_plan_tool = next(t for t in srv.TOOLS if t["name"] == srv.PLAN_TOOL)
_props = _plan_tool["inputSchema"]["properties"]
case("D2 schema 有 proposal（array，item 必填 day+stops）",
     _props.get("proposal", {}).get("type") == "array"
     and _props["proposal"]["items"].get("required") == ["day", "stops"])
case("D3 schema 有 dry_run（boolean）",
     _props.get("dry_run", {}).get("type") == "boolean")
case("D4 required 仍只有 query（proposal 可选 = 共存不替换）",
     _plan_tool["inputSchema"].get("required") == ["query"])
case("D5 plan_trip 仍不带 _meta（面板入口唯一，M6.3 领地）",
     "_meta" not in _plan_tool)
case("D6 description 写明契约：先 find_pois / dry_run 校验 / 不传走旧路径",
     "tripplanner_find_pois" in _plan_tool["description"]
     and "dry_run" in _plan_tool["description"]
     and "不传 proposal" in _plan_tool["description"])
case("D7 run_plan 支持 proposal/dry_run 并走 POST",
     "proposal" in inspect.signature(srv.run_plan).parameters
     and 'payload = {"proposal": proposal or []}' in
     open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
          encoding="utf-8").read())

# ---- MCP 行为（打桩远端，不打网络）----
print("\n== 用例 E：MCP 行为（打桩远端）")
_captured = {}


def _fake_remote(path, params=None, payload=None, method="GET", timeout=180.0):
    _captured["path"], _captured["payload"], _captured["method"] = path, payload, method
    return {"ok": True, "dry_run": True, "validation": {
        "ok": False, "days": 2, "n_stops": 3, "n_matched": 2, "grounding_rate": 0.667,
        "unmatched": [{"name": "不存在的假景点XYZ", "day": 2,
                       "candidates": [{"id": _real[0]["id"], "name": _real[0]["name"]}]}],
        "warnings": ["Day 2 排了 8 个点（建议 ≤5，超出的更可能被里程/时间约束剔除）"],
        "note": "匹配口径与正式规划一致"}}


srv.remote_request = _fake_remote


def _call(args):
    r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": srv.PLAN_TOOL, "arguments": args}})
    assert r and "result" in r, r
    return r["result"]


_prop = [{"day": 1, "stops": [{"id": "HZ001"}]},
         {"day": 2, "stops": [{"name": "西湖"}, {"name": "不存在的假景点XYZ"}]}]
res = _call({"query": "杭州2天", "proposal": _prop, "dry_run": True})
sc = res.get("structuredContent") or {}
txt = "\n".join(c.get("text", "") for c in (res.get("content") or []))
case("E1 dry_run 走 POST 且 body 带 proposal",
     _captured["method"] == "POST" and (_captured["payload"] or {}).get("proposal") == _prop
     and (_captured["payload"] or {}).get("dry_run") == "1",
     f"method={_captured['method']}")
case("E2 回执结构化：ok=False + unmatched 透传",
     sc.get("dry_run") and sc.get("ok") is False
     and sc["validation"]["unmatched"][0]["name"] == "不存在的假景点XYZ")
case("E3 回执文本：命中数 / ❌ 未命中+候选 / ⚠️ 告警 / 下一步指引",
     "2/3" in txt and "不存在的假景点XYZ" in txt and "候选" in txt
     and "≤5" in txt and "dry_run" in txt)
case("E4 dry_run 不碰面板状态（opened_any 不变）",
     srv.SESSION.opened_any is False)

# 无 validation 字段的坏回执 → 不崩
srv.remote_request = lambda path, **kw: {"ok": True}
res = _call({"query": "杭州2天", "dry_run": True})
case("E5 云端缺 validation 字段 → 优雅空回执不崩",
     (res.get("structuredContent") or {}).get("validation") == {})

# ==================== 用例 F：观测（徽标 / 摘要）====================
# 报障（2026-10-11 Kenny）：宿主侧看不到 planSource=agent 徽标 —— planSource 只落账
# 不渲染＝观测缺陷。徽标挂面板 mode 徽标旁 + 摘要文本「提案来源=…」。
print("\n== 用例 F：观测（面板徽标 / plan_summary）")
_html = open(os.path.join(ROOT, "mcp", "panel", "plan_panel.html"),
             encoding="utf-8").read()
case("F1 面板徽标：planSource=agent → 「Agent 提案」",
     'String(plan.planSource || "") === "agent"' in _html
     and "Agent 提案</span>" in _html
     and "modeBadge + srcBadge + violBadge" in _html)
case("F2 PANEL_VERSION 已 +1（= m21）", 'PANEL_VERSION = "m21"' in
     open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
          encoding="utf-8").read())
case("F3 plan_summary 文本带「提案来源=」", "提案来源=" in
     inspect.getsource(srv.plan_summary))
_sm = srv.plan_summary({"city": "杭州", "days": 2, "dayList": [], "latency_s": 1,
                        "planSource": "agent"})
case("F4 摘要：agent → 「调用方 agent」", "提案来源=调用方 agent" in _sm)
_sm2 = srv.plan_summary({"city": "杭州", "days": 2, "dayList": [], "latency_s": 1})
case("F5 摘要：旧数据无字段 → 「-」不崩", "提案来源=-" in _sm2)

# ==================== 用例 G：引导通道（instructions / description / prompts）====================
# Phase 2.5（2026-10-11）：让任何宿主 agent 自动走提案路径 —— 三层引导：
#   initialize.instructions（注入系统提示，零配置）+ PLAN_TOOL description 硬化 +
#   prompts plan-trip 模板（显式入口）。
print("\n== 用例 G：引导通道（自动走提案路径）")
_init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
_init_res = _init["result"]
case("G1 initialize 响应含 instructions 且点出提案工作流",
     "instructions" in _init_res
     and "find_pois" in _init_res["instructions"]
     and "proposal" in _init_res["instructions"]
     and "tripplanner_open_plan" in _init_res["instructions"])
case("G2 capabilities 声明 prompts",
     "prompts" in _init_res.get("capabilities", {}))
_pl = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}})
_pnames = [p.get("name") for p in _pl["result"].get("prompts", [])]
case("G3 prompts/list 提供 plan-trip 模板", "plan-trip" in _pnames)
_pg = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                  "params": {"name": "plan-trip",
                             "arguments": {"query": "苏州2天园林"}}})
_msgs = _pg["result"]["messages"]
_ptext = _msgs[0]["content"]["text"] if _msgs else ""
case("G4 prompts/get 返回消息且正文含完整工作流",
     _msgs and _msgs[0]["role"] == "user"
     and "find_pois" in _ptext and "proposal" in _ptext
     and "dry_run" in _ptext and "open_plan" in _ptext)
case("G5 prompts/get 未知名 → -32602",
     srv.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                 "params": {"name": "nope"}}).get("error", {}).get("code") == -32602)
_ptool = next(t for t in srv.TOOLS if t["name"] == srv.PLAN_TOOL)
_pd = _ptool["description"]
case("G6 PLAN_TOOL description：默认路径话术 + 量化激励 + 退回条件",
     "默认应该走这条路" in _pd and "56%" in _pd
     and "仅当" in _pd and "慢速默认路径" in _pd)
case("G7 SERVER_VERSION = 0.5.7-m5", srv.SERVER_VERSION == "0.5.7-m5")

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{'、'.join(FAIL)}")
    sys.exit(1)
print("✅ 全部通过")
