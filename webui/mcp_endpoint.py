#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M5.x 原型：把 TripPlanner MCP server 搬到云端 —— `/mcp`（Streamable HTTP）端点。

## 这是什么 / 为什么
WorkBuddy 宿主「自定义 MCP」通道**本身支持远端 URL**（裸 `url` 条目＝Streamable HTTP，
见 `.workbuddy/memory/MCP-GOTCHAS.md` 的取证链）⇒ 同一份 tripplanner server 不必走
stdio、可由云主机提供 ⇒ 收包人**零安装**（既不装 exe 也不用上架）。

本模块**只做 transport 适配**，不复制任何业务口径：
  · 工具表 / 面板资源 / 全部处理逻辑 ⇒ 按文件路径加载 `mcp/tripplanner_mcp_server.py`
    并复用它的 `TOOLS`、`VIEWER_URI`、`read_panel()`、`handle()`（**唯一口径**，
    避免「工具名/描述/schema 复制两份后漂移」这条历史教训）；
  · 业务后端 ⇒ 仍是同一个 `webui/server.py` 的 `/api/*`（`handle()` 内部 `remote_request`）。

⚠️ 与既有结论一致：面板 → 宿主的 `tools/call` 在本宿主是**死的** ⇒ 面板仍**直连 `/api/*`**，
   `/mcp` 只服务「模型侧」（initialize / tools/list / tools/call）。**云端 HTTP 拆不掉。**

## Transport（MCP 2025-06-18 Streamable HTTP）最小实现
  POST /mcp   body=JSON-RPC（单条或 batch）
      有 id → 200 `application/json`，回单条 JSON-RPC response
      无 id → 202 Accepted（notification，无 body）
  GET  /mcp → **405**（本 server 不提供 SSE 流）
      🔴 必须是 405 不是 404：@modelcontextprotocol/sdk 的 StreamableHTTPClientTransport
      靠 405 判定「服务端不支持 standalone SSE」；回 404 会被当成「连错端点」。
  响应头 `Mcp-Session-Id`：仅当客户端该带而未带（或首次 initialize）时下发。
  不做 SSE 流式回包 —— 本 server 的响应都是单条、非流式，回 `application/json` 即可
  （客户端发的 `Accept: application/json, text/event-stream` 两者都接受）。

## 会话隔离（为什么必须做）
`tripplanner_mcp_server.SESSION` 是**模块级全局**，而它承载的是**一次会话的行程状态**
（`last_plan`）与诊断计数。云端是单进程多用户（`ThreadingHTTPServer`）⇒ 不隔离的话，
第二个人的 `get_plan` / 面板会拿到**第一个人的行程**。

故按 MCP 标准 `Mcp-Session-Id` 隔离：每个会话一份 `_Session`，互不影响。

⚠️ 2026-10-10（M6.6）：本文件早年还写着「同一会话内保留『已开不再开面板』语义」——
那种服务端封锁**已删除**（它拦不住宿主，只会让同一会话的**第二个需求**出不来面板；
详见 `tripplanner_mcp_server.py` 文件头的 M6.3 / M6.6 说明）。**会话隔离仍需保留**，
但理由只剩「状态隔离」这一条。

⚠️ 并发模型（2026-10-12 更新）：早年 `handle()` 读**模块全局** `SESSION`，故用一把
   `_exec_lock` **全串行**执行（「设 SESSION + 调 handle」不能被打断）。P0 修复把
   `SESSION` 改成**线程局部代理**（`_SessionProxy` + `bind()`）后，串行的理由消失，
   已改为**有界并发**：重工具过 `_gate`（Semaphore，默认 8 路），轻工具直放。
   见下方「并发闸」注释。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SERVER_FILE = os.path.join(_ROOT, "mcp", "tripplanner_mcp_server.py")

# 云端常见部署形态：`python webui/server.py` ⇒ sys.path[0] 是 webui/，不含仓库根，
# 且 `mcp/` 没有 `__init__.py`（也不是可 import 的包）⇒ 不做 `import mcp.x`，
# 改按**文件路径**加载，模块名带下划线前缀避免与 pip 的 `mcp` 包（Anthropic 官方包）撞名。
_MOD_NAME = "_tripplanner_mcp_server"

