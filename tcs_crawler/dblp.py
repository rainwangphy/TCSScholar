"""DBLP 抓取逻辑。

两步：
1. 抓会议目录页 https://dblp.org/db/conf/<key>/index.html，解析出历届
   proceedings 的 TOC（如 db/conf/stoc/stoc2023.bht）。
2. 对每届 TOC 调用 DBLP 检索 API：
   https://dblp.org/search/publ/api?q=toc:<bht>:&format=json
   服务端每次最多返回 100 条，用 f 偏移翻页。
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass

from .http import HttpClient, HttpError
from .models import Author, Paper
from .venues import Venue

log = logging.getLogger(__name__)

SEARCH_API = "https://dblp.org/search/publ/api"
PAGE_SIZE = 100  # DBLP 服务端硬上限，请求更大也只返回 100

# ITCS 等 LIPIcs 会议会把卷首内容作为独立条目收录
FRONT_MATTER_RE = re.compile(
    r"^(front matter|frontmatter|preface|table of contents|conference organization|"
    r"author index|title page|foreword|editorial|proceedings of|invited talk abstract)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Toc:
    """一届会议的论文集。"""

    bht: str  # db/conf/stoc/stoc2023.bht
    stem: str  # stoc2023
    year: int | None

    @property
    def url(self) -> str:
        return f"https://dblp.org/{self.bht[:-4]}.html"


@dataclass
class TocResult:
    """一届会议的抓取结果。抓到一半失败时也会保留已拿到的部分。"""

    toc: "Toc"
    papers: list[Paper]
    expected: int | None = None  # DBLP 声称的总条数（过滤前）
    complete: bool = True
    error: str = ""


def _parse_year(stem: str, prefix: str) -> int | None:
    """从 TOC 文件名推断年份：stoc2023 -> 2023，focs60 -> 1960，soda90 -> 1990。

    仅用于抓取前的年份过滤；最终写出的年份以 DBLP 记录里的 year 字段为准。
    """
    tail = stem[len(prefix):]
    m = re.match(r"^(\d{2,4})", tail)
    if not m:
        return None
    num = int(m.group(1))
    if len(m.group(1)) == 4:
        return num
    # 两位数年份只出现在 2000 年之前的老届次
    return 1900 + num


def discover_tocs(client: HttpClient, venue: Venue, force: bool = False) -> list[Toc]:
    """从会议目录页解析出全部历届 proceedings。"""
    body = client.get(venue.index_url, force=force)
    pattern = re.compile(
        rf"{re.escape(venue.dblp_dir)}/{re.escape(venue.toc_prefix)}[0-9][A-Za-z0-9\-]*\.html"
    )
    stems: dict[str, Toc] = {}
    for path in pattern.findall(body):
        stem = path.rsplit("/", 1)[-1][: -len(".html")]
        bht = f"db/{venue.dblp_dir}/{stem}.bht"
        stems[stem] = Toc(bht=bht, stem=stem, year=_parse_year(stem, venue.toc_prefix))

    tocs = sorted(stems.values(), key=lambda t: (t.year or 0, t.stem))
    log.info("[%s] 发现 %d 届 proceedings", venue.slug, len(tocs))
    if not tocs:
        log.warning(
            "[%s] 未从 %s 解析到任何 proceedings，DBLP 页面结构可能已变化",
            venue.slug, venue.index_url,
        )
    return tocs


def _hits(payload: dict) -> tuple[list[dict], int]:
    result = payload.get("result", {})
    hits = result.get("hits", {})
    total = int(hits.get("@total", 0))
    hit = hits.get("hit", [])
    if isinstance(hit, dict):  # 只有一条时 DBLP 返回对象而非数组
        hit = [hit]
    return hit, total


def _authors(info: dict) -> list[Author]:
    raw = (info.get("authors") or {}).get("author", [])
    if isinstance(raw, (dict, str)):
        raw = [raw]
    out = []
    for a in raw:
        if isinstance(a, str):
            out.append(Author(name=html.unescape(a)))
        else:
            out.append(Author(name=html.unescape(a.get("text", "")), pid=a.get("@pid", "")))
    return [a for a in out if a.name]


def _clean_title(title: str) -> str:
    title = html.unescape(title or "").strip()
    # DBLP 标题通常以句点结尾，去掉以便后续比对；缩写结尾（如 "... in P.")不多见
    if title.endswith(".") and not title.endswith(".."):
        title = title[:-1]
    return title


def parse_hit(hit: dict, venue: Venue, toc: Toc) -> Paper | None:
    info = hit.get("info") or {}
    title = _clean_title(info.get("title", ""))
    if not title:
        return None
    try:
        year = int(info.get("year") or toc.year or 0)
    except (TypeError, ValueError):
        year = toc.year or 0
    return Paper(
        venue=venue.slug,
        venue_full=venue.name,
        year=year,
        title=title,
        authors=_authors(info),
        pages=info.get("pages", "") or "",
        doi=info.get("doi", "") or "",
        ee=info.get("ee", "") or "",
        dblp_key=info.get("key", "") or "",
        dblp_url=info.get("url", "") or "",
        toc_key=toc.bht,
        type=info.get("type", "") or "",
        access=info.get("access", "") or "",
    )


def fetch_toc_papers(
    client: HttpClient,
    venue: Venue,
    toc: Toc,
    *,
    force: bool = False,
    include_editorship: bool = False,
    include_front_matter: bool = False,
) -> TocResult:
    """抓取一届会议的全部论文（自动翻页）。

    某一页失败不会丢掉已抓到的部分，结果里会标记 complete=False，
    重跑时已缓存的页不会重复请求。
    """
    papers: list[Paper] = []
    seen: set[str] = set()
    offset = 0
    total = None

    while True:
        try:
            payload_text = client.get(
                SEARCH_API,
                params={
                    "q": f"toc:{toc.bht}:",
                    "h": PAGE_SIZE,
                    "f": offset,
                    "c": 0,  # 不需要 completion 建议
                    "format": "json",
                },
                force=force,
            )
        except HttpError as e:
            log.error("[%s] %s 第 %d 条起抓取失败：%s", venue.slug, toc.stem, offset, e)
            return TocResult(toc, papers, total, complete=False, error=str(e))

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as e:
            log.error("[%s] %s 返回的不是合法 JSON: %s", venue.slug, toc.stem, e)
            return TocResult(toc, papers, total, complete=False, error=f"JSON 解析失败: {e}")

        hit, total_now = _hits(payload)
        if total is None:
            total = total_now

        for h in hit:
            paper = parse_hit(h, venue, toc)
            if paper is None:
                continue
            if not include_editorship and paper.type == "Editorship":
                continue
            if not include_front_matter and FRONT_MATTER_RE.match(paper.title):
                continue
            key = paper.dblp_key or f"{paper.title}|{','.join(paper.author_names)}"
            if key in seen:
                continue
            seen.add(key)
            papers.append(paper)

        offset += len(hit)
        if not hit or offset >= (total or 0):
            break

    complete = total is None or offset >= total
    if not complete:
        log.warning("[%s] %s 只取到 %d/%d 条", venue.slug, toc.stem, offset, total)

    log.info("[%s] %s -> %d 篇", venue.slug, toc.stem, len(papers))
    return TocResult(toc, papers, total, complete=complete)
