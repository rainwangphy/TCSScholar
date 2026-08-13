"""带限速、重试和本地缓存的 HTTP 客户端（只用标准库，无第三方依赖）。

DBLP 对自动化访问的要求是"温和"：默认串行 + 每次请求间隔 1.5 秒，
遇到 429 / 503 时按 Retry-After 或指数退避重试。
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "TCSScholar/0.1 (academic metadata collection; "
    "contact: set --user-agent to your email)"
)

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int | None, msg: str):
        super().__init__(f"{msg} (url={url}, status={status})")
        self.url = url
        self.status = status


class HttpClient:
    def __init__(
        self,
        cache_dir: Path | None = None,
        delay: float = 3.0,
        timeout: float = 60.0,
        max_retries: int = 6,
        user_agent: str = DEFAULT_UA,
        use_cache: bool = True,
        cache_ttl_days: float | None = None,
        max_delay: float = 20.0,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.delay = delay
        self.base_delay = delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.use_cache = use_cache and self.cache_dir is not None
        self.cache_ttl = cache_ttl_days * 86400 if cache_ttl_days else None
        self._last_request = 0.0
        self._ok_streak = 0
        self.stats = {"cache_hits": 0, "requests": 0, "retries": 0, "throttled": 0}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 缓存 ----------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.body"  # type: ignore[union-attr]

    def _read_cache(self, url: str) -> str | None:
        if not self.use_cache:
            return None
        path = self._cache_path(url)
        if not path.exists():
            return None
        if self.cache_ttl is not None and time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        return path.read_text(encoding="utf-8")

    def _write_cache(self, url: str, body: str) -> None:
        if not self.use_cache:
            return
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    # ---------- 请求 ----------

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _slow_down(self) -> None:
        """被限流后自适应加大间隔，整个会话生效。"""
        new_delay = min(self.max_delay, max(self.delay * 1.6, self.base_delay * 1.6))
        if new_delay > self.delay:
            log.warning("被限流，请求间隔 %.1fs -> %.1fs", self.delay, new_delay)
        self.delay = new_delay
        self._ok_streak = 0
        self.stats["throttled"] += 1

    def _speed_up(self) -> None:
        """连续成功一段时间后缓慢恢复。"""
        self._ok_streak += 1
        if self._ok_streak >= 20 and self.delay > self.base_delay:
            self.delay = max(self.base_delay, self.delay / 1.3)
            self._ok_streak = 0

    def get(self, url: str, params: dict | None = None, force: bool = False) -> str:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        if not force:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats["cache_hits"] += 1
                log.debug("cache hit %s", url)
                return cached

        body = self._fetch(url)
        self._write_cache(url, body)
        return body

    def _fetch(self, url: str) -> str:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip",
                    "Accept": "*/*",
                },
            )
            try:
                self.stats["requests"] += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    self._speed_up()
                    return raw.decode(charset, errors="replace")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in RETRY_STATUS or attempt == self.max_retries:
                    raise HttpError(url, e.code, f"HTTP 请求失败: {e.reason}") from e
                # 429/5xx 在 DBLP 上通常就是限流，之后往往会直接掐连接
                self._slow_down()
                sleep_for = self._retry_delay(e, attempt)
                log.warning(
                    "HTTP %s，%.1fs 后重试 (%d/%d) %s",
                    e.code, sleep_for, attempt + 1, self.max_retries, url,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt == self.max_retries:
                    raise HttpError(url, None, f"网络错误: {e}") from e
                # 连接被重置多半也是限流的表现，退避要足够长
                self._slow_down()
                sleep_for = min(180.0, 10.0 * (2 ** attempt))
                log.warning(
                    "网络错误 %s，%.1fs 后重试 (%d/%d)",
                    e, sleep_for, attempt + 1, self.max_retries,
                )
            self.stats["retries"] += 1
            time.sleep(sleep_for)

        raise HttpError(url, None, f"重试耗尽: {last_err}")

    @staticmethod
    def _retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = err.headers.get("Retry-After") if err.headers else None
        if retry_after:
            try:
                return min(180.0, float(retry_after))
            except ValueError:
                pass
        return min(180.0, 10.0 * (2 ** attempt))
