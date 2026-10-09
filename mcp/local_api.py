#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TripPlanner 本地 HTTP API（M2.1）—— 面板 ↔ 服务端的**备用通路**。

## 为什么需要它（2026-10-09 取证结论，不是猜测）

MCP App 面板 → 宿主 → MCP server 的 `tools/call` 通道在**本宿主上是死的**：

  - 宿主 `~/.workbuddy/logs/mcp-apps-diag.log` **有** `rpc.callTool
    toolName=tripplanner_replace_poi args={...}` 与 `proxyAppToolCall.ok`；
  - 但请求**从未到达任何 MCP server 进程** —— `output/mcp_debug.log` 无记录、
    `output/mcp_last_plan.json` 未改写；
  - 宿主从 `callTool` 到 `.ok` 只花 **12ms**（真实换点要跑一遍 TOPTW 求解，几百 ms 起）；
  - 返回体恒为 `contentCount=1, textLen=43, structuredContentKeys=[]` —— 而我们实测
    `panel_ready` 才是 43 字符、`get_plan` 有行程时是 286 字符 ⇒ **不是我们的输出**；
  - 面板里点「调 tripplanner_ping」**10s 超时**（决定性判据）。

⇒ 面板交互（换点 / 挪时段 / 导出）**不能**依赖那条通道。

## 本模块怎么做

把同样的能力开在 **127.0.0.1 的本地 HTTP** 上（宿主 CSP 的 `connectDomains`
已放行 `http://127.0.0.1:*`），面板直接 `fetch` 就行。

设计要点：

  - **独立常驻进程**：宿主对 stdio MCP server 是**按需 spawn**、随时可能回收
    （`mcp_debug.log` 里 pid 每隔几秒就换一个）。挂在 server 进程内的 HTTP 会随它一起死，
    所以本脚本以 detached 方式常驻，由 server 在 `resources/read` 时**确保它在跑**，
    并把 `端口 + token` 注入面板 HTML。
  - **单一实现**：业务逻辑全部复用 `tripplanner_mcp_server` 的 `kernel()` /
    `apply_day()` / `plan_to_markdown()`，**绝不复制一份**（本项目教训：
    多副本实现必然漂移）。
  - **状态走磁盘**：行程仍是 `output/mcp_last_plan.json`（跨进程共享的唯一真相），
    本进程与 MCP server 读同一个文件。
  - **端口尽量固定**：优先 51900 起的一小段，让面板 URL 跨重启稳定；被占用才退化随机端口。
  - **token 校验**：只监听 127.0.0.1，且要求 `X-Tripplanner-Token`（**跨重启稳定**，
    由 `srv.local_api_token()` 生成一次后落盘）—— 防本机其它网页驱动行程，
    同时保证「本进程被宿主回收后重启」不会让面板手里那份 token 失效。

## ⚠️ 改了这里（或 `tripplanner_mcp_server.py`）必须 bump `SERVER_VERSION`

本进程是**常驻**的，`ensure_local_api()` 靠 `/api/health` 里回带的 `version`
判断要不要杀掉重拉。而且本模块 `import tripplanner_mcp_server` 取内核 ——
**改那个文件同样影响本进程**。只改代码不 +1 版本号 ⇒ 旧进程继续用旧逻辑服务面板，
表现为「代码改了但行为没变」，且没有任何报错。

## 用法

由 `tripplanner_mcp_server.ensure_local_api()` 以 detached 子进程方式拉起，不需要手工运行。
重复拉起是安全的：启动时若发现**同版本**既有实例还活着，本进程直接退出。
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tripplanner_mcp_server as srv  # noqa: E402  业务逻辑唯一来源

HOST = "127.0.0.1"
PREFERRED_PORTS = list(range(51900, 51920))     # 先试固定段，让面板 URL 稳定
STAGE = "M2.1"


# ---------------------------------------------------------------------------
# 业务：与 MCP 工具分支**同语义**，只是换了个入口（都调 srv / poi_swap）
# ---------------------------------------------------------------------------
def _do_replace(args: dict) -> dict:
    city = str(args.get("city") or "").strip()
    day = args.get("day")
    poi_id = str(args.get("poi_id") or "").strip()
    day_ids = args.get("day_ids") or []
    if not (city and day and poi_id and day_ids):
        return {"ok": False, "error": "参数缺失（需要 city / day / poi_id / day_ids）"}
    cur = srv.read_plan_state()
    if not cur:
        return {"ok": False, "error": "还没有行程，请先让助手生成行程"}

    k, kerr = srv.kernel()
    if kerr:
        return {"ok": False, "error": f"规划内核加载失败：{kerr}"}

    _code, data = k["poi_swap"].replace_poi(
        city, poi_id, day, day_ids,
        used_ids=args.get("used_ids") or [],
        date0=args.get("date0"),
        query=args.get("query") or "",
        hotel_text=args.get("hotel_text"))
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error") or "换点失败"}

    plan = srv.apply_day(cur, data["day"])
    srv.write_plan_state(plan)
    sw = data["swap"]
    srv.dbg("local_api.replace.ok", city=city, day=day, frm=sw.get("from"), to=sw.get("to"))
    return {"ok": True, "swap": sw, "day": data["day"], "plan": plan}


