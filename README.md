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

另有一页每日 arXiv 速读：**https://rainwangphy.github.io/TCSScholar/daily.html**，见下面的
[每日 arXiv 速读](#每日-arxiv-速读)。

本地重新生成：

```bash
python analyze.py                                        # 读 data/ 里的抓取结果，写出 data/site_data.json
python fetch_abstracts.py --mailto you@example.com       # 可选：从 OpenAlex 补摘要（约 20-40 分钟）
python build_site.py                                     # 产出 site/index.html 和 site/abstracts/
```

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
模型写中文速读，发布在 **https://rainwangphy.github.io/TCSScholar/daily.html**。

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
3. **模型** — 只把排在前面的 15 篇送给 Gemini，出「主要结果 / 技术手段 / 为什么值得看 /
   新颖度 / 1-5 星」；另出一段当日综述。约 16 次调用，一天几万 token。

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

### 自动化

[.github/workflows/daily.yml](.github/workflows/daily.yml) 每天 06:00 UTC 跑一次，抓昨天的论文、
提交 `site/daily/`，然后把部署那条流水线当可复用 workflow 调起来——Actions 用 `GITHUB_TOKEN`
推的 commit 不会再触发 `on: push`，所以必须显式调用。

要让它真的调模型，得在仓库 Settings → Secrets → Actions 里加一个 `GEMINI_API_KEY`；
没加也不会失败，只是每天都只有规则层的结果。也可以在 Actions 页面手动触发，能指定日期和天数。

和会议论文库那条线不一样的是：**每日这条线 CI 能完整跑完**。它不依赖 gitignore 掉的 `data/`，
结果直接写进随仓库提交的 `site/daily/`，那里既是网页数据源也是持久化存储。日积月累每天约
80KB，真嫌多可以用 `--keep-days N` 只留最近 N 天。

### 为什么是独立一页

`site/index.html` 把 2.5MB 的分析数据内联在文件里，每天重新构建再提交一份 2.5MB 的 HTML
不现实，CI 也没有 `data/` 可以重新生成它。`daily.html` 反过来：页面本身是不变的静态文件，
数据运行时从 `daily/*.json` 取，所以 CI 每天只需要提交一个 80KB 的 JSON。

代价是这一页**必须联网**才能看到内容（双击本地文件打开时，浏览器的同源策略会挡掉 fetch，
页面会提示你起个本地服务器）。公式用 CDN 上的 KaTeX 渲染，CDN 被挡掉时退化成显示 LaTeX 原文，
信息不会少。

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
tcs_crawler/
  venues.py               会议 → DBLP key 映射
  http.py                 限速 / 重试 / 缓存的 HTTP 客户端（纯标准库）
  dblp.py                 目录页解析 + 检索 API 翻页
  arxiv.py                arXiv Atom API，按提交日取当天新论文
  ranking.py              每日论文的可解释打分与精选
  gemini.py               Gemini REST 客户端（纯标准库）
  digest.py               每日速读与综述的提示词和输出 schema
  topics.py               关键词主题分类规则
  models.py               Paper / Author 数据模型
  storage.py              JSONL / CSV / SQLite 输出
  cli.py                  命令行
  prolific_authors.json   五大会议高产作者表（analyze.py 产出，随仓库提交）
site/
  template.html           会议论文库的页面模板（__SITE_DATA__ 占位）
  index.html              构建产物，自包含
  daily.html              每日速读页，静态；数据运行时从 daily/ 取
  daily/YYYY-MM-DD.json   每天一份结果，同时也是这个功能的持久化存储
  daily/index.json        日期目录
```

## 可能的扩展

- **摘要**：DBLP 不提供摘要。ITCS 走 LIPIcs（drops.dagstuhl.de）有开放摘要；STOC/FOCS/SODA/EC
  可以用 DOI 去 Crossref (`api.crossref.org/works/<doi>`) 或 Semantic Scholar API 补齐。
- **把每日预印本和会议论文对上**：多数 TCS 论文先挂 arXiv 后进会议，可以用标题匹配把
  `site/daily/` 里的记录和 `data/tcs_papers.jsonl` 关联起来，看某篇预印本后来中了哪个会。
- **引用数**：Semantic Scholar 的 `/graph/v1/paper/DOI:<doi>` 可以拿到引用数。

上面这些都建议复用 `tcs_crawler/http.py` 里的客户端（带缓存和限速），新增一个 enrich 步骤即可。
