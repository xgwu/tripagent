# -*- coding: utf-8 -*-
"""Agent-Proposal 守卫（Phase 1 内核能力保留 / 对外入口已退役）。

⚠️ 2026-10-11 Kenny：「彻底移除」——`plan_trip` 的 `proposal` / `dry_run`
**对外参数已删除**（SERVER 0.5.9-m5）。原因：撤引导（e361571）不足以杜绝 —— 宿主
按 `appDefinitionSignature` 缓存工具定义，`listTools` 又可能超时，只要 schema 里还有
proposal，agent 就有机会走提案（实测复发）。现在 = **agent 只理解需求 + 调 plan_trip**，
提案一律由服务端自己做。

内核与云端能力**保留**（服务端提案走同一条 `proposal_planner.plan`；未来若要恢复只需
在 MCP 层加回参数）：
  A. `proposal_planner.validate_proposal`：校验回执（确定性口径与正式规划同一套
     `_ground_one`）—— 命中 / 未命中+候选 / 跨天重复 / 天号越界 / 超员 / 空天；
  B. `plan()` 注入点：`agent_proposal` 参数存在、A 阶段跳过、`plan_source` 全路径落账；
  C. webui `/api/plan` 接线：POST body 合并、`use_prop` 绕过 LLM 结果缓存、
     `planSource` 随归一化下发。
**对外入口已退役（反向钉住，防复发）**：
  D. MCP 工具面：schema **无** proposal/dry_run、description 无提案话术、
     `plan_trip` 仍**不带** `_meta`（面板入口唯一，test_panel_single_open 的领地）；
  E. MCP 行为：**硬传** proposal/dry_run 也被无视（仍走 GET、payload 为空）；
  F. 观测：面板徽标 / plan_summary「提案来源=」；G. 无引导（无 instructions 硬化、
     无 prompts 能力）。

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
case("C1 WEBUI_VERSION = m14（飞机段）", sv.WEBUI_VERSION == "m14", sv.WEBUI_VERSION)
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

# ==================== 用例 D：MCP 工具面（proposal/dry_run 对外已退役）====================
# 2026-10-11 Kenny：「彻底移除」—— 宿主可能持缓存的旧工具定义、listTools 也可能超时，
# 只要对外 schema 里还有 proposal，agent 就有机会走提案。故从 schema 直接拿掉。
print("\n== 用例 D：MCP 工具面（proposal/dry_run 对外已退役）")
srv = _load_mcp()
case("D1 SERVER_VERSION = 0.6.1-m5（移除 proposal/dry_run）",
     srv.SERVER_VERSION == "0.6.1-m5", srv.SERVER_VERSION)
_plan_tool = next(t for t in srv.TOOLS if t["name"] == srv.PLAN_TOOL)
_props = _plan_tool["inputSchema"]["properties"]
case("D2 schema 已无 proposal（agent 无从传提案）", "proposal" not in _props)
case("D3 schema 已无 dry_run", "dry_run" not in _props)
case("D4 schema 保留 copy_mode（lazy 快路径仍在）",
     _props.get("copy_mode", {}).get("enum") == ["full", "lazy"])
case("D5 required 仍只有 query", _plan_tool["inputSchema"].get("required") == ["query"])
case("D6 plan_trip 仍不带 _meta（面板入口唯一，M6.3 领地）",
     "_meta" not in _plan_tool)
case("D7 description 已无 proposal/dry_run 话术（仍保留 copy_mode 引导）",
     "proposal" not in _plan_tool["description"]
     and "dry_run" not in _plan_tool["description"]
     and "copy_mode" in _plan_tool["description"])
case("D8 run_plan 签名已无 proposal/dry_run",
     "proposal" not in inspect.signature(srv.run_plan).parameters
     and "dry_run" not in inspect.signature(srv.run_plan).parameters)
case("D9 源码里不再出现 proposal 走 POST 的 payload 构造（彻底清理）",
     'payload = {"proposal": proposal or []}' not in
     open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
          encoding="utf-8").read())

# ---- MCP 行为（打桩远端，不打网络）：硬传 proposal/dry_run 也必须被无视 ----
print("\n== 用例 E：MCP 行为 —— 硬传 proposal/dry_run 也不生效（打桩远端）")
_captured = {}


def _fake_remote(path, params=None, payload=None, method="GET", timeout=180.0):
    _captured["path"], _captured["params"] = path, params
    _captured["payload"], _captured["method"] = payload, method
    return {"ok": True, "plan": {
        "city": "杭州", "days": 2, "mode": "m7_proposal", "planSource": "server",
        "latency_s": 1.0, "totalTravelKm": 0.0, "totalViolations": 0,
        "generatedAt": "2026-10-11 13:40:00", "query": "杭州2天",
        "dayList": [{"day": 1, "weekday": "周一", "theme": "西湖", "reason": "r",
                     "tips": "t", "finish": "18:00", "timeline": []}]}}


srv.remote_request = _fake_remote


def _call(args):
    r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": srv.PLAN_TOOL, "arguments": args}})
    assert r and "result" in r, r
    return r["result"]


_prop = [{"day": 1, "stops": [{"id": "HZ001"}]},
         {"day": 2, "stops": [{"name": "西湖"}]}]
res = _call({"query": "杭州2天", "proposal": _prop, "dry_run": True})
sc = res.get("structuredContent") or {}
case("E1 硬传 proposal/dry_run → 仍走 GET 且 payload 为空（参数已不被消费）",
     _captured["method"] == "GET" and _captured["payload"] is None,
     f"method={_captured['method']} payload={_captured['payload']}")
case("E2 URL 参数里也不含 proposal/dry_run",
     "proposal" not in (_captured["params"] or {})
     and "dry_run" not in (_captured["params"] or {}))
case("E3 正常返回行程（不再有 dry_run 回执分支）",
     sc.get("ok") is True and bool(sc.get("plan")) and "dry_run" not in sc,
     f"sc_keys={sorted(sc.keys())}")
case("E4 不碰面板状态（opened_any 不变）", srv.SESSION.opened_any is False)

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
# ⚠️ 版本号随面板改动递增，本用例是「改了面板要记得 +1」的检查器，不是钉死某一版。
#   2026-10-13 面板改到 m22（移除行程种子 + 会话改为运行时索取）⇒ 同步期望。
case("F2 PANEL_VERSION 已 +1（= m24）", 'PANEL_VERSION = "m24"' in
     open(os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"),
          encoding="utf-8").read())
case("F3 plan_summary 文本带「提案来源=」", "提案来源=" in
     inspect.getsource(srv.plan_summary))
_sm = srv.plan_summary({"city": "杭州", "days": 2, "dayList": [], "latency_s": 1,
                        "planSource": "agent"})
case("F4 摘要：agent → 「调用方 agent」", "提案来源=调用方 agent" in _sm)
_sm2 = srv.plan_summary({"city": "杭州", "days": 2, "dayList": [], "latency_s": 1})
case("F5 摘要：旧数据无字段 → 「-」不崩", "提案来源=-" in _sm2)

# ==================== 用例 G：引导通道已撤（默认走服务端提案）====================
# 2026-10-11 回退（Kenny）：Phase 2.5 曾用三层引导（instructions 硬化 + PLAN_TOOL
# description 硬化 + prompts plan-trip 模板）把宿主 agent 推向「agent 提案」路径，
# **实测效果不佳** ⇒ 撤掉引导、回到「agent 只理解需求 + 调 plan_trip、服务端自己提案」。
# 以下守卫**反向**钉住「没有引导」，防有人再把引导加回来。
print("\n== 用例 G：引导通道已撤（默认走服务端提案）")
_init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
_init_res = _init["result"]
_instr = _init_res.get("instructions") or ""
case("G1 instructions 仍是中性总纲（点出 open_plan 定稿节律）",
     "tripplanner_open_plan" in _instr and "定稿" in _instr)
case("G2 instructions 不再引导 agent 自拟提案（无提案话术/量化激励）",
     "优先" not in _instr and "自己拟" not in _instr and "56%" not in _instr)
case("G3 instructions 明说「服务端会自己完成点位提案」",
     "服务端会自己完成" in _instr)
case("G4 capabilities 不再声明 prompts",
     "prompts" not in _init_res.get("capabilities", {}))
case("G5 prompts/list → -32601（方法已移除）",
     srv.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/list",
                 "params": {}}).get("error", {}).get("code") == -32601)
case("G6 prompts/get → -32601（方法已移除）",
     srv.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                 "params": {"name": "plan-trip"}}).get("error", {}).get("code") == -32601)
_ptool = next(t for t in srv.TOOLS if t["name"] == srv.PLAN_TOOL)
_pd = _ptool["description"]
case("G7 PLAN_TOOL 描述不再硬化（无「默认应该走这条路」/「56%」）",
     "默认应该走这条路" not in _pd and "56%" not in _pd)
case("G8 PLAN_TOOL 描述已无任何提案话术（proposal/dry_run 全清）",
     "proposal" not in _pd and "dry_run" not in _pd)
case("G9 SERVER_VERSION = 0.6.1-m5", srv.SERVER_VERSION == "0.6.1-m5")

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{'、'.join(FAIL)}")
    sys.exit(1)
print("✅ 全部通过")
