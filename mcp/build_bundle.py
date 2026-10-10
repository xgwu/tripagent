#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键盘出**可分享的 MCP App 安装包**（Windows / macOS / Linux）。

为什么需要它：`build_exe.py` 只解决 Windows 的 onefile exe。mac/linux 上**没有 exe 形态**
（PyInstaller 必须在目标 OS 上构建，**不支持交叉编译**），只能给「bash 启动器 + 纯标准库 .py」
的源码包。这个脚本把三种形态收敛成一条命令，并统一做冒烟 + 打 zip。

用法：
    python mcp/build_bundle.py                      # 按当前系统产出（win=exe，mac/linux=源码包）
    python mcp/build_bundle.py --target mac         # ★可在 Windows 上生成 mac 源码包
    python mcp/build_bundle.py --target win --binary     # 真打 exe（须在 Windows 上跑）
    python mcp/build_bundle.py --target mac --binary     # 真打 Mach-O（须在 mac 上跑）
    python mcp/build_bundle.py --stage-only         # 只铺目录：不冒烟、不打包（廉价门禁用）
    python mcp/build_bundle.py --self-check         # 自检铺包链路（release_gate --mcp 会调）

产物（都在 `dist/` 下）：
    stage-<target>/tripplanner-mcp/   铺好的包目录
    tripplanner-mcp-<target>-share-<YYYYMMDD>.zip   给别人发的 zip

⚠️ `remote.json` 内含**明文口令**（「凭据随包分发」是本人取舍）⇒ 只发给信任的人。

形态速查：
    win            tripplanner-mcp.exe（单文件、零依赖、8 MB）
    mac/linux      启动器 `tripplanner-mcp` + `mcp/tripplanner_mcp_server.py`（需系统有 python3；
                   好处是**完全绕开 Gatekeeper 对未签名二进制的拦截**）
    --binary       本机原生 PyInstaller（mac 上得到真 Mach-O，不依赖 python3）
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DIST = os.path.join(REPO, "dist")
SERVER = os.path.join(HERE, "tripplanner_mcp_server.py")
PANEL = os.path.join(HERE, "panel", "plan_panel.html")

# bundle_check 与本文件同目录；`python mcp/build_bundle.py` 时 sys.path[0] 就是 HERE。
sys.path.insert(0, HERE)
import bundle_check  # noqa: E402   （必须先于 argparse 用到，故放这里）

TARGETS = ("win", "mac", "linux")

LAUNCHER = """#!/bin/bash
# TripPlanner MCP App —— macOS / Linux 启动器
# 作用：找到 python3，把 stdio 直接交给 tripplanner_mcp_server.py（宿主按 MCP stdio 协议对话）。
# 为什么用启动器而不是把 python3 路径写进注册：位置随安装方式而变，而 WorkBuddy
# 宿主 spawn 时的 PATH 可能很干净，写死路径容易「找不到命令」。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then PY="$(command -v python3 2>/dev/null || true)"; fi
if [ -z "$PY" ]; then
  echo "[tripplanner] 找不到 python3。 macOS: xcode-select --install ｜ Linux: apt install python3" >&2
  exit 127
fi

export TRIPPLANNER_HOME="$DIR"
exec "$PY" "$DIR/mcp/tripplanner_mcp_server.py"
"""

MCP_JSON = """{
  "mcpServers": {
    "tripplanner": {
      "type": "stdio",
      "command": "<你放的那个目录>/%(exe)s",
      "args": [],
      "cwd": "<你放的那个目录>",
      "env": {
        "TRIPPLANNER_HOME": "<你放的那个目录>"
      }
    }
  }
}
"""

