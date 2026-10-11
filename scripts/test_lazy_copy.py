#!/usr/bin/env python3
"""Phase 3 文案懒生成（copy_mode=lazy）守卫。

背景（2026-10-11 Kenny）：重拍行程时每天文案全额重生成（占总 LLM 调用 ~45%），
重拍 N 次就白付 N 遍。方案 = 文案懒生成：重拍/探索轮 copy_mode=lazy 跳过文案
（compose 阶段4），定稿那次 open_plan 自动 POST /api/copy 对最终行程补跑。

四组用例：
  A 内核 —— compose(skip_copy) 零文案 LLM / theme 保留 / reason·tips 空 / copy_mode 落账
  B plan_view —— copyMode 透传 / arrive 透传（补文案的「早到等待」事实需要）
  C webui —— copy_mode 解析与 400 分支 / lazy 绕过 LLM 缓存 / POST /api/copy 端点
  D MCP —— schema/description 引导 / run_plan 透传 / open_plan 自动补文案 / 幂等 / 摘要标注

用法：python scripts/test_lazy_copy.py   （CI 友好，非 0 退出码）
"""
import importlib.util
import inspect
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL = []
N = 0


def case(name, cond, detail=""):
    global N
    N += 1
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


print("== 用例 A：内核 compose(skip_copy)")
from src import m2_planner, poi_db  # noqa: E402

_city = poi_db.load_city("苏州")
_orig_chat = m2_planner.llm_client.chat
_calls = {"n": 0}


def _fake_chat(messages, **kw):
    _calls["n"] += 1
    # REGEN_PROMPT 期望 JSON：{"days":[{"day":N,"theme":…,"reason":…,"tips":[…]}]}
    day = 1
    try:
        import re as _re
        m = _re.search(r"Day (\d+)：", messages[-1]["content"])
        if m:
            day = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return json.dumps({"days": [{"day": day, "theme": f"假主题D{day}",
                                 "reason": f"假理由D{day}", "tips": ["假tip"]}]},
                      ensure_ascii=False)


_dm = {}
_picked = []
for p in _city["pois"]:
    pp = poi_db.parse_poi(p, _city)
    if pp.get("category") in ("景点", "风景名胜") or "园林" in str(pp.get("name", "")):
        _picked.append(pp["id"])
    if len(_picked) >= 4:
        break
if len(_picked) < 2:  # 兜底：拿前几个 id
    _picked = [poi_db.parse_poi(p, _city)["id"] for p in _city["pois"][:4]]
_dm[1] = _picked[:4]
_dm[2] = _picked[1:4]
_themes = {1: {"theme": "提案主题一"}, 2: {"theme": "提案主题二"}}

# A1 skip_copy=True：零 LLM 文案调用
m2_planner.llm_client.chat = _fake_chat
_calls["n"] = 0
try:
    r_lazy = m2_planner.compose(_city, "苏州2天园林", 2, _dm, dict(_themes),
                                use_llm=True, skip_copy=True)
    case("A1 skip_copy=True → 文案 LLM 零调用", _calls["n"] == 0, f"calls={_calls['n']}")
except Exception as e:  # noqa: BLE001
    case("A1 skip_copy=True → 文案 LLM 零调用", False, f"{type(e).__name__}: {e}")
finally:
    m2_planner.llm_client.chat = _orig_chat

# A2 lazy 落账 + theme 保留 + reason/tips 空
case("A2 copy_mode=lazy 落账", r_lazy.get("copy_mode") == "lazy")
_d1 = next(d for d in r_lazy["itinerary"]["days"] if d["day"] == 1)
case("A3 theme 沿用提案值（不空）", _d1.get("theme") == "提案主题一", repr(_d1.get("theme")))
case("A4 reason/tips 留空", _d1.get("reason") == "" and _d1.get("tips") == [])

# A5 full（默认）路径照旧：文案 LLM 被调用 + copy_mode=full
m2_planner.llm_client.chat = _fake_chat
_calls["n"] = 0
try:
    r_full = m2_planner.compose(_city, "苏州2天园林", 2, _dm, dict(_themes),
                                use_llm=True)
    case("A5 默认路径 → 文案 LLM 照常调用", _calls["n"] >= 1, f"calls={_calls['n']}")
    case("A6 默认路径 copy_mode=full", r_full.get("copy_mode") == "full")
    _f1 = next(d for d in r_full["itinerary"]["days"] if d["day"] == 1)
    case("A7 默认路径文案被写入", _f1.get("reason") != "")
except Exception as e:  # noqa: BLE001
    case("A5 默认路径 → 文案 LLM 照常调用", False, f"{type(e).__name__}: {e}")
finally:
    m2_planner.llm_client.chat = _orig_chat

# A8 proposal_planner.plan 接受 skip_copy（签名存在）
case("A8 proposal_planner.plan 签名含 skip_copy",
     "skip_copy" in inspect.signature(
         __import__("src.proposal_planner", fromlist=["plan"]).plan).parameters)

print("\n== 用例 B：plan_view 透传")
from src import plan_view  # noqa: E402