_server = None
_load_lock = threading.Lock()

_sessions: dict = {}
_sess_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 🔴 并发闸（2026-10-12，P0 后续）—— 取代原先的 `_exec_lock`「全串行」
#
# 历史：`handle()` 曾读**模块全局** `SESSION`，故必须「设 SESSION + 调 handle」
#   用 `_exec_lock` 串行（见文件头旧注）。P0 修复把 `SESSION` 改成线程局部代理后，
#   **串行的理由消失** —— 但直接去锁会变成「无上限并发」：N 个用户同刻进来 = N 路 LLM，
#   会刷爆上游（限流/成本失控）⇒ 用**有界并发闸**替代：允许 8 路同时跑、超出等待。
#
# 只闸「重工具」（触发服务端 LLM / 重排）：plan_trip / open_plan / replace / move / export。
# 轻工具（ping / list_cities / panel_ready / get_plan）**直放**，保证面板刷新与握手手感。
# 未知工具默认**受闸**（fail-safe：宁可等，不可无限耗资源）。
# ---------------------------------------------------------------------------
MAX_CONCURRENCY = max(1, int(os.environ.get("TRIPAGENT_MCP_CONCURRENCY") or 8))
_gate = threading.Semaphore(MAX_CONCURRENCY)
# 等闸超时：超过则回「系统繁忙」而不是无限排队（排队几十秒用户也等不到结果）
GATE_WAIT_S = float(os.environ.get("TRIPAGENT_MCP_GATE_WAIT_S") or 180.0)

# 轻工具白名单（不需要并发闸）—— 与 tripplanner_mcp_server 的工具名保持一致
_LIGHT_TOOLS = {
    "tripplanner_ping",
    "tripplanner_list_cities",
    "tripplanner_panel_ready",
    "tripplanner_get_plan",
}

MAX_SESSIONS = 64  # 简单上限，防长期运行内存单调增长


def _load():
    """懒加载并缓存 `mcp/tripplanner_mcp_server.py` 模块对象。"""
    global _server
    if _server is not None:
        return _server
    with _load_lock:
        if _server is None:
            if not os.path.isfile(_SERVER_FILE):
                raise RuntimeError(f"找不到 MCP server 源文件：{_SERVER_FILE}")
            spec = importlib.util.spec_from_file_location(_MOD_NAME, _SERVER_FILE)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[_MOD_NAME] = mod
            spec.loader.exec_module(mod)
            _server = mod
    return _server


def available() -> bool:
    """给 server.py 探活用：能不能加载 MCP server 源（`/api/health` 会带这个）。"""
    try:
        _load()
        return True
    except Exception:
        return False


def server_info() -> dict:
    """面板/诊断用：远端 MCP 端点自报身份（版本闸门与 stdio 侧同一来源）。"""
    m = _load()
    return {
        "name": m.SERVER_NAME,
        "version": m.SERVER_VERSION,
        "panel_version": m.PANEL_VERSION,
        "viewer_uri": m.VIEWER_URI,
        "tools": len(m.TOOLS),
    }


def _new_session() -> tuple:
    m = _load()
    sid = uuid.uuid4().hex
    with _sess_lock:
        _sessions[sid] = m._Session(sid)   # 🔴 P0：sid 注入会话对象（隔离的唯一真相）
        if len(_sessions) > MAX_SESSIONS:
            for k in list(_sessions.keys())[: len(_sessions) - MAX_SESSIONS // 2]:
                if k != sid:
                    _sessions.pop(k, None)
    return sid, _sessions[sid]


def _ensure_session(sid_hint):
    """返回 `(sid, session_obj)`；`sid_hint` 为空或未知 → 新建。"""
    with _sess_lock:
        s = _sessions.get(sid_hint or "")
    if s is not None:
        return sid_hint, s
    return _new_session()


def drop_session(sid: str) -> bool:
    """客户端 `DELETE /mcp`（terminateSession）时清会话。幂等，返回是否真的删掉了一个。"""
    if not sid:
        return False
    with _sess_lock:
        return _sessions.pop(sid, None) is not None


def _error(id_, code: int, message: str, status: int = 200) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": code, "message": message}},
                      ensure_ascii=False).encode("utf-8")
    return {"status": status, "session_id": None, "body": body}