def _do_move(args: dict) -> dict:
    city = str(args.get("city") or "").strip()
    day = args.get("day")
    ordered = [str(x) for x in (args.get("ordered_ids") or [])]
    if not (city and day and ordered):
        return {"ok": False, "error": "参数缺失（需要 city / day / ordered_ids）"}

    cur = srv.read_plan_state()
    if not cur:
        return {"ok": False, "error": "还没有行程，请先让助手生成行程"}

    # 点集校验：拖拽只改顺序，**不允许**悄悄丢点或加点
    day_ids = []
    for d in cur.get("dayList") or []:
        if d.get("day") == day:
            day_ids = [s.get("id") for s in (d.get("timeline") or [])
                       if s.get("type") == "poi"]
    if day_ids and set(ordered) != set(day_ids):
        return {"ok": False,
                "error": f"新顺序的点集与当天不一致（原 {len(day_ids)} 个，收到 {len(ordered)} 个）"}

    k, kerr = srv.kernel()
    if kerr:
        return {"ok": False, "error": f"规划内核加载失败：{kerr}"}

    _code, data = k["poi_swap"].reorder_day(
        city, day, ordered,
        date0=args.get("date0"),
        query=args.get("query") or "",
        hotel_text=args.get("hotel_text"))
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error") or "调整顺序失败"}

    plan = srv.apply_day(cur, data["day"])
    srv.write_plan_state(plan)
    respected = bool(data.get("order_respected"))
    srv.dbg("local_api.move.ok", city=city, day=day, respected=respected)
    return {"ok": True, "order_respected": respected, "day": data["day"], "plan": plan}


def _do_export(args: dict) -> dict:
    plan = srv.read_plan_state()
    if not plan:
        return {"ok": False, "error": "还没有行程可导出，请先生成行程"}
    fmt = str(args.get("format") or "markdown").lower()
    if fmt not in ("markdown", "md"):
        return {"ok": False, "error": f"暂不支持的导出格式：{fmt}（目前仅 markdown）"}
    text = srv.plan_to_markdown(plan)
    fname = f"TripPlanner-{plan.get('city') or 'trip'}-{plan.get('days') or ''}天.md"
    srv.dbg("local_api.export.ok", city=plan.get("city"), chars=len(text))
    return {"ok": True, "filename": fname, "mimeType": "text/markdown", "text": text}


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "TripPlannerLocal/0.1"

    # 默认实现会把每条请求打到 stderr（父进程可能已把它接到 DEVNULL），
    # 改为落到 output/mcp_debug.log，排查时能对齐时间轴。
    def log_message(self, fmt, *args):
        try:
            srv.dbg("local_api.http", line=fmt % args)
        except Exception:
            pass

    # ---- 工具 ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Tripplanner-Token")
        self.send_header("Access-Control-Max-Age", "600")
        # Chrome 的 Private Network Access：公网页 → 本地回环 需要显式放行，
        # 否则预检直接被拒（面板表现为 fetch 失败但不告诉你为什么）。
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _token_ok(self) -> bool:
        want = getattr(self.server, "token", "") or ""
        got = self.headers.get("X-Tripplanner-Token") or ""
        return bool(want) and got == want

    def _path(self) -> str:
        return (self.path or "").split("?")[0].rstrip("/") or "/"

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self._path()
        # health **不校验 token** —— 它就是给「你还在吗」用的探针
        if path in ("/", "/api/health"):
            return self._json(200, {
                "ok": True, "server": srv.SERVER_NAME, "version": srv.SERVER_VERSION,
                "stage": STAGE, "pid": os.getpid(), "ts": srv._now(),
            })
        if not self._token_ok():
            # ⚠️ 把「token 被拒」也落日志：这是「health 显示可用、其余全失败」的分水岭，
            #    不记下来就只能靠猜（踩过：面板拿的是上一代进程注入的旧 token）。
            srv.dbg("local_api.token.reject", path=path, method="GET",
                    has_token=bool(self.headers.get("X-Tripplanner-Token")))
            return self._json(403, {"ok": False, "error": "token 校验失败"})
        if path == "/api/plan":
            plan = srv.read_plan_state()
            return self._json(200, {"ok": True, "hasPlan": plan is not None, "plan": plan})
        return self._json(404, {"ok": False, "error": f"未知路径 {path}"})

    def do_POST(self):
        path = self._path()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            args = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(args, dict):
                args = {}
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"请求体解析失败：{e}"})

        if not self._token_ok():
            srv.dbg("local_api.token.reject", path=path, method="POST",
                    has_token=bool(self.headers.get("X-Tripplanner-Token")))
            return self._json(403, {"ok": False, "error": "token 校验失败"})

        try:
            # 面板侧的动作轨迹（观测缺陷也是缺陷：面板是黑盒，失败时得能留下证词）。
            # 面板每次交互都会打一条，哪怕后面主调用失败 —— 于是「点了没反应」到底是
            # 「面板没发」还是「服务端没收」一眼可分。
            if path == "/api/log":
                try:
                    srv.dbg("panel.trace", **{str(k): v for k, v in args.items()})
                except Exception as e:                          # noqa: BLE001
                    srv.dbg("panel.trace.fail", err=repr(e))
                return self._json(200, {"ok": True})
            if path == "/api/replace":
                return self._json(200, _do_replace(args))

            if path == "/api/move":
                return self._json(200, _do_move(args))
            if path == "/api/export":
                return self._json(200, _do_export(args))
        except Exception as e:                                   # noqa: BLE001
            import traceback
            srv.dbg("local_api.error", path=path, err=repr(e))
            return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "trace": traceback.format_exc()[-600:]})
        return self._json(404, {"ok": False, "error": f"未知路径 {path}"})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _write_portfile(port: int, token: str) -> None:
    path = srv.local_api_portfile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid(),
                   "server": srv.SERVER_NAME, "version": srv.SERVER_VERSION,
                   "stage": STAGE, "ts": srv._now()}, f, ensure_ascii=False)
    os.replace(tmp, path)          # 原子写：server 侧可能正在读


