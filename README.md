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
crawl.py                  入口
tcs_crawler/
  venues.py               会议 → DBLP key 映射
  http.py                 限速 / 重试 / 缓存的 HTTP 客户端（纯标准库）
  dblp.py                 目录页解析 + 检索 API 翻页
  models.py               Paper / Author 数据模型
  storage.py              JSONL / CSV / SQLite 输出
  cli.py                  命令行
```

## 可能的扩展

- **摘要**：DBLP 不提供摘要。ITCS 走 LIPIcs（drops.dagstuhl.de）有开放摘要；STOC/FOCS/SODA/EC
  可以用 DOI 去 Crossref (`api.crossref.org/works/<doi>`) 或 Semantic Scholar API 补齐。
- **预印本**：多数 TCS 论文有 arXiv 或 ECCC 版本，可用标题在 arXiv API 上做匹配。
- **引用数**：Semantic Scholar 的 `/graph/v1/paper/DOI:<doi>` 可以拿到引用数。

上面这些都建议复用 `tcs_crawler/http.py` 里的客户端（带缓存和限速），新增一个 enrich 步骤即可。
