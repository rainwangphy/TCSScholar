# TCSScholar

抓取理论计算机科学（TCS）顶会论文元数据的爬虫，覆盖 **FOCS / STOC / SODA / ITCS / EC**。

数据源是 [DBLP](https://dblp.org)：官方检索 API，元数据规范、覆盖完整（FOCS 可回溯到 1960 年），
比逐个爬会议官网稳定得多。抓取的是**书目元数据**（标题、作者、年份、页码、DOI、链接），不下载论文全文。

## 快速开始

零第三方依赖，Python 3.9+ 直接跑：

```bash
# 抓全部 5 个会议的全部年份（约 210 届，受 DBLP 限速影响通常需要 20-60 分钟）
python crawl.py

# 只抓最近几年的 STOC 和 FOCS
python crawl.py -v stoc focs --since 2020

# 输出 CSV + SQLite 到 data/ 目录
python crawl.py --formats csv sqlite -o data

# 每个会议再单独出一份文件
python crawl.py --per-venue
```

首次运行建议带上自己的邮箱作为 User-Agent（DBLP 的推荐做法，被限流时更不容易被封）：

```bash
python crawl.py --user-agent "TCSScholar/0.1 (mailto:you@example.com)"
```

## 支持的会议

| slug | 会议 | DBLP 目录 | 起始年份 |
|------|------|-----------|----------|
| `focs` | IEEE Symposium on Foundations of Computer Science | `conf/focs` | 1960（1960–1974 名为 SWAT）|
| `stoc` | ACM Symposium on Theory of Computing | `conf/stoc` | 1969 |
| `soda` | ACM-SIAM Symposium on Discrete Algorithms | `conf/soda` | 1990 |
| `itcs` | Innovations in Theoretical Computer Science | `conf/innovations` | 2010（2010–2011 名为 ICS）|
| `ec` | ACM Conference on Economics and Computation | `conf/sigecom` | 1999（2014 年前名为 EC on Electronic Commerce）|

`python crawl.py --list-venues` 可以随时查看。

## 在线版

分析结果已经发布成一个静态页面：**https://rainwangphy.github.io/TCSScholar/**

页面分四个 tab：**Daily digest**（每日 arXiv 速读）、**Topic analysis**、**Browse papers**、
**Open problems**（未解问题清单），URL 里带 `#daily` / `#analysis` / `#browse` / `#open`
可以直接进某个 tab，`#2026-08-31` 这样的日期直接进那天的速读，`#op-p-vs-np` 直接定位到
某个未解问题。详见下面的 [每日 arXiv 速读](#每日-arxiv-速读)和
[未解问题清单](#未解问题清单)。

配色是牛皮纸风格（bone 底 + 黏土色强调），跟随系统深浅色；页面文案和模型输出都是英文。

本地重新生成：

```bash
python analyze.py                                        # 读 data/ 里的抓取结果，写出 data/site_data.json
python fetch_abstracts.py --mailto you@example.com       # 可选：从 OpenAlex 补摘要（约 20-40 分钟）
python build_site.py                                     # 产出 site/index.html 和 site/abstracts/
```

`site/daily/` 和 `site/open/` 这两份数据不进 `index.html`，页面在运行时去 fetch，所以改完
它们不用重新构建页面。

提交后 GitHub Actions（[.github/workflows/pages.yml](.github/workflows/pages.yml)）会自动部署到 Pages。

### 摘要

DBLP 不提供摘要，所以单独跑 [fetch_abstracts.py](fetch_abstracts.py) 从
[OpenAlex](https://openalex.org)（免费、无需 API key）补，产出 `data/abstracts.jsonl`。
两条匹配路径：有 DOI 的按 DOI 批量查（一次 50 个），没有的按标题搜索，但**只有标题归一化后
完全相等、年份相差不超过 1 年才采信**——宁可漏也不能张冠李戴。

**注意 OpenAlex 的配额是按信用点算的，不是按请求数**：日限 1000 点，`filter=` 查询 1 点，
但 `title.search` 一次要 **10 点**。所以 DOI 批量路径 299 次请求只花约 300 点就跑完了
（14141/14932 命中，94.7%），而标题路径 2818 篇需要 28180 点——免费额度下要 28 天。
配额耗尽时返回 429 且 `Retry-After` 长达 10 小时，看起来像限速，其实是当天额度已经用完。

因此目前线上覆盖率是 **14141/16959（83.4%）**，缺口几乎全在 DBLP 里没有 DOI 的论文上：
FOCS 96.5%、EC 96.2%、STOC 92.9%，但 SODA 只有 59.2%（它有 40% 的论文在 DBLP 里没 DOI）。

`--title-delay` 只能调请求间隔，救不了配额。真要补齐这批，得换成申请 Semantic Scholar 的
免费 API key（按请求限速，不按信用点），或者把标题路径分几天跑。脚本每 200 篇存一次盘，
DOI 路径结束也存一次，中途停掉不会丢已有成果；`.cache/` 里的响应可以直接重放，不用重抓。

摘要总量约 19MB，全部内联会让页面涨到 20MB 以上，所以 `build_site.py` 把它们按
`compact.papers` 的下标每 200 篇切成一片放进 `site/abstracts/`，页面上点开某篇时才去取
它所在的那一片（约 230KB），取过的缓存住。**分片的顺序必须和嵌进页面的 `papers` 数组
严格一致**，因为下标就是定位方式；两者由 `build_site.py` 同一次构建产出，不会走偏。

由此带来两点：`site/index.html` 本身仍是自包含的，分析部分离线可用，但**摘要需要联网**
（双击本地文件打开时，浏览器的 CORS 策略会挡掉分片请求）；检索框只搜标题和作者，
不搜摘要正文——那需要先把 19MB 全下下来。

`data/` 不进版本库，所以 CI 无法重新生成页面：线上跑的永远是你本地构建后提交的
`site/index.html` 和 `site/abstracts/`。数据更新后要重新提交这两者。

## 每日 arXiv 速读

除了会议论文库，还有一条独立的每日流水线：抓 arXiv 上当天新上传的 TCS 预印本，排序后让
模型写英文速读，发布在首页的 **Daily digest** tab
（**https://rainwangphy.github.io/TCSScholar/#daily**）。

```bash
python daily.py                      # 抓昨天（UTC），分析并写进 site/daily/
python daily.py --date 2026-08-27    # 指定某天
python daily.py --days 7             # 回填最近 7 天（已有的会跳过，加 --force 覆盖）
python daily.py --no-llm             # 只跑规则层，一次模型都不调
python daily.py --top 25             # 多分析几篇，默认 15
```

覆盖 `cs.CC`、`cs.DS`、`cs.DM`、`cs.GT`、`cs.CG` 五个分类，按 `submittedDate` 取当天**首次
提交**（v1）的论文，所以拿到的是真正的新论文，不含旧论文的修订版。同一篇被多个分类命中时
只算一次。一天大约四十篇。

### 三层流水线

每一层都能单独降级，上一层的产物不依赖下一层跑成功：

1. **抓取** — arXiv 官方 Atom API，复用 `tcs_crawler/http.py` 的限速客户端。
2. **规则** — 用 `topics.py` 的关键词规则对**标题加摘要**打主题标签（比只看标题漏得少），
   再算一个可解释的分数排序。
3. **模型** — 只把排在前面的 15 篇送给 Gemini，出 result / method / significance /
   novelty / 1-5 星；另出一段当日综述。约 16 次调用，一天几万 token。

没有 API key 或调用失败时第 3 层整层跳过，页面照常显示前两层的结果，只是那几篇没有速读。

### 打分是怎么算的

四项相加，每一项的得分和理由都会写进 JSON 的 `score_parts`，页面上原样展开显示：

| 项 | 上限 | 说明 |
|---|---|---|
| 主题 | 2.0 | 命中的核心主题数 |
| 核心分类 | 1.0 + 1.0 | 主分类在五个核心分类里，以及跨了几个 |
| 作者 | 3.0 | 作者在五大会议的历史发文量，取 log 压缩 |
| 结果强度 | 4.0 | 摘要里「解决公开问题」「首个」「最优」「改进」这类措辞 |
| 已被接收 | 1.5 | `journal_ref` 或 comment 里写了 accepted / to appear |
| 非新结果 | −3.0 | 标题是 survey / erratum / note 一类 |

作者那一项按**姓名**匹配 `tcs_crawler/prolific_authors.json`（`analyze.py` 顺带产出，收录在
五大会议发过 3 篇以上的 3492 位作者）——arXiv 那边没有 DBLP 的作者 id，对不上，所以重名会被
合并，这也是它权重压在 3 分以内的原因。

**这个分数衡量的是「值得优先看一眼」，不是论文质量**，漏掉好论文是常态。页面上写明了这一点。

### 模型分析的边界

输入只有标题、作者、分类和摘要，模型没读过正文。提示词里要求它摘要没交代的地方直接写
「摘要未说明」而不是顺着标题编，但仍可能误读、把作者的宣称当既定事实、或者把公式抄错。
星级是模型的主观判断。页面页脚把这些都讲清楚了。

输出走 Gemini 的 `responseSchema` 结构化约束，比让它写自由文本再正则抠字段稳得多。key 的读取
顺序是环境变量 `GEMINI_API_KEY` → `api_keys/gemini_api.txt`（后者在 `.gitignore` 里）。

### 公式那两个坑

模型输出里的 LaTeX 有两个静默失效的地方，`digest.py` 各修一层：

1. **转义被吃掉**。模型偶尔漏写一个反斜杠，而 `\t` `\n` `\r` `\f` `\b` 恰好都是**合法**的
   JSON 转义，于是 `json.loads` 把 `"\tilde"` 无声地解析成 制表符 + `ilde`，宏名就没了，
   解析器一句警告都没有。`repair_escapes()` 照着控制字符补回来：制表符/换页/退格在单行散文里
   没有正当用途，见到就还原；`\n` `\r` 有可能是真换行，只在后面紧跟着已知宏名且不粘着别的
   字母时才动。
2. **少了定界符**。页面靠 `$...$` 才知道哪段要按数学排版，模型约 2% 的字段会写裸 LaTeX。
   提示词里明确要求加，`normalize_math()` 再兜一层：从每个 `\command` 出发向两边吃"数学安全"
   的字符（中文和中文标点是天然边界），括号不配平就原样放过——宁可显示 LaTeX 原文，
   也好过让 KaTeX 报错标红。

### 自动化

[.github/workflows/daily.yml](.github/workflows/daily.yml) 每天 06:00 UTC 跑一次，抓昨天的论文、
提交 `site/daily/`，然后把部署那条流水线当可复用 workflow 调起来——Actions 用 `GITHUB_TOKEN`
推的 commit 不会再触发 `on: push`，所以必须显式调用。

要让它真的调模型，得在仓库 Settings → Secrets → Actions 里加一个 `GEMINI_API_KEY`；
没加也不会失败，只是每天都只有规则层的结果。也可以在 Actions 页面手动触发，能指定日期和天数。

和会议论文库那条线不一样的是：**每日这条线 CI 能完整跑完**。它不依赖 gitignore 掉的 `data/`，
结果直接写进随仓库提交的 `site/daily/`，那里既是网页数据源也是持久化存储。日积月累每天约
80KB，真嫌多可以用 `--keep-days N` 只留最近 N 天。

### 为什么每日数据不内联

`site/index.html` 把 2.6MB 的会议分析数据内联在文件里；每日数据如果也内联，就得每天重新
构建再提交一份 2.6MB 的 HTML，而 CI 上没有 `data/`，根本重新生成不了它。

所以 Daily digest 这个 tab 反过来做：**页面本身不含任何每日数据**，第一次点开这个 tab 时才去
`fetch` `daily/index.json` 和某一天的 `daily/YYYY-MM-DD.json`。于是 CI 每天只需要提交一个
80KB 的 JSON，`index.html` 一动不动——这条线因此能完全自动跑。

代价是这个 tab **必须联网**才有内容（双击本地文件打开时，浏览器的同源策略会挡掉 fetch，
tab 里会提示你起个本地服务器）；另外两个 tab 仍然完全离线可用。公式用 CDN 上的 KaTeX 渲染，
CDN 被挡掉时退化成显示 LaTeX 原文，信息不会少。

`site/daily.html` 现在只是个重定向桩，把早先发出去的链接（含 `#YYYY-MM-DD` 深链）转到
`index.html#daily`。

## 未解问题清单

第三条线：维护一份"TCS 里还没解决的问题"的清单，发布在首页的 **Open problems** tab
（**https://rainwangphy.github.io/TCSScholar/#open**）。里面其实是**两类**问题：

- **Still open** — 领域级的大问题（P vs NP、UGC、k-server），手写维护，每条都写清楚
  *目前进展到哪*：最好的上下界、卡在哪个 barrier、哪些特例已经证了。看的是「离解决还有多远」。
- **From papers** — 作者在自己论文结论里留下的小问题，从全文里抽出来的。大问题几十年不动，
  拿来挑题没用；这一类才是给想找个题做的人看的。见
  [论文里的小问题](#论文里的小问题给挑题的人)。

```bash
python open_problems.py                # 扫每日归档，判新候选，写 site/open/index.json
python open_problems.py --no-llm       # 只跑规则层，一次模型都不调
python open_problems.py --search       # 额外去 arXiv 按登记表里的 queries 查一轮
python open_problems.py --days 30      # 只扫最近 30 天的归档
python open_problems.py --check-refs   # 核对登记表里的每条参考链接
```

### 清单是手写的，脚本只加线索

[problems/registry.json](problems/registry.json) 是**唯一的事实来源**，手工维护，
`open_problems.py` 只读不写。当前 37 条：31 个未解、6 个已解决。

脚本做的是另一件事——**找线索**：

1. **规则层（不发网络请求）** — `site/daily/` 里已经存着每天全部 arXiv TCS 论文的标题和
   摘要，所以"有没有人解决了某个公开问题"绝大部分时候直接在已有归档里扫就行。匹配条件是
   登记表里的关键词加上"解决了某某猜想"这类措辞（`RESOLVE_RX`），或者两个以上关键词加上
   一句推进现有界的说法。实测 111 篇里筛出 4 篇，噪声可控。
2. **arXiv 检索（`--search`）** — 每日归档只覆盖那五个核心分类，而很多组合和几何的结果挂在
   `math.CO`；这条路径按登记表里的 `queries` 再查一轮补上。
3. **模型层** — 把筛出来的送给 Gemini 判 `resolves` / `major progress` / `related` /
   `unrelated`，连同置信度、一句话结论、**它引的那句摘要原文**和一条"要先核对什么"的提醒。
   判成 `unrelated` 的不进产物：只有关键词匹配这一条信息，摆到页面上除了制造噪声没有别的作用。

关键词是会有假朋友的，这一层的作用就是兜住它们。实测里最典型的一个：图论说的 **subcubic 是
"最大度为 3"**，和"真正次立方时间"毫无关系，于是三篇 subcubic 图论的论文被当成 APSP 的候选
送去判定，全部判回 `unrelated`。模型这关拦住了，但白花三次调用——所以这类词后来直接从
`watch.terms` 里换成了 `truly subcubic`。

页面上这三层是分开显示的：问题自身的证据是页面自己的文字，每条线索则明确标着是哪个模型
判的，并把它依据的那句话原样引出来——因为那部分没有人核对过。

### 脚本永远不会自己说"已解决"

宣称证明了 P≠NP 的预印本每年都有好几篇，绝大多数是错的；模型只看摘要更判不了对错。所以：

- 模型判 `resolves` 且置信度不低时，问题只会被标成 `claimed`，在页面最上面的
  **Review queue** 里列出来，等人去读那篇论文；
- 真正改状态，是人去编辑 `registry.json`，把 `status` 改成 `resolved` 并补一条 `resolution`
  记录（谁、哪年、怎么证的、链接）——`validate()` 会强制要求这条记录带链接，
  没有链接的"已解决"不许提交。

解决掉的问题**不会被删掉**，而是移出未解列表、进到页面下方的 Resolved 区。清单本身的价值
有一半在这里：从提出到解决隔了多少年、最后是被哪套技术拿下的。目前存了六条，包括
Huang 2019 的敏感性猜想、Marcus–Spielman–Srivastava 2013 的 Kadison–Singer，以及
Bubeck–Coester–Rabani 2022 **证否**的随机 k-server 猜想——证否也是解决。

### 怎么增删一条

编辑 [problems/registry.json](problems/registry.json)，然后跑一次 `python open_problems.py --no-llm`——
`validate()` 会先把格式问题全部报出来（缺字段、id 重复、area 不在表里、标了 resolved 却没写
resolution），有错就直接退出，不会写出一个半残的产物。

一条问题长这样：

| 字段 | 说明 |
|------|------|
| `id` | URL 里用，`#op-<id>` 直接定位到它，定了就别改 |
| `title` / `statement` / `why` | 标题、精确陈述（可写 LaTeX，用 `$...$`）、为什么值得关心 |
| `area` | 必须是 `areas` 里已有的键 |
| `posed` | 提出年份，页面上按它排"开放最久" |
| `evidence` | `{year, text}` 列表——**目前进展到哪**，页面上按年份排成一条时间线 |
| `refs` | `{label, url}` 列表；arXiv 的再加 `arxiv` 和 `title`，`--check-refs` 会拿标题去核对 |
| `watch.terms` | 关键词，命中标题或摘要才算候选 |
| `watch.queries` | arXiv 检索式，`--search` 时用 |
| `watch.dismissed` | `{arxiv, note}` 列表：人工读过并否掉的论文，两个字段都必填 |
| `resolution` | 只有 resolved 才写：`{year, by, text, url}`，**url 必填** |

**有人宣称解决了，但你读完觉得不成立**：往 `watch.dismissed` 里加一条
`{"arxiv": "2512.11820", "note": "第 4 节的论证是循环的"}`。不加的话它会永远挂在复核队列里——
判定结果是缓存的，模型每次都会给同样的答案，而"已经有人读过、不成立"这个信息只存在于读它的
那个人脑子里。写进登记表之后它既不再占模型调用，也不再刷屏，但仍会以"reviewed and set aside"
显示在问题卡片上：读者听说"有人证明了这个"的时候，应该看得到它被看过，以及凭什么被放下。

**问题被解决了**：把 `status` 改成 `resolved`，补上 `resolution`。不用删——页面会自动把它
移出未解列表、放进下方的 Resolved 区。

**新问题**：往 `problems` 数组里加一条就行，顺序无所谓，页面自己排。

### 论文里的小问题（给挑题的人）

登记表上那三十几条全是几十年不动的大问题——正因为如此，**它们不适合用来挑题**。所以还有
第二类：作者在自己论文结论里留下的公开问题，数量大、难度低得多、也具体得多。

```bash
python paper_questions.py                  # 抓最近 8 天里没处理过的论文
python paper_questions.py --days 30        # 铺底：把 30 天的归档都过一遍
python paper_questions.py --no-llm         # 只抓全文，报命中率，不调模型
```

**为什么非要抓全文。** 一开始想的是扫摘要，实测直接否掉了这条路：111 篇论文里只有 **5%**
的摘要提到公开问题，而且那 5 篇全都是「我们**解决了**别人留下的公开问题」，不是留新问题。
同一批论文的**全文**里，**71%** 出现了公开问题的措辞，好几篇还专门开了一节
（"Open Questions"、"Concluding Remarks"、"Conclusion and Future Work"）。
**作者是在结论里留问题的，不在摘要里。**

所以走 `arxiv.org/html/<id>`（arXiv 自己从 LaTeX 生成的 HTML，2023 年底以后的投稿基本都有），
拿不到就退 `ar5iv`，两个都没有就跳过（只交了 PDF 的）。实测 6 篇里 5 篇有 arXiv HTML，
剩下 1 篇 ar5iv 兜住了。这两个站点比 `export.arxiv.org` 宽松得多，没触发过限流。

拿到 HTML 之后切成小节，取标题像 Conclusion / Discussion / Open Questions / Future Work 的
整节，加上正文里任何出现公开问题措辞的段落，拼成一段（上限 7000 字）送给模型抽取。

### 抽取的那条铁律：引不出原句就丢掉

模型很容易顺着标题替作者「补」一个听起来很合理的公开问题——那种东西看着像真的，
但**没有人说过**。所以每条抽出来的问题都必须附一句**原文**，`verify_quote()` 再拿这句话
回到原文里比对：先看是不是子串，不是的话退一步看 5 元词组的重合度（≥60%）——
既挡得住整句编造，又不会因为模型顺手规范化了一个空格就把真引用误杀。**对不上的整条丢掉。**

页面上每条都把这句原文引出来，读者可以自己点进论文核对。

每条问题还带三样东西，都是从原文里能落实的，不是让模型拍难度：

| 字段 | 说明 |
|------|------|
| `shape` | 解决它意味着什么：`close a quantitative gap` / `remove an assumption` / `extend to a broader setting` / `new direction`。越靠前越具体，越靠后越开放 |
| `emphasis` | `highlighted`（作者当成主要遗留问题，甚至单开一节）还是 `in passing`（顺带一提） |
| `background` | 上手前需要先懂什么，要求写具体技术，不许写「扎实的 TCS 功底」 |

**没有让模型打难度分。** 只看一段结论就给「这题好不好做」评个 1-5 分，是它给不出可靠答案的
那类问题；上面这三样是文本里能落实的事实，读者据此自己判断更靠谱。

### 两条线的分工

|  | 登记表（Still open） | 论文里的（From papers） |
|--|--|--|
| 来源 | 手写 `problems/registry.json` | 从论文全文里抽 |
| 数量 | 31 条 | 每天十几到二十几条 |
| 追踪解决 | 追，有 Review queue 和 Resolved 区 | **不追**——量太大，只标日期和出处 |
| 保留 | 永久 | `--keep-days` 默认 180 天 |
| 用途 | 看领域卡在哪 | **挑题** |

产物 `site/open/questions.json` 是**累积**的：每次只处理没处理过的论文（抓不到全文的也记上，
免得每周为它白跑一趟），已有的原样保留。重新处理某篇时会先把它上一轮的问题摘掉再接上新的——
不这么做的话 `--redo` 会把同一篇的问题追加两份，这个坑踩过。

**想铺底的话注意 `export.arxiv.org` 的限流。** 抓全文走的是 `arxiv.org/html`，很宽松
（92 篇连抓一次限流都没触发）；但要多几天的候选就得先用 `daily.py --days N` 补每日归档，
那条路走的是 `export.arxiv.org`，它的限流严得多，而且是**跨脚本累计**的——
`open_problems.py --search` 连发几十条检索之后，`daily.py` 会被按在 429 上退避到 180 秒，
一天要磨八九分钟。真要回填就单独找个时间跑，别跟 `--search` 挤在一起；
`daily.py` 会跳过已有的日子，分几次跑没有副作用。

### 证据链接是可核对的

"提供 evidence"这句话要站得住，链接就不能是死的。`--check-refs` 会把登记表里每条链接都请求
一遍；arXiv 链接还多查一步：**把 abs 页的真实标题取回来和登记表里写的比对**，链接活着但指向
另一篇论文同样算错。当前 56 条链接全部通过，6 条 arXiv 引用的标题全部对上。

CI 每周也跑一次这个核对，结果写进 job summary，但**不会**因此让整条流水线失败——链接失效
是要人去修的事，不该挡住当周的清单更新。

### 增量成本

判一篇要花一次模型调用，而摘要不会变，所以判过的 `(问题, arXiv id)` 组合直接复用，
每周跑一次的真实成本只有当周新出现的那几篇。

缓存单独存在 `site/open/verdicts.json`，**不是从 index.json 里反推的**——一开始是反推的，
结果跑一次 `--no-llm` 就会写出一份线索更少的产物，连带把上一轮的判定一起冲掉，下次全部重判。
缓存不该被渲染产物的形状绑架。
`--per-problem`（默认 3）和 `--max-judge`（默认 40）再压一道上限，`--rejudge` 才会全部重来。

默认的 40 是给**每周增量**用的（一周下来真正新出现的候选没那么多）。**第一次给一份新清单
铺底**时不够——31 个问题每个判 3 篇就是 93 次，跑到一半预算就见底，后面的问题一条线索都没有。
铺底时把 `--max-judge` 调大，分几次跑也行：判过的进缓存，第二趟只判上一趟没轮到的。

**HTTP 缓存在这里必须有有效期**（`--cache-ttl-days`，默认 1 天），这一点和每日那条线正相反：
每日查的是不同的日期区间，URL 天然不同，永久缓存没问题；而这里的检索式**每次都一模一样**
（"最近 N 篇匹配的论文"），永久缓存会让下一周原样重放上一周的结果，新论文一篇也看不到。
链接核对同理——缓存住就永远检不出失效的链接。

[.github/workflows/open-problems.yml](.github/workflows/open-problems.yml) 每周一 07:00 UTC 跑一次
（比每日那条晚一小时，免得两条流水线同时往仓库推），提交 `site/open/`，再把部署流水线调起来。

产物目前约 290KB（gzip 后约 80KB），和每日那条线一样是**点开这个 tab 时才 fetch** 的，
`index.html` 一动不动。大头是每条线索都原样存了摘要——判定是模型给的，读者得能自己核对
它依据的是什么，这个空间值得花。真嫌大就调小 `--keep-signals`（默认 6）。

## 输出

默认写到 `data/`：

- `tcs_papers.jsonl` — 每行一篇论文的完整 JSON，作者保留 DBLP 的 `pid`（用于重名消歧）
- `tcs_papers.csv` — 扁平化表格，作者用 `; ` 连接，可直接丢进 Excel / pandas
- `tcs_papers.db` — SQLite，含 `papers` / `authors` / `paper_authors` 三张表，方便做作者维度的统计

字段：

| 字段 | 说明 |
|------|------|
| `venue` / `venue_full` | 会议短名 / 全称 |
| `year` | 年份（以 DBLP 记录为准）|
| `title` | 标题（已去掉 DBLP 惯例的结尾句点）|
| `authors` | 作者列表；JSONL 里含 `name`（已去掉重名编号后缀）、`raw_name`、`pid` |
| `pages` | 页码 |
| `doi` / `ee` | DOI 与出版商链接 |
| `dblp_key` / `dblp_url` | DBLP 记录 id（全局唯一，可做主键）与页面链接 |
| `access` | `open` / `closed`，是否开放获取 |
| `toc_key` | 来源 proceedings，如 `db/conf/stoc/stoc2023.bht` |

SQLite 用起来大概是这样：

```sql
-- 各会议历年论文数
SELECT venue, year, COUNT(*) FROM papers GROUP BY venue, year ORDER BY venue, year;

-- 发文最多的作者（按 pid 去重，避免同名混淆）
SELECT name, COUNT(*) c FROM paper_authors GROUP BY pid ORDER BY c DESC LIMIT 20;

-- 某作者在各会议的分布
SELECT p.venue, COUNT(*) FROM papers p
JOIN paper_authors a ON a.dblp_key = p.dblp_key
WHERE a.name = 'Aaron Sidford' GROUP BY p.venue;
```

## 常用参数

```
-v, --venues       要抓的会议，默认全部
    --since/--until 年份区间（含端点）
-o, --out          输出目录，默认 data/
    --formats      jsonl / csv / sqlite，可多选，默认 jsonl csv
    --per-venue    每个会议额外单独输出一份
    --delay        请求间隔秒数，默认 3.0
    --max-delay    被限流时自适应放大的上限，默认 20
    --cache-dir    HTTP 缓存目录，默认 .cache/
    --no-cache     禁用缓存
    --refresh      忽略已有缓存强制重抓（更新最新一届时用）
    --cache-ttl-days  缓存有效期，默认永久
    --user-agent   建议填自己的邮箱
    --include-editorship / --include-front-matter  保留论文集条目 / 卷首目录条目
```

## 工作原理

1. 抓会议目录页 `https://dblp.org/db/conf/<key>/index.html`，正则解析出历届 proceedings
   的 TOC 文件名（`stoc2023.html` → `db/conf/stoc/stoc2023.bht`）。老届次用两位年份命名
   （`focs60`、`soda90`），代码会正确还原成 1960 / 1990。
2. 对每届 TOC 调 DBLP 检索 API：`https://dblp.org/search/publ/api?q=toc:<bht>:&format=json`。
   服务端单次最多返回 100 条（请求更大的 `h` 也会被截断），因此用 `f` 偏移翻页。
3. 过滤掉论文集自身的 editorship 条目，以及 LIPIcs 类会议（ITCS）里的卷首 / 目录 / 前言条目。

## 关于限速与缓存

DBLP 对自动化访问限流比较严：触发后会先返回 429，接着直接掐断连接。所以：

- 默认串行请求、间隔 3 秒，**不要为了快把 `--delay` 调到 1 秒以下**；
- 遇到 429 或连接重置会自适应把间隔放大（上限 `--max-delay`），连续成功后再慢慢降回来；
- 每个 URL 的响应都缓存在 `.cache/`，中断后直接重跑即可，已抓的页不会重复请求；
- 某届抓到一半失败不会丢掉已抓部分，运行结束会列出不完整的届次，重跑一次通常就补齐了。

想更新最新一届（论文还在陆续录入 DBLP 时）：

```bash
python crawl.py --since 2026 --refresh
```

## 目录结构

```
crawl.py                  会议论文抓取入口
analyze.py                主题分析，产出 site_data.json 和作者先验表
fetch_abstracts.py        从 OpenAlex 补摘要
build_site.py             把分析结果嵌进模板，产出 site/index.html
daily.py                  每日 arXiv 抓取 + 分析入口
open_problems.py          未解问题清单：找候选解决方案 + 判定入口
paper_questions.py        从论文结论里抽作者留下的公开问题
problems/
  registry.json           未解问题登记表（手工维护，脚本只读不写）
tcs_crawler/
  venues.py               会议 → DBLP key 映射
  http.py                 限速 / 重试 / 缓存的 HTTP 客户端（纯标准库）
  dblp.py                 目录页解析 + 检索 API 翻页
  arxiv.py                arXiv Atom API，按提交日取当天新论文
  ranking.py              每日论文的可解释打分与精选
  gemini.py               Gemini REST 客户端（纯标准库）
  digest.py               每日速读与综述的提示词和输出 schema
  openprob.py             未解问题的登记表校验、候选发现、判定与链接核对
  fulltext.py             取 arXiv HTML 全文，切出结论/讨论那几节
  paperq.py               从结论里抽公开问题的提示词、schema 与引文核对
  topics.py               关键词主题分类规则
  models.py               Paper / Author 数据模型
  storage.py              JSONL / CSV / SQLite 输出
  cli.py                  命令行
  prolific_authors.json   五大会议高产作者表（analyze.py 产出，随仓库提交）
site/
  template.html           页面模板，四个 tab（__SITE_DATA__ 占位）
  index.html              构建产物，自包含
  daily.html              重定向桩，转到 index.html#daily（旧链接兼容）
  daily/YYYY-MM-DD.json   每天一份结果，同时也是这个功能的持久化存储
  daily/index.json        日期目录
  open/index.json         未解问题清单的构建产物（登记表 + 机器找到的线索）
  open/verdicts.json      模型判定缓存，判过的不再判
  open/questions.json     论文里抽出的公开问题（累积存储）
```

## 可能的扩展

- **摘要**：DBLP 不提供摘要。ITCS 走 LIPIcs（drops.dagstuhl.de）有开放摘要；STOC/FOCS/SODA/EC
  可以用 DOI 去 Crossref (`api.crossref.org/works/<doi>`) 或 Semantic Scholar API 补齐。
- **把每日预印本和会议论文对上**：多数 TCS 论文先挂 arXiv 后进会议，可以用标题匹配把
  `site/daily/` 里的记录和 `data/tcs_papers.jsonl` 关联起来，看某篇预印本后来中了哪个会。
- **引用数**：Semantic Scholar 的 `/graph/v1/paper/DOI:<doi>` 可以拿到引用数。

上面这些都建议复用 `tcs_crawler/http.py` 里的客户端（带缓存和限速），新增一个 enrich 步骤即可。
