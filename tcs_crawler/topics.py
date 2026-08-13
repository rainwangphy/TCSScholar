"""基于标题关键词的 TCS 主题分类。

说明：这是**初步**的启发式分类，不是语义模型。规则是按 TCS 常见子领域手工整理的
正则表达式，一篇论文可以命中多个主题（multi-label），也可能一个都不命中
（记为"其他/未分类"）。用于看趋势和分布足够，不适合当作严格的领域标注。
"""

from __future__ import annotations

import re

# (id, 中文名, English, 正则)
# 顺序无关（多标签），但更具体的领域尽量用更精确的词，避免被宽泛词吃掉
TOPIC_RULES: list[tuple[str, str, str, str]] = [
    ("graph", "图算法", "Graph Algorithms",
     r"\bgraph|\bplanar\b|spanning tree|shortest path|\bmax(imum)?[- ]flow|\bmin(imum)?[- ]cut|"
     r"\bmatching\b|connectivity|\bminor(s)?\b|treewidth|\bcut(s)? in|vertex cover|independent set|"
     r"\bcolou?ring\b|\bclique|hamiltonian|\bst-connect|expander decomposition|\bdigraph|"
     r"\bnetwork(s)?\b|\bsteiner\b|\btsp\b|traveling salesman|\bflow(s)? in|\btree(s)?\b|"
     r"\bspanner(s)?\b|\brouting\b|\bshortest[- ]path|\bdominating set|\bgirth\b|\bsubgraph"),

    ("approx", "近似算法", "Approximation",
     r"approximat|\bptas\b|\bfptas\b|inapproxim|approximability|\bhardness of approx|"
     r"integrality gap|\blp relaxation|rounding"),

    ("complexity", "复杂性理论", "Complexity Theory",
     r"complexity class|\bnp-(complete|hard)|\bpspace|\bp vs np|\bpcp\b|probabilistically checkable|"
     r"circuit lower bound|circuit complexity|\bproof complexity|time hierarchy|space hierarchy|"
     r"derandomiz|\bbpp\b|\bcomplexity of\b|\bcomputational complexity|natural proofs|"
     r"\bsat\b|satisfiability|\bcsp\b|constraint satisfaction|dichotomy|\bunique games|"
     r"\bboolean function|\bdecision tree complexity|query complexity|\bformula size"),

    ("crypto", "密码学", "Cryptography",
     r"cryptograph|encrypt|\bzero[- ]knowledge|secure (multi[- ]?party )?computation|obfuscat|"
     r"\bsignature(s)?\b|commitment scheme|\bsecret sharing|homomorphic|\boblivious transfer|"
     r"pseudorandom function|\bone[- ]way function|\bprotocol(s)? for|\bauthenticat|"
     r"\blattice[- ]based|\blwe\b|\bmpc\b|\bprivacy amplification|\bsnark|verifiable"),

    ("quantum", "量子计算", "Quantum Computing",
     r"\bquantum|\bqubit|entangle|\bqma\b|\bqip\b"),

    ("game", "博弈论与机制设计", "Game Theory & Mechanism Design",
     r"\bauction|mechanism design|\bequilibri|\bnash\b|\bgame(s)?\b|incentive|price of anarchy|"
     r"\bbidder|\bbidding|\brevenue\b|\bstrategyproof|strategy[- ]proof|truthful|\bpricing\b|"
     r"\bmarket(s)?\b|\bmatching market|\bvoting\b|social choice|\bcontract(s)?\b|"
     r"\bfair (division|allocation)|envy[- ]free|\bcake cutting|\bstable matching|\bcombinatorial auction"),

    ("learning", "学习理论", "Learning Theory",
     r"\blearning\b|\blearnab|\bpac\b|\bregret\b|\bbandit|\bboosting\b|sample complexity|"
     r"neural network|\bdeep learning|\bclassifier|\bclustering\b|\bk-means|mixture model|"
     r"\bregression\b|\bestimation of|distribution learning|\btesting distributions"),

    ("ds", "数据结构", "Data Structures",
     r"data structure|\bdictionar|\bhash(ing|ed)?\b|priority queue|\bsuccinct|\boracle(s)?\b|"
     r"\bindex(ing)?\b|\bself[- ]adjust|\bunion[- ]find|\bpredecessor\b|\bheap(s)?\b|"
     r"\bfully dynamic|\bdynamic (graph|maintain|algorithm)"),

    ("online", "在线算法", "Online Algorithms",
     r"\bonline\b|competitive (ratio|analysis)|\bk-server|\bcaching\b|\bpaging\b|\bprophet\b|"
     r"\bsecretary problem|\bmetrical task"),

    ("random", "随机化与伪随机", "Randomness & Pseudorandomness",
     r"\brandom(ized|ness|ly)?\b|pseudorandom|\bexpander|\bextractor|\bsampling\b|markov chain|"
     r"\bmixing time|\bmonte carlo|\bderandom|\bprobabilistic method|\brandom walk|\bmcmc\b"),

    ("sublinear", "流算法与亚线性", "Streaming & Sublinear",
     r"\bstreaming\b|\bsketch(ing|es)?\b|sublinear|property testing|\bproperty tester|"
     r"sparse recovery|compressed sensing|\bdata stream|\bsample-based"),

    ("distributed", "分布式与并行", "Distributed & Parallel",
     r"\bdistributed\b|\bparallel\b|\bpram\b|\blocal model|\bcongest\b|self[- ]stabiliz|"
     r"\bconsensus\b|fault[- ]toleran|\bbyzantine|\bsynchronous|\basynchronous|\bmapreduce|"
     r"\bmassively parallel|\bshared memory|\bblockchain|\bledger"),

    ("geometry", "计算几何", "Computational Geometry",
     r"\bgeometr|convex hull|\bvoronoi|\btriangulat|nearest neighbor|\bpoint set|\barrangement(s)?\b|"
     r"\bpolygon|\bembedding(s)? into|\bmetric space|\bdoubling dimension|\bdimension reduction"),

    ("coding", "编码与信息论", "Coding & Information Theory",
     r"\bcode(s)?\b|\bcoding\b|\bdecod|list[- ]decod|error[- ]correct|\bentropy\b|"
     r"information theoretic|communication complexity|\binformation complexity"),

    ("opt", "优化与线性规划", "Optimization & LP/SDP",
     r"linear program|semidefinite|\bsdp\b|convex optimization|\bgradient|submodular|\bmatroid|"
     r"integer program|\bsimplex method|interior point|\bconvex body|\bpolytope|\bfirst[- ]order method|"
     r"\blocal search|\bheuristic(s)?\b|\bsimulated annealing"),

    ("privacy", "差分隐私", "Differential Privacy",
     r"differential(ly)? privat|\bprivacy\b|\bprivate (data|learning|algorithm|query|estimation)"),

    ("finegrained", "细粒度复杂性", "Fine-Grained Complexity",
     r"fine[- ]grained|\bseth\b|\b3sum\b|conditional lower bound|orthogonal vectors|"
     r"\bapsp\b|\ball[- ]pairs shortest"),

    ("algebraic", "代数与计数", "Algebraic & Counting",
     r"matrix multiplication|polynomial identity|\balgebraic\b|\bcounting\b|\bpermanent\b|"
     r"\b#p\b|\btensor\b|\barithmetic circuit|\bdeterminant|\bpartition function|\bholant|"
     r"\bmatri(x|ces)\b|\beigenvalue|\bspectral\b|\bpolynomial(s)?\b(?!\s+time)|\bfourier\b"),

    ("strings", "字符串算法", "String Algorithms",
     r"\bstring(s)?\b|pattern matching|edit distance|\bsuffix (tree|array)|\btext index|"
     r"longest common subsequence|\blcs\b|\bcompression\b"),

    ("scheduling", "调度与装箱", "Scheduling & Packing",
     r"\bscheduling\b|bin packing|load balanc|\bjob(s)?\b|\bmakespan|\bknapsack|\bbin[- ]covering|"
     r"\bpacking\b|\bcovering\b|\ballocation of|\bresource allocation"),

    ("logic", "逻辑与形式语言", "Logic, Automata & Formal Languages",
     r"\bautomat(a|on|ic)\b|\blogic(s)?\b|model checking|\btemporal\b|\bverification\b|"
     r"\bfirst[- ]order logic|\bdefinability|\bdescriptive complexity|\bgrammar(s)?\b|"
     r"\b(formal |regular |context[- ](free|sensitive) )?language(s)?\b|\bturing machine|"
     r"\bcomputab(le|ility)|\bdecidab|\brecursive(ly)? (function|enumerable|defined)|"
     r"\brewriting\b|\bsemantics\b|\bprogram(ming)? language|\btype system|\blambda calculus"),

    ("circuits", "电路与布尔函数", "Circuits & Boolean Functions",
     r"\bcircuit(s|ry)?\b|\bboolean\b|\bthreshold (function|logic|organ|gate)|\bswitching (theory|"
     r"function|network|circuit)|\blogic design|\bgate(s)?\b|\badder\b|\bfault detection|"
     r"\bsequential machine|\bfinite[- ]state|\bbranching program|\bformula(s|e)? for"),

    ("sorting", "排序与选择", "Sorting & Searching",
     r"\bsort(ing|ed)?\b|\bselection\b|\bmedian\b|\bsearching\b|\bpermutation(s)?\b|\bmerging\b|"
     r"\bcomparison(s)? (model|based)|\bbinary search"),

    ("ecommerce", "电子商务与网络经济", "E-Commerce & Internet Economics",
     r"e-?commerce|e-?business|\brecommender|\breputation\b|\badvertis|information goods|"
     r"\bconsumer(s)?\b|\bshopping\b|\bcatalog|\bweb (site|server|search)|\binternet\b|"
     r"\bonline (market|store|retail)|\bsupply chain|\bbusiness model|\bcrowdsourc|\bsponsored search"),
]

_COMPILED = [(tid, zh, en, re.compile(pat, re.IGNORECASE)) for tid, zh, en, pat in TOPIC_RULES]

TOPIC_IDS = [t[0] for t in TOPIC_RULES]
TOPIC_ZH = {t[0]: t[1] for t in TOPIC_RULES}
TOPIC_EN = {t[0]: t[2] for t in TOPIC_RULES}

OTHER = "other"
TOPIC_ZH[OTHER] = "其他/未分类"
TOPIC_EN[OTHER] = "Other / Unclassified"


def classify(title: str) -> list[str]:
    """返回命中的主题 id 列表；一个都没命中时返回 ['other']。"""
    hits = [tid for tid, _zh, _en, rx in _COMPILED if rx.search(title)]
    return hits or [OTHER]
