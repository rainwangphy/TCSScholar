"""用 Gemini 分析当天精选论文，并写一段当日综述。

只喂标题、作者、分类和摘要——模型看不到正文，所以提示词里反复要求它**只根据
摘要说话**，说不清的地方写"摘要未说明"，而不是顺着标题编一个。页面上也会标明
这些分析出自模型、依据只有摘要。
"""

from __future__ import annotations

import logging
import re

from .arxiv import CAT_NAMES, ArxivPaper
from .gemini import Gemini
from .topics import TOPIC_ZH

log = logging.getLogger(__name__)

PAPER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "result": {"type": "STRING"},
        "method": {"type": "STRING"},
        "significance": {"type": "STRING"},
        "novelty": {
            "type": "STRING",
            "enum": ["突破", "显著改进", "稳步推进", "偏应用/工程", "难以判断"],
        },
        "rating": {"type": "INTEGER"},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["result", "method", "significance", "novelty", "rating", "keywords"],
    "propertyOrdering": ["result", "method", "significance", "novelty", "rating", "keywords"],
}

PAPER_PROMPT = """你是一位理论计算机科学（TCS）研究者，正在给同行写每日预印本速读。

下面是一篇今天上传到 arXiv 的论文。请**只根据给出的摘要**作答，不要引入摘要里
没有的信息；摘要没交代清楚的地方，直接写"摘要未说明"。

标题：{title}
作者：{authors}
arXiv 分类：{cats}
摘要：
{abstract}

**公式一律用 `$...$` 包起来**（比如写 `$O(n\\log n)$` 而不是裸的 `O(n\\log n)`），
页面靠这对定界符才知道哪一段要按数学排版。

请用简体中文填写：
- result：这篇论文的主要结果是什么。一到两句，要具体到界、条件、模型（比如
  "把 X 问题的近似比从 2 改进到 1.5，适用于稠密图"），不要写"研究了 X 问题"这种空话。
- method：拿到这个结果的关键技术手段。一句话，摘要没说就写"摘要未说明"。
- significance：为什么值得同行看一眼。一到两句，可以说它解决了什么长期问题、
  和已知结果的关系、或者局限在哪。别吹。
- novelty：从给定的几档里挑一个，判断不了就选"难以判断"。
- rating：1-5 的整数，表示对 TCS 同行的推荐优先级，5 表示当天最该读的那种。
  评分要克制：多数论文是 3，只有确实解决了公开问题或有明显突破才给 5。
- keywords：3 到 5 个关键词，用该领域的英文术语原文。"""

DIGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "themes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"name": {"type": "STRING"}, "note": {"type": "STRING"}},
                "required": ["name", "note"],
                "propertyOrdering": ["name", "note"],
            },
        },
    },
    "required": ["headline", "summary", "themes"],
    "propertyOrdering": ["headline", "summary", "themes"],
}

DIGEST_PROMPT = """你是一位理论计算机科学研究者，在给同行写 {date} 这天的 arXiv 速读开篇。

这天 arXiv 的 cs.CC / cs.DS / cs.DM / cs.GT / cs.CG 一共上传了 {total} 篇新论文。
按关键词规则统计，主题分布是：
{topic_lines}

以下是按规则打分排在前面的 {n} 篇（只给了标题和主分类）：
{titles}

公式同样要用 `$...$` 包起来。

请用简体中文写：
- headline：一句话概括这天的整体面貌，不超过 40 字。别用"精彩纷呈"这类空话，
  要落到具体主题上。
- summary：两到三句话，说说这天的论文集中在哪些方向、有没有值得注意的动向。
  只能依据上面给出的标题和统计，看不出趋势就老实说"当天没有明显集中的方向"。
- themes：2 到 4 个当天较突出的主题，每个给 name（主题名）和 note（一句话说明，
  点出是哪几篇撑起来的）。"""


# 已经带定界符的片段，原样跳过
_DELIMITED = re.compile(r"\$\$[^$]*\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", re.S)
_MATH_CMD = re.compile(r"\\[a-zA-Z]+")
# 可以安全地并进公式里的字符。刻意不含中日韩文字和中文标点——它们正是公式的天然边界
_MATH_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "\\{}()[]^_+-=/*.,:;<>|~'!& "
    "αβγδεζηθικλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ±×÷≤≥≠≈∈∉⊆∀∃∑∏√∞"
)
_TRIM = " ,.;:!&"
_SPAN = 80  # 单侧最多吃这么多字符，免得一个反斜杠把半句话吞进公式


