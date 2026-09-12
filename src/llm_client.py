# -*- coding: utf-8 -*-
"""OpenAI 兼容 Chat API 客户端（纯标准库 urllib）。"""
import json, os, urllib.request

DEFAULT_BASE = "https://api.deepseek.com"


def _cfg():
    """优先 DeepSeek；否则退回 OpenAI 兼容配置。"""
    dk = os.environ.get("DEEPSEEK_API_KEY") or ""
    if dk:
        return dk, "https://api.deepseek.com", os.environ.get("M1_MODEL") or "deepseek-chat"
    key = os.environ.get("OPENAI_API_KEY") or ""
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("M1_MODEL") or "gpt-4o-mini"
    return key, base, model


def llm_available() -> bool:
    return bool(_cfg()[0])


def chat(messages: list, temperature: float = 0.4, response_json: bool = True,
         timeout: int = 120, seed: int | None = 42, retries: int = 3):
    """调用 chat/completions；429/5xx 指数退避重试；最终失败抛异常，由上层降级。"""
    import time
    body_base = {"model": _cfg()[2], "messages": messages, "temperature": temperature}
    if seed is not None:
        body_base["seed"] = seed  # 评测可复现性：固定采样种子
    if response_json:
        body_base["response_format"] = {"type": "json_object"}
    last_err = None
    for attempt in range(retries):
        try:
            return _post(body_base, timeout)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt * 2)  # 2s / 4s / 8s
                continue
            raise
    raise last_err


def _post(body: dict, timeout: int):
    api_key, base, _ = _cfg()
    if not api_key:
        raise RuntimeError("no api key")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_json_safe(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json object found")
    return json.loads(text[start:end + 1])
