"""交互式配置大模型服务，写完立即发一次真实请求验证。

为什么不用手改 .env：Base URL 少写个 /v1、key 多带个换行、模型名写错，
这三种错误全都表现为「调用失败」，靠猜很费时间。这里一次问清楚，当场验证。

用法：双击 set_model.bat
"""

import os
import sys

import llm

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

PRESETS = [
    ("DeepSeek（推荐，便宜、中文强）", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("阿里百炼 / 通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("本地 Ollama（免费，需先启动 ollama）", "http://localhost:11434/v1", "qwen2.5:3b"),
    ("自定义（自己填 Base URL 和模型名）", "", ""),
]


def set_key(lines, key, value):
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    return out


def main():
    print("=" * 56)
    print("  配置大模型服务")
    print("=" * 56)
    for i, (name, _, _) in enumerate(PRESETS, 1):
        print(f"  {i}. {name}")
    print()

    choice = input("选一个（1-5，直接回车默认 1）: ").strip() or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= len(PRESETS):
        print("输入无效")
        return
    name, base_url, model = PRESETS[int(choice) - 1]

    if not base_url:
        base_url = input("Base URL（到 /v1 这一层）: ").strip().rstrip("/")
        model = input("模型名: ").strip()

    key = input("API Key（Ollama 随便填 ollama）: ").strip().strip('"').strip("'")

    if not base_url or not model or not key:
        print("\n[错误] 三项都不能为空")
        return

    if not base_url.endswith("/v1") and "compatible-mode" not in base_url and "paas/v4" not in base_url:
        print(f"[提醒] Base URL 一般是到 /v1 结尾，你的写的是 {base_url}，如果失败记得检查")

    with open(ENV, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    lines = set_key(lines, "MODEL_BASE_URL", base_url)
    lines = set_key(lines, "MODEL_API_KEY", key)
    lines = set_key(lines, "MODEL_NAME", model)
    with open(ENV, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\n已写入 .env：{name}")
    print(f"  BASE_URL = {base_url}")
    print(f"  MODEL    = {model}")
    print(f"  KEY      = {key[:6]}…{key[-4:]}（长度 {len(key)}）")

    # 写进 os.environ，不然下面这次验证读不到新值
    os.environ["MODEL_BASE_URL"] = base_url
    os.environ["MODEL_API_KEY"] = key
    os.environ["MODEL_NAME"] = model

    print("\n正在发一条真实请求验证（大概几秒）...")
    try:
        reply = llm.chat_text(
            "你是一个连通性测试助手。",
            "只回复两个字：可以",
            temperature=0,
        )
    except llm.LLMError as exc:
        print(f"\n[失败] {exc}")
        print("\n排查顺序：")
        print("  1. API Key 是否完整（有没有少字符、有没有带空格）")
        print("  2. Base URL 是否到 /v1 这一层结束")
        print("  3. 模型名是否拼错（deepseek-chat / qwen-plus / glm-4-flash）")
        print("  4. 账户是否还有额度")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"\n[失败] {type(exc).__name__}: {exc}")
        return

    print(f"\n[成功] 模型返回：{reply.strip()[:60]}")
    print("\n下一步：")
    print("  1. 先跑  python try_detect.py --batch   调提示词")
    print("  2. 再双击 run.bat，在群里发一条真任务")


if __name__ == "__main__":
    sys.exit(main())
