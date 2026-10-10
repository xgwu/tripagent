#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 TripPlanner MCP App 打成**单文件 exe**，产出可直接分享的目录。

用法（Windows）：
    D:\\Users\\xgwu\\.workbuddy\\binaries\\python\\envs\\tripagent\\Scripts\\python.exe mcp/build_exe.py
    ... mcp/build_exe.py --clean          # 先清 build/ dist/
    ... mcp/build_exe.py --base http://1.2.3.4 --auth user:pass   # 覆盖 remote.json 内容

产出 `dist/`：
    tripplanner-mcp.exe   单文件，对方**零依赖**（连 Python 都不用装）
    remote.json           后端地址 + Basic 凭据 ← 与 exe 必须同目录
    mcp.json              **含 `<你放的那个目录>` 占位符**的注册片段（给对方改路径用）
    使用说明.txt           给对方看的三步（含 SmartScreen / 排障 / 卸载）
                          ⚠️ 本机自用别改这个文件，跑 `mcp/register.py --exe <path>` 即可。

⚠️ 安全提示：`remote.json` 里是**明文口令**（「凭据随包分发」是本人的明确取舍：
   免去对方配置成本）。因此**只发给自己信任的人**，且这条链路上后端是 http 明文
   （公网 IP 直连）—— 想更稳就上 HTTPS（见 deploy/cvm/README 与 mcp/README 的 M4 节）。

⚠️ 为什么能打成这么小：M5 的 server 是**瘦客户端**，实际 import 全是标准库
   （自检里有 AST 守卫守着这条）。打包时再显式 `--exclude-module` 掉 `src` /
   `ortools` 等：即使将来有人不小心把内核引回来，也会在打包阶段**当场暴露**，
   而不是打出一个几百 MB 的包。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(HERE, "tripplanner_mcp_server.py")
PANEL = os.path.join(HERE, "panel", "plan_panel.html")
EXE_NAME = "tripplanner-mcp"
DIST = os.path.join(REPO, "dist")
WORK = os.path.join(REPO, "build", "mcp-exe")

# 显式排除（瘦客户端本就不该依赖它们；留着是**打包地雷**）
EXCLUDES = ["src", "ortools", "numpy", "pandas", "matplotlib", "tkinter",
            "PIL", "scipy", "requests", "yaml", "lxml"]

USE_TXT = """TripPlanner MCP App —— 使用说明
================================

共 3 步。全程不需要装 Python、不需要任何依赖。

【1】放好文件
    把**整个文件夹**（不是只拿 exe）拷到任意目录，例：D:\\TripPlannerMCP
    拷完确认这三样还在同一层：
        tripplanner-mcp.exe     主程序
        remote.json             后端地址（**必须与 exe 同目录**，否则连不上）
        mcp.json                注册片段（第 2 步用）
    ⚠️ 不要放进 OneDrive / 网盘同步目录 —— 同步会占用文件，可能导致启动失败。

【2】注册到 WorkBuddy（二选一）

    ▸ 推荐：表单添加（保存即生效，不用重启）
      WorkBuddy → 左下角「+」→ 连接器 → 到「自定义 MCP」→ 添加，按下面填：
          名称    tripplanner
          类型    stdio
          命令    <你放的那个目录>\\tripplanner-mcp.exe
          参数    （留空）
          环境    （留空）
      填完在连接器列表里找到 tripplanner → 点「信任」。
      生效判据：对话里问「有哪些城市可以规划」，能列出 31 个城市即成功。

    ▸ 备选：改 mcp.json
      把本文件夹里 mcp.json 的 <你放的那个目录> 全部替换成你的真实目录
      （共 3 处：command / cwd / TRIPPLANNER_HOME），再把这段合并进
      %USERPROFILE%\\.workbuddy\\mcp.json，然后**完全退出** WorkBuddy 再启动
      （宿主只在启动时读一次这个文件）。

【3】开始用
    在对话里说「帮我规划杭州2天，喜欢自然风光」，
    行程面板会渲染在对话下方（时间线 / 每日主题与里程 / POI 缩略图 / 路线地图）。
    面板里可「🔁 换一个」「⠿ 拖拽调顺序」「⬇ 导出 Markdown」。

【说明】
- 行程规划（含大模型）全部在**云端**完成；本机不跑模型、不需要密钥、不需要联网下载数据。
- 面板地图与缩略图需要联网（高德）。断网时行程照样能看，只是没有地图。
- 首次运行 Windows 可能弹「已保护你的电脑」（SmartScreen）——
  PyInstaller 打的单文件 exe 常见误报，点「更多信息 → 仍要运行」即可；
  若杀软隔离，把该目录加入白名单。
- 面板显示「后端不可达」时，按顺序查：
    ① remote.json 是否与 exe 同目录；
    ② 浏览器打开 {base}/api/health 是否弹出账号密码框（要能访问才算通）；
    ③ 对话里问「有哪些城市可以规划」，能列出即服务正常。
- 行程缓存在「exe 同目录的 output\\」下，删掉即可重置。
  面板里「重新拉取行程」依赖云端会话（保留 2 小时），过期后重新生成一次即可。
- 所有回包都可能是降级结果（云端限流/网络抖动时）：面板会如实标红，不会假装成功。

【卸载】删掉这个文件夹，并在 WorkBuddy 连接器里移除 tripplanner 即可。
"""