_raw = {"mode": "m7_proposal", "query": "q", "days": 1, "copy_mode": "lazy",
        "latency_s": 1.0, "total_travel_km": 0,
        "itinerary": {"days": [{"day": 1, "theme": "t", "weekday": "周一",
                                "finish": "18:00", "travel_km": 0, "travel_h": 0,
                                "timeline": [{"type": "poi", "id": "X1", "name": "点",
                                              "start": "09:00", "end": "11:00",
                                              "arrive": "08:50"}]}]}}
_np = plan_view.normalize_plan(_raw, "苏州")
case("B1 normalize_plan 透传 copyMode=lazy", _np.get("copyMode") == "lazy")
case("B2 缺 copy_mode → 默认 full",
     plan_view.normalize_plan({"mode": "m", "days": 1, "latency_s": 1,
                               "itinerary": {"days": []}}, "苏州").get("copyMode") == "full")
_tl = _np["dayList"][0]["timeline"][0]
case("B3 normalize_day poi 透传 arrive", _tl.get("arrive") == "08:50")

print("\n== 用例 C：webui /api/plan 接线 + /api/copy 端点")
_sv = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("C1 copy_mode 解析与非法值 400",
     'copy_mode = (q.get("copy_mode") or "full").strip().lower()' in _sv
     and "copy_mode 只支持 full/lazy" in _sv)
case("C2 lazy×mode!=m7 → 400", "copy_mode=lazy 仅支持 mode=m7" in _sv)
case("C3 lazy×多城 → 400", "copy_mode=lazy 暂不支持多城联游" in _sv)
case("C4 lazy×内核缺失 → 500", "懒文案规划内核不可用" in _sv)
case("C5 lazy 注入 skip_copy", '_plan_args["skip_copy"] = True' in _sv)
case("C6 lazy 绕过 LLM 缓存", 'copy_mode != "lazy"' in _sv)
case("C7 POST /api/copy 端点存在", '"/api/copy"' in _sv and "文案生成失败" in _sv)
case("C8 幂等分支（full 原样返回）", '"regenerated": False' in _sv)
case("C9 补完置 full + 会话回写",
     'plan["copyMode"] = "full"' in _sv and "_session_put(sess, plan)" in _sv)
case("C10 WEBUI_VERSION = m12", 'WEBUI_VERSION = "m12"' in _sv)

