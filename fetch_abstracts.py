#!/usr/bin/env python3
"""从 OpenAlex 补齐论文摘要，产出 data/abstracts.jsonl。

DBLP 不提供摘要，所以这一步单独跑。OpenAlex 免费、无需 key，实测覆盖率约 92%，
连 1960 年代的论文都有。

两条匹配路径：
  1. 有 DOI 的（约 88%）：按 DOI 批量查，一次 50 个。
  2. 没 DOI 的（主要是 1990-2000 年代的 SODA）：按标题搜，但只有标题归一化后
     完全相等且年份相差不超过 1 年才采信——宁可漏，不可张冠李戴。

OpenAlex 把摘要存成倒排索引（词 -> 位置列表），需要还原成正文。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tcs_crawler.http import HttpClient, HttpError

SRC = Path("data") / "tcs_papers.jsonl"
OUT = Path("data") / "abstracts.jsonl"
API = "https://api.openalex.org/works"

# OpenAlex 的 OR 过滤器上限是 50 个值
DOI_BATCH = 50
# 礼貌池（带 mailto）允许 10 req/s，留出余量
DELAY = 0.15

log = logging.getLogger("abstracts")


def inverted_to_text(inv: dict[str, list[int]] | None) -> str | None:
    """把 OpenAlex 的 abstract_inverted_index 还原成正文。"""
    if not inv:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort()
    text = " ".join(word for _, word in positions).strip()
    return text or None


_norm_re = re.compile(r"[^a-z0-9]+")


def norm_title(t: str) -> str:
    return _norm_re.sub("", t.lower())


def clean_abstract(text: str | None) -> str | None:
    """丢掉明显不是摘要的内容。"""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    # 太短的多半是 "No abstract available." 之类的占位符
    if len(text) < 40:
        return None
    if norm_title(text).startswith("noabstract"):
        return None
    return text


def save(out: dict[str, str]) -> None:
    """随时可以落盘。DOI 路径跑完就存一次，标题路径每 200 篇再存一次——
    后者容易被限流拖很久，中途停掉不该把前面的成果一起赔进去。"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for key, text in sorted(out.items()):
            f.write(json.dumps({"dblp_key": key, "abstract": text}, ensure_ascii=False) + "\n")
    tmp.replace(OUT)  # 原子替换，别让中断留下半个文件


def load_papers() -> list[dict]:
    if not SRC.exists():
        raise SystemExit(f"找不到 {SRC}，请先运行 python crawl.py")
    return [json.loads(line) for line in SRC.open(encoding="utf-8") if line.strip()]


# arxiv.org/abs/…、arxiv.org/pdf/… 和 DataCite 那套 doi.org/10.48550/arXiv.… 都要认
_arxiv_re = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)"
    r"([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})",
    re.IGNORECASE)


def arxiv_id(work: dict) -> str | None:
    """从 OpenAlex 的 locations 里抠出 arXiv ID（去掉 v2 这类版本后缀）。"""
    for loc in work.get("locations") or []:
        for url in (loc.get("landing_page_url"), loc.get("pdf_url")):
            m = _arxiv_re.search(url or "")
            if m:
                return m.group(1)
    return None


