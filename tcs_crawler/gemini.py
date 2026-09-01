"""Gemini REST 客户端（只用标准库）。

仓库其它部分都是零第三方依赖，这里也保持一致：直接打 REST 接口，CI 上不用
pip install 任何东西。key 通过 header 传，不进 URL，免得出现在日志里。

结构化输出走 responseSchema，让模型只能返回约定形状的 JSON——比让它写自由文本
再去正则抠字段稳得多。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY_FILE = Path(__file__).resolve().parent.parent / "api_keys" / "gemini_api.txt"
DEFAULT_MODEL = "gemini-3.6-flash"

RETRY_STATUS = {429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    pass


def load_key(explicit: str | None = None) -> str | None:
    """环境变量 GEMINI_API_KEY 优先（CI 用 secret 注入），其次本地 key 文件。"""
    if explicit:
        return explicit.strip()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    return None


class Gemini:
    def __init__(
        self,
        key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        max_retries: int = 4,
        delay: float = 1.0,
    ):
        self.key = key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.stats = {"calls": 0, "retries": 0, "failures": 0, "in_tokens": 0, "out_tokens": 0}
        self._last = 0.0

    def json(self, prompt: str, schema: dict, temperature: float = 0.3,
             max_tokens: int = 4096) -> dict | None:
        """要一段符合 schema 的 JSON 回来；失败返回 None，由调用方决定怎么降级。"""
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        raw = self._post(body)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            # 开了 thinking 的模型偶尔会把预算花光、正文截断成半截 JSON
            log.warning("模型返回的不是合法 JSON（前 200 字）：%s", raw[:200])
            self.stats["failures"] += 1
            return None

    def _post(self, body: dict) -> str | None:
        url = f"{BASE}/models/{self.model}:generateContent"
        data = json.dumps(body).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

            req = urllib.request.Request(
                url, data=data,
                headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
            )
            try:
                self.stats["calls"] += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return self._extract(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code not in RETRY_STATUS or attempt == self.max_retries:
                    log.error("Gemini HTTP %s：%s", e.code, detail)
                    self.stats["failures"] += 1
                    return None
                sleep_for = min(120.0, 5.0 * (2 ** attempt))
                log.warning("Gemini HTTP %s，%.0fs 后重试 (%d/%d)",
                            e.code, sleep_for, attempt + 1, self.max_retries)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                if attempt == self.max_retries:
                    log.error("Gemini 请求失败：%s", e)
                    self.stats["failures"] += 1
                    return None
                sleep_for = min(120.0, 5.0 * (2 ** attempt))
                log.warning("Gemini 网络错误 %s，%.0fs 后重试", e, sleep_for)
            self.stats["retries"] += 1
            time.sleep(sleep_for)
        return None

    def _extract(self, payload: dict) -> str | None:
        usage = payload.get("usageMetadata", {})
        self.stats["in_tokens"] += usage.get("promptTokenCount", 0) or 0
        self.stats["out_tokens"] += usage.get("candidatesTokenCount", 0) or 0

        cands = payload.get("candidates") or []
        if not cands:
            log.warning("Gemini 没有返回候选（可能被安全策略拦下）")
            self.stats["failures"] += 1
            return None
        cand = cands[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
        if not text:
            log.warning("Gemini 返回正文为空，finishReason=%s", cand.get("finishReason"))
            self.stats["failures"] += 1
            return None
        return text

    def report(self) -> str:
        s = self.stats
        return (f"Gemini 调用 {s['calls']} 次（重试 {s['retries']}，失败 {s['failures']}），"
                f"token in/out {s['in_tokens']}/{s['out_tokens']}")
