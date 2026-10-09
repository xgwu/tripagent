#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TripPlanner MCP App —— M0 协议 spike + M1 前置改造（纯标准库，零依赖）

M0 目的：在写正式代码之前，用最小成本验证三件事
  V1  自建（非市场）MCP server 的 UI 资源能否被 WorkBuddy 渲染成面板
  V2  面板侧的 AppBridge 握手（ui/initialize）是否按 ext-apps 标准协议工作
  V3  面板能否反向调工具（tools/call），即 M1/M2 的交互链路是否通
  → 三项已于 2026-10-09 全部实测通过。

M1 前置（本轮）：解决「每次调用都新开一扇面板」的堆叠问题，采用 **A + C 组合**：
  - C（进程级兜底）：模块级 `_session` 记录本进程是否已开过面板。保证「极端情况下也不会堆面板」。
  - A（精确判据）：面板侧握手成功后经 `tripplanner_panel_ready` 自报 `appInstanceId`，
    server 记录「活跃面板实例」。收到 `open_plan` 时若该实例已存在 ⇒ 不开新面板，
    改为回摘要 + 提示「面板已打开」。
  为什么不是「判据只用 appInstanceId」：`appInstanceId` 由宿主中间层生成，
  `rpc.openHostApp params` 里**不含**它，`proxyAppToolCall` 转发给 MCP server 的
  `tools/call` 也只有 `name` + `arguments` ⇒ **server 侧拿不到**。
  所以必须让面板自己报上来（A），并且不能用它当唯一开关（C 兜底）。

设计要点（照 app.asar 源码挖出的硬约束）：
  - 工具是否「带面板」**完全取决于** tool._meta.ui.resourceUri；不用手写 isApp
  - resources/read 返回的 HTML 有 256KiB 预取阈值 → 面板必须自包含、不引兄弟资源
  - 宿主用 document.write 注入（非 srcdoc）→ 相对路径会 404，故面板不做任何外部引用
  - 图片走高德直链 store.is.autonavi.com → 必须写进 csp.resourceDomains 才不被静默拦掉
  - stdout 只能输出 JSON-RPC；一切日志走 stderr