def _fail(msg: str) -> int:
    print("✗ " + msg)
    return 1


def exe_path() -> str:
    return os.path.join(DIST, EXE_NAME + (".exe" if os.name == "nt" else ""))


def _unlock(target: str) -> None:
    """腾出被占用的产物文件。

    ⚠️ 2026-10-10 实踩：宿主**按需 spawn** 本 exe，且进程不会立刻回收 ⇒ 重新打包时
    `dist/tripplanner-mcp.exe` 被占用。Windows 下**运行中的 exe 删不掉、但能改名**
    （loader 以 FILE_SHARE_DELETE 打开），所以先改名腾位，再让 PyInstaller 写新文件。
    （顺带说明：本机沙箱还把 `os.remove` 重定向到 safe-delete/回收站，同样会失败。）
    """
    if not os.path.exists(target):
        return
    try:
        os.remove(target)
        return
    except OSError:
        pass
    aside = target + ".old"
    if os.path.exists(aside):
        try:
            os.remove(aside)
        except OSError:
            aside = f"{target}.old{os.getpid()}"
    try:
        os.replace(target, aside)
        print(f"⚠️ {os.path.basename(target)} 正被占用（宿主进程还活着）"
              f"：已改名为 {os.path.basename(aside)} 腾位")
    except OSError as e:                                          # noqa: BLE001
        print(f"⚠️ 腾不出 {os.path.basename(target)}（{e}）—— 构建可能失败")


