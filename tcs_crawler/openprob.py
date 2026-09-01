"""开放问题清单：登记表、候选解决方案的发现与判定。

和每日速读那条线的关系：`site/daily/` 里已经存着每天全部 arXiv TCS 论文的标题和
摘要，所以"有没有人解决了某个公开问题"这件事，绝大部分时候**不需要再发一次网络
请求**——直接在已有的每日归档里扫就行。`--search` 才会额外去 arXiv 查。

三层，和每日那条线一样每层都能单独降级：

  1. 登记表  — problems/registry.json，手工维护，脚本只读不写
  2. 规则    — 关键词 + "解决了某某猜想"这类措辞，从候选里筛出值得看的几篇
  3. 模型    — 只把筛出来的送给 Gemini 判"这篇到底解决了没有"，出结论加证据

**脚本永远不会自己把一个问题标成已解决。** TCS 里宣称证明了大猜想的预印本每年都有，
绝大多数是错的；模型判一句"resolves"远不足以作数。命中高置信度的候选只会把问题
标成 `claimed`（待人工复核），页面上原样显示模型的结论和它引的那句证据，
真正改状态是人去编辑 registry.json、并附上一条 resolution 记录。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .arxiv import API, NS, _parse_entry
from .gemini import Gemini
from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

REGISTRY = Path("problems") / "registry.json"

STATUSES = {"open", "claimed", "resolved"}
REQUIRED = ("id", "title", "area", "status", "statement", "why")

# 候选发现：作者自己宣称"解决/证否了某个猜想或公开问题"的措辞。
# 只有命中这一层（或下面的弱信号加多个关键词）才值得占一次模型调用。
RESOLVE_RX = re.compile(
    r"\b(resolv|settl|prove|proving|proof of|disprov|refut|answer(s|ing)?)\w*\b[^.]{0,60}"
    r"\b(conjecture|hypothesis|open (problem|question)|question of)\b"
    r"|\b(conjecture|hypothesis|open (problem|question))\b[^.]{0,40}"
    r"\b(is (true|false)|holds|fails|resolved|settled|refuted|disproved)\b"
    r"|\bcounterexample to\b|\b(affirmative|negative) answer\b"
    r"|\bwe (prove|show|establish) the\b[^.]{0,40}\bconjecture\b",
    re.IGNORECASE,
)
# 弱信号：没直接说"解决猜想"，但确实是把某个界推进了一步
PROGRESS_RX = re.compile(
    r"\bimprov\w*\b|\bfirst\b|\bbreak(s|ing)? the\b|\bnew (upper|lower) bound\b"
    r"|\bbetter than\b|\bbarrier\b|\bnearly[- ]optimal\b|\btight\b",
    re.IGNORECASE,
)

VERDICTS = ["resolves", "major progress", "related", "unrelated"]
VERDICT_RANK = {v: i for i, v in enumerate(VERDICTS)}

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": VERDICTS},
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "claim": {"type": "STRING"},
        "evidence": {"type": "STRING"},
        "caveat": {"type": "STRING"},
    },
    "required": ["verdict", "confidence", "claim", "evidence", "caveat"],
    "propertyOrdering": ["verdict", "confidence", "claim", "evidence", "caveat"],
}

JUDGE_PROMPT = """You are a theoretical computer science researcher triaging arXiv
preprints against a watchlist of well-known open problems.

THE OPEN PROBLEM
Title: {problem}
Statement: {statement}
Where it stands: {state}

THE PREPRINT
Title: {title}
Authors: {authors}
arXiv categories: {cats}
Abstract:
{abstract}

Decide **from the abstract alone** whether this preprint bears on that open problem. You
have not read the paper and cannot verify any proof; you are judging what the authors
claim and how close that claim is to the problem above. Be conservative — a preprint that
mentions the problem, cites it as motivation, or solves a special case does NOT resolve it.

Write in English and wrap every piece of mathematics in `$...$` delimiters.