print("\n== 用例 D：MCP 工具契约与行为")
_spec = importlib.util.spec_from_file_location(
    "_tp_server_lazy", os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"))
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

_ptool = next(t for t in srv.TOOLS if t["name"] == srv.PLAN_TOOL)
_cm = _ptool["inputSchema"]["properties"].get("copy_mode") or {}
case("D1 schema copy_mode enum full/lazy", _cm.get("enum") == ["full", "lazy"])
case("D2 description 引导：重拍 lazy + 定稿自动补",
     "lazy" in _cm.get("description", "") and "定稿" in _cm.get("description", "")
     and "open_plan" in _cm.get("description", ""))
case("D3 PLAN_TOOL description 含 lazy 引导", "重拍 / 探索阶段可传" in _ptool["description"])

# D4 run_plan 透传 copy_mode
_captured = {}


def _fake_remote(path, params=None, payload=None, method="GET", timeout=180.0):
    _captured.update({"path": path, "params": params or {}, "method": method})
    return {"ok": True, "plan": {"city": "苏州", "days": 1, "mode": "m7_proposal",
                                 "dayList": [{"day": 1, "theme": "t", "reason": "",
                                              "timeline": [], "travel_km": 0}],
                                 "totalTravelKm": 0, "totalViolations": 0,
                                 "candidates": 0, "latency_s": 1,
                                 "copyMode": "lazy"}}


srv.remote_request = _fake_remote
srv.run_plan("苏州2天园林", 2, "苏州", None, None, copy_mode="lazy")
case("D4 run_plan 透传 copy_mode=lazy",
     _captured["params"].get("copy_mode") == "lazy")

# D5 open_plan 对 lazy 行程自动补文案
_orig_write = srv.write_plan_state
_written = {"n": 0}
srv.write_plan_state = lambda plan: _written.__setitem__("n", _written["n"] + 1)
srv.SESSION.last_plan = {"city": "苏州", "days": 1, "copyMode": "lazy",
                         "dayList": [{"day": 1, "theme": "t", "reason": "",
                                      "timeline": []}]}
_copy_hits = {"n": 0}


def _fake_remote2(path, params=None, payload=None, method="GET", timeout=180.0):
    if path == "/api/copy":
        _copy_hits["n"] += 1
        assert method == "POST" and payload.get("session")
        return {"ok": True, "regenerated": True, "days": [1],
                "plan": {"city": "苏州", "days": 1, "copyMode": "full",
                         "dayList": [{"day": 1, "theme": "t", "reason": "补好了",
                                      "timeline": []}]}}
    return {"ok": True, "plan": srv.SESSION.last_plan}


srv.remote_request = _fake_remote2
r_open = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": srv.OPEN_TOOL, "arguments": {}}})
case("D5 open_plan 对 lazy 行程调 POST /api/copy", _copy_hits["n"] == 1)
case("D6 补文案结果写回 last_plan + 磁盘",
     srv.SESSION.last_plan.get("copyMode") == "full"
     and srv.SESSION.last_plan["dayList"][0]["reason"] == "补好了"
     and _written["n"] >= 1)
_sc = r_open["result"]["structuredContent"]
case("D7 open_plan 返回补好文案的行程", _sc.get("plan", {}).get("copyMode") == "full")

# D8 幂等：full 行程 open 不调 /api/copy
srv.remote_request = _fake_remote
srv.SESSION.last_plan = {"city": "苏州", "days": 1, "copyMode": "full",
                         "dayList": [{"day": 1, "theme": "t", "reason": "有文案",
                                      "timeline": []}]}
srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": srv.OPEN_TOOL, "arguments": {}}})
case("D8 full 行程 open_plan 不调 /api/copy", _copy_hits["n"] == 1)

# D9 补文案失败不拦开面板
def _fake_remote3(path, params=None, payload=None, method="GET", timeout=180.0):
    if path == "/api/copy":
        raise RuntimeError("网络抖动")
    return {"ok": True, "plan": srv.SESSION.last_plan}


srv.remote_request = _fake_remote3
srv.SESSION.last_plan = {"city": "苏州", "days": 1, "copyMode": "lazy",
                         "dayList": [{"day": 1, "theme": "t", "reason": "",
                                      "timeline": []}]}
r_fail = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": srv.OPEN_TOOL, "arguments": {}}})
case("D9 补文案失败 → open_plan 照常返回（fail-open）",
     r_fail["result"]["structuredContent"].get("hasPlan") is True)

# D10 plan_summary lazy 标注
_sm = srv.plan_summary({"city": "苏州", "days": 1, "copyMode": "lazy", "dayList": [],
                        "latency_s": 1})
case("D10 摘要标注 lazy 模式与下一步", "文案懒生成" in _sm and "open_plan" in _sm)
_sm2 = srv.plan_summary({"city": "苏州", "days": 1, "dayList": [], "latency_s": 1})
case("D11 无 copyMode 旧数据摘要不受影响", "文案懒生成" not in _sm2)

case("D12 SERVER_VERSION = 0.5.7-m5", srv.SERVER_VERSION == "0.5.7-m5")

# ========== G 组：换点/挪时段后自动补当天文案（2026-10-11 报障） ==========
# 报障「点击更换 POI 点位后，当天文案在面板上消失了」——keep_day_meta 刻意只保
# theme（旧 reason/tips 逐点点名已过期），正解是按新时间轴重写而非清空了事。
_sv_txt = open(os.path.join(ROOT, "webui", "server.py"), encoding="utf-8").read()
case("G1 _regen_one_day_copy 定义（复用 _regen_reasons 单天口径）",
     "def _regen_one_day_copy(" in _sv_txt
     and 'only_days=[nd.get("day")]' in _sv_txt)
case("G2 /api/replace 接线（full 才补，lazy 跳过）",
     _sv_txt.count('_plan_in.get("copyMode") == "lazy"') >= 2
     and "entry = _regen_one_day_copy(" in _sv_txt)
case("G3 /api/move 对称接线（挪时段后旧「依次游览」同样过期）",
     _sv_txt.count("entry = _regen_one_day_copy(") == 2)
case("G4 fail-open：异常吞掉返回 None 不拖垮换点",
     "except Exception:  # noqa: BLE001\n        pass\n    return None" in _sv_txt)
case("G5 WEBUI_VERSION = m12", 'WEBUI_VERSION = "m12"' in _sv_txt)
# 行为级：_regen_one_day_copy 用归一化 day 真跑（打桩 LLM）
_ws = importlib.util.spec_from_file_location(
    "_webui_lazy_g", os.path.join(ROOT, "webui", "server.py"))
wsv = importlib.util.module_from_spec(_ws)
_ws.loader.exec_module(wsv)
import src.llm_client as _llcg  # noqa: E402
_orig_chat_g = _llcg.chat
_llcg.chat = lambda messages, **kw: json.dumps(
    {"days": [{"day": 1, "theme": "新主题", "reason": "新文案",
               "tips": ["新提示"]}]}, ensure_ascii=False)
try:
    _nd = plan_view.normalize_day(
        {"day": 1, "weekday": "周一", "finish": "17:00", "travel_km": 0,
         "travel_h": 0,
         "timeline": [{"type": "poi", "id": "SZ001", "name": "拙政园",
                       "start": "09:00", "end": "11:00", "arrive": "08:50"}]},
        plan_view.poi_coords("苏州"))
    _entry = wsv._regen_one_day_copy("苏州", "苏州园林", _nd)
    case("G6 行为级：归一化 day 输入 → 返回新文案 entry",
         isinstance(_entry, dict) and _entry.get("reason") == "新文案",
         json.dumps(_entry, ensure_ascii=False)[:120] if _entry else "None")
finally:
    _llcg.chat = _orig_chat_g

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{'、'.join(FAIL)}")
    sys.exit(1)
print(f"✅ 全部通过（{N} 项）")