def fetch_by_doi(client: HttpClient, papers: list[dict], out: dict[str, str],
                 arxiv: dict[str, str], extra: dict) -> list[dict]:
    """按 DOI 批量取，返回没法走这条路的论文（DOI 含分隔符）。"""
    safe = [p for p in papers if "|" not in p["doi"] and "," not in p["doi"]]
    skipped = len(papers) - len(safe)
    if skipped:
        log.warning("%d 篇的 DOI 含分隔符，按标题路径处理", skipped)

    by_doi = {p["doi"].lower(): p for p in safe}
    total = (len(safe) + DOI_BATCH - 1) // DOI_BATCH
    for bi in range(0, len(safe), DOI_BATCH):
        chunk = safe[bi : bi + DOI_BATCH]
        params = {
            # locations 用来捞 arXiv ID：出版商存进来的摘要经常只有第一段，
            # arXiv 上才是作者的完整版，后面再按 ID 批量补
            "filter": "doi:" + "|".join("https://doi.org/" + p["doi"] for p in chunk),
            "select": "doi,abstract_inverted_index,locations",
            "per-page": DOI_BATCH,
            **extra,
        }
        try:
            data = json.loads(client.get(API, params=params))
        except (HttpError, json.JSONDecodeError) as e:
            log.error("DOI 批次 %d 失败：%s", bi // DOI_BATCH + 1, e)
            continue
        for work in data.get("results", []):
            doi = (work.get("doi") or "").replace("https://doi.org/", "").lower()
            paper = by_doi.get(doi)
            if not paper:
                continue
            text = clean_abstract(inverted_to_text(work.get("abstract_inverted_index")))
            if text:
                out[paper["dblp_key"]] = text
            aid = arxiv_id(work)
            if aid:
                arxiv[paper["dblp_key"]] = aid
        done = bi // DOI_BATCH + 1
        if done % 20 == 0 or done == total:
            log.info("DOI 批次 %d/%d，已拿到 %d 篇摘要", done, total, len(out))

    return [p for p in papers if "|" in p["doi"] or "," in p["doi"]]


def fetch_by_title(client: HttpClient, papers: list[dict], out: dict[str, str],
                   extra: dict) -> None:
    """按标题搜索，严格校验后才采信。"""
    hits = 0
    for i, p in enumerate(papers, 1):
        # title.search 里的双引号和反斜杠会破坏查询语法
        query = p["title"].replace('"', " ").replace("\\", " ").strip()
        if len(query) < 8:
            continue
        params = {
            "filter": f"title.search:{query}",
            "select": "display_name,publication_year,abstract_inverted_index",
            "per-page": 5,
            **extra,
        }
        try:
            data = json.loads(client.get(API, params=params))
        except (HttpError, json.JSONDecodeError) as e:
            log.debug("标题搜索失败 %s：%s", p["dblp_key"], e)
            continue
        want = norm_title(p["title"])
        for work in data.get("results", []):
            got = norm_title(work.get("display_name") or "")
            year = work.get("publication_year")
            # 标题必须完全一致；年份允许差 1（会议年 vs 出版年）
            if got != want:
                continue
            if year and abs(year - p["year"]) > 1:
                continue
            text = clean_abstract(inverted_to_text(work.get("abstract_inverted_index")))
            if text:
                out[p["dblp_key"]] = text
                hits += 1
            break
        if i % 200 == 0:
            log.info("标题匹配 %d/%d，命中 %d", i, len(papers), hits)
            save(out)
    log.info("标题路径完成：%d/%d 命中", hits, len(papers))


ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_BATCH = 100
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch_arxiv(client: HttpClient, arxiv: dict[str, str], out: dict[str, str]) -> int:
    """按 arXiv ID 批量取摘要，比出版商版本长才替换。

    出版商存进 Crossref/OpenAlex 的摘要经常被截到第一段（实测约四成明显偏短），
    arXiv 上是作者提交的完整版。id_list 一次能查上百篇，所以这一步很便宜。
    """
    ids = sorted(set(arxiv.values()))
    by_id: dict[str, list[str]] = {}
    for key, aid in arxiv.items():
        by_id.setdefault(aid, []).append(key)

    replaced = added = 0
    total = (len(ids) + ARXIV_BATCH - 1) // ARXIV_BATCH
    for bi in range(0, len(ids), ARXIV_BATCH):
        chunk = ids[bi : bi + ARXIV_BATCH]
        try:
            body = client.get(ARXIV_API, params={"id_list": ",".join(chunk),
                                                 "max_results": ARXIV_BATCH})
            root = ET.fromstring(body)
        except (HttpError, ET.ParseError) as e:
            log.error("arXiv 批次 %d 失败：%s", bi // ARXIV_BATCH + 1, e)
            continue
        for entry in root.findall(f"{ATOM}entry"):
            raw_id = (entry.findtext(f"{ATOM}id") or "")
            m = _arxiv_re.search(raw_id)
            if not m:
                continue
            text = clean_abstract(entry.findtext(f"{ATOM}summary"))
            if not text:
                continue
            for key in by_id.get(m.group(1), []):
                old = out.get(key)
                if old is None:
                    out[key] = text
                    added += 1
                elif len(text) > len(old) * 1.05:  # 明显更长才换，避免来回抖动
                    out[key] = text
                    replaced += 1
        done = bi // ARXIV_BATCH + 1
        if done % 10 == 0 or done == total:
            log.info("arXiv 批次 %d/%d，补全 %d 篇、新增 %d 篇", done, total, replaced, added)
    log.info("arXiv 路径完成：补全 %d 篇，新增 %d 篇", replaced, added)
    return replaced + added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mailto", default="", help="OpenAlex 礼貌池邮箱，强烈建议填")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇，用于试跑")
    ap.add_argument("--no-title-match", action="store_true", help="跳过无 DOI 论文的标题匹配")
    ap.add_argument("--no-arxiv", action="store_true",
                    help="跳过用 arXiv 补全被出版商截断的摘要")
    ap.add_argument("--title-delay", type=float, default=1.0,
                    help="标题搜索的请求间隔（秒）。这个端点比 DOI 批量查更容易被限流")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)

    papers = load_papers()
    if args.limit:
        papers = papers[: args.limit]

    ua = f"TCSScholar/0.1 (mailto:{args.mailto})" if args.mailto else "TCSScholar/0.1"
    client = HttpClient(cache_dir=Path(".cache"), delay=DELAY, max_delay=8.0,
                        user_agent=ua, use_cache=not args.no_cache)
    # mailto 走礼貌池，限速更宽松；必须作为参数传，拼进 URL 会撞上 get() 自己加的 ?
    extra = {"mailto": args.mailto} if args.mailto else {}

    with_doi = [p for p in papers if p.get("doi")]
    without_doi = [p for p in papers if not p.get("doi")]
    log.info("%d 篇论文：%d 有 DOI，%d 无 DOI", len(papers), len(with_doi), len(without_doi))

    out: dict[str, str] = {}
    arxiv: dict[str, str] = {}
    leftover = fetch_by_doi(client, with_doi, out, arxiv, extra) or []
    log.info("DOI 路径完成：%d/%d 命中", len(out), len(with_doi))
    log.info("其中 %d 篇有 arXiv 版本", len(arxiv))
    save(out)  # 检查点：后面的路径容易被限流，别把已有成果押在它身上

    if arxiv and not args.no_arxiv:
        # arXiv 要求请求间隔 3 秒；这个客户端是串行的，直接调速就够
        client.delay = client.base_delay = 3.0
        fetch_arxiv(client, arxiv, out)
        save(out)

    if not args.no_title_match:
        todo = without_doi + leftover + [p for p in with_doi if p["dblp_key"] not in out]
        log.info("标题路径待处理 %d 篇（含 DOI 路径未命中的）", len(todo))
        client.delay = client.base_delay = args.title_delay
        fetch_by_title(client, todo, out, extra)

    save(out)

    chars = sum(len(t) for t in out.values())
    log.info("HTTP: %s", client.stats)
    print(f"写入 {OUT}：{len(out)}/{len(papers)} 篇有摘要 "
          f"({len(out)/len(papers)*100:.1f}%)，共 {chars/1024/1024:.1f} MB 文本")


if __name__ == "__main__":
    main()