- verdict: one of the given values.
  * "resolves" — the authors claim to settle exactly this problem, in full generality,
    either affirmatively or by refutation. Use this only when the claim, if correct, ends
    the problem.
  * "major progress" — a substantially better bound, a large special case, or a barrier
    result that changes what is known.
  * "related" — same area or same techniques, but does not move the problem.
  * "unrelated" — the keyword match was a coincidence.
- confidence: how sure you are of that classification given only the abstract.
- claim: one sentence stating what the authors claim, in the problem's own terms.
- evidence: the sentence from the abstract that carries the claim, quoted as closely as
  the abstract allows. If no sentence supports a strong verdict, say so here.
- caveat: what a reader would have to check before believing it — an unusual assumption,
  a weaker model than the problem asks for, a restriction to special cases, no stated
  peer review. If nothing stands out, write "Nothing obvious from the abstract"."""


# ── 登记表 ──────────────────────────────────────────────────────────

def load_registry(path: Path = REGISTRY) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    reg = json.loads(path.read_text(encoding="utf-8"))
    problems = reg.get("problems") or []
    errs = validate(reg)
    if errs:
        raise SystemExit("registry.json 有问题：\n  " + "\n  ".join(errs))
    log.info("登记表 %d 条：%s", len(problems),
             ", ".join(f"{s} {sum(1 for p in problems if p['status'] == s)}"
                       for s in ("open", "claimed", "resolved")))
    return reg


def validate(reg: dict) -> list[str]:
    """登记表是手写的，所以格式错误要当场报出来，而不是让页面上出现空白卡片。"""
    errs: list[str] = []
    areas = reg.get("areas") or {}
    seen: set[str] = set()
    for i, p in enumerate(reg.get("problems") or []):
        where = p.get("id") or f"#{i}"
        for k in REQUIRED:
            if not p.get(k):
                errs.append(f"{where}: 缺字段 {k}")
        if p.get("id") in seen:
            errs.append(f"{where}: id 重复")
        seen.add(p.get("id", ""))
        if p.get("status") not in STATUSES:
            errs.append(f"{where}: status={p.get('status')!r}，只能是 {sorted(STATUSES)}")
        if p.get("area") not in areas:
            errs.append(f"{where}: area={p.get('area')!r} 不在 areas 里")
        if p.get("status") == "resolved" and not p.get("resolution"):
            errs.append(f"{where}: 标成 resolved 就必须写 resolution（谁、哪年、链接）")
        res = p.get("resolution")
        if res and not res.get("url"):
            errs.append(f"{where}: resolution 没有链接——已解决的问题必须能查证")
        for d in (p.get("watch") or {}).get("dismissed") or []:
            if not d.get("arxiv") or not d.get("note"):
                errs.append(f"{where}: dismissed 每条都要写 arxiv 和 note——"
                            "不写理由的否决，下一个人没法复核")
    return errs


def state_summary(problem: dict, limit: int = 3) -> str:
    """给模型看的"目前进展到哪"，取登记表里最近的几条 evidence。"""
    ev = sorted(problem.get("evidence") or [], key=lambda e: -(e.get("year") or 0))[:limit]
    return " ".join(f"({e.get('year', '?')}) {e['text']}" for e in ev) or "Not recorded."


# ── 候选发现 ────────────────────────────────────────────────────────

def _hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t and t.lower() in text]


def dismissed_ids(problem: dict) -> set[str]:
    """人工看过并否掉的论文。

    没有这个口子，一篇宣称证明了 P≠NP 的预印本会永远挂在复核队列里——判定结果是
    缓存的，模型每次都会给同样的答案，而"有人已经读过、不成立"这个信息只存在于
    读它的那个人脑子里。写进登记表，它才既不再占模型调用，也不再刷屏。
    """
    return {d.get("arxiv", "") for d in (problem.get("watch") or {}).get("dismissed") or []}


def match(problem: dict, paper: dict) -> tuple[float, list[str]] | None:
    """规则层：这篇论文值不值得为这个问题花一次模型调用。

    返回 (分数, 命中的关键词)，不值得则返回 None。分数只用来排序，不是判断。
    """
    terms = (problem.get("watch") or {}).get("terms") or []
    if not terms:
        return None
    if paper.get("arxiv_id") in dismissed_ids(problem):
        return None
    text = f"{paper.get('title', '')}. {paper.get('abstract', '')}".lower()
    hit = _hits(text, terms)
    if not hit:
        return None

    strong = bool(RESOLVE_RX.search(text))
    weak = bool(PROGRESS_RX.search(text))
    # 一个关键词加一句"我们解决了某猜想"就够；只是提了两次关键词还得再有点进展措辞，
    # 否则综述和引言里顺带提一句的论文会把候选池灌满
    if not strong and not (len(hit) >= 2 and weak):
        return None

    score = round(min(3.0, len(hit) * 0.8) + (3.0 if strong else 0.0) + (0.5 if weak else 0.0), 2)
    return score, hit


def scan_daily(daily_dir: Path, problems: list[dict], days: int = 0) -> tuple[dict, dict]:
    """在已有的每日归档里找候选。不发任何网络请求。

    返回 ({problem_id: [candidate, ...]}, 扫描统计)。
    """
    files = sorted(daily_dir.glob("20*.json"), reverse=True)
    if days > 0:
        files = files[:days]
    out: dict[str, list[dict]] = {p["id"]: [] for p in problems}
    seen, dates = 0, []
    for path in files:
        try:
            day = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("跳过损坏的 %s", path)
            continue
        dates.append(day.get("date", ""))
        for paper in day.get("papers") or []:
            seen += 1
            for p in problems:
                m = match(p, paper)
                if m:
                    out[p["id"]].append(_candidate(paper, day.get("date", ""), *m, "daily archive"))
    # 文件名是新到旧排的，所以第一个是最近的一天
    stats = {"days": len(dates), "papers": seen,
             "from": dates[-1] if dates else "", "to": dates[0] if dates else ""}
    return out, stats


def _candidate(paper: dict, day: str, score: float, hit: list[str], source: str) -> dict:
    return {
        "arxiv_id": paper.get("arxiv_id", ""),
        "title": paper.get("title", ""),
        "authors": (paper.get("authors") or [])[:6],
        "abstract": paper.get("abstract", ""),
        "cats": paper.get("cats") or [],
        "url": paper.get("abs_url") or f"https://arxiv.org/abs/{paper.get('arxiv_id', '')}",
        "date": day or (paper.get("published") or "")[:10],
        "comment": paper.get("comment", ""),
        "journal_ref": paper.get("journal_ref", ""),
        "score": score,
        "hits": hit,
        "source": source,
    }


def search_arxiv(client: HttpClient, problem: dict, limit: int = 30,
                 force: bool = False) -> list[dict]:
    """按登记表里的 queries 去 arXiv 查，取最近提交的若干篇。

    每日归档只覆盖脚本上线之后的日子，而且只有那五个核心分类；老问题的解决方案
    可能挂在 math.CO 或 quant-ph 上。这条路径补的就是这两块。
    """
    queries = (problem.get("watch") or {}).get("queries") or []
    out: list[dict] = []
    for q in queries:
        try:
            body = client.get(API, params={
                "search_query": q, "start": 0, "max_results": limit,
                "sortBy": "submittedDate", "sortOrder": "descending",
            }, force=force)
            root = ET.fromstring(body)
        except (HttpError, ET.ParseError) as e:
            log.warning("arXiv 查询失败 %r：%s", q, e)
            continue
        for entry in root.findall("atom:entry", NS):
            p = _parse_entry(entry)
            if not p:
                continue
            m = match(problem, p.to_dict())
            if m:
                out.append(_candidate(p.to_dict(), p.published[:10], *m, f"arXiv search: {q}"))
    return out


# ── 模型判定 ────────────────────────────────────────────────────────

def judge(g: Gemini, problem: dict, cand: dict) -> dict:
    """让模型判这篇到底有没有解决这个问题。失败返回 {}，页面上就只显示规则层。"""
    from .digest import normalize_math, repair_escapes  # 循环引用，用到时再引

    prompt = JUDGE_PROMPT.format(
        problem=problem["title"],
        statement=problem["statement"],
        state=state_summary(problem),
        title=cand["title"],
        authors=", ".join(cand["authors"]) or "(none listed)",
        cats=", ".join(cand["cats"]) or "(none)",
        abstract=cand["abstract"] or "(no abstract)",
    )
    out = g.json(prompt, JUDGE_SCHEMA, temperature=0.1, max_tokens=2048)
    if not out:
        log.info("重试一次：%s × %s", problem["id"], cand["title"][:50])
        out = g.json(prompt, JUDGE_SCHEMA, temperature=0.6, max_tokens=2048)
    if not out:
        return {}
    if out.get("verdict") not in VERDICT_RANK:
        out["verdict"] = "related"
    for k in ("claim", "evidence", "caveat"):
        if isinstance(out.get(k), str):
            out[k] = normalize_math(repair_escapes(out[k]))
    out["model"] = g.model
    return out


def flag_of(signals: list[dict]) -> str:
    """有没有需要人工复核的候选。返回 '' / 'progress' / 'claimed'。"""
    for s in signals:
        v = (s.get("verdict") or {}).get("verdict")
        c = (s.get("verdict") or {}).get("confidence")
        if v == "resolves" and c in ("high", "medium"):
            return "claimed"
    for s in signals:
        if (s.get("verdict") or {}).get("verdict") == "major progress":
            return "progress"
    return ""


def sort_signals(signals: list[dict]) -> list[dict]:
    def key(s):
        v = (s.get("verdict") or {}).get("verdict", "related")
        return (VERDICT_RANK.get(v, 9), -s.get("score", 0), s.get("date", ""))
    return sorted(signals, key=key)


# ── 参考链接核对 ────────────────────────────────────────────────────

_NORM = re.compile(r"[^a-z0-9]+")


def _norm_title(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()


def check_refs(client: HttpClient, problems: list[dict], force: bool = False) -> list[dict]:
    """核对登记表里的每条链接。

    "有证据"这句话要站得住，链接就不能是死的。arXiv 链接还多查一步：把 abs 页的
    真实标题取回来和登记表里写的比对，链接活着但指向另一篇论文同样算错。
    """
    problems_by_ref: list[tuple[str, dict]] = []
    for p in problems:
        for r in p.get("refs") or []:
            problems_by_ref.append((p["id"], r))
        if p.get("resolution"):
            problems_by_ref.append((p["id"], p["resolution"]))

    results = []
    for pid, ref in problems_by_ref:
        url = ref.get("url", "")
        if not url:
            continue
        row = {"problem": pid, "url": url, "label": ref.get("label") or ref.get("by", "")}
        arxiv_id = ref.get("arxiv")
        try:
            if arxiv_id:
                body = client.get(API, params={"id_list": arxiv_id, "max_results": 1}, force=force)
                root = ET.fromstring(body)
                entry = root.find("atom:entry", NS)
                got = _parse_entry(entry) if entry is not None else None
                if got is None:
                    row.update(ok=False, why="arXiv 上查不到这个 id")
                elif _norm_title(got.title) != _norm_title(ref.get("title", "")):
                    row.update(ok=False, why=f"标题对不上，arXiv 上是：{got.title}")
                else:
                    row.update(ok=True, why=got.title)
            else:
                client.get(url, force=force)
                row.update(ok=True, why="200")
        except (HttpError, ET.ParseError) as e:
            row.update(ok=False, why=str(e)[:160])
        results.append(row)
    return results
