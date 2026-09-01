#!/usr/bin/env python3
"""抓 arXiv 当天新上传的 TCS 论文，分析后写进 site/daily/。

三层流水线，每层都能单独降级：

  1. 抓取  — arXiv Atom API，按 v1 提交日期取 cs.CC/cs.DS/cs.DM/cs.GT/cs.CG
  2. 规则  — 关键词主题分类 + 可解释打分，排出当天最值得看的若干篇
  3. 模型  — 只对排在前面的那几篇调 Gemini，出中文速读；另出一段当日综述

没有 API key 或模型调用失败时，第 3 层整层跳过，页面照常显示前两层的结果。

产物直接写进 site/daily/（不是 gitignore 掉的 data/），因为 GitHub Actions 每天
跑完要把它提交回仓库——这就是这个功能的持久化存储。

用法:
    python daily.py                      # 抓昨天（UTC），分析并写盘
    python daily.py --date 2026-08-27    # 指定某天
    python daily.py --days 7             # 回填最近 7 天（跳过已有的）
    python daily.py --no-llm             # 只跑规则层，不调模型
    python daily.py --top 20 --force     # 多分析几篇，且覆盖已有结果
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tcs_crawler.arxiv import CORE_CATS, ArxivPaper, default_day, fetch_day, parse_day
from tcs_crawler.digest import analyze_paper, daily_digest
from tcs_crawler.gemini import DEFAULT_MODEL, Gemini, load_key
from tcs_crawler.http import HttpClient
from tcs_crawler.ranking import rank
from tcs_crawler.topics import TOPIC_EN, TOPIC_ZH

log = logging.getLogger("daily")

OUT_DIR = Path("site") / "daily"
UA = "TCSScholar-daily/0.1 (https://github.com/rainwangphy/TCSScholar)"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="daily.py",
        description="抓取并分析 arXiv 每日新上传的 TCS 论文，产出 site/daily/ 下的每日 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python daily.py                      # 昨天（UTC）\n"
            "  python daily.py --date 2026-08-27\n"
            "  python daily.py --days 7             # 回填最近 7 天\n"
            "  python daily.py --no-llm             # 不调模型，只跑规则\n"
        ),
    )
    p.add_argument("--date", type=parse_day, default=None,
                   help="要抓的日期 YYYY-MM-DD（UTC），默认昨天")
    p.add_argument("--days", type=int, default=1,
                   help="从 --date 往前连抓几天，默认 1")
    p.add_argument("--cats", nargs="+", default=CORE_CATS, metavar="CAT",
                   help=f"arXiv 分类，默认 {' '.join(CORE_CATS)}")
    p.add_argument("--top", type=int, default=15,
                   help="送去模型深度分析的篇数，默认 15")
    p.add_argument("--no-llm", action="store_true", help="跳过模型分析，只跑规则层")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini 模型，默认 {DEFAULT_MODEL}")
    p.add_argument("--api-key", default=None, help="Gemini key，默认取 GEMINI_API_KEY 或 api_keys/")
    p.add_argument("--force", action="store_true", help="已有当天结果时也重抓重分析")
    p.add_argument("--delay", type=float, default=3.0, help="arXiv 请求间隔秒数，默认 3.0")
    p.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存")
    p.add_argument("--keep-days", type=int, default=0, metavar="N",
                   help="只保留最近 N 天的结果文件，0（默认）表示全部保留")
    p.add_argument("-o", "--out", type=Path, default=OUT_DIR, help=f"输出目录，默认 {OUT_DIR}")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p


def run_day(day: date, args, client: HttpClient, g: Gemini | None) -> dict | None:
    papers: list[ArxivPaper] = fetch_day(client, day, cats=args.cats, force=args.refresh)
    if not papers:
        log.warning("[%s] 一篇也没抓到（周末和节假日本来就少）", day)
        return None

    picked = rank(papers, args.top)

    topic_counts = Counter()
    for p in papers:
        topic_counts.update(p.topics)
    cat_counts = Counter(p.primary_cat for p in papers)

    digest = {}
    if g is not None:
        log.info("[%s] 送 %d 篇给 %s 分析", day, len(picked), g.model)
        for i, p in enumerate(picked, 1):
            log.info("  (%d/%d) %s", i, len(picked), p.title[:70])
            p.analysis = analyze_paper(g, p)
        digest = daily_digest(g, day.isoformat(), papers, picked, dict(topic_counts))

    picked_ids = {p.arxiv_id for p in picked}
    # 按分数降序存，前端直接顺着渲染即可
    ordered = sorted(papers, key=lambda p: (-p.score, p.title))
    return {
        "date": day.isoformat(),
        "generated": _now(),
        "cats": args.cats,
        "total": len(papers),
        "picked": [p.arxiv_id for p in picked],
        "topic_counts": dict(topic_counts.most_common()),
        "cat_counts": dict(cat_counts.most_common()),
        "topic_names": {t: {"zh": TOPIC_ZH.get(t, t), "en": TOPIC_EN.get(t, t)}
                        for t in topic_counts},
        "digest": digest,
        "llm": bool(g) and any(p.analysis for p in picked),
        "model": g.model if g else "",
        "papers": [dict(p.to_dict(), picked=p.arxiv_id in picked_ids) for p in ordered],
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune(out_dir: Path, keep: int) -> int:
    """只留最近 keep 天的结果文件。每天约 80KB，攒几年才值得清，所以默认不清。"""
    files = sorted(out_dir.glob("20*.json"), reverse=True)
    for path in files[keep:]:
        path.unlink()
    return max(0, len(files) - keep)


def rebuild_index(out_dir: Path) -> dict:
    """扫一遍已有的每日文件重建目录。

    重建而不是增量追加：手动删掉某天的文件后，目录不该还挂着一条死链接。
    """
    days = []
    for path in sorted(out_dir.glob("20*.json"), reverse=True):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("跳过损坏的 %s", path)
            continue
        top = sorted(d.get("topic_counts", {}).items(), key=lambda kv: -kv[1])[:3]
        days.append({
            "date": d["date"],
            "total": d["total"],
            "picked": len(d.get("picked", [])),
            "llm": d.get("llm", False),
            "headline": (d.get("digest") or {}).get("headline", ""),
            "topics": [{"id": t, "zh": d.get("topic_names", {}).get(t, {}).get("zh", t), "n": n}
                       for t, n in top],
        })
    index = {"updated": _now(), "days": days,
             "total_papers": sum(d["total"] for d in days)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    if args.days < 1:
        print("--days 至少是 1", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    start = args.date or default_day()
    targets = [start - timedelta(days=i) for i in range(args.days)]

    g: Gemini | None = None
    if not args.no_llm:
        key = load_key(args.api_key)
        if key:
            g = Gemini(key, model=args.model)
        else:
            log.warning("没有 Gemini key（设 GEMINI_API_KEY 或写 api_keys/gemini_api.txt），"
                        "本次只跑规则层")

    client = HttpClient(cache_dir=Path(".cache"), delay=args.delay, user_agent=UA)

    written = 0
    for day in targets:
        path = args.out / f"{day.isoformat()}.json"
        if path.exists() and not args.force:
            log.info("[%s] 已存在，跳过（--force 可覆盖）", day)
            continue
        record = run_day(day, args, client, g)
        if record is None:
            continue
        path.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        written += 1
        n_an = sum(1 for p in record["papers"] if p.get("analysis"))
        print(f"{day} — {record['total']} 篇，精选 {len(record['picked'])} 篇，"
              f"模型分析 {n_an} 篇 → {path} ({path.stat().st_size / 1024:.0f} KB)")
        if record["digest"].get("headline"):
            print(f"  综述：{record['digest']['headline']}")

    if args.keep_days > 0:
        dropped = prune(args.out, args.keep_days)
        if dropped:
            print(f"清理了 {dropped} 天的旧结果（--keep-days {args.keep_days}）")

    index = rebuild_index(args.out)
    print(f"\n目录已更新：{len(index['days'])} 天 / {index['total_papers']} 篇 → {args.out}/index.json")
    if g:
        print(g.report())
    if written == 0:
        log.warning("这次没有写入任何新的一天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