def _wrap_bare(seg: str) -> str:
    """给没加定界符的公式补上 `$...$`。

    提示词已经要求模型自己加了，但它偶尔会忘（实测约 2% 的字段）。这里从每个
    `\command` 出发向两边吃"数学安全"的字符——中文和中文标点天然截断——再把
    括号不配平之类拿不准的情况原样放过，宁可显示成 LaTeX 原文，也好过让
    KaTeX 报错标红。
    """
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        m = _MATH_CMD.search(seg, i)
        if not m:
            out.append(seg[i:])
            break
        lo, hi = m.start(), m.end()
        while lo > i and lo > m.start() - _SPAN and seg[lo - 1] in _MATH_OK:
            lo -= 1
        while hi < n and hi < m.end() + _SPAN and seg[hi] in _MATH_OK:
            hi += 1
        # 向左别把普通英文单词吃进来（"the first algorithm \tilde{O}(n)"）
        pre = seg[lo:m.start()]
        last_word = None
        for w in re.finditer(r"[a-z]{4,}\s", pre):
            last_word = w
        if last_word:
            lo += last_word.end()
        while lo < m.start() and seg[lo] in _TRIM:
            lo += 1
        while hi > m.end() and seg[hi - 1] in _TRIM:
            hi -= 1

        frag = seg[lo:hi]
        if frag.count("{") != frag.count("}"):
            # 括号不配平，多半是吃过界了，这一段放弃
            out.append(seg[i:m.end()])
            i = m.end()
            continue
        out.append(seg[i:lo])
        out.append("$" + frag + "$")
        i = hi
    return "".join(out)


def normalize_math(text: str) -> str:
    if not text or "\\" not in text:
        return text
    parts: list[str] = []
    last = 0
    for m in _DELIMITED.finditer(text):
        parts.append(_wrap_bare(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_wrap_bare(text[last:]))
    return "".join(parts)


def _cats(p: ArxivPaper) -> str:
    return ", ".join(f"{c}（{CAT_NAMES[c]}）" if c in CAT_NAMES else c for c in p.cats)


def analyze_paper(g: Gemini, p: ArxivPaper) -> dict:
    """返回分析结果；调用失败时返回 {} —— 页面上这篇就只显示规则层的信息。"""
    prompt = PAPER_PROMPT.format(
        title=p.title,
        authors=", ".join(p.authors) or "（未列出）",
        cats=_cats(p),
        abstract=p.abstract or "（arXiv 未提供摘要）",
    )
    out = g.json(prompt, PAPER_SCHEMA, temperature=0.2, max_tokens=4096)
    if not out:
        # 见过一次模型在低温下退化成重复字符、把 token 预算耗光、JSON 截成半截的情况。
        # 采样本身是随机的，换个温度重来一次基本就好了；再不行才认输。
        log.info("重试一次：%s", p.title[:60])
        out = g.json(prompt, PAPER_SCHEMA, temperature=0.7, max_tokens=4096)
    if not out:
        log.warning("分析失败，跳过：%s", p.title[:60])
        return {}
    # 模型偶尔会给出范围外的评分，夹一下免得前端画出六颗星
    try:
        out["rating"] = max(1, min(5, int(out.get("rating", 3))))
    except (TypeError, ValueError):
        out["rating"] = 3
    for k in ("result", "method", "significance"):
        if isinstance(out.get(k), str):
            out[k] = normalize_math(out[k])
    out["model"] = g.model
    return out


def daily_digest(g: Gemini, day: str, papers: list[ArxivPaper], picked: list[ArxivPaper],
                 topic_counts: dict[str, int]) -> dict:
    topic_lines = "\n".join(
        f"  - {TOPIC_ZH.get(t, t)}：{c} 篇"
        for t, c in sorted(topic_counts.items(), key=lambda kv: -kv[1])[:10]
    ) or "  （无）"
    titles = "\n".join(f"  {i}. [{p.primary_cat}] {p.title}" for i, p in enumerate(picked, 1))
    prompt = DIGEST_PROMPT.format(
        date=day, total=len(papers), topic_lines=topic_lines, n=len(picked), titles=titles
    )
    out = g.json(prompt, DIGEST_SCHEMA, temperature=0.4, max_tokens=4096)
    if not out:
        return {}
    for k in ("headline", "summary"):
        if isinstance(out.get(k), str):
            out[k] = normalize_math(out[k])
    for t in out.get("themes") or []:
        if isinstance(t.get("note"), str):
            t["note"] = normalize_math(t["note"])
    out["model"] = g.model
    return out
