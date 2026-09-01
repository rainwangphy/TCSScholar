"""给当天的 arXiv 论文打分排序，挑出送去做深度分析的那几篇。

为什么要先筛：一天四十来篇，全喂给模型既费额度又没必要——大部分论文靠标题和
主题标签就够看了。这里用**可解释的规则**排个序，只有排在前面的才值得花一次
模型调用。每一项得分都记在 `score_parts` 里，页面上会原样显示，避免出现
"不知道为什么它被选中了"的黑箱。

分数不是"论文质量"，只是"值得优先看一眼"的启发式，不要当评价用。
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .arxiv import ArxivPaper, CORE_CATS
from .topics import classify

PROLIFIC = Path(__file__).with_name("prolific_authors.json")

# 摘要里出现这些说法，通常意味着作者自己认为拿到了强结果。
# 权重是拍的，但相对次序有讲究：解决公开问题 > 首个结果 > 改进现有界。
CLAIM_PATTERNS: list[tuple[str, float, str]] = [
    (r"\bresolv(e|es|ing)\b.{0,40}\b(conjecture|open (problem|question))|"
     r"\bsettl(e|es|ing)\b.{0,40}\b(conjecture|open (problem|question)|complexity)|"
     r"\banswer(s|ing)? .{0,30}\bopen (problem|question)", 3.0, "解决公开问题"),
    (r"\bfirst\b.{0,30}\b(algorithm|bound|construction|proof|separation|result)|"
     r"\bwe give the first|\bthe first (polynomial|sub|near|constant|nontrivial)", 2.0, "首个结果"),
    (r"\boptimal\b|\btight(ly)? (bound|analysis)|\bmatching lower bound|\bnearly[- ]optimal", 1.5, "最优/紧界"),
    (r"\bimprov(e|es|ing|ed)\b.{0,40}\b(bound|running time|approximation|complexity|factor)|"
     r"\bbreak(s|ing)? the\b|\bbeat(s|ing)? the\b", 1.2, "改进已有界"),
    (r"\blower bound|\bhardness\b|\binapproximab|\bimpossibility\b|\bseparation\b", 1.0, "下界/不可能性"),
    (r"\bdisprov(e|es|ing)\b|\bcounterexample\b|\brefut(e|es|ing)\b", 2.0, "证伪"),
]
_CLAIMS = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in CLAIM_PATTERNS]

# 反向信号：综述、教程、勘误之类不是新结果
NEGATIVE = re.compile(
    r"\b(a )?survey\b|\btutorial\b|\berratum\b|\bcorrigendum\b|\bcomment on\b|"
    r"\ba note on\b|\bunpublished draft\b|\bwithdrawn\b", re.IGNORECASE
)


def load_prolific() -> dict[str, int]:
    """五大会议的高产作者表（analyze.py 产出，随仓库提交）。

    只是个先验：在 FOCS/STOC/SODA/ITCS/EC 反复发论文的人，新预印本值得优先看。
    按姓名匹配，所以重名会误判——这也是它权重被压在 3 分以内的原因。
    """
    if not PROLIFIC.exists():
        return {}
    return json.loads(PROLIFIC.read_text(encoding="utf-8"))


def score(paper: ArxivPaper, prolific: dict[str, int]) -> tuple[float, dict]:
    parts: dict[str, float] = {}
    notes: list[str] = []

    # 1. 主题：命中的核心主题越多，越像一篇正经的 TCS 论文（上限 2 分）
    n_topics = len([t for t in paper.topics if t != "other"])
    if n_topics:
        parts["主题"] = round(min(2.0, n_topics * 0.7), 2)

    # 2. 主分类在核心 TCS 里，比只是交叉列表过来的更相关
    if paper.primary_cat in CORE_CATS:
        parts["核心分类"] = 1.0
    cross = len([c for c in paper.cats if c in CORE_CATS])
    if cross > 1:
        parts["跨分类"] = round(min(1.0, (cross - 1) * 0.5), 2)

    # 3. 作者先验：取最高产的那位，log 压缩，免得一个大牛就把榜单锁死
    best = max((prolific.get(a, 0) for a in paper.authors), default=0)
    if best:
        parts["作者"] = round(min(3.0, math.log2(best + 1)), 2)
        top = max(paper.authors, key=lambda a: prolific.get(a, 0))
        notes.append(f"{top} 在五大会议有 {best} 篇")

    # 4. 摘要里的结果强度信号（每类只记一次，避免同义反复刷分）
    text = f"{paper.title}. {paper.abstract}"
    claim = 0.0
    for rx, w, label in _CLAIMS:
        if rx.search(text):
            claim += w
            notes.append(label)
    if claim:
        parts["结果强度"] = round(min(4.0, claim), 2)

    # 5. 已被会议/期刊接收，是同行评议给过的信号
    if paper.journal_ref or re.search(r"\b(accepted|to appear)\b", paper.comment, re.IGNORECASE):
        parts["已被接收"] = 1.5
        notes.append("comment/journal-ref 显示已被接收")

    if NEGATIVE.search(paper.title):
        parts["非新结果"] = -3.0
        notes.append("标题看起来不是新结果")

    total = round(sum(parts.values()), 2)
    return total, {"parts": parts, "notes": notes}


def rank(papers: list[ArxivPaper], top: int) -> list[ArxivPaper]:
    """给每篇打分（就地写回 topics/score/score_parts），返回得分最高的 top 篇。"""
    prolific = load_prolific()
    for p in papers:
        # 标题加摘要一起过主题规则：只看标题的话，arXiv 预印本漏得比会议论文更多
        p.topics = classify(f"{p.title}. {p.abstract}")
        p.score, p.score_parts = score(p, prolific)
    return sorted(papers, key=lambda p: (-p.score, p.title))[:top]
