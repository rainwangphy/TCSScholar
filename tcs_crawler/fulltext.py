"""取 arXiv 论文的全文，抽出「作者自己说还没解决」的那几段。

为什么非要全文：实测 111 篇里只有 **5%** 的摘要提到公开问题，而且那 5 篇全都是
「我们解决了别人留下的公开问题」，不是留新问题；同一批论文的**全文**里有 **71%**
出现了公开问题的措辞，好些还专门开了一节（"Open Questions" / "Concluding Remarks" /
"Conclusion and Future Work"）。作者是在结论里留问题的，不是在摘要里。

两个源，按顺序退：

  1. arxiv.org/html/<id><v>  — arXiv 自己从 LaTeX 生成的 HTML，2023 年底以后的投稿基本都有
  2. ar5iv.labs.arxiv.org/html/<id>  — 老论文的兜底，覆盖到更早

两个都拿不到就跳过这篇（扫描件、只有 PDF 的投稿）。抓取复用 tcs_crawler/http.py 的
限速缓存客户端；实测这两个站点比 export.arxiv.org 宽松得多，但仍然按老规矩慢慢来。
"""

from __future__ import annotations

import html
import logging
import re

from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

HTML_URL = "https://arxiv.org/html/{id}{ver}"
AR5IV_URL = "https://ar5iv.labs.arxiv.org/html/{id}"

# 作者留问题时的说法。比 ranking.py 里那套「宣称解决了」的措辞宽——这里宁可多捞，
# 后面还有模型和「必须引出原句」两道关。
QUESTION_RX = re.compile(
    r"\bopen (problem|question|direction|issue)s?\b"
    r"|\bremains?\s+(an?\s+)?open\b|\bstill open\b"
    r"|\b(we|it)\s+leaves?\b[^.]{0,50}\bopen\b"
    r"|\bleft\s+(as\s+)?(an?\s+)?open\b"
    r"|\bwe (conjecture|do not know|don't know)\b"
    r"|\bit (is|remains) (not known|unknown|unclear|an interesting question)\b"
    r"|\b(natural|intriguing|interesting|obvious|main|first) (next |further )?"
    r"(open )?(question|problem|direction|step)\b"
    r"|\bwould be (very )?interesting to\b"
    r"|\bfuture (work|research|direction)s?\b"
    r"|\bwe (do not|fail to|are unable to) (resolve|settle|determine)\b",
    re.IGNORECASE,
)

# 结论/讨论/展望这类小节的标题。作者留问题几乎都在这里面。
SECTION_RX = re.compile(
    r"\b(conclusion|concluding|discussion|open (problem|question)|future (work|direction)"
    r"|further (work|direction|research)|outlook|summary and)\b",
    re.IGNORECASE,
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# LaTeXML 把公式的 LaTeX 原文放在 **alttext** 属性里（不是 alt——写错过一次，
# 结果整个 <math> 落到下面的去标签那一步，MathML 的字形和 <annotation> 里的
# LaTeX 一起漏进正文，引文变成「Ω ⁡ $( 1 / n ) \rm\Omega(1/n)$」这种样子）。
# 留 LaTeX 原文比留字形有用：模型看得懂 $O(n\log n)$，看不懂一堆 <mi><mo>。
_MATH = re.compile(r'<math[^>]*?\balttext="([^"]*)"[^>]*>.*?</math>', re.S)
_DROP = re.compile(r"<(script|style|nav|footer)[^>]*>.*?</\1>", re.S | re.I)
# 引用标记去掉。留着的话引出来的原句里全是「[ BATS07 , GE08 , Har07 ]」，
# 读起来碎，也白占 token；去掉不影响引文核对——比对用的是同一份文本。
_CITE = re.compile(r'<cite[^>]*>.*?</cite>', re.S | re.I)
_HEAD = re.compile(r'<h([1-6])[^>]*class="ltx_title[^"]*"[^>]*>(.*?)</h\1>', re.S)


def fetch(client: HttpClient, arxiv_id: str, version: str = "v1",
          force: bool = False) -> str | None:
    """取一篇论文的 HTML 全文；两个源都拿不到返回 None。"""
    for url in (HTML_URL.format(id=arxiv_id, ver=version or "v1"),
                AR5IV_URL.format(id=arxiv_id)):
        try:
            return client.get(url, force=force)
        except HttpError as e:
            log.debug("拿不到 %s：%s", url, e)
    log.info("没有 HTML 全文，跳过 %s（多半是只交了 PDF）", arxiv_id)
    return None


def _text(fragment: str) -> str:
    s = _DROP.sub(" ", fragment)
    s = _CITE.sub(" ", s)
    s = _MATH.sub(lambda m: " $" + html.unescape(m.group(1)).strip() + "$ ", s)
    s = _TAG.sub(" ", s)
    return _WS.sub(" ", html.unescape(s)).strip()


def sections(body: str) -> list[tuple[str, str]]:
    """按小节标题把正文切开，返回 [(标题, 正文), ...]。

    切不开（没有 LaTeXML 的标题类名）时退化成一整块，让调用方自己去段落里找。
    """
    marks = [(m.start(), m.end(), _text(m.group(2))) for m in _HEAD.finditer(body)]
    if not marks:
        return [("", _text(body))]
    out = []
    for i, (start, end, title) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        out.append((title, _text(body[end:stop])))
    return out


def excerpts(body: str, cap: int = 7000) -> tuple[str, list[str]]:
    """抽出可能含公开问题的片段，拼成一段给模型看的文本。

    两条路一起走，因为两种写法都常见：
      - 整节：标题像 Conclusion / Open Questions / Future Work 的，整节都要；
      - 单句：正文任何地方出现「open problem」这类措辞的，连同前后文各取一段。

    返回 (拼好的文本, 命中的小节标题)。截到 cap 是为了不让一次调用吞掉整篇论文——
    结论一般在末尾，所以命中的整节优先，零散句子排后面。
    """
    secs = sections(body)
    named, loose = [], []
    for title, text in secs:
        if not text:
            continue
        if title and SECTION_RX.search(title):
            named.append((title, text))
        elif QUESTION_RX.search(text):
            for para in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
                if QUESTION_RX.search(para):
                    loose.append((title, para))

    hit_titles = [t for t, _ in named]
    chunks: list[str] = []
    size = 0
    for title, text in named:
        piece = f"## {title}\n{text}"
        chunks.append(piece[: max(0, cap - size)])
        size += len(piece)
        if size >= cap:
            break
    for title, para in loose:
        if size >= cap:
            break
        piece = f"## {title or 'body'}\n{para}"
        chunks.append(piece[: max(0, cap - size)])
        size += len(piece)
    return "\n\n".join(c for c in chunks if c.strip()), hit_titles