用法：由 WorkBuddy 以 stdio 方式拉起，不需要手工运行。
"""
from __future__ import annotations

import io
import json
import os
import sys

SERVER_NAME = "tripplanner"
SERVER_VERSION = "0.0.2-m1pre"
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_PATH = os.path.join(HERE, "panel", "plan_panel.html")

VIEWER_URI = "ui://tripplanner/plan"
UI_MIME = "text/html;profile=mcp-app"

# 宿主侧读取的就是这一段（_meta.ui.csp.*）—— 缺了它 iframe 会被静默拦掉
# resourceDomains 必须放行高德图片域名，否则 <img src="https://store.is.autonavi.com/...">
# 会被宿主的两层 CSP（HTTP 头 + 注入 meta）静默拦掉 —— 不报错，只是图不显示。
AMAP_IMG_DOMAINS = [
    "https://store.is.autonavi.com",
    "https://store.is.autonavi.com:*",
    "https://*.amap.com",
    "https://*.autonavi.com",
]

UI_RESOURCE_META = {
    "ui": {
        "csp": {
            "frameDomains": ["http://127.0.0.1:*"],
            "connectDomains": ["http://127.0.0.1:*"],
            "resourceDomains": ["http://127.0.0.1:*"] + AMAP_IMG_DOMAINS,
        },
        "permissions": {},
        "prefersBorder": False,
    }
}

OPEN_TOOL = "tripplanner_open_plan"
PING_TOOL = "tripplanner_ping"
PANEL_READY_TOOL = "tripplanner_panel_ready"


# ---------------------------------------------------------------------------
# A + C 面板分流状态
# ---------------------------------------------------------------------------
class _Session:
    """本 server 进程内的一次「会话」状态。stdio server 由宿主按需拉起，
    一个进程对应宿主侧的一个连接；进程重启即重置，正合语义。

    C（兜底）：opened_any  —— 本进程是否已经开过面板。
    A（精确）：instances   —— 面板自报的 appInstanceId 集合（谁在看着这个面板）。
    """

    def __init__(self) -> None:
        self.opened_any = False
        self.instances: set[str] = set()
        self.proposals: int = 0

    def register_panel(self, instance_id: str | None, label: str = "") -> dict:
        iid = (instance_id or "").strip()
        if iid:
            self.instances.add(iid)
        self.opened_any = True
        return {
            "openedAny": self.opened_any,
            "instances": sorted(self.instances),
            "count": len(self.instances),
            "label": label,
        }

    def should_open_panel(self, instance_id: str | None) -> bool:
        """要不要**新开**一扇面板。

        面板侧已在看（实例已登记）→ 不开。
        本进程已经开过任意一扇    → 不开（C 兜底，防实例 id 拿不到时堆面板）。
        否则                      → 开。
        """
        iid = (instance_id or "").strip()
        if iid and iid in self.instances:
            return False
        if self.opened_any:
            return False
        return True


SESSION = _Session()

TOOLS = [
    {
        "name": OPEN_TOOL,
        "description": (
            "打开 TripPlanner 行程面板。调用后 WorkBuddy 会在对话下方渲染行程面板。"
            "当前处于 M0 协议验证阶段，面板内容是协议探针而非真实行程。"
            "注意：面板已打开时再次调用**不会**再开一扇，而是回一段摘要（A+C 分流）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_instance_id": {
                    "type": "string",
                    "description": (
                        "可选。面板侧自报的宿主实例 id（见 tripplanner_panel_ready）。"
                        "模型调用时通常留空；面板内反向调用时带上，用于精确判重。"
                    ),
                }
            },
            "additionalProperties": False,
        },
        # 这一行就是「带面板」的全部秘密
        "_meta": {"ui": {"resourceUri": VIEWER_URI, "visibility": ["model", "app"]}},
    },
    {
        "name": PING_TOOL,
        "description": (
            "对照组：与 open_plan 同处一个 server，但**没有** _meta.ui.resourceUri。"
            "用来确认「只有带 resourceUri 的工具才开面板」；同时也是面板内反向调用的靶子。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "任意回显内容，用于确认参数链路"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": PANEL_READY_TOOL,
        "description": (
            "【面板内部调用，模型不要直接调】面板握手成功后自报家门："
            "把宿主注入的 appInstanceId 交给 server，server 据此建立「活跃面板实例」名单，"
            "使 open_plan 能判断「面板已经开着」而不再重复开一扇。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_instance_id": {
                    "type": "string",
                    "description": "宿主提供给面板的 appInstanceId（面板从 hostContext 里取）",
                },
                "label": {"type": "string", "description": "可选备注，便于日志排查"},
            },
            "additionalProperties": False,
        },
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def log(msg: str, **kw) -> None:
    """日志一律走 stderr —— stdout 被 JSON-RPC 独占，写脏一个字节宿主就断连。"""
    extra = " " + json.dumps(kw, ensure_ascii=False) if kw else ""
    try:
        sys.stderr.write(f"[tripplanner-mcp] {msg}{extra}\n")
        sys.stderr.flush()
    except Exception:
        pass


def send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def ok(id_, res):
    return {"jsonrpc": "2.0", "id": id_, "result": res}


def fail(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def read_panel() -> str:
    try:
        with open(PANEL_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log("panel read failed", err=str(e), path=PANEL_PATH)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'></head>"
            "<body style=\"font-family:system-ui;padding:20px\">"
            "<h3>TripPlanner 面板文件读取失败</h3>"
            f"<pre>{e}</pre><p>路径：{PANEL_PATH}</p></body></html>"
        )


def handle(msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    is_notification = id_ is None
    log("<-", method=method, id=id_)

    if method == "initialize":
        pv = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return ok(id_, {
            "protocolVersion": pv,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else ok(id_, {})

    if method == "tools/list":
        return ok(id_, {"tools": TOOLS})

    if method == "resources/list":
        return ok(id_, {"resources": [{
            "uri": VIEWER_URI,
            "name": "tripplanner-plan-panel",
            "mimeType": UI_MIME,
        }]})

    if method == "resources/templates/list":
        return ok(id_, {"resourceTemplates": []})

    if method == "prompts/list":
        return ok(id_, {"prompts": []})

    if method == "resources/read":
        uri = params.get("uri")
        if uri != VIEWER_URI:
            return fail(id_, -32602, f"Resource not found: {uri}")
        text = read_panel()
        log("resources/read", uri=uri, html_bytes=len(text.encode("utf-8")))
        return ok(id_, {"contents": [{
            "uri": VIEWER_URI,
            "mimeType": UI_MIME,
            "text": text,
            "_meta": UI_RESOURCE_META,
        }]})

    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            return fail(id_, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}

        if name == OPEN_TOOL:
            iid = args.get("app_instance_id")
            # 判定「要不要开一扇新的」——这个工具带 resourceUri，宿主**只看工具本身**：
            # 只要 model 调了它，宿主就会渲染面板。所以「不开新面板」只能靠
            # ① 面板已在看（A：实例已登记）或 ② 本进程开过（C：兜底）时
            #    让模型**别走这个工具**，而这里返回的文案负责把这一点讲清楚。
            if SESSION.should_open_panel(iid):
                SESSION.opened_any = True
                if iid:
                    SESSION.instances.add(iid.strip())
                log("open_plan: 首次开面板", instance=iid, opened_any=True)
                return ok(id_, {
                    "content": [{"type": "text", "text": "行程面板已打开（M0 协议验证）。"}],
                    "structuredContent": {
                        "server": SERVER_NAME,
                        "version": SERVER_VERSION,
                        "stage": "M0",
                        "panelOpened": True,
                        "appInstanceId": iid or "",
                    },
                })
            # 已开过 —— 宿主仍会渲染面板（它不看返回值），但至少在返回里说清楚，
            # 并把摘要给出来，避免用户以为「又开了一扇新的」。
            log("open_plan: 面板已存在，回摘要", instance=iid,
                known=len(SESSION.instances), opened_any=SESSION.opened_any)
            return ok(id_, {
                "content": [{
                    "type": "text",
                    "text": (
                        "行程面板**已经打开了**（在对话上方/下方），没有重复开启。\n"
                        "如需刷新面板内容，直接在面板里操作即可。"
                    ),
                }],
                "structuredContent": {
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "stage": "M0",
                    "panelOpened": False,
                    "reason": "panel-already-open",
                    "knownInstances": sorted(SESSION.instances),
                },
            })

        if name == PING_TOOL:
            note = args.get("note") or "(无)"
            log("ping hit", note=note)
            return ok(id_, {
                "content": [{"type": "text", "text": f"pong <- {note}"}],
                "structuredContent": {
                    "pong": True,
                    "note": note,
                    "server": SERVER_NAME,
                    "ts": _now(),
                },
            })

        if name == PANEL_READY_TOOL:
            iid = args.get("app_instance_id")
            label = args.get("label") or ""
            info = SESSION.register_panel(iid, label)
            log("panel_ready", instance=iid, label=label, count=info["count"])
            missing = "" if (iid or "").strip() else "（未取到 appInstanceId，已退化为 C 兜底）"
            return ok(id_, {
                "content": [{
                    "type": "text",
                    "text": f"面板已登记：{info['count']} 个活跃实例 {missing}".strip(),
                }],
                "structuredContent": {
                    "registered": True,
                    "appInstanceId": iid or "",
                    "knownInstances": info["instances"],
                    "count": info["count"],
                    "hasInstanceId": bool((iid or "").strip()),
                    "policy": "A+C",
                    "server": SERVER_NAME,
                },
            })

    if is_notification:
        return None
    return fail(id_, -32601, f"Method not found: {method}")


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    # 幂等 reconfigure：既保证 UTF-8，也不会像重建 TextIOWrapper 那样在 GC 时关掉底层 buffer
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except Exception:
            pass

    log("start", pid=os.getpid(), version=SERVER_VERSION, panel=PANEL_PATH)

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log("bad json", err=str(e), raw=line[:200])
            continue

        try:
            out = handle(msg)
        except Exception as e:  # 任何异常都不能让进程崩掉，否则面板直接消失
            log("handler crash", err=repr(e), method=msg.get("method"))
            out = fail(msg.get("id"), -32603, f"internal error: {e}")

        if out is not None:
            send(out)

    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
