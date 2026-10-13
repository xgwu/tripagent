# -*- coding: utf-8 -*-
"""MCP 并发守卫（2026-10-12，P0 后续：去掉全串行锁，改有界并发）。

背景：`mcp_endpoint._handle_one` 曾用 `_exec_lock` **全串行**（理由：`handle()` 读模块
全局 `SESSION`）。P0 修复把 `SESSION` 改成线程局部代理后该理由消失，锁改为
**有界并发闸**（`_gate` Semaphore，默认 8）：重工具过闸、轻工具直放。

本文件钉住四条：
  A. 并发真相：3 个重工具并发 ⇒ **实质并行**（服务端打点测，不用客户端耗时）
  B. 闸语义：轻工具（ping/get_plan/…）**不过闸**；重工具/未知工具**过闸**
  C. 闸上限：并发超上限时**等待**（不丢请求），上限内全放行
  D. 会话隔离在并发下仍成立（每个线程各读各的）

用法：python scripts/test_mcp_concurrency.py
"""
import importlib.util
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FAIL = []


def case(name, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {name}{('｜' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


spec = importlib.util.spec_from_file_location(
    "_mcp_ep_conc", os.path.join(ROOT, "webui", "mcp_endpoint.py"))
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)
m = ep._load()
ep._load()   # ensure loaded


# ---- A：并发真相（服务端打点）----
# 用「同刻进入 handle 的请求数」判定并行：handle 内记录并发峰值 + 各自 sleep
_in_flight = {"cur": 0, "peak": 0}
_lock = threading.Lock()
_orig_handle = m.handle


def _fake_handle(msg):
    with _lock:
        _in_flight["cur"] += 1
        _in_flight["peak"] = max(_in_flight["peak"], _in_flight["cur"])
    try:
        time.sleep(0.6)      # 模拟重工具服务端耗时
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}
    finally:
        with _lock:
            _in_flight["cur"] -= 1


m.handle = _fake_handle

N = 4
t0 = time.time()
th = [threading.Thread(target=lambda i=i: ep.dispatch(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "tripplanner_plan_trip",
                               "arguments": {"query": "x"}}}).encode(),
        f"sess-c{i}"))
      for i in range(N)]
for t in th:
    t.start()
for t in th:
    t.join()
wall = time.time() - t0
m.handle = _orig_handle

case(f"A1 {N} 个重工具并发 ⇒ 服务端并发峰值 = {N}（真并行）",
     _in_flight["peak"] == N, f"peak={_in_flight['peak']}")
case(f"A2 墙钟≈{0.6:.1f}s（并行）而非 {N*0.6:.1f}s（串行）",
     wall < 0.6 * 2, f"墙钟={wall:.2f}s")

# ---- B：闸语义（哪些工具过闸）----
def _needs(name_or_method, is_tool=True):
    if is_tool:
        return ep._needs_gate({"method": "tools/call", "params": {"name": name_or_method}})
    return ep._needs_gate({"method": name_or_method})


case("B1 重工具 plan_trip 过闸", _needs("tripplanner_plan_trip") is True)
case("B2 重工具 open_plan 过闸", _needs("tripplanner_open_plan") is True)
case("B3 重工具 replace_poi 过闸", _needs("tripplanner_replace_poi") is True)
case("B4 重工具 move_poi 过闸", _needs("tripplanner_move_poi") is True)
case("B5 重工具 export 过闸", _needs("tripplanner_export") is True)
case("B6 轻工具 ping **不过闸**", _needs("tripplanner_ping") is False)
case("B7 轻工具 get_plan **不过闸**", _needs("tripplanner_get_plan") is False)
case("B8 轻工具 list_cities **不过闸**", _needs("tripplanner_list_cities") is False)
case("B9 轻工具 panel_ready **不过闸**", _needs("tripplanner_panel_ready") is False)
case("B10 未知工具**默认过闸**（fail-safe）", _needs("some_new_tool") is True)
case("B11 协议方法 initialize 不过闸", _needs("initialize", is_tool=False) is False)
case("B12 协议方法 tools/list 不过闸", _needs("tools/list", is_tool=False) is False)

# ---- C：闸上限（并发超限等待，不丢请求）----
_orig_gate = ep._gate
ep._gate = threading.Semaphore(2)      # 临时收紧到 2
_active = {"cur": 0, "peak": 0}
_lk2 = threading.Lock()


def _slow_handle(msg):
    with _lk2:
        _active["cur"] += 1
        _active["peak"] = max(_active["peak"], _active["cur"])
    try:
        time.sleep(0.3)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}
    finally:
        with _lk2:
            _active["cur"] -= 1


m.handle = _slow_handle
_done = {"n": 0}


def _one(i):
    ep.dispatch(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                            "params": {"name": "tripplanner_plan_trip",
                                       "arguments": {}}}).encode(), f"s{i}")
    with _lk2:
        _done["n"] += 1


th2 = [threading.Thread(target=_one, args=(i,)) for i in range(6)]
for t in th2:
    t.start()
for t in th2:
    t.join()
m.handle = _orig_handle
ep._gate = _orig_gate

case("C1 闸上限 2 时服务端并发峰值 ≤2（闸生效）",
     _active["peak"] <= 2, f"peak={_active['peak']}")
case("C2 超上限的请求**等待后全部完成**（不丢请求）",
     _done["n"] == 6, f"完成 {_done['n']}/6")

# ---- D：并发下会话隔离仍成立 ----
# ⚠️ 注意：`_ensure_session` 对**未知 sid 会新建**（宽松设计，防 SDK 重连卡死）⇒
#   dispatch 传 "sess-iso-N" 会各建一个新会话，`SESSION.sid` 是**新 uuid**（正确行为）。
#   故判据不是「等于传入值」，而是**各请求看到自己的会话**：每个线程返回的 sid
#   必须与该请求**外部回传的 session_id 一致**，且互不相同（无串会话）。
_seen = {}
_lk3 = threading.Lock()


def _iso_handle(msg):
    time.sleep(0.1)
    sid = m.SESSION.sid           # 本线程绑定的会话
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"sid": sid}}


m.handle = _iso_handle
outs = {}


def _iso_one(i):
    body = json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                       "params": {"name": "tripplanner_plan_trip", "arguments": {}}}).encode()
    res = ep.dispatch(body, f"sess-iso-{i}")
    got_sid = json.loads(res["body"].decode())["result"]["sid"]
    # 该请求**自己**的会话 id 由 out["session_id"] 回传（未知 sid 会新建一个）
    outs[i] = (got_sid, res["session_id"])


th3 = [threading.Thread(target=_iso_one, args=(i,)) for i in range(4)]
for t in th3:
    t.start()
for t in th3:
    t.join()
m.handle = _orig_handle

# 判据一：每个线程内，handle 看到的 sid == 该请求回传的 session_id（同一会话，无错绑）
same = all(got == given for got, given in outs.values())
# 判据二：4 个线程的 sid 互不相同（无跨线程串会话）
distinct = len({got for got, _ in outs.values()}) == 4
case("D1 并发下每个请求看到**自己的**会话（线程隔离成立）",
     same and distinct,
     f"自洽={same} 互异={distinct} :: {[g[:8] for g, _ in outs.values()]}")

print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过（并发 + 闸语义 + 上限 + 隔离）")
