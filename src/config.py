# -*- coding: utf-8 -*-
"""统一配置加载：secrets.json（密钥，gitignore）+ config.json（业务配置，进 git）合并。

合并顺序：config.json 为底，secrets.json 逐键覆盖（同名键密钥优先）。
任一文件缺失均容忍（返回另一份或空 dict）。webui/server.py 与 scripts/ 共用。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config() -> dict:
    cfg = {}
    for name in ("config.json", "secrets.json"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except (json.JSONDecodeError, OSError):
            pass  # 解析失败按缺文件处理，调用方自行兜底
    return cfg
