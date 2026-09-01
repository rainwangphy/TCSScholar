#!/usr/bin/env python3
"""维护"TCS 未解问题"清单：从 arXiv 上找可能的解决方案，产出 site/open/index.json。

清单本身是手写的（problems/registry.json），这个脚本只做两件事：

  1. 在**已有的**每日归档 site/daily/*.json 里扫一遍，看有没有哪篇新预印本宣称
     解决了清单上的某个问题；`--search` 会再额外去 arXiv 查一轮，补上不在
     那五个核心分类里的论文（很多组合和几何的结果挂在 math.CO）。
  2. 把筛出来的候选送给 Gemini 判一句"这到底解决了没有"，连同它引的那句证据
     一起写进产物。

**脚本不会自己把问题标成已解决。** 宣称证明了大猜想的预印本每年都有，绝大多数
是错的，模型只看摘要更判不了对错。高置信度的候选只会让问题在页面上显示成
"claimed — 待复核"，真正改状态是人去编辑 registry.json 并附上 resolution 记录。

用法:
    python open_problems.py                    # 扫每日归档，判新候选，写盘
    python open_problems.py --no-llm           # 只跑规则层，不调模型
    python open_problems.py --search           # 额外去 arXiv 查一轮
    python open_problems.py --days 30          # 只扫最近 30 天的归档
    python open_problems.py --check-refs       # 核对登记表里的每条参考链接
    python open_problems.py --rejudge          # 忽略缓存，全部重判
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from tcs_crawler.gemini import DEFAULT_MODEL, Gemini, load_key
from tcs_crawler.http import HttpClient
from tcs_crawler.openprob import (
    REGISTRY, check_refs, flag_of, judge, load_registry, scan_daily, search_arxiv,
    sort_signals,
)

log = logging.getLogger("open")

DAILY_DIR = Path("site") / "daily"
OUT_DIR = Path("site") / "open"
UA = "TCSScholar-openproblems/0.1 (https://github.com/rainwangphy/TCSScholar)"

# 摘要原样存进产物，页面上展开显示——判定是模型给的，读者得能自己核对它依据的是什么。
# 截断只是为了不让产物无限膨胀，arXiv 摘要极少超过这个长度。
ABSTRACT_CAP = 1600


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="open_problems.py",
        description="维护 TCS 未解问题清单，产出 site/open/index.json。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python open_problems.py                # 扫每日归档 + 判新候选\n"
            "  python open_problems.py --no-llm       # 只跑规则层\n"
            "  python open_problems.py --search       # 额外去 arXiv 查一轮\n"
            "  python open_problems.py --check-refs   # 核对参考链接\n"
        ),
    )
    p.add_argument("--registry", type=Path, default=REGISTRY, help=f"登记表，默认 {REGISTRY}")
    p.add_argument("--daily", type=Path, default=DAILY_DIR, help=f"每日归档目录，默认 {DAILY_DIR}")
    p.add_argument("-o", "--out", type=Path, default=OUT_DIR, help=f"输出目录，默认 {OUT_DIR}")
    p.add_argument("--days", type=int, default=0, metavar="N",
                   help="只扫最近 N 天的归档，0（默认）表示全部")
    p.add_argument("--search", action="store_true",
                   help="额外去 arXiv 按登记表里的 queries 查一轮（会发网络请求）")
    p.add_argument("--search-limit", type=int, default=30,
                   help="每条 query 取最近几篇，默认 30")
    p.add_argument("--per-problem", type=int, default=3,
                   help="每个问题每次最多新判几篇，默认 3")
    p.add_argument("--max-judge", type=int, default=40,
                   help="本次最多调用模型几次，默认 40")
    p.add_argument("--keep-signals", type=int, default=6,
                   help="每个问题在产物里最多保留几条线索，默认 6")
    p.add_argument("--rejudge", action="store_true", help="忽略已有判定，全部重判")
    p.add_argument("--no-llm", action="store_true", help="跳过模型判定，只跑规则层")
    p.add_argument("--check-refs", action="store_true",
                   help="只核对登记表里的参考链接，不做别的")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini 模型，默认 {DEFAULT_MODEL}")
    p.add_argument("--api-key", default=None, help="Gemini key，默认取 GEMINI_API_KEY 或 api_keys/")
    p.add_argument("--delay", type=float, default=3.0, help="HTTP 请求间隔秒数，默认 3.0")
    p.add_argument("--cache-ttl-days", type=float, default=1.0, metavar="D",
                   help="HTTP 缓存有效期，默认 1 天（见下方说明），0 表示永久")
    p.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_verdicts(out_dir: Path) -> dict[tuple[str, str], dict]:
    """已有的判定结果，按 (问题, arXiv id) 索引。

    判定一篇要花一次模型调用，而摘要不会变，所以判过的就不再判——每周跑一次的
    增量成本因此只有当周真正新出现的那几篇。

    它是**独立的一个文件**，不是从 index.json 里反推的。曾经就是从产物里反推的，
    结果是：跑一次 `--no-llm`（或者不带 `--search`）会写出一份线索更少的产物，
    连带把上一轮的判定一起冲掉，下次又得全部重判。缓存不该被渲染产物的形状绑架。
    """
    path = out_dir / "verdicts.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("%s 损坏，这次全部重判", path)
        return {}
    out = {}
    for key, v in (raw.get("verdicts") or {}).items():
        pid, _, aid = key.partition("/")
        if pid and aid:
            out[(pid, aid)] = v
    return out


def save_verdicts(out_dir: Path, cache: dict[tuple[str, str], dict]) -> Path:
    path = out_dir / "verdicts.json"
    path.write_text(json.dumps(
        {"updated": _now(), "verdicts": {f"{pid}/{aid}": v for (pid, aid), v in sorted(cache.items())}},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def collect(args, problems: list[dict], client: HttpClient | None) -> tuple[dict, dict]:
    """规则层：每个问题一批去重后的候选，按分数降序。"""
    found, stats = scan_daily(args.daily, problems, args.days)
    if args.search and client is not None:
        for p in problems:
            extra = search_arxiv(client, p, args.search_limit, force=args.refresh)
            found[p["id"]].extend(extra)
            if extra:
                log.info("[%s] arXiv 检索补了 %d 篇候选", p["id"], len(extra))

    for pid, cands in found.items():
        best: dict[str, dict] = {}
        for c in cands:
            cur = best.get(c["arxiv_id"])
            if cur is None or c["score"] > cur["score"]:
                best[c["arxiv_id"]] = c
        found[pid] = sorted(best.values(), key=lambda c: (-c["score"], c["date"]))
    return found, stats


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    reg = load_registry(args.registry)
    problems = reg["problems"]

    # 缓存要有有效期。每日那条线每天查的是不同的日期区间，URL 天然不同，永久缓存
    # 没问题；这里的检索式**每次都一模一样**（"最近 N 篇匹配的论文"），永久缓存会让
    # 下一周原样重放上一周的结果，新论文一篇也看不到。链接核对同理，缓存住就永远
    # 检不出失效的链接。
    client = HttpClient(cache_dir=Path(".cache"), delay=args.delay, user_agent=UA,
                        cache_ttl_days=args.cache_ttl_days or None)

    if args.check_refs:
        rows = check_refs(client, problems, force=args.refresh)
        bad = [r for r in rows if not r["ok"]]
        for r in rows:
            print(("  ok  " if r["ok"] else "  BAD ") + f"{r['problem']:26} {r['url']}")
            if not r["ok"]:
                print(f"        {r['why']}")
        print(f"\n{len(rows)} 条链接，{len(bad)} 条有问题")
        return 1 if bad else 0

    watched = [p for p in problems if p["status"] != "resolved"]
    found, stats = collect(args, watched, client if args.search else None)

    g: Gemini | None = None
    if not args.no_llm:
        key = load_key(args.api_key)
        if key:
            g = Gemini(key, model=args.model)
        else:
            log.warning("没有 Gemini key（设 GEMINI_API_KEY 或写 api_keys/gemini_api.txt），"
                        "本次只跑规则层")

    args.out.mkdir(parents=True, exist_ok=True)
    cache = {} if args.rejudge else load_verdicts(args.out)
    budget = args.max_judge
    reused = judged = 0

    out_problems = []
    for p in problems:
        entry = {k: v for k, v in p.items() if k != "watch"}
        signals: list[dict] = []
        fresh = 0  # 这个问题这一轮新判了几篇
        for cand in found.get(p["id"], []):
            sig = dict(cand)
            sig["abstract"] = (cand.get("abstract") or "")[:ABSTRACT_CAP]
            key = (p["id"], cand["arxiv_id"])
            if key in cache:
                sig["verdict"] = cache[key]
                reused += 1
            elif g is None or budget <= 0 or fresh >= args.per_problem:
                # 没模型、超预算，或这个问题这轮的配额用完了——剩下的候选排在后面，
                # 下次再说。规则层命中但没判过的不进产物：只有关键词匹配这一条信息，
                # 摆到页面上除了制造噪声没有别的作用。
                continue
            else:
                log.info("[%s] 判定 %s %s", p["id"], cand["arxiv_id"], cand["title"][:60])
                v = judge(g, p, cand)
                fresh += 1  # 判失败也算，免得同一篇在一轮里反复重试
                budget -= 1
                if not v:
                    continue
                sig["verdict"] = v
                cache[key] = v  # 立刻进缓存，这一轮后面和下一轮都不必重判
                judged += 1
            # unrelated 是规则层误命中，没有保留价值
            if (sig.get("verdict") or {}).get("verdict") == "unrelated":
                continue
            signals.append(sig)

        signals = sort_signals(signals)[: args.keep_signals]
        entry["signals"] = signals
        entry["flag"] = flag_of(signals)
        entry["candidates"] = len(found.get(p["id"], []))
        # 人工否掉的照实留在产物里：读者应该看得到"有人这么宣称过，以及为什么不算"
        entry["dismissed"] = (p.get("watch") or {}).get("dismissed") or []
        out_problems.append(entry)

    counts = {s: sum(1 for p in out_problems if p["status"] == s)
              for s in ("open", "claimed", "resolved")}
    payload = {
        "updated": _now(),
        "note": reg.get("note", ""),
        "areas": reg["areas"],
        "counts": counts,
        "signal_count": sum(len(p["signals"]) for p in out_problems),
        "flagged": [p["id"] for p in out_problems if p["flag"] == "claimed"],
        "scanned": stats,
        "searched": bool(args.search),
        "llm": bool(g),
        "model": g.model if g else "",
        "problems": out_problems,
    }

    path = args.out / "index.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    vpath = save_verdicts(args.out, cache)

    print(f"\n{counts['open']} 个未解 · {counts['claimed']} 个待复核 · {counts['resolved']} 个已解决"
          f" → {path} ({path.stat().st_size / 1024:.0f} KB)")
    print(f"扫了 {stats['days']} 天 / {stats['papers']} 篇归档"
          + (f"（{stats['from']} → {stats['to']}）" if stats["days"] else ""))
    print(f"线索 {payload['signal_count']} 条（新判 {judged}，复用 {reused}）"
          f" · 判定缓存 {len(cache)} 条 → {vpath}")
    if payload["flagged"]:
        print("\n⚠️  有候选宣称解决了这些问题，需要人工复核后再改 registry.json：")
        for pid in payload["flagged"]:
            p = next(x for x in out_problems if x["id"] == pid)
            for s in p["signals"]:
                if (s.get("verdict") or {}).get("verdict") == "resolves":
                    print(f"  {pid}: {s['title']}\n    {s['url']}\n    {s['verdict']['claim']}")
    if g:
        print(g.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
