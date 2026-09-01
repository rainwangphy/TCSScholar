"""用 Gemini 分析当天精选论文，并写一段当日综述。输出是英文，和网站其余部分一致。

只喂标题、作者、分类和摘要——模型看不到正文，所以提示词里反复要求它**只根据
摘要说话**，说不清的地方写 "not stated in the abstract"，而不是顺着标题编一个。
页面上也会标明这些分析出自模型、依据只有摘要。
"""

from __future__ import annotations

import logging
import re

from .arxiv import CAT_NAMES, ArxivPaper
from .gemini import Gemini
from .topics import TOPIC_EN

log = logging.getLogger(__name__)

PAPER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "result": {"type": "STRING"},
        "method": {"type": "STRING"},
        "significance": {"type": "STRING"},
        "novelty": {
            "type": "STRING",
            "enum": ["Breakthrough", "Significant improvement", "Incremental",
                     "Applied", "Hard to tell"],
        },
        "rating": {"type": "INTEGER"},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["result", "method", "significance", "novelty", "rating", "keywords"],
    "propertyOrdering": ["result", "method", "significance", "novelty", "rating", "keywords"],
}

PAPER_PROMPT = """You are a theoretical computer science researcher writing a daily
briefing on new arXiv preprints for your colleagues.

Below is a paper uploaded to arXiv today. Answer **from the abstract alone** — do not
bring in anything the abstract does not state. Where the abstract does not say, write
"Not stated in the abstract" rather than guessing from the title.

Title: {title}
Authors: {authors}
arXiv categories: {cats}
Abstract:
{abstract}

Write in English, and wrap **every** piece of mathematics in `$...$` delimiters (write
`$O(n\\log n)$`, never a bare `O(n\\log n)`) — the page relies on those delimiters to
typeset it.

Fill in:
- result: the paper's main result, in one or two sentences. Be concrete about bounds,
  conditions and model (e.g. "improves the approximation ratio for X from 2 to 1.5 on
  dense graphs"), never a vague "studies the problem of X".
- method: the key technical device that gets the result, in one sentence. If the abstract
  does not say, write "Not stated in the abstract".
- significance: why a colleague should look, in one or two sentences. Say what long-standing
  question it settles, how it relates to known results, or where it is limited. Do not hype it.
- novelty: pick one of the given values; choose "Hard to tell" when you cannot judge.
- rating: an integer 1-5 for how urgently a TCS colleague should read it, 5 being the
  must-read of the day. Be stingy: most papers are a 3, and only a genuine breakthrough or
  a resolved open problem earns a 5.
- keywords: 3 to 5 keywords, using the field's standard English terminology."""

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

DIGEST_PROMPT = """You are a theoretical computer science researcher writing the opening
of a daily arXiv briefing for {date}.

That day, arXiv's cs.CC / cs.DS / cs.DM / cs.GT / cs.CG saw {total} new submissions.
By keyword rules, the topic breakdown is:
{topic_lines}

Here are the {n} that the ranking rules put on top (titles and primary category only):
{titles}

Write in English, wrapping any mathematics in `$...$`:
- headline: one sentence, at most 20 words, capturing the shape of the day. No filler like
  "an exciting day" — name the actual topics.
- summary: two or three sentences on where the day's papers cluster and whether anything
  stands out. Base it only on the titles and counts above; if no direction is clearly
  dominant, say so plainly.
- themes: 2 to 4 themes that stood out, each with a name and a one-sentence note that
  points at the specific papers carrying it."""


# ── 反斜杠转义修复 ───────────────────────────────────────────────────
# 模型返回的 JSON 里偶尔会漏写一个反斜杠，而 \t \n \r \f \b 恰好都是**合法**的
# JSON 转义，于是 json.loads 会把 "\tilde" 静默解析成 制表符 + "ilde"，宏名就没了，
# 解析器一声不吭。只能在解析之后照着控制字符把它补回来。
_CTRL_LETTER = {"\t": "t", "\f": "f", "\b": "b"}  # 这些在单行散文里没有正当用途
# \n 和 \r 有可能真的是换行，所以只在后面紧跟着已知宏名时才还原
_NL_MACROS = {
    "n": ["newcommand", "nsubseteq", "nsupseteq", "nonumber", "nrightarrow", "notin",
          "nabla", "ncong", "nleq", "ngeq", "nmid", "neq", "not", "nu", "ne"],
    "r": ["rightleftharpoons", "rightarrow", "rangle", "rfloor", "rceil", "right",
          "rho", "rm"],
}
_NL_MACROS = {k: sorted(v, key=len, reverse=True) for k, v in _NL_MACROS.items()}


def repair_escapes(text: str) -> str:
    """把被 JSON 转义吃掉的 LaTeX 宏名还原成反斜杠形式。"""
    if not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c in _CTRL_LETTER and nxt.isascii() and nxt.isalpha():
            out.append("\\" + _CTRL_LETTER[c])
            i += 1
            continue
        if c in ("\n", "\r"):
            letter = "n" if c == "\n" else "r"
            rest = text[i + 1:]
            for macro in _NL_MACROS[letter]:
                tail = macro[1:]
                # 还要求宏名后面不是字母，否则真换行接一个小写词（"…done.\never…"）
                # 会被当成 \ne 还原
                after = rest[len(tail):len(tail) + 1]
                if rest.startswith(tail) and not (after.isascii() and after.isalpha()):
                    out.append("\\" + macro)
                    i += len(macro)  # 1 个控制字符 + len(macro)-1 个残留字母
                    break
            else:
                out.append(c)
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


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
    return ", ".join(f"{c} ({CAT_NAMES[c]})" if c in CAT_NAMES else c for c in p.cats)


def analyze_paper(g: Gemini, p: ArxivPaper) -> dict:
    """返回分析结果；调用失败时返回 {} —— 页面上这篇就只显示规则层的信息。"""
    prompt = PAPER_PROMPT.format(
        title=p.title,
        authors=", ".join(p.authors) or "(none listed)",
        cats=_cats(p),
        abstract=p.abstract or "(no abstract provided by arXiv)",
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
            out[k] = normalize_math(repair_escapes(out[k]))
    out["model"] = g.model
    return out


def daily_digest(g: Gemini, day: str, papers: list[ArxivPaper], picked: list[ArxivPaper],
                 topic_counts: dict[str, int]) -> dict:
    topic_lines = "\n".join(
        f"  - {TOPIC_EN.get(t, t)}: {c}"
        for t, c in sorted(topic_counts.items(), key=lambda kv: -kv[1])[:10]
    ) or "  (none)"
    titles = "\n".join(f"  {i}. [{p.primary_cat}] {p.title}" for i, p in enumerate(picked, 1))
    prompt = DIGEST_PROMPT.format(
        date=day, total=len(papers), topic_lines=topic_lines, n=len(picked), titles=titles
    )
    out = g.json(prompt, DIGEST_SCHEMA, temperature=0.4, max_tokens=4096)
    if not out:
        return {}
    for k in ("headline", "summary"):
        if isinstance(out.get(k), str):
            out[k] = normalize_math(repair_escapes(out[k]))
    for t in out.get("themes") or []:
        if isinstance(t.get("note"), str):
            t["note"] = normalize_math(repair_escapes(t["note"]))
    out["model"] = g.model
    return out
