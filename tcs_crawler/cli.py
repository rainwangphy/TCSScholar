"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .dblp import discover_tocs, fetch_toc_papers
from .http import HttpClient, HttpError
from .models import Paper
from .storage import write_csv, write_jsonl, write_sqlite
from .venues import DEFAULT_VENUES, VENUES, get_venue

log = logging.getLogger("tcs_crawler")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawl.py",
        description="抓取 TCS 顶会（FOCS / STOC / SODA / ITCS / EC）论文元数据，数据源为 DBLP。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python crawl.py                                  # 抓全部 5 个会议的所有年份\n"
            "  python crawl.py -v stoc focs --since 2015        # 只抓 STOC/FOCS 2015 年以后\n"
            "  python crawl.py --formats csv sqlite -o data     # 指定输出格式和目录\n"
            "  python crawl.py --since 2026 --refresh           # 忽略缓存，刷新最新一届\n"
        ),
    )
    p.add_argument(
        "-v", "--venues", nargs="+", default=DEFAULT_VENUES, metavar="VENUE",
        help=f"要抓的会议，默认全部：{' '.join(DEFAULT_VENUES)}",
    )
    p.add_argument("--since", type=int, default=None, help="起始年份（含）")
    p.add_argument("--until", type=int, default=None, help="结束年份（含）")
    p.add_argument("-o", "--out", type=Path, default=Path("data"), help="输出目录，默认 data/")
    p.add_argument(
        "--formats", nargs="+", default=["jsonl", "csv"],
        choices=["jsonl", "csv", "sqlite"], help="输出格式，默认 jsonl csv",
    )
    p.add_argument(
        "--per-venue", action="store_true",
        help="每个会议额外单独输出一份文件",
    )
    p.add_argument(
        "--delay", type=float, default=3.0,
        help="请求间隔秒数，默认 3.0；DBLP 限流较严，调小容易被掐连接",
    )
    p.add_argument(
        "--max-delay", type=float, default=20.0,
        help="被限流时自适应放大的间隔上限，默认 20 秒",
    )
    p.add_argument("--timeout", type=float, default=60.0, help="单次请求超时秒数")
    p.add_argument("--max-retries", type=int, default=6, help="最大重试次数")
    p.add_argument("--cache-dir", type=Path, default=Path(".cache"), help="HTTP 缓存目录")
    p.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    p.add_argument("--refresh", action="store_true", help="忽略已有缓存，强制重新请求")
    p.add_argument(
        "--cache-ttl-days", type=float, default=None,
        help="缓存有效期（天），超期自动重新请求；默认永久有效",
    )
    p.add_argument(
        "--user-agent", default=None,
        help="自定义 User-Agent，建议填上你的邮箱（DBLP 推荐做法）",
    )
    p.add_argument("--include-editorship", action="store_true", help="保留论文集本身的编辑条目")
    p.add_argument("--include-front-matter", action="store_true", help="保留卷首/目录等条目")
    p.add_argument("--list-venues", action="store_true", help="列出支持的会议后退出")
    p.add_argument("-q", "--quiet", action="store_true", help="只输出警告和错误")
    p.add_argument("--debug", action="store_true", help="输出调试日志")
    return p


def list_venues() -> None:
    print(f"{'slug':<6} {'DBLP':<18} {'起始':<6} 会议")
    for slug in DEFAULT_VENUES:
        v = VENUES[slug]
        note = f"  ({v.note})" if v.note else ""
        print(f"{v.slug:<6} {v.dblp_dir:<18} {v.first_year:<6} {v.name}{note}")


def print_summary(papers: list[Paper]) -> None:
    by_venue: dict[str, Counter] = defaultdict(Counter)
    for p in papers:
        by_venue[p.venue][p.year] += 1

    print("\n=== 抓取汇总 ===")
    for slug in sorted(by_venue):
        years = by_venue[slug]
        total = sum(years.values())
        span = f"{min(years)}-{max(years)}" if years else "-"
        print(f"{slug.upper():<6} {total:>6} 篇  {len(years):>3} 届  {span}")
    print(f"{'合计':<6} {len(papers):>6} 篇")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_venues:
        list_venues()
        return 0

    try:
        venues = [get_venue(s) for s in args.venues]
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2

    ua_kwargs = {"user_agent": args.user_agent} if args.user_agent else {}
    client = HttpClient(
        cache_dir=None if args.no_cache else args.cache_dir,
        delay=args.delay,
        max_delay=args.max_delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        use_cache=not args.no_cache,
        cache_ttl_days=args.cache_ttl_days,
        **ua_kwargs,
    )

    all_papers: list[Paper] = []
    failures: list[str] = []

    for venue in venues:
        try:
            tocs = discover_tocs(client, venue, force=args.refresh)
        except HttpError as e:
            log.error("[%s] 目录页抓取失败，跳过：%s", venue.slug, e)
            failures.append(f"{venue.slug}: index")
            continue

        selected = [
            t for t in tocs
            if (args.since is None or (t.year or 0) >= args.since)
            and (args.until is None or (t.year or 9999) <= args.until)
        ]
        log.info("[%s] 待抓 %d/%d 届", venue.slug, len(selected), len(tocs))

        venue_papers: list[Paper] = []
        for i, toc in enumerate(selected, 1):
            log.info("[%s] (%d/%d) %s", venue.slug, i, len(selected), toc.stem)
            result = fetch_toc_papers(
                client, venue, toc,
                force=args.refresh,
                include_editorship=args.include_editorship,
                include_front_matter=args.include_front_matter,
            )
            venue_papers.extend(result.papers)
            if not result.complete:
                failures.append(
                    f"{venue.slug}/{toc.stem}: 已抓 {len(result.papers)}/{result.expected} 条"
                    f"{' - ' + result.error if result.error else ''}"
                )

        # 抓取后再按 DBLP 记录里的真实年份过滤一次
        if args.since is not None:
            venue_papers = [p for p in venue_papers if p.year >= args.since]
        if args.until is not None:
            venue_papers = [p for p in venue_papers if p.year <= args.until]

        all_papers.extend(venue_papers)

        if args.per_venue and venue_papers:
            write_outputs(venue_papers, args, stem=venue.slug, venues=[venue.slug])

    if not all_papers:
        log.warning("没有抓到任何论文")
    else:
        write_outputs(all_papers, args, stem="tcs_papers", venues=[v.slug for v in venues])
        print_summary(all_papers)

    print(
        f"\n请求 {client.stats['requests']} 次，"
        f"缓存命中 {client.stats['cache_hits']} 次，"
        f"重试 {client.stats['retries']} 次，"
        f"限流 {client.stats['throttled']} 次"
    )
    if failures:
        print(f"\n以下 {len(failures)} 项不完整（直接重跑即可，已缓存的页不会重复请求）：", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


def write_outputs(papers: list[Paper], args, stem: str, venues: list[str]) -> None:
    papers = sorted(papers, key=lambda p: (p.venue, p.year, p.title))
    out: Path = args.out
    if "jsonl" in args.formats:
        write_jsonl(papers, out / f"{stem}.jsonl")
    if "csv" in args.formats:
        write_csv(papers, out / f"{stem}.csv")
    if "sqlite" in args.formats:
        write_sqlite(papers, out / "tcs_papers.db", replace_venues=venues)
