# -*- coding: utf-8 -*-
"""对照 HEAD 与工作区，找出「被删除但**仍被真实引用**」的顶层名字。

为什么需要它：M5 删死码时把 HEAD 的 632-859 行整段误删（228 行），其中
`class _Session` / `SESSION` / `TOOLS` / `TOOL_BY_NAME` 四个定义**仍在被使用**
—— 但只有 `tools/call` 路径才碰 `TOOL_BY_NAME`，所以 `import` 静默正常，
直到「打包产物冒烟」才炸出来。这类「整段误删」必须用工具兜，不能靠眼睛。

判定用 AST：把**字符串常量清空**再 unparse ⇒ 注释、docstring、提示文案里
的解释性提及（例如「☠️ M5 已删除：`kernel()` …」）不会误报。
"""
import ast
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本轮 M5 动过的所有 py 文件（HEAD 可比对的）
TARGETS = [
    "mcp/tripplanner_mcp_server.py",
    "mcp/panel_guard.py",
    "mcp/register.py",
    "mcp/selftest.py",
    "src/plan_view.py",
    "webui/server.py",
]


def strip_literals(tree):
    """把所有字符串常量清空 —— 注释/docstring 里的提及不该算引用。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return tree


def top_names(src: str):
    tree = ast.parse(src)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def code_only(src: str) -> str:
    """剥掉字符串内容后的源码（用于搜引用）。"""
    return ast.unparse(strip_literals(ast.parse(src)))


def assigned_anywhere(src: str) -> set:
    """全文件（任意作用域）里被赋值过的名字。

    为什么需要：`ENTRY` 这种「HEAD 里是顶层常量、改成在 main() 分支内赋值」是
    合法重构 —— 只看顶层名会误报。真正危险的是**任何地方都没赋值**却仍被调用。
    """
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
    return out


def head_src(path: str):
    out = subprocess.run(["git", "show", "HEAD:" + path.replace(os.sep, "/")],
                         cwd=ROOT, capture_output=True)
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8")


def main() -> int:
    rc = 0
    for rel in TARGETS:
        full = os.path.join(ROOT, rel)
        if not os.path.exists(full):
            continue
        with io.open(full, encoding="utf-8") as f:
            cur = f.read()
        old = head_src(rel)
        if old is None:
            print(f"[新增文件] {rel}  (HEAD 无基线，跳过)")
            continue

        assigned = assigned_anywhere(cur)
        removed = sorted(top_names(old) - top_names(cur))
        body = code_only(cur)
        risky = [n for n in removed
                 if n not in assigned and (
                     re.search(r"(?<![\w.])%s\s*[(.\[]" % re.escape(n), body)
                     or re.search(r"(?<![\w.])%s\b\s*(?==|,|\)|:|$)" % re.escape(n),
                                  body, re.M))]

        if risky:
            rc = 1
            print(f"❌ {rel} —— 已删除但**代码里仍在引用**：")
            for n in risky:
                lines = [i + 1 for i, ln in enumerate(code_only(cur).splitlines())
                         if re.search(r"(?<![\w.])%s\b" % re.escape(n), ln)]
                print(f"     - {n}  (unparse 后行 {lines[:8]})")
        else:
            print(f"✅ {rel} —— 消失 {len(removed)} 个顶层名，全部无残留引用")
    return rc


if __name__ == "__main__":
    sys.exit(main())
