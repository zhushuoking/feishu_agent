"""大模型调用客户端（OpenAI 兼容接口）。

为什么不用 openai 官方包：这里只需要一个 POST，多一个依赖就多一份版本兼容风险。
任何提供 /chat/completions 的服务都能用：DeepSeek、通义、智谱、Kimi、本地 Ollama 等。

.env 需要三项：
    MODEL_BASE_URL=https://api.deepseek.com/v1
    MODEL_API_KEY=sk-xxxx
    MODEL_NAME=deepseek-chat
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

# 在这里统一加载 .env：try_detect / set_model / main 任何入口都能拿到配置，
# 不用每个脚本各写一遍（之前漏了，表现为「模型未配置」但 .env 明明填好了）
load_dotenv()

DEFAULT_TIMEOUT = 30.0
MAX_RETRY = 2


class LLMError(RuntimeError):
    """模型调用失败。上层据此把事件转人工，不要吞掉。"""


def _config() -> Dict[str, str]:
    return {
        "base_url": os.getenv("MODEL_BASE_URL", "").strip().rstrip("/"),
        "api_key": os.getenv("MODEL_API_KEY", "").strip(),
        "model": os.getenv("MODEL_NAME", "").strip(),
    }


def is_configured() -> bool:
    cfg = _config()
    return all(cfg.values())


def _extract_json(raw: str) -> Dict[str, Any]:
    """从模型返回里抠出 JSON。

    即使开了 JSON Mode，个别服务仍会包一层 ```json 代码块，这里统一兜底。
    """
    text = (raw or "").strip()
    if not text:
        raise LLMError("模型返回为空")

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"返回内容不是合法 JSON：{exc}") from exc

    raise LLMError(f"返回内容里找不到 JSON：{text[:200]}")


def _ensure_direct(target_url: str) -> None:
    """让模型域名绕过本机代理。

    代理会把 TLS 连接掐断（SSL: UNEXPECTED_EOF_WHILE_READING），而 DeepSeek / 通义 /
    智谱这些国内服务本来也不需要代理。真要接需要代理的境外服务，把
    MODEL_BYPASS_PROXY 设成 0 即可恢复走系统代理。
    """
    if os.getenv("MODEL_BYPASS_PROXY", "1").strip() == "0":
        return
    host = urlparse(target_url).hostname
    if not host:
        return
    current = os.getenv("NO_PROXY", "")
    if host not in current:
        merged = ",".join(filter(None, [current, host]))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged


def _model_url(cfg: Dict[str, str]) -> str:
    url = f"{cfg['base_url']}/chat/completions"
    _ensure_direct(url)
    return url


def chat_json(system_prompt: str, user_prompt: str, *,
              temperature: float = 0.1,
              model: Optional[str] = None) -> Dict[str, Any]:
    """调用模型并返回解析后的 dict。失败抛 LLMError。"""
    cfg = _config()
    if not is_configured():
        raise LLMError(
            "模型未配置。请在 .env 里填 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME"
        )

    url = f"{cfg['base_url']}/chat/completions"
    _ensure_direct(url)
    payload: Dict[str, Any] = {
        "model": model or cfg["model"],
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    timeout = float(os.getenv("MODEL_TIMEOUT", DEFAULT_TIMEOUT))
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRY + 1):
        try:
            body = dict(payload)
            # JSON Mode 能显著减少废话，但不是所有服务都支持，所以放在第二轮去重试时去掉
            if attempt == 0:
                body["response_format"] = {"type": "json_object"}

            resp = httpx.post(url, json=body, headers=headers, timeout=timeout)

            if resp.status_code in (400, 404, 422) and "response_format" in body:
                # 服务不认识 JSON Mode，去掉重试
                body.pop("response_format", None)
                resp = httpx.post(url, json=body, headers=headers, timeout=timeout)

            if resp.status_code == 429:
                raise LLMError("模型限流（429），稍后重试")
            if resp.status_code >= 400:
                raise LLMError(f"模型返回 {resp.status_code}：{resp.text[:300]}")

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return _extract_json(content)

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < MAX_RETRY:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise LLMError(f"网络异常，重试 {MAX_RETRY} 次仍失败：{exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"返回结构不符合 OpenAI 格式：{exc}") from exc

    raise LLMError(f"调用失败：{last_error}")


def chat_text(system_prompt: str, user_prompt: str, *,
              temperature: float = 0.3) -> str:
    """纯文本调用，调试提示词时用。"""
    cfg = _config()
    if not is_configured():
        raise LLMError("模型未配置，请在 .env 填入 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME")

    resp = httpx.post(
        _model_url(cfg),
        json={
            "model": cfg["model"],
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=float(os.getenv("MODEL_TIMEOUT", DEFAULT_TIMEOUT)),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