def remote_payload(base: str | None = None, auth: str | None = None) -> dict:
    """`remote.json` 的内容（线上后端地址 + 凭据）—— **唯一实现**。

    `build_bundle.py` 也要写 remote.json，所以这里必须只有一个出口：两处各写一遍
    就会漂移（本项目已因「同一口径多处各写」吃过教训）。
    配置来源 = server 模块的 `remote_config()`（其内部已处理 secrets.json / 环境优先级）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tpmcp_build", SERVER)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    cfg = srv.remote_config()
    return {"base": (base or cfg.get("base") or "").rstrip("/"),
            "auth": auth or cfg.get("auth") or ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="先删除 build/ 与 dist/")
    ap.add_argument("--base", help="覆盖 remote.json 的后端 base（默认取 secrets.json / 环境变量）")
    ap.add_argument("--auth", help='覆盖 remote.json 的凭据，形如 "user:pass"')
    ap.add_argument("--no-dist-docs", action="store_true", help="不生成 mcp.json / 使用说明.txt")
    args = ap.parse_args()

    if not os.path.exists(SERVER):
        return _fail(f"找不到 {SERVER}")
    if not os.path.exists(PANEL):
        return _fail(f"找不到面板文件 {PANEL}")

    if args.clean:
        for d in (DIST, os.path.join(REPO, "build")):
            shutil.rmtree(d, ignore_errors=True)
            print(f"已清理 {d}")
        # 被宿主占用时 rmtree 会静默跳过 ⇒ 这里再显式腾一次位
        _unlock(exe_path())

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✗ 没装 PyInstaller。请先执行：")
        print(f'    "{sys.executable}" -m pip install --upgrade pyinstaller')
        return 1

    # ---- 1) 依赖自查：只准标准库（否则 exe 会莫名其妙巨大 / 打不出来）----
    import ast
    with open(SERVER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    third = sorted(m for m in mods
                   if m not in sys.stdlib_module_names and m != "__future__")
    print(f"实际 import：{sorted(mods)}")
    if third:
        return _fail(f"瘦客户端出现了非标准库依赖：{third} —— 先把它改掉再打包")
    print("✓ 依赖全是标准库（打包后对方零依赖）")

    # ---- 2) PyInstaller 单文件 ----
    os.makedirs(DIST, exist_ok=True)
    _unlock(exe_path())      # 宿主可能正拿着旧 exe（见 _unlock 注释）
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", EXE_NAME,
        # 面板 HTML 必须随包（resources/read 直接读文件返回给宿主）
        "--add-data", f"{PANEL}{os.pathsep}panel",
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", WORK,
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(SERVER)
    print("\n$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return _fail(f"PyInstaller 失败（exit={rc}）")
    exe = os.path.join(DIST, EXE_NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.exists(exe):
        return _fail(f"没看到产物 {exe}")
    print(f"\n✓ exe 已生成：{exe}  （{os.path.getsize(exe) / 1048576:.1f} MB）")

    # ---- 3) remote.json（**随包分发**的后端地址 + 凭据）----
    sys.path.insert(0, REPO)
    payload = remote_payload(args.base, args.auth)
    base, auth = payload["base"], payload["auth"]
    if not base:
        return _fail("没有后端 base（secrets.json 里配 remote_basic_auth，或用 --base 指定）")
    rp = os.path.join(DIST, "remote.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({"base": base, "auth": auth}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✓ remote.json 已写入（base={base}，auth={'有' if auth else '**无**'}）")
    if not auth:
        print("  ⚠️ 没有凭据：后端开了 Basic 认证的话，面板会如实报「后端不可达」。")

    # ---- 4) 注册片段 + 使用说明 ----
    if not args.no_dist_docs:
        # ⚠️ 这里**故意写占位符**而不是本机绝对路径。
        #    为什么：这个文件是给**收包的人**看的；写死 `D:/Users/xgwu/...`
        #    会让对方原样粘贴后得到一个「看着对、实际不存在」的路径，
        #    宿主只报一句 spawn 失败，极难自查。占位符则逼着对方改，改了才对。
        #    本机自用不走这个文件 —— 走 `mcp/register.py --exe <path>`。
        ph = "<你放的那个目录>"
        entry = {"type": "stdio", "command": ph + "/tripplanner-mcp.exe", "args": [],
                 "cwd": ph,
                 # TRIPPLANNER_HOME 指到包目录：remote.json / output 都在这里
                 "env": {"TRIPPLANNER_HOME": ph}}
        mp = os.path.join(DIST, "mcp.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"tripplanner": entry}}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tp = os.path.join(DIST, "使用说明.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(USE_TXT.format(base=base))
        print("✓ mcp.json（含占位符，给对方改路径）/ 使用说明.txt 已写入")
        print(f"  本机自用请跑：python mcp/register.py --exe {exe}")

    print("\n" + "=" * 68)
    print("可以分享了：把 **整个 dist/ 目录**（exe + remote.json + 说明）打包发出去。")
    print("⚠️ remote.json 内含**明文口令** —— 只发给自己信任的人。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