def _needs_gate(msg) -> bool:
    """该请求是否要过并发闸（重工具）。轻工具与协议方法直放。"""
    method = (msg or {}).get("method")
    if method != "tools/call":
        return False                       # initialize / tools/list / ping 等协议方法不过闸
    name = ((msg.get("params") or {}).get("name") or "")
    return name not in _LIGHT_TOOLS         # 未知工具默认受闸（fail-safe）


def _handle_one(msg, sid_hint: str | None, out: dict):
    """处理一条 JSON-RPC 消息，返回 response dict 或 None（notification）。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": (msg or {}).get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}

    m = _load()
    method = msg.get("method")

    # initialize 一律新建会话（spec：initialize 不该带 session 头）；
    # 其余方法用客户端带来的 sid，未知则新建（宽松，避免 SDK 重连时卡死）。
    sid, sess = _new_session() if method == "initialize" else _ensure_session(sid_hint)
    if method == "initialize" or sid != (sid_hint or ""):
        out["session_id"] = sid

    # 🔴 2026-10-12：`_exec_lock` 已删除（P0 修复后 SESSION 是线程局部的，串行不再必要）。
    #   `bind()` 把本请求的会话绑到**本线程**；handle() 内 `SESSION.xxx` 只看到它，
    #   多请求并发互不可见 ⇒ 可安全并行。
    if not _needs_gate(msg):
        m.SESSION.bind(sess)
        return m.handle(msg)

    # 重工具：过有界并发闸（8 路），超出等待；等不到则回友好错误（不无限排队）
    if not _gate.acquire(timeout=GATE_WAIT_S):
        log(f"并发闸超时（>{GATE_WAIT_S:.0f}s 未获槽位），已拒绝")
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32000,
                          "message": f"系统繁忙：当前并发规划已达上限（{MAX_CONCURRENCY}），"
                                     f"请稍后重试"}}
    try:
        m.SESSION.bind(sess)
        return m.handle(msg)
    finally:
        _gate.release()


def dispatch(body: bytes, sid_hint: str | None = None) -> dict:
    """处理一次 POST /mcp。

    入参：原始请求体字节、请求头里的 `Mcp-Session-Id`（可空）。
    返回：`{"status": int, "session_id": str|None, "body": bytes}` ——
          由 `webui/server.py` 负责写回 HTTP（本模块不碰 socket，便于直接单测）。
    """
    try:
        _load()
    except Exception as e:  # noqa: BLE001 —— 加载失败要能说清原因，不能静默 500
        return _error(None, -32603, f"MCP server 源加载失败：{e}", status=500)

    try:
        msg = json.loads(body.decode("utf-8")) if body else None
    except Exception:  # noqa: BLE001 —— JSON 解析失败是客户端问题，按 JSON-RPC 规矩回 -32700
        return _error(None, -32700, "Parse error", status=400)

    if msg is None or (isinstance(msg, list) and not msg):
        return _error(None, -32600, "Invalid Request", status=400)

    out: dict = {}
    if isinstance(msg, list):  # batch：逐条处理，notification 不产生响应
        resps = [r for r in (_handle_one(x, sid_hint, out) for x in msg) if r is not None]
        if not resps:
            return {"status": 202, "session_id": out.get("session_id"), "body": b""}
        return {"status": 200, "session_id": out.get("session_id"),
                "body": json.dumps(resps, ensure_ascii=False).encode("utf-8")}

    resp = _handle_one(msg, sid_hint, out)
    if resp is None:  # notification → 202 无 body
        return {"status": 202, "session_id": out.get("session_id"), "body": b""}
    return {"status": 200, "session_id": out.get("session_id"),
            "body": json.dumps(resp, ensure_ascii=False).encode("utf-8")}
