"""arXiv 每日新论文抓取（Atom API，只用标准库）。

和 DBLP 那条线的区别：DBLP 抓的是**已发表**的会议论文（有 DOI、有页码），
这里抓的是**当天上传**的预印本，两者的字段和用途都不一样，所以单独一套模型。

按 v1 提交日期取数（`submittedDate` 过滤的是首次提交时间），因此拿到的是当天
真正新上传的论文，不含旧论文的修订版。arXiv 的公告是工作日制，周末上传的论文
会攒到周一公告，但提交日期仍是当天——我们按提交日切，周末的那两天会偏少。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

API = "http://export.arxiv.org/api/query"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# 核心 TCS 分类。cs.LG / quant-ph 之类量太大且多数不是 TCS，靠交叉列表能捞到的
# 那部分已经在这五个分类里了，不单独抓。
CORE_CATS = ["cs.CC", "cs.DS", "cs.DM", "cs.GT", "cs.CG"]

CAT_NAMES = {
    "cs.CC": "Computational Complexity",
    "cs.DS": "Data Structures and Algorithms",
    "cs.DM": "Discrete Mathematics",
    "cs.GT": "Computer Science and Game Theory",
    "cs.CG": "Computational Geometry",
    "cs.IT": "Information Theory",
    "cs.CR": "Cryptography and Security",
    "cs.LO": "Logic in Computer Science",
    "cs.LG": "Machine Learning",
    "cs.AI": "Artificial Intelligence",
    "cs.DC": "Distributed, Parallel, and Cluster Computing",
    "quant-ph": "Quantum Physics",
    "math.CO": "Combinatorics",
    "math.OC": "Optimization and Control",
    "math.PR": "Probability",
    "stat.ML": "Machine Learning (stat)",
}

# 一次要 100 条；arXiv 建议不要超过 2000，且分页之间要留间隔（HttpClient 已限速）
PAGE = 100
MAX_PAGES = 30  # 3000 篇的保险丝，正常一天几十篇


@dataclass
class ArxivPaper:
    arxiv_id: str  # 不带版本号，如 2608.23929
    version: str  # v1
    title: str
    abstract: str
    authors: list[str]
    primary_cat: str
    cats: list[str]
    published: str  # v1 提交时间，ISO8601
    updated: str
    abs_url: str
    pdf_url: str
    doi: str = ""
    comment: str = ""
    journal_ref: str = ""
    # 后续流水线填的字段
    topics: list[str] = field(default_factory=list)
    score: float = 0.0
    score_parts: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _text(node: ET.Element | None) -> str:
    """Atom 里的 title / summary 带换行和缩进，压成一行。"""
    if node is None or node.text is None:
        return ""
    return re.sub(r"\s+", " ", node.text).strip()


def _parse_entry(entry: ET.Element) -> ArxivPaper | None:
    raw_id = _text(entry.find("atom:id", NS))
    m = re.search(r"abs/(.+?)(v(\d+))?$", raw_id)
    if not m:
        log.warning("无法解析 arXiv id: %s", raw_id)
        return None
    arxiv_id, version = m.group(1), (m.group(2) or "")

    cats = [c.get("term", "") for c in entry.findall("atom:category", NS)]
    primary = entry.find("arxiv:primary_category", NS)
    pdf = ""
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf":
            pdf = link.get("href", "")

    return ArxivPaper(
        arxiv_id=arxiv_id,
        version=version,
        title=_text(entry.find("atom:title", NS)),
        abstract=_text(entry.find("atom:summary", NS)),
        authors=[_text(a.find("atom:name", NS)) for a in entry.findall("atom:author", NS)],
        primary_cat=(primary.get("term", "") if primary is not None else (cats[0] if cats else "")),
        cats=[c for c in cats if c],
        published=_text(entry.find("atom:published", NS)),
        updated=_text(entry.find("atom:updated", NS)),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=pdf or f"https://arxiv.org/pdf/{arxiv_id}",
        doi=_text(entry.find("arxiv:doi", NS)),
        comment=_text(entry.find("arxiv:comment", NS)),
        journal_ref=_text(entry.find("arxiv:journal_ref", NS)),
    )


def _query(client: HttpClient, search: str, start: int, force: bool) -> tuple[list[ArxivPaper], int]:
    body = client.get(
        API,
        params={
            "search_query": search,
            "start": start,
            "max_results": PAGE,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        },
        force=force,
    )
    root = ET.fromstring(body)
    total_node = root.find("opensearch:totalResults", NS)
    total = int(total_node.text) if total_node is not None and total_node.text else 0
    papers = [p for p in (_parse_entry(e) for e in root.findall("atom:entry", NS)) if p]
    return papers, total


def fetch_day(
    client: HttpClient,
    day: date,
    cats: list[str] | None = None,
    force: bool = False,
) -> list[ArxivPaper]:
    """抓某一天（UTC）首次提交、且属于 cats 之一的全部论文。

    arXiv 的 submittedDate 范围两端都是闭区间，用 0000–2359 而不是跨到次日 0000，
    免得次日零点整提交的论文被算两天。
    """
    cats = cats or CORE_CATS
    lo = day.strftime("%Y%m%d") + "0000"
    hi = day.strftime("%Y%m%d") + "2359"
    cat_clause = " OR ".join(f"cat:{c}" for c in cats)
    search = f"({cat_clause}) AND submittedDate:[{lo} TO {hi}]"

    out: dict[str, ArxivPaper] = {}
    start = 0
    for page in range(MAX_PAGES):
        try:
            papers, total = _query(client, search, start, force)
        except (HttpError, ET.ParseError) as e:
            log.error("arXiv 第 %d 页抓取失败：%s", page + 1, e)
            break
        if not papers:
            break
        for p in papers:
            out.setdefault(p.arxiv_id, p)  # 同一篇被多个分类命中时只留一份
        start += len(papers)
        log.info("[arxiv] %s 已取 %d/%d", day.isoformat(), len(out), total)
        if start >= total:
            break
    else:
        log.warning("到达分页上限 %d，可能还有未取完的论文", MAX_PAGES)

    return sorted(out.values(), key=lambda p: p.published)


def default_day(today: date | None = None) -> date:
    """默认抓昨天（UTC）：当天的还在陆续上传，抓了也是残缺的。"""
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=1)


def parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()
