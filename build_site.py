#!/usr/bin/env python3
"""把分析结果嵌进 HTML 模板，产出一个自包含、可离线打开的页面。"""

from __future__ import annotations

import json
from pathlib import Path

SITE = Path("site")
TEMPLATE = SITE / "template.html"
DATA = Path("data") / "site_data.json"
OUT = SITE / "index.html"


def main() -> None:
    if not DATA.exists():
        raise SystemExit(f"找不到 {DATA}，请先运行 python analyze.py")

    payload = json.loads(DATA.read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    main()