BUILD_SH = """#!/bin/bash
# 在**本机**把本包的 server 打成单文件二进制（可选；需要联网装 pyinstaller）。
# 产物：./bin/tripplanner-mcp —— 之后运行不再依赖系统 python3。
# ⚠️ PyInstaller 只能在本机 OS 上构建 ⇒ Windows 上做不出 mac 版，必须在 %(osname)s 上跑本脚本。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then echo "找不到 python3：先装 python3" >&2; exit 1; fi

# 用独立 venv，避免 Homebrew / 发行版 python 的 PEP 668「externally-managed」报错
"$PY" -m venv "$DIR/.buildvenv"
"$DIR/.buildvenv/bin/python" -m pip install --upgrade pip pyinstaller

"$DIR/.buildvenv/bin/python" -m PyInstaller --noconfirm --clean --onefile --console \\
  --name tripplanner-mcp \\
  --add-data "$DIR/mcp/panel/plan_panel.html:panel" \\
  --distpath "$DIR/bin" --workpath "$DIR/.build" --specpath "$DIR/.build" \\
  --exclude-module src --exclude-module ortools --exclude-module numpy \\
  --exclude-module pandas --exclude-module matplotlib --exclude-module tkinter \\
  --exclude-module PIL --exclude-module scipy --exclude-module requests \\
  --exclude-module yaml --exclude-module lxml \\
  "$DIR/mcp/tripplanner_mcp_server.py"

echo
echo "✓ 产物：$DIR/bin/tripplanner-mcp"
echo "  把注册里的「命令」换成它的绝对路径（环境变量仍设 TRIPPLANNER_HOME=$DIR）"
"""

MAC_DOC = """TripPlanner MCP App —— 使用说明（macOS）
=========================================

mac 上**没有 exe**（Windows 的 exe 不能在 mac 上跑，PyInstaller 也不能跨系统编译）。
本包用「启动器 + 纯 Python 脚本」形态：脚本只依赖 Python 标准库，
**不需要 pip 装任何东西**，也不需要密钥、不需要下载数据。

【0】先确认有 python3（多数 mac 自带；装过 Xcode 命令行工具的都有）
    打开「终端」，输入：   python3 -V
    ▸ 打印出 3.8 以上版本号            ⇒ 继续第 1 步
    ▸ 提示 command not found          ⇒ 先装一个（都要联网）：
          xcode-select --install      # 官方，弹窗点「安装」即可
          或装 Homebrew 后 brew install python

【1】放好文件
    把**整个文件夹**拷到任意目录，例：~/TripPlannerMCP
    拷完确认这几样在同一层：
        tripplanner-mcp                       启动器（第 2 步用）
        mcp/                                  内含 tripplanner_mcp_server.py 与 panel/plan_panel.html
        remote.json                           后端地址（**必须与启动器同目录**）
        mcp.json                              注册片段（第 2 步用）
        build_macos.sh                        可选：打出不依赖 python3 的单文件二进制
    ⚠️ 别放进 iCloud Drive / 网盘同步目录 —— 同步会占用文件，可能导致启动失败。

【1.5】给启动器加可执行权限（若提示 Permission denied 才需要）
    终端执行（路径换成你的）：
        chmod +x ~/TripPlannerMCP/tripplanner-mcp
    若弹「无法打开，因为来自身份不明的开发者/未验证的开发者」，再执行：
        xattr -dr com.apple.quarantine ~/TripPlannerMCP

【2】注册到 WorkBuddy（二选一）

    ▸ 推荐：表单添加（保存即生效，不用重启）
      WorkBuddy → 左下角「+」→ 连接器 → 「自定义 MCP」→ 添加，按下表填：
          名称    tripplanner
          类型    stdio
          命令    <你放的那个目录>/tripplanner-mcp
          参数    （留空）
          环境    TRIPPLANNER_HOME = <你放的那个目录>
      填完在连接器列表里找到 tripplanner → 点「信任」。
      生效判据：对话里问「有哪些城市可以规划」，能列出 31 个城市即成功。

      ▸ 若「命令=启动器」不被接受，改用 python3 直连：
          命令    /usr/bin/python3        （或你 which python3 得到的绝对路径）
          参数    <你放的那个目录>/mcp/tripplanner_mcp_server.py
          环境    TRIPPLANNER_HOME = <你放的那个目录>

    ▸ 备选：改 mcp.json
      把本文件夹 mcp.json 里的 <你放的那个目录> 全部替换成真实目录（共 3 处），
      再把这段合并进 ~/.workbuddy/mcp.json，然后**完全退出** WorkBuddy 再启动。

【3】开始用
    在对话里说「帮我规划杭州2天，喜欢自然风光」，
    行程面板会渲染在对话下方（时间线 / 每日主题与里程 / POI 缩略图 / 路线地图）。

【可选】想要一个「不依赖 python3」的单文件二进制
    在这台 mac 上、本目录内执行：   bash build_macos.sh
    产物 bin/tripplanner-mcp；把它绝对路径填进注册的「命令」即可。

【说明】
- 行程规划（含大模型）全部在**云端**完成；本机不跑模型、不需要密钥、不联网下载数据。
- 面板地图与缩略图需要联网（高德）。断网时行程照样能看，只是没有地图。
- macOS 可能拦截未签名程序（见 1.5 的 xattr）；被 MDM 管控的 mac 可能禁止运行未签名程序。
- 面板显示「后端不可达」时，按顺序查：
    ① remote.json 是否与启动器同目录；
    ② 浏览器打开 {BASE}/api/health 是否弹出账号密码框（要能访问才算通）；
    ③ 对话里问「有哪些城市可以规划」，能列出即服务正常。
- 行程缓存在「本目录的 output/」下，删掉即可重置。
- 所有回包都可能是降级结果（云端限流/网络抖动时）：面板会如实标红，不会假装成功。

【卸载】删掉这个文件夹，并在 WorkBuddy 连接器里移除 tripplanner 即可。
"""

