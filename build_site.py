#!/usr/bin/env python3
"""把分析结果嵌进 HTML 模板，产出页面本体和按需加载的摘要分片。

index.html 保持自包含（分析部分离线可用）；摘要另算——全部内联会让页面涨到 20MB
以上，所以切成分片放在 site/abstracts/ 下，点开某篇时才去取它所在的那一片。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

SITE = Path("site")
TEMPLATE = SITE / "template.html"
DATA = Path("data") / "site_data.json"
ABSTRACTS = Path("data") / "abstracts.jsonl"
OUT = SITE / "index.html"
ABS_DIR = SITE / "abstracts"

# 每片多少篇。200 篇约 230KB，点开一篇的代价可以接受，分片数也不至于太碎。
SHARD = 200


def load_abstracts() -> dict[str, str]:
    if not ABSTRACTS.exists():
        return {}
    out = {}
    for line in ABSTRACTS.open(encoding="utf-8"):
        if line.strip():
            rec = json.loads(line)
            out[rec["dblp_key"]] = rec["abstract"]
    return out


def write_shards(papers: list[list], abstracts: dict[str, str]) -> int:
    """按 compact.papers 的下标切片，第 i 篇落在 i // SHARD 片的 i % SHARD 位。

    下标即定位方式，所以分片必须和嵌进页面的 papers 数组同序——两者都由这里同一次
    构建产出，不会走偏。
    """
    if ABS_DIR.exists():
        shutil.rmtree(ABS_DIR)  # 换数据后重建，避免留下上一轮的孤儿分片
    ABS_DIR.mkdir(parents=True)

    n = 0
    for start in range(0, len(papers), SHARD):
        chunk = papers[start : start + SHARD]
        # dblp_key 在 compact 记录里是第 6 个字段
        texts = [abstracts.get(p[5], "") for p in chunk]
        n += sum(1 for t in texts if t)
        path = ABS_DIR / f"{start // SHARD}.json"
        path.write_text(json.dumps(texts, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    return n


def main() -> None:
    if not DATA.exists():
        raise SystemExit(f"找不到 {DATA}，请先运行 python analyze.py")

    payload = json.loads(DATA.read_text(encoding="utf-8"))
    abstracts = load_abstracts()
    papers = payload["compact"]["papers"]

    if abstracts:
        n = write_shards(papers, abstracts)
        # 给每篇加一个「有没有摘要」的标记，前端据此决定要不要显示展开按钮，
        # 免得点开才发现是空的
        for p in papers:
            p.append(1 if abstracts.get(p[5]) else 0)
        payload["meta"]["abs_shard"] = SHARD
        payload["meta"]["abs_count"] = n
    else:
        print("没有 data/abstracts.jsonl，跳过摘要（先跑 python fetch_abstracts.py）")
        if ABS_DIR.exists():
            shutil.rmtree(ABS_DIR)

    # 内联进 <script> 时必须让 < 无法闭合标签，否则标题里出现 </script> 会截断页面
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    html = TEMPLATE.read_text(encoding="utf-8")
    if "__SITE_DATA__" not in html:
        raise SystemExit("模板里找不到 __SITE_DATA__ 占位符")
    html = html.replace("__SITE_DATA__", blob)

    OUT.write_text(html, encoding="utf-8")
    print(f"写入 {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  {payload['meta']['total']} 篇论文 · {len(payload['topics'])} 个主题 · "
          f"{payload['meta']['year_min']}-{payload['meta']['year_max']}")
    if abstracts:
        shards = len(list(ABS_DIR.glob("*.json")))
        size = sum(f.stat().st_size for f in ABS_DIR.glob("*.json")) / 1024 / 1024
        print(f"  摘要 {payload['meta']['abs_count']} 篇，切成 {shards} 片共 {size:.1f} MB")


if __name__ == "__main__":
    main()
