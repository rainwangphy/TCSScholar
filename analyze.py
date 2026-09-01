#!/usr/bin/env python3
"""对抓取到的论文做初步主题分析，产出网页所需的数据。

输出 data/site_data.json，供 build_site.py 嵌入网页。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from tcs_crawler.topics import OTHER, TOPIC_EN, TOPIC_IDS, TOPIC_ZH, classify
from tcs_crawler.venues import VENUES

DATA = Path("data")
SRC = DATA / "tcs_papers.jsonl"
OUT = DATA / "site_data.json"
# daily.py 的作者先验表。它随仓库提交（不像 data/），因为 CI 上跑每日 arXiv
# 分析时没有 data/ 可读。
PROLIFIC = Path("tcs_crawler") / "prolific_authors.json"
PROLIFIC_MIN = 3  # 少于 3 篇的作者信号太弱，留着只会把文件撑大

# 术语趋势分析用的停用词（结构性词汇，不承载主题信息）
STOP = set("""
a an the of on in for with and or to from via by is are was new some all its their our we you it
using use based towards toward at as be that this these those can how what when where which more
most better fast faster simple simpler general improved optimal near tight lower upper bounds bound
problem problems algorithm algorithms complexity time space efficient hard hardness extended abstract
preliminary version note remarks notes case study results result approach method methods framework
applications application analysis theory model models general first second one two three number
over under between within without into about after before during through while than then also
""".split())

TERM_RE = re.compile(r"[a-z][a-z\-']{2,}")


def load() -> list[dict]:
    if not SRC.exists():
        raise SystemExit(f"找不到 {SRC}，请先运行 python crawl.py")
    return [json.loads(line) for line in SRC.open(encoding="utf-8")]


def bucket(year: int) -> str:
    """按 5 年分桶，便于看趋势。"""
    start = (year // 5) * 5
    return f"{start}-{start + 4}"


def analyze(rows: list[dict]) -> dict:
    venues = [v for v in ["focs", "stoc", "soda", "itcs", "ec"]]
    topics = TOPIC_IDS + [OTHER]

    # --- 逐篇打标签 ---
    labels: list[list[str]] = []
    for r in rows:
        labels.append(classify(r["title"]))

    # --- 基础统计 ---
    per_venue_year: dict[str, dict[int, int]] = {v: defaultdict(int) for v in venues}
    for r in rows:
        per_venue_year[r["venue"]][r["year"]] += 1

    topic_total = Counter()
    topic_by_venue: dict[str, Counter] = {v: Counter() for v in venues}
    topic_by_bucket: dict[str, Counter] = defaultdict(Counter)
    bucket_total: Counter = Counter()

    for r, labs in zip(rows, labels):
        b = bucket(r["year"])
        bucket_total[b] += 1
        for t in labs:
            topic_total[t] += 1
            topic_by_venue[r["venue"]][t] += 1
            topic_by_bucket[b][t] += 1

    # --- 作者统计 ---
    author_papers: Counter = Counter()
    author_name: dict[str, str] = {}
    author_venues: dict[str, Counter] = defaultdict(Counter)
    author_years: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for a in r["authors"]:
            key = a["pid"] or a["name"]
            author_papers[key] += 1
            author_name[key] = a["name"]
            author_venues[key][r["venue"]] += 1
            author_years[key].append(r["year"])

    top_authors = [
        {
            "name": author_name[k],
            "count": c,
            "venues": dict(author_venues[k].most_common()),
            "span": [min(author_years[k]), max(author_years[k])],
        }
        for k, c in author_papers.most_common(60)
    ]

    # --- 术语趋势：近 12 年 vs 更早 ---
    recent_lo, recent_hi = 2014, 2026
    past_lo, past_hi = 1995, 2013
    recent_terms, past_terms = Counter(), Counter()
    n_recent = n_past = 0
    for r in rows:
        y = r["year"]
        terms = {w for w in TERM_RE.findall(r["title"].lower()) if w not in STOP}
        if recent_lo <= y <= recent_hi:
            n_recent += 1
            recent_terms.update(terms)
        elif past_lo <= y <= past_hi:
            n_past += 1
            past_terms.update(terms)

    trends = []
    for term, c in recent_terms.items():
        if c < 25:  # 太稀疏的词噪声大
            continue
        r_rate = c / n_recent * 1000
        p_rate = past_terms.get(term, 0) / max(n_past, 1) * 1000
        # 加平滑，避免从 0 起步的词占据全部榜单
        ratio = (r_rate + 0.3) / (p_rate + 0.3)
        trends.append({
            "term": term, "recent": c, "past": past_terms.get(term, 0),
            "recent_rate": round(r_rate, 2), "past_rate": round(p_rate, 2),
            "ratio": round(ratio, 2),
        })
    trends.sort(key=lambda x: -x["ratio"])
    rising = trends[:28]
    declining = [t for t in sorted(trends, key=lambda x: x["ratio"]) if t["past"] >= 25][:28]

    # --- 网页浏览用的紧凑数据 ---
    author_index: dict[str, int] = {}
    author_list: list[str] = []
    for r in rows:
        for a in r["authors"]:
            if a["name"] not in author_index:
                author_index[a["name"]] = len(author_list)
                author_list.append(a["name"])

    tid = {t: i for i, t in enumerate(topics)}
    vid = {v: i for i, v in enumerate(venues)}
    papers = [
        [
            vid[r["venue"]],
            r["year"],
            r["title"],
            [author_index[a["name"]] for a in r["authors"]],
            r["doi"],
            r["dblp_key"],
            [tid[t] for t in labs],
        ]
        for r, labs in zip(rows, labels)
    ]
    papers.sort(key=lambda p: (-p[1], p[0], p[2]))

    coverage = 1 - topic_total[OTHER] / len(rows)
    return {
        "meta": {
            "total": len(rows),
            "editions": len({(r["venue"], r["year"]) for r in rows}),
            "authors": len(author_list),
            "year_min": min(r["year"] for r in rows),
            "year_max": max(r["year"] for r in rows),
            "coverage": round(coverage, 4),
            "avg_labels": round(sum(len(l) for l in labels) / len(rows), 2),
        },
        "venues": [
            {"id": v, "name": VENUES[v].name, "count": sum(per_venue_year[v].values()),
             "years": sorted(per_venue_year[v]), "first": min(per_venue_year[v]),
             "last": max(per_venue_year[v])}
            for v in venues
        ],
        "topics": [{"id": t, "zh": TOPIC_ZH[t], "en": TOPIC_EN[t], "count": topic_total[t]}
                   for t in topics],
        "per_venue_year": {v: {str(y): c for y, c in sorted(per_venue_year[v].items())}
                           for v in venues},
        "topic_by_venue": {v: dict(topic_by_venue[v]) for v in venues},
        "topic_by_bucket": {b: dict(topic_by_bucket[b]) for b in sorted(topic_by_bucket)},
        "bucket_total": {b: c for b, c in sorted(bucket_total.items())},
        "top_authors": top_authors,
        "rising": rising,
        "declining": declining,
        "compact": {"venues": venues, "authors": author_list, "papers": papers},
    }


def write_prolific(rows: list[dict]) -> int:
    """导出「在五大会议发过 >= PROLIFIC_MIN 篇」的作者表，供 daily.py 打分用。

    按姓名而不是 pid 存：arXiv 那边只有显示名，没有 DBLP 的作者 id，对不上。
    重名因此会被合并——这也是打分时它权重不高的原因。
    """
    counts: Counter = Counter()
    for r in rows:
        for a in r["authors"]:
            counts[a["name"]] += 1
    table = {name: n for name, n in counts.items() if n >= PROLIFIC_MIN}
    PROLIFIC.parent.mkdir(parents=True, exist_ok=True)
    PROLIFIC.write_text(
        json.dumps(dict(sorted(table.items())), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(table)


def main() -> None:
    rows = load()
    result = analyze(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    m = result["meta"]
    size = OUT.stat().st_size / 1024 / 1024
    print(f"{m['total']} 篇 / {m['editions']} 届 / {m['authors']} 位作者 / "
          f"{m['year_min']}-{m['year_max']}")
    print(f"主题覆盖率 {m['coverage']*100:.1f}%，平均标签 {m['avg_labels']}")
    print(f"写入 {OUT} ({size:.1f} MB)")
    n_prolific = write_prolific(rows)
    print(f"写入 {PROLIFIC}（{n_prolific} 位发过 {PROLIFIC_MIN}+ 篇的作者，daily.py 的作者先验）")
    print("\n上升最快的术语:", ", ".join(t["term"] for t in result["rising"][:12]))
    print("下降最快的术语:", ", ".join(t["term"] for t in result["declining"][:12]))


if __name__ == "__main__":
    main()
