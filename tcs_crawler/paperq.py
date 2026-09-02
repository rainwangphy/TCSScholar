"""从论文结论里抽出作者自己留下的公开问题。

和登记表那条线的分工：登记表上的是**领域级的大问题**（P vs NP、UGC、k-server），
几十年不动一次，值得逐条手写并追踪谁解决了它；这里抽的是**论文级的小问题**——
作者在结论里写「我们没能处理 X」「Y 是否成立仍未知」——数量大、单条寿命短、
多数难度也低得多。想挑个题做的人要看的是后者。

一条铁律：**只抽论文真的写了的**。模型很容易顺着标题替作者「补」一个听起来合理的
公开问题出来，那种东西看着像真的，但没人说过。所以每条都必须引一句原文，
`verify_quote()` 再拿这句话回原文里比对，对不上的整条丢掉。
"""

from __future__ import annotations

import logging
import re

from .gemini import Gemini

log = logging.getLogger(__name__)

SHAPES = [
    "close a quantitative gap",
    "remove an assumption",
    "extend to a broader setting",
    "new direction",
]

QUESTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "quote": {"type": "STRING"},
                    "context": {"type": "STRING"},
                    "shape": {"type": "STRING", "enum": SHAPES},
                    "emphasis": {"type": "STRING", "enum": ["highlighted", "in passing"]},
                    "background": {"type": "STRING"},
                    "area": {"type": "STRING"},
                },
                "required": ["question", "quote", "context", "shape", "emphasis",
                             "background", "area"],
                "propertyOrdering": ["question", "quote", "context", "shape", "emphasis",
                                     "background", "area"],
            },
        },
    },
    "required": ["questions"],
}

QUESTION_PROMPT = """You are a theoretical computer science researcher reading a new
preprint to find problems a colleague could actually pick up and work on.

PAPER
Title: {title}
Authors: {authors}
arXiv categories: {cats}
Abstract:
{abstract}

EXCERPTS FROM THE PAPER (its conclusion, discussion and any passage mentioning an open
problem — this is the paper's own text, not a summary):
{excerpt}

Extract the open questions **the authors themselves state** in those excerpts. This is an
extraction task, not a brainstorming task:

- If the excerpts state no open question, return an empty list. That is a normal, correct
  answer — do not manufacture one to fill the list.
- Never invent a question that sounds plausible for this topic. Every entry must be
  something the text actually says is unresolved.
- `quote` must be copied from the excerpts **verbatim**, one or two sentences, with no
  paraphrasing or ellipsis. It is checked against the source text and the entry is
  discarded if it does not match.
- Skip vague gestures ("we plan to explore applications", "many questions remain") that a
  reader could not act on. Keep the ones with a definite mathematical statement.
- At most 4 questions; if there are more, keep the ones stated most definitely.

Write in English and wrap every piece of mathematics in `$...$` delimiters.

For each question:
- question: state it precisely and self-containedly, so someone who has not read the paper
  understands what is being asked. Name the object, the setting and the quantity — not
  "improve the bound" but "close the gap between the $O(n\\log n)$ upper bound and the
  $\\Omega(n)$ lower bound for $X$ on bounded-degree graphs".
- quote: the sentence from the excerpts that states it. Verbatim.
- context: what the paper does establish, and what exactly it leaves out — the reader needs
  to know where the frontier is before deciding to work on it.
- shape: pick the value that describes what solving it means.
- emphasis: "highlighted" if the paper presents it as a main open problem (its own section,
  or called out as the natural next step); "in passing" if it is a remark.
- background: what a person would need to know or have read to start on it, in one
  sentence. Be concrete about techniques, not "a strong background in TCS".
- area: exactly one of: {areas}."""


_WS = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def verify_quote(quote: str, source: str, min_overlap: float = 0.6) -> bool:
    """这句话到底在不在原文里。

    模型被要求逐字抄，但它偶尔会顺手规范化空白、把公式换个写法、或者把两句话接起来。
    所以先看是不是子串，不是的话退一步看 5 元词组的重合度——既挡得住整句编造，
    又不会因为一个空格把真引用误杀。
    """
    q, s = _norm(quote), _norm(source)
    if len(q) < 20:
        return False
    if q in s:
        return True
    words = q.split()
    if len(words) < 5:
        return False
    grams = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
    hit = sum(1 for g in grams if g in s)
    return hit / len(grams) >= min_overlap


def balance_math(text: str) -> str:
    """把 `$` 定界符配平。

    抽出来的引文偶尔会在公式中间被截断（模型只抄了半句），留下一个落单的 `$`；
    LaTeXML 的 alttext 紧挨着已有的 `$` 时也会拼出 `$$`。KaTeX 遇到不配对的定界符
    会把后面一整段都当成数学吃掉，一句话就整个花掉——所以宁可少一个定界符，
    让那截公式退化成纯文本显示。
    """
    if not text or "$" not in text:
        return text
    text = text.replace("$$", "$")  # 这里的公式全是行内的，不会有真的 display math
    if text.count("$") % 2:
        i = text.rfind("$")
        text = text[:i] + text[i + 1:]
    return text


def extract(g: Gemini, paper: dict, excerpt: str, areas: dict) -> list[dict]:
    """返回这篇论文里作者留下的公开问题；抽不到或调用失败返回 []。"""
    prompt = QUESTION_PROMPT.format(
        title=paper.get("title", ""),
        authors=", ".join((paper.get("authors") or [])[:8]) or "(none listed)",
        cats=", ".join(paper.get("cats") or []) or "(none)",
        abstract=(paper.get("abstract") or "(no abstract)")[:2000],
        excerpt=excerpt,
        areas=", ".join(sorted(areas)),
    )
    out = g.json(prompt, QUESTION_SCHEMA, temperature=0.2, max_tokens=4096)
    if not out:
        return []

    from .digest import normalize_math, repair_escapes  # 循环引用，用到时再引

    kept: list[dict] = []
    for q in out.get("questions") or []:
        if not q.get("question") or not q.get("quote"):
            continue
        if not verify_quote(q["quote"], excerpt):
            # 引不出原句的，就是模型自己想的——整条丢掉，不留痕迹地降级
            log.info("引文对不上原文，丢弃：%s", q["question"][:70])
            continue
        if q.get("area") not in areas:
            q["area"] = "algorithms"
        if q.get("shape") not in SHAPES:
            q["shape"] = "new direction"
        for k in ("question", "context", "background", "quote"):
            if isinstance(q.get(k), str):
                q[k] = balance_math(normalize_math(repair_escapes(q[k])))
        kept.append(q)
    return kept[:4]