def _existing_alive():
    """已有**同版本**实例还活着就直接返回它的 base（本进程随即退出）。

    ⚠️ 必须连同版本一起比：常驻进程会一直活着，只比「端口有人应答」的话，
    改了代码后新进程会误以为「老的还在、我可以退」，面板就永远吃旧逻辑。
    """
    d = srv._local_api_read_portfile()
    if d and d.get("port"):
        base = f"http://127.0.0.1:{int(d['port'])}"
        info = srv._local_api_info(base)
        if info and str(info.get("server") or "") == srv.SERVER_NAME \
                and str(info.get("version") or "") == srv.SERVER_VERSION:
            return base
    return None


def _bind():
    for port in PREFERRED_PORTS:
        try:
            return port, ThreadingHTTPServer((HOST, port), _Handler)
        except OSError:
            continue
    return 0, ThreadingHTTPServer((HOST, 0), _Handler)          # 退化为随机端口


def _in_job():
    """本进程是否落在某个 job object 里 —— 宿主的 job 会「关掉即全杀」。

    纯诊断用：`local_api.spawn` 里的 mode 若显示 `launcher`（没能 breakaway）
    且这里是 True，就说明进程生命周期仍被宿主牵着，需要再想办法。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # ⚠️ 必须显式声明 argtypes：不声明时 64 位下伪句柄会被截断，调用直接失败
        #    （踩过：日志里 `in_job: null`，看起来像「不在 job 里」，其实是没测出来）。
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_int)]
        k.IsProcessInJob.restype = ctypes.c_int
        out = ctypes.c_int(0)
        if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(out)):
            return None
        return bool(out.value)
    except Exception:
        return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except Exception:
            pass

    alive = _existing_alive()
    if alive:
        srv.dbg("local_api.skip", reason="already-alive", base=alive)
        return 0

    want, httpd = _bind()
    port = httpd.server_address[1]
    # token **跨重启稳定**：面板 HTML 里注入过的那份必须一直有效，
    # 否则会出现「health 说可用、实际请求全 403」的假象。见 srv.local_api_token 文档。
    token = srv.local_api_token()
    httpd.token = token                      # type: ignore[attr-defined]
    _write_portfile(port, token)
    srv.dbg("local_api.listen", base=f"http://{HOST}:{port}", preferred=bool(want),
            in_job=_in_job(), pid=os.getpid())

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
