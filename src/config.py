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


# 密钥/配置 → 环境变量 的映射（与 webui/server.py 的注入表保持一致，
# 改这里必须同步改那边，反之亦然）
ENV_MAP = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "amap_key": "AMAP_KEY",
    "m1_model": "M1_MODEL",
}


def apply_env(cfg: dict | None = None) -> list:
    """把配置注入 os.environ，返回实际注入的变量名列表（CLI / 脚本入口用）。

    ⚠️ 为什么需要它：`load_config()` 只**返回**合并后的 dict，从不写环境变量；
    而 llm_client / 高德调用一律惰性读 os.environ。CLI 或脚本直接调 planner 时
    若不注入，LLM 会**静默降级**到离线链路——表现为「跑得飞快（0.0s）、结果看着
    还挺合理」，极易被误判成验证通过。2026-09-15 实际踩到：`eval_regression --llm`
    与多组冒烟全部 0.0s 完成，实为全程降级（llm_client 抛 "no api key" 被上层
    吞掉后走 m1 兜底）。webui/server.py 有自己的注入逻辑，不受影响。

    不覆盖已存在的环境变量（显式 export 优先）。
    """
    data = cfg if cfg is not None else load_config()
    injected = []
    for cfg_key, env_key in ENV_MAP.items():
        val = data.get(cfg_key)
        if val and not os.environ.get(env_key):
            os.environ[env_key] = str(val)
            injected.append(env_key)
    return injected