LINUX_DOC = """TripPlanner MCP App —— 使用说明（Linux）
========================================

本包用「启动器 + 纯 Python 脚本」形态：脚本只依赖 Python 标准库，
**不需要 pip 装任何东西**，也不需要密钥、不需要下载数据。

【0】先确认有 python3
    终端输入：   python3 -V
    ▸ 打印出 3.8 以上版本号   ⇒ 继续
    ▸ 没有的话：  sudo apt install -y python3      （Debian/Ubuntu）
                  sudo dnf install -y python3      （Fedora/RHEL）

【1】放好文件
    把**整个文件夹**拷到任意目录，例：~/TripPlannerMCP
    拷完确认这几样在同一层：
        tripplanner-mcp     启动器（第 2 步用）
        mcp/                内含 tripplanner_mcp_server.py 与 panel/plan_panel.html
        remote.json         后端地址（**必须与启动器同目录**）
        mcp.json            注册片段（第 2 步用）
        build_linux.sh      可选：打出不依赖 python3 的单文件二进制

【1.5】给启动器加可执行权限
    终端执行：   chmod +x ~/TripPlannerMCP/tripplanner-mcp

【2】注册到 WorkBuddy（二选一）

    ▸ 推荐：表单添加（保存即生效，不用重启）
          名称    tripplanner
          类型    stdio
          命令    <你放的那个目录>/tripplanner-mcp
          参数    （留空）
          环境    TRIPPLANNER_HOME = <你放的那个目录>
      填完在连接器列表里找到 tripplanner → 点「信任」。
      生效判据：对话里问「有哪些城市可以规划」，能列出 31 个城市即成功。

      ▸ 若「命令=启动器」不被接受，改用 python3 直连：
          命令    /usr/bin/python3
          参数    <你放的那个目录>/mcp/tripplanner_mcp_server.py
          环境    TRIPPLANNER_HOME = <你放的那个目录>

    ▸ 备选：改 mcp.json
      替换其中 3 处 <你放的那个目录>，合并进 ~/.workbuddy/mcp.json，完全退出 WorkBuddy 再启动。

【3】开始用
    在对话里说「帮我规划杭州2天，喜欢自然风光」。

【可选】不依赖 python3 的单文件二进制：  bash build_linux.sh   → 产物 bin/tripplanner-mcp

【说明】
- 行程规划（含大模型）全在**云端**完成；本机不跑模型、不需要密钥、不下载数据。
- 面板地图与缩略图需要联网（高德）；断网时行程照样能看，只是没有地图。
- 面板显示「后端不可达」时，查：① remote.json 是否同目录；② 浏览器打开 {BASE}/api/health
  是否弹账号密码框；③ 问「有哪些城市可以规划」能否列 31 城。
- 行程缓存在「本目录的 output/」下，删掉即可重置。

【卸载】删掉这个文件夹，并在 WorkBuddy 连接器里移除 tripplanner 即可。
"""


