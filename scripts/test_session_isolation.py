# -*- coding: utf-8 -*-
"""P0 会话隔离守卫 + 红队（2026-10-12）。

背景（Kenny 实测，跨用户行程泄露）：
  A 用户规划「贵州5日」→ B 用户规划「杭州2日」→ A 的面板显示**杭州**。
  根因：① `session_id()` 从**全局单文件**读（所有用户共用一把钥匙）；
        ② `last_plan` 落盘是**全局单文件**（写一次覆盖所有人）；
        ③ `SESSION` 是**模块级全局**，`mcp_endpoint` 每请求覆盖它（且 proxy 缺
           `__setattr__` 时赋值会落在共享对象上）。
  修法：`_Session` 自带 sid + 落盘按会话分文件 + `SESSION` 改线程局部代理 + `bind()`。

本文件三条：
  A. 会话隔离（正向）：两个会话各自写/读，**互不可见**。
  B. 红队：模拟「A 写 → B 读」的攻击序列，断言 B 拿不到 A 的行程。
  C. 落盘文件隔离：不同 sid → 不同文件（且不落在旧全局单文件上）。

用法：python scripts/test_session_isolation.py
"""
import importlib.util
import os
import sys
import tempfile

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


# 用**临时目录**做 state_dir，避免污染真实 output/
_TMP = tempfile.mkdtemp(prefix="tp_isolation_")

spec = importlib.util.spec_from_file_location(
    "_tp_isolation", os.path.join(ROOT, "mcp", "tripplanner_mcp_server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
srv.state_dir = lambda: _TMP              # 隔离磁盘
srv.dbg = lambda *a, **k: None
srv.log = lambda *a, **k: None
srv.read_plan_state_orig = None


def _mk(sid):
    return srv._Session(sid)


# ---- A：会话隔离（正向）----
A = _mk("sess-A")
B = _mk("sess-B")
PLAN_A = {"city": "贵州(贵阳+黄果树+安顺+小七孔)", "days": 5,
          "dayList": [{"day": 1, "timeline": []}]}
PLAN_B = {"city": "杭州", "days": 2, "dayList": [{"day": 1, "timeline": []}]}

A.last_plan = dict(PLAN_A)
B.last_plan = dict(PLAN_B)
case("A1 两会话内存互不干扰（A 读到贵州）", A.last_plan.get("city") == PLAN_A["city"],
     A.last_plan.get("city"))
case("A2 两会话内存互不干扰（B 读到杭州）", B.last_plan.get("city") == PLAN_B["city"],
     B.last_plan.get("city"))

# 落盘路径隔离
pa, pb = srv.plan_state_path("sess-A"), srv.plan_state_path("sess-B")
case("A3 两会话落盘到**不同文件**", pa != pb and "sess-A" in pa and "sess-B" in pb,
     f"{os.path.basename(pa)} vs {os.path.basename(pb)}")
case("A4 不再使用全局单文件 mcp_last_plan.json",
     not pa.endswith(os.sep + "mcp_last_plan.json")
     and not pb.endswith(os.sep + "mcp_last_plan.json"))

# 磁盘回读隔离（模拟进程重启 / 新会话对象）
A2 = _mk("sess-A")
B2 = _mk("sess-B")
case("A5 新会话对象从磁盘回读到**自己的**行程（A→贵州）",
     A2.last_plan.get("city") == PLAN_A["city"], A2.last_plan.get("city"))
case("A6 新会话对象从磁盘回读到**自己的**行程（B→杭州）",
     B2.last_plan.get("city") == PLAN_B["city"], B2.last_plan.get("city"))

# ---- B：红队（攻击序列）----
# 攻击：A 写入贵州 → B 会话**不写任何东西**直接读 → 必须读不到 A 的行程
C = _mk("sess-C")
A.last_plan = dict(PLAN_A)          # A 再写一次
case("B1 红队：C 会话直接读，拿不到 A 的贵州行程（无串数据）",
     (C.last_plan or {}).get("city") != PLAN_A["city"],
     f"C 读到 {(C.last_plan or {}).get('city')!r}（应为空/None）")

# 攻击 2：SESSION 代理——绑定 A 后写，再绑定 B 读，不得看到 A
srv.SESSION.bind(A)
srv.SESSION.last_plan = dict(PLAN_A)
srv.SESSION.bind(B)
case("B2 红队：SESSION 代理换绑后不串（B 视角看不到 A 的贵州）",
     (srv.SESSION.last_plan or {}).get("city") != PLAN_A["city"],
     f"B 视角 {(srv.SESSION.last_plan or {}).get('city')!r}")

# 攻击 3：proxy 赋值**不得**落在 proxy 自身（历史缺陷：__setattr__ 缺失时
# 赋值创建实例属性 ⇒ 所有线程共享）
srv.SESSION.bind(A)
srv.SESSION.__dict__.pop("last_plan", None)   # 清掉可能的历史残留
srv.SESSION.last_plan = dict(PLAN_A)
case("B3 proxy 赋值不落在 proxy 实例上（__setattr__ 生效）",
     "last_plan" not in srv.SESSION.__dict__,
     f"proxy.__dict__ keys={sorted(srv.SESSION.__dict__.keys())}")

# ---- C：会话 sid 传递 ----
case("C1 会话对象携带 sid", A.sid == "sess-A" and B.sid == "sess-B")
# session_id() 在云端（绑定会话后）返回本会话 sid
srv.SESSION.bind(_mk("sess-XYZ"))
case("C2 session_id() 返回**本会话** sid（不再读全局文件）",
     srv.session_id() == "sess-XYZ", srv.session_id())
srv.SESSION.bind(_mk("sess-OTHER"))
case("C3 换绑后 session_id() 随之改变（无模块级缓存污染）",
     srv.session_id() == "sess-OTHER", srv.session_id())

# ---- D：并发隔离（多线程各自会话）----
import threading  # noqa: E402
_results = {}


def _worker(tag):
    s = _mk(f"sess-{tag}")
    srv.SESSION.bind(s)
    srv.SESSION.last_plan = {"city": f"city-{tag}", "days": 1,
                             "dayList": [{"day": 1, "timeline": []}]}
    _results[tag] = (srv.SESSION.last_plan or {}).get("city")


_threads = [threading.Thread(target=_worker, args=(t,)) for t in ("T1", "T2", "T3")]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
case("D1 多线程各自会话互不串（每线程读到自己的城市）",
     _results == {"T1": "city-T1", "T2": "city-T2", "T3": "city-T3"}, str(_results))

# 清理临时目录
import shutil  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过（会话隔离 + 红队 3 条 + 并发隔离）")
