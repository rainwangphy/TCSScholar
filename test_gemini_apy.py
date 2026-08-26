#!/usr/bin/env python3
"""Gemini API 连通性测试。

用法:
    python3 test_gemini_apy.py                 # 跑全部检查
    python3 test_gemini_apy.py -m gemini-2.5-pro
    python3 test_gemini_apy.py -p "用一句话解释 P vs NP"

API key 读取顺序: 环境变量 GEMINI_API_KEY -> api_keys/gemini_api.txt
只依赖 requests, 直接调 REST 接口, 不需要装 google-genai。
"""

import argparse
import json
import os
import pathlib
import sys
import time

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY_FILE = pathlib.Path(__file__).with_name("api_keys") / "gemini_api.txt"
DEFAULT_MODEL = "gemini-3.6-flash"
TIMEOUT = 60


def load_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        print(f"[key] 来自环境变量 GEMINI_API_KEY ({mask(key)})")
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            print(f"[key] 来自 {KEY_FILE.name} ({mask(key)})")
            return key
    sys.exit(f"找不到 API key: 设置 GEMINI_API_KEY 或写入 {KEY_FILE}")


def mask(key: str) -> str:
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "…"


def headers(key: str) -> dict:
    # 用 header 传 key, 避免 key 出现在 URL / 日志里
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def explain(resp: requests.Response) -> str:
    hints = {
        400: "请求体或 key 格式有问题",
        401: "key 无效",
        403: "key 无权限, 或该 key 未开通 Generative Language API",
        404: "模型名不存在 (先看 list_models 的输出)",
        429: "配额用完 / 频率超限",
    }
    hint = hints.get(resp.status_code, "")
    try:
        detail = resp.json().get("error", {}).get("message", resp.text[:300])
    except ValueError:
        detail = resp.text[:300]
    return f"HTTP {resp.status_code} {hint}\n       {detail}"


def list_models(key: str) -> list:
    print("\n=== 1. 列出可用模型 ===")
    try:
        r = requests.get(f"{BASE}/models", headers=headers(key), timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  ✗ 网络错误: {e}")
        return []
    if r.status_code != 200:
        print(f"  ✗ {explain(r)}")
        return []
    models = [
        m["name"].removeprefix("models/")
        for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    print(f"  ✓ 支持 generateContent 的模型 {len(models)} 个, 部分列出:")
    for name in models[:15]:
        print(f"      {name}")
    if len(models) > 15:
        print(f"      … 其余 {len(models) - 15} 个省略")
    return models


def generate(key: str, model: str, prompt: str) -> bool:
    print(f"\n=== 2. 单轮生成 (model={model}) ===")
    print(f"  prompt: {prompt}")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
    }
    t0 = time.time()
    try:
        r = requests.post(
            f"{BASE}/models/{model}:generateContent",
            headers=headers(key),
            json=body,
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"  ✗ 网络错误: {e}")
        return False
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"  ✗ {explain(r)}")
        return False

    data = r.json()
    cands = data.get("candidates", [])
    if not cands:
        print(f"  ✗ 没有候选返回 (可能被安全策略拦截): {json.dumps(data)[:300]}")
        return False
    cand = cands[0]
    text = "".join(
        p.get("text", "") for p in cand.get("content", {}).get("parts", [])
    ).strip()
    usage = data.get("usageMetadata", {})
    print(f"  ✓ {dt:.2f}s | finishReason={cand.get('finishReason')} | "
          f"token in/out/total="
          f"{usage.get('promptTokenCount')}/{usage.get('candidatesTokenCount')}/"
          f"{usage.get('totalTokenCount')}")
    if not text:
        # 2.5 系列开了 thinking 时, 可能全部预算都花在思考上
        print("  ! 正文为空, 原始返回:", json.dumps(cand)[:400])
        return False
    print("  ---- 回复 ----")
    for line in text.splitlines():
        print(f"  {line}")
    return True


def stream(key: str, model: str, prompt: str) -> bool:
    print(f"\n=== 3. 流式生成 (model={model}) ===")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    try:
        r = requests.post(
            f"{BASE}/models/{model}:streamGenerateContent",
            headers=headers(key),
            params={"alt": "sse"},
            json=body,
            timeout=TIMEOUT,
            stream=True,
        )
    except requests.RequestException as e:
        print(f"  ✗ 网络错误: {e}")
        return False
    if r.status_code != 200:
        print(f"  ✗ {explain(r)}")
        return False

    chunks = 0
    print("  ", end="", flush=True)
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        for cand in obj.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if part.get("text"):
                    chunks += 1
                    print(part["text"].replace("\n", "\n  "), end="", flush=True)
    print()
    if chunks == 0:
        print("  ✗ 没收到任何文本分片")
        return False
    print(f"  ✓ 收到 {chunks} 个分片")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="测试 Gemini API 是否可用")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"默认 {DEFAULT_MODEL}")
    ap.add_argument("-p", "--prompt", default="用一句中文回答: 你现在用的是哪个模型?")
    ap.add_argument("--skip-list", action="store_true", help="跳过列模型")
    ap.add_argument("--skip-stream", action="store_true", help="跳过流式测试")
    args = ap.parse_args()

    key = load_key()
    results = {}

    if not args.skip_list:
        models = list_models(key)
        results["list_models"] = bool(models)
        if models and args.model not in models:
            print(f"\n  ! 模型 {args.model} 不在可用列表里, 可用的比如: {models[:3]}")

    results["generate"] = generate(key, args.model, args.prompt)
    if not args.skip_stream:
        results["stream"] = stream(key, args.model, "从 1 数到 5, 每个数字一行。")

    print("\n=== 结果 ===")
    for name, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