def current_target() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def exe_name(target: str) -> str:
    return "tripplanner-mcp.exe" if target == "win" else "tripplanner-mcp"


def _fail(msg: str) -> int:
    print("✗ " + msg)
    return 1


def _write_lf(path: str, text: str) -> None:
    """强制 LF（mac/linux 上带 CRLF 的 .sh 会报 `\\r: command not found`）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _fresh_stage(target: str) -> tuple[str, str]:
    stage_root = os.path.join(DIST, f"stage-{target}")
    if os.path.isdir(stage_root):
        shutil.rmtree(stage_root, ignore_errors=True)
    pkg = os.path.join(stage_root, "tripplanner-mcp")
    os.makedirs(pkg)
    return stage_root, pkg


def stage_source(target: str, payload: dict | None = None) -> tuple[str, str]:
    """铺 mac/linux 源码包。**可在任意 OS 上生成**（只是不能冒烟启动器本身）。

    `payload` 为 None 时走 `build_exe.remote_payload()`（正常出包路径）；自检模式会
    显式传入哨兵值，好让**没有 secrets.json 的机器也能验布局**（自检只验结构，不验凭据）。
    """
    stage_root, pkg = _fresh_stage(target)
    os.makedirs(os.path.join(pkg, "mcp", "panel"))
    shutil.copy2(SERVER, os.path.join(pkg, "mcp", "tripplanner_mcp_server.py"))
    shutil.copy2(PANEL, os.path.join(pkg, "mcp", "panel", "plan_panel.html"))

    # remote.json —— 复用 build_exe.remote_payload()（唯一出口，别在这再写一遍读配置逻辑）
    import build_exe
    if payload is None:
        payload = build_exe.remote_payload()
    if not payload["base"]:
        raise RuntimeError("没有后端 base（secrets.json 缺 remote_basic_auth）")
    import json
    with open(os.path.join(pkg, "remote.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    _write_lf(os.path.join(pkg, "tripplanner-mcp"), LAUNCHER)
    _write_lf(os.path.join(pkg, "mcp.json"), MCP_JSON % {"exe": exe_name(target)})
    osname = "macOS" if target == "mac" else "Linux"
    _write_lf(os.path.join(pkg, f"build_{'macos' if target == 'mac' else 'linux'}.sh"),
              BUILD_SH % {"osname": osname})
    _write_lf(os.path.join(pkg, "使用说明-%s.txt" % ("Mac" if target == "mac" else "Linux")),
              (MAC_DOC if target == "mac" else LINUX_DOC).replace("{BASE}", payload["base"]))

    for n in ("tripplanner-mcp", f"build_{'macos' if target == 'mac' else 'linux'}.sh"):
        try:
            os.chmod(os.path.join(pkg, n), 0o755)
        except OSError:
            pass
    return stage_root, pkg


def stage_win(args) -> tuple[str, str]:
    """Windows：委托 `build_exe.py` 打 exe，再把 4 件套搬进 stage-win/。"""
    cmd = [sys.executable, os.path.join(HERE, "build_exe.py")]
    if args.clean:
        cmd.append("--clean")
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if subprocess.call(cmd) != 0:
        raise RuntimeError("build_exe.py 失败")
    stage_root, pkg = _fresh_stage("win")
    for n in (exe_name("win"), "remote.json", "mcp.json", "使用说明.txt"):
        shutil.copy2(os.path.join(DIST, n), os.path.join(pkg, n))
    return stage_root, pkg


def verify(target: str, pkg: str, binary: bool) -> bundle_check.Result:
    """把铺好的包当真 server 拉起，走一遍消费方路径（共用 bundle_check）。"""
    env = {"TRIPPLANNER_HOME": pkg}
    if target == "win" or binary:
        launch = [os.path.join(pkg, exe_name(target))]
    else:
        # 源码/启动器形态：启动器本身在别的 OS 上没法执行 ⇒ 直接跑它 exec 的那个脚本，
        # 验的是**包布局 + 协议层**（面板就位、remote.json 就位、真实规划）。启动器那
        # 十几行 bash 需在目标机实测（README 已注明）。
        launch = [sys.executable, os.path.join(pkg, "mcp", "tripplanner_mcp_server.py")]
    return bundle_check.run(launch, cwd=pkg, env_extra=env)


def zip_pkg(target: str, stage_root: str, pkg: str, out_path: str | None = None) -> str:
    zpath = out_path or os.path.join(DIST, "tripplanner-mcp-%s-share-%s.zip"
                                     % (target, time.strftime("%Y%m%d")))
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(stage_root):
            for n in sorted(files):
                p = os.path.join(root, n)
                arc = os.path.relpath(p, stage_root).replace(os.sep, "/")
                # 带可执行位：否则解压后启动器不可执行（宿主报 Permission denied）
                ex = n.endswith((".sh", ".command")) or ("." not in n)
                zi = zipfile.ZipInfo(arc, date_time=time.localtime()[:6])
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = ((0o100755 if ex else 0o100644) << 16)
                with open(p, "rb") as f:
                    z.writestr(zi, f.read())
    return zpath


# ---------------------------------------------------------------------------
# 自检（`--self-check`）：**廉价**验「出包链路自己有没有坏」
# ---------------------------------------------------------------------------
# 为什么单独做：打包脚本坏了，正常只有真出包时才暴露（win 要跑一分钟 PyInstaller、
# mac 要有人在 mac 上试）。而「铺包 + 打 zip」这段是纯 Python，任意 OS 都能跑，且恰好是
# **唯一会让分享包静默不可用**的地方 —— 三个坑都无声无息：
#   ① zip 丢可执行位 ⇒ 解压后启动器不可执行 ⇒ 宿主只报一句 spawn 失败（不说权限）
#   ② .sh 写成 CRLF  ⇒ mac/linux 报 `\r: command not found`
#   ③ mcp.json 占位符少一处 ⇒ 收包人照抄注册，command/cwd/env 指着「<你放的那个目录>」
# 本函数只铺目录 + 打一次**临时** zip 再删掉：不跑 PyInstaller、不碰云端（不耗限流配额）。
SENTINEL_PAYLOAD = {"base": "http://127.0.0.1:1", "auth": ""}
PLACEHOLDER = "<你放的那个目录>"


class _SelfCheck:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, cond, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"   ✓ {name}")
        else:
            self.failed.append(name)
            print(f"   ✗ {name}" + (f"  [{detail}]" if detail else ""))
        return bool(cond)


def _zip_modes(zpath: str) -> dict:
    with zipfile.ZipFile(zpath) as z:
        return {i.filename: (i.external_attr >> 16) for i in z.infolist()}


def self_check() -> int:
    import json
    import tempfile

    c = _SelfCheck()
    print("铺包自检（不跑 PyInstaller、不碰云端）")

    # 凭据可有可无：自检验的是**结构**，不是凭据。没有 secrets.json 的机器也该能跑。
    import build_exe
    try:
        payload = build_exe.remote_payload()
    except Exception:                                            # noqa: BLE001
        payload = {"base": "", "auth": ""}
    degraded = not payload.get("base")
    if degraded:
        payload = dict(SENTINEL_PAYLOAD)
        print("   ℹ️ 无后端凭据（secrets.json 缺 remote_basic_auth）⇒ 用哨兵 base 只验结构")

    os.makedirs(DIST, exist_ok=True)

    for target in ("mac", "linux"):
        doc = "使用说明-%s.txt" % ("Mac" if target == "mac" else "Linux")
        build_sh = "build_%s.sh" % ("macos" if target == "mac" else "linux")
        print(f"\n-- {target} --")

        try:
            stage_root, pkg = stage_source(target, payload=payload)
        except Exception as e:                                   # noqa: BLE001
            c.ok(f"{target}: 铺包成功", False, str(e))
            continue
        c.ok(f"{target}: 铺包成功", True)

        # ① 文件集必须完整（少一个 = 收包人第 1 步就对不上说明）
        want = sorted(["tripplanner-mcp", "mcp/tripplanner_mcp_server.py",
                       "mcp/panel/plan_panel.html", "remote.json", "mcp.json",
                       build_sh, doc])
        got = sorted(os.path.relpath(os.path.join(r, n), pkg).replace(os.sep, "/")
                     for r, _d, fs in os.walk(pkg) for n in fs)
        c.ok(f"{target}: 文件集完整（{len(want)} 项）", got == want,
             f"多={sorted(set(got) - set(want))} 少={sorted(set(want) - set(got))}")

        # ② 启动器：LF + shebang + TRIPPLANNER_HOME（缺它则 server 找不到 remote.json/panel）
        lb = open(os.path.join(pkg, "tripplanner-mcp"), "rb").read()
        c.ok(f"{target}: 启动器无 CRLF", b"\r" not in lb)
        c.ok(f"{target}: 启动器 shebang + 注入 TRIPPLANNER_HOME",
             lb.startswith(b"#!/bin/bash") and b'export TRIPPLANNER_HOME="$DIR"' in lb)
        sb = open(os.path.join(pkg, build_sh), "rb").read()
        c.ok(f"{target}: {build_sh} 无 CRLF 且可解析", b"\r" not in sb and sb.startswith(b"#!/bin/bash"))

        # ③ 注册片段：占位符恰好 3 处（command / cwd / env），且是合法 JSON
        mj = open(os.path.join(pkg, "mcp.json"), encoding="utf-8").read()
        c.ok(f"{target}: mcp.json 占位符 3 处", mj.count(PLACEHOLDER) == 3,
             f"实际 {mj.count(PLACEHOLDER)}")
        try:
            entry = json.loads(mj)["mcpServers"]["tripplanner"]
            c.ok(f"{target}: mcp.json 结构（command/cwd/env）",
                 bool(entry.get("command") and entry.get("cwd")
                      and entry.get("env", {}).get("TRIPPLANNER_HOME")))
        except Exception as e:                                   # noqa: BLE001
            c.ok(f"{target}: mcp.json 结构（command/cwd/env）", False, str(e))

        # ④ remote.json 必须与启动器同层且可解析（少它 = 面板「后端不可达」）
        try:
            rj = json.load(open(os.path.join(pkg, "remote.json"), encoding="utf-8"))
            c.ok(f"{target}: remote.json 与启动器同层且可解析", bool(rj.get("base")))
        except Exception as e:                                   # noqa: BLE001
            c.ok(f"{target}: remote.json 与启动器同层且可解析", False, str(e))

        # ⑤ zip 回合：可执行位（丢它 = 解压后 Permission denied，且错误信息完全不提权限）
        tmp = os.path.join(tempfile.gettempdir(),
                           f"_bundle_selfcheck_{target}.zip")
        try:
            zip_pkg(target, stage_root, pkg, out_path=tmp)
            modes = _zip_modes(tmp)
            names = set(modes)
            arc_want = {"tripplanner-mcp/" + g for g in want}
            # 报错详情用「差集」而不是对称差：对称差会把「本来就有、只是不在包内」的
            # 文件也列进「多」，读的人会以为包里有它（踩过，观测缺陷也是缺陷）。
            c.ok(f"{target}: zip 含全部文件", names == arc_want,
                 f"多={sorted(names - arc_want)} 少={sorted(arc_want - names)}")
            c.ok(f"{target}: zip 内启动器带可执行位",
                 modes.get(f"tripplanner-mcp/{build_sh}", 0) & 0o111 != 0
                 and modes.get("tripplanner-mcp/tripplanner-mcp", 0) & 0o111 != 0)
            c.ok(f"{target}: zip 内数据文件不带可执行位",
                 modes.get("tripplanner-mcp/mcp.json", 1) & 0o111 == 0)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ⑥ win：exe 通常不存在（要 PyInstaller 现打）⇒ 有就验 4 件套，没有就跳过（不算失败）
    print("\n-- win --")
    exe = os.path.join(DIST, exe_name("win"))
    if os.path.exists(exe):
        missing = [n for n in ("remote.json", "mcp.json", "使用说明.txt")
                   if not os.path.exists(os.path.join(DIST, n))]
        c.ok("win: dist/ 四件套齐（exe + remote.json + mcp.json + 说明）",
             not missing, f"缺 {missing}")
    else:
        print("   · 跳过（dist/ 无 exe；跑 build_bundle --target win 后此项才有意义）")

    total = c.passed + len(c.failed)
    print(f"\n{'🟢' if not c.failed else '🔴'} 铺包自检 {c.passed}/{total}")
    if c.failed:
        for f in c.failed:
            print("   - " + f)
        return 1
    if degraded:
        print("   （注：本次无凭据，凭据正确性由 --target mac 真出包时验）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=TARGETS, help="产物目标系统（默认=当前系统）")
    ap.add_argument("--binary", action="store_true",
                    help="强制原生 PyInstaller 二进制（仅当 target 与当前系统一致）")
    ap.add_argument("--clean", action="store_true", help="先清 build/ 与 dist/")
    ap.add_argument("--no-verify", action="store_true", help="跳过冒烟（省一次真实云端调用）")
    ap.add_argument("--no-zip", action="store_true", help="只铺目录，不打包 zip")
    ap.add_argument("--stage-only", action="store_true", help="等价 --no-verify --no-zip")
    ap.add_argument("--self-check", action="store_true",
                    help="只验铺包链路（文件集/LF/可执行位/占位符）：不打 exe、不冒烟、不碰云端")
    args = ap.parse_args()
    if args.self_check:
        return self_check()
    if args.stage_only:
        args.no_verify = args.no_zip = True

    target = args.target or current_target()
    here = current_target()
    if args.binary and target != here:
        return _fail(f"--binary 只能在本机 OS 上构建（PyInstaller 不支持交叉编译）："
                     f"target={target}，本机={here}")
    if target == "win" and here != "win":
        return _fail("win 形态（exe）必须在 Windows 上构建；其它系统请用 --target mac/linux 出源码包")

    if args.clean:
        for d in (os.path.join(REPO, "build"), DIST):
            shutil.rmtree(d, ignore_errors=True)
            print(f"已清理 {d}")
    os.makedirs(DIST, exist_ok=True)

    for p in (SERVER, PANEL):
        if not os.path.exists(p):
            return _fail(f"找不到 {p}")

    # ---- 1) 铺包 ----
    print(f"\n=== 目标形态：{target}（本机 {here}）===")
    try:
        if target == "win":
            stage_root, pkg = stage_win(args)
        else:
            stage_root, pkg = stage_source(target)
    except Exception as e:                                       # noqa: BLE001
        return _fail(str(e))
    print("✓ 已铺包：" + os.path.relpath(pkg, REPO))
    for root, _dirs, files in os.walk(pkg):
        for n in sorted(files):
            p = os.path.join(root, n)
            print("   %-46s %8d B" % (os.path.relpath(p, pkg).replace(os.sep, "/"),
                                      os.path.getsize(p)))

    # ---- 2) 冒烟 ----
    if not args.no_verify:
        print("\n=== 冒烟（消费方路径）===")
        r = verify(target, pkg, args.binary)
        bundle_check.print_result(r)
        # 冒烟会在包目录里产生 output/ —— 分享包不带运行时状态
        shutil.rmtree(os.path.join(pkg, "output"), ignore_errors=True)
        if r.failed:
            print("\n[FAIL] 冒烟未通过：")
            for f in r.failed:
                print("   - " + f)
            return 1
        if target != here and not args.binary:
            print("\n  ℹ️  说明：源码包布局 + 协议层已验；启动器那十几行 bash 需在目标机实测。")
        print("[PASS] 冒烟全绿")

    # ---- 3) 打包 ----
    if args.no_zip:
        print("\n（--no-zip）目录已铺好：" + os.path.relpath(pkg, REPO))
        return 0
    zpath = zip_pkg(target, stage_root, pkg)
    print("\n" + "=" * 66)
    print(f"可以分享了：{os.path.relpath(zpath, REPO)}"
          f"（{os.path.getsize(zpath) / 1048576:.2f} MB）")
    print("⚠️ remote.json 内含**明文口令** —— 只发给自己信任的人。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
