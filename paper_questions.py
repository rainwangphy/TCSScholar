#!/usr/bin/env python3
"""从每日归档里的论文全文中，抽出作者自己留下的公开问题，产出 site/open/questions.json。

和 open_problems.py 的分工：那边是**领域级的大问题**（P vs NP、UGC、k-server），
几十年不动一次，手写登记、追踪谁解决了它；这里是**论文级的小问题**——作者在结论里
写「我们没能处理 X」「Y 是否成立仍未知」——数量大、寿命短、难度也低得多。
想挑个题做的人要看的是后者。

流水线：

  1. 从 site/daily/*.json 里取每天排名靠前的那几篇（复用已经算好的排序）
  2. 抓 arxiv.org/html（拿不到退 ar5iv），切出结论/讨论/展望那几节
  3. 送给 Gemini 抽问题，每条必须引一句原文，引不出的整条丢掉

为什么要全文：实测 111 篇里只有 5% 的**摘要**提到公开问题，而且全是「我们解决了别人的
公开问题」；同一批论文的**全文**有 71% 出现公开问题的措辞。作者是在结论里留问题的。

产物是**累积**的：每次只处理没处理过的论文，已有的问题原样保留。

用法:
    python paper_questions.py                  # 抓最近 8 天里没处理过的论文
    python paper_questions.py --days 30        # 铺底：把 30 天的归档都过一遍
    python paper_questions.py --no-llm         # 只抓全文，报命中率，不调模型
    python paper_questions.py --max-papers 50  # 本次最多处理 50 篇
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tcs_crawler.fulltext import excerpts, fetch
from tcs_crawler.gemini import DEFAULT_MODEL, Gemini, load_key
from tcs_crawler.http import HttpClient
from tcs_crawler.openprob import REGISTRY
from tcs_crawler.paperq import extract

log = logging.getLogger("paperq")

DAILY_DIR = Path("site") / "daily"
OUT_DIR = Path("site") / "open"
UA = "TCSScholar-openproblems/0.1 (https://github.com/rainwangphy/TCSScholar)"

MIN_EXCERPT = 200  # 短于这个的片段没什么可抽的，省一次调用


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper_questions.py",
        description="从论文结论里抽出作者留下的公开问题，产出 site/open/questions.json。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python paper_questions.py                 # 最近 8 天的新论文\n"
            "  python paper_questions.py --days 30       # 铺底\n"
            "  python paper_questions.py --no-llm        # 只看全文命中率\n"
        ),
    )
    p.add_argument("--daily", type=Path, default=DAILY_DIR, help=f"每日归档目录，默认 {DAILY_DIR}")
    p.add_argument("-o", "--out", type=Path, default=OUT_DIR, help=f"输出目录，默认 {OUT_DIR}")
    p.add_argument("--registry", type=Path, default=REGISTRY, help="登记表，只用来取 areas 词表")
    p.add_argument("--days", type=int, default=8, metavar="N",
                   help="只看最近 N 天的归档，默认 8（配合每周跑），0 表示全部")
    p.add_argument("--top", type=int, default=15, metavar="N",
                   help="每天取排名前 N 篇，默认 15（和每日速读的精选一致）")
    p.add_argument("--max-papers", type=int, default=200,
                   help="本次最多处理几篇，默认 200")
    p.add_argument("--keep-days", type=int, default=180, metavar="N",
                   help="产物里只留最近 N 天的问题，默认 180，0 表示全留")
    p.add_argument("--no-llm", action="store_true", help="只抓全文并报命中率，不调模型")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini 模型，默认 {DEFAULT_MODEL}")
    p.add_argument("--api-key", default=None, help="Gemini key，默认取 GEMINI_API_KEY 或 api_keys/")
    p.add_argument("--delay", type=float, default=2.0, help="全文抓取间隔秒数，默认 2.0")
    p.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存")
    p.add_argument("--redo", action="store_true", help="忽略已处理记录，全部重来")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_existing(out_dir: Path) -> dict:
    """已有的产物。这个文件是**累积存储**，不是从别处重算出来的渲染结果，
    所以每次都要在它上面追加，绝不能整份重写。"""
    path = out_dir / "questions.json"
    if not path.exists():
        return {"questions": [], "seen": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("%s 损坏，这次从头来", path)
        return {"questions": [], "seen": []}
    d.setdefault("questions", [])
    d.setdefault("seen", [])
    return d


def pick_papers(daily_dir: Path, days: int, top: int) -> list[dict]:
    """从每日归档里取候选。归档里已经按分数排好序了，直接取前 top 篇。"""
    files = sorted(daily_dir.glob("20*.json"), reverse=True)
    if days > 0:
        files = files[:days]
    out = []
    for path in files:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("跳过损坏的 %s", path)
            continue
        for p in (d.get("papers") or [])[:top]:
            out.append(dict(p, day=d.get("date", "")))
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    areas = json.loads(args.registry.read_text(encoding="utf-8"))["areas"]
    store = load_existing(args.out)
    seen = set() if args.redo else set(store["seen"])

    papers = pick_papers(args.daily, args.days, args.top)
    todo = [p for p in papers if p["arxiv_id"] not in seen][: args.max_papers]
    print(f"归档里 {len(papers)} 篇候选，其中 {len(todo)} 篇没处理过"
          + (f"（本次上限 {args.max_papers}）" if len(papers) - len(seen) > args.max_papers else ""))
    if not todo:
        print("没有新论文可处理")

    g: Gemini | None = None
    if not args.no_llm:
        key = load_key(args.api_key)
        if key:
            g = Gemini(key, model=args.model)
        else:
            log.warning("没有 Gemini key，这次只抓全文报命中率")

    client = HttpClient(cache_dir=Path(".cache"), delay=args.delay, user_agent=UA)

    with_text = with_excerpt = with_q = 0
    fresh: list[dict] = []
    for i, p in enumerate(todo, 1):
        body = fetch(client, p["arxiv_id"], p.get("version") or "v1", force=args.refresh)
        seen.add(p["arxiv_id"])  # 抓不到也记上，免得每周都为它白跑一趟
        if not body:
            continue
        with_text += 1
        excerpt, heads = excerpts(body)
        if len(excerpt) < MIN_EXCERPT:
            log.debug("[%s] 结论里没有公开问题的措辞", p["arxiv_id"])
            continue
        with_excerpt += 1
        if g is None:
            continue
        log.info("(%d/%d) 抽取 %s %s", i, len(todo), p["arxiv_id"], p["title"][:56])
        qs = extract(g, p, excerpt, areas)
        if not qs:
            continue
        with_q += 1
        for q in qs:
            fresh.append(dict(q, **{
                "arxiv_id": p["arxiv_id"],
                "title": p["title"],
                "authors": (p.get("authors") or [])[:6],
                "url": p.get("abs_url") or f"https://arxiv.org/abs/{p['arxiv_id']}",
                "date": p.get("day") or (p.get("published") or "")[:10],
                "cats": p.get("cats") or [],
                "sections": heads[:3],
                "model": g.model,
            }))

    # 重新处理过的论文，先把它上一轮的问题摘掉再接上新的——否则 --redo（或者
    # 修好抽取逻辑之后重跑）会把同一篇的问题追加两份。
    redone = {p["arxiv_id"] for p in todo}
    kept = [q for q in store["questions"] if q.get("arxiv_id") not in redone]
    dropped = len(store["questions"]) - len(kept)
    if dropped:
        print(f"重新处理的论文，替换掉它们原有的 {dropped} 条问题")
    questions = kept + fresh
    if args.keep_days > 0:
        cutoff = (date.today() - timedelta(days=args.keep_days)).isoformat()
        before = len(questions)
        questions = [q for q in questions if q.get("date", "") >= cutoff]
        if before != len(questions):
            print(f"按 --keep-days {args.keep_days} 清掉了 {before - len(questions)} 条旧问题")
    # 新的排前面；seen 永远保留（只是些 id，很便宜），清掉它只会导致重复抓取
    questions.sort(key=lambda q: (q.get("date", ""), q.get("arxiv_id", "")), reverse=True)

    payload = {
        "updated": _now(),
        "model": g.model if g else "",
        "areas": areas,
        "shapes": sorted({q["shape"] for q in questions}),
        "stats": {
            "papers_seen": len(seen),
            "papers_this_run": len(todo),
            "with_text": with_text,
            "with_excerpt": with_excerpt,
            "with_questions": with_q,
            "questions": len(questions),
            "new_questions": len(fresh),
        },
        "seen": sorted(seen),
        "questions": questions,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")

    print(f"\n本次 {len(todo)} 篇：{with_text} 篇拿到全文，{with_excerpt} 篇结论里有公开问题的措辞，"
          f"{with_q} 篇抽出了问题")
    print(f"新增 {len(fresh)} 条，累计 {len(questions)} 条 → {path} "
          f"({path.stat().st_size / 1024:.0f} KB)")
    print(client.stats)
    if g:
        print(g.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
