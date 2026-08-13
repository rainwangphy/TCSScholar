#!/usr/bin/env python3
"""TCS 顶会论文抓取入口。用法见 python crawl.py --help"""

import sys

from tcs_crawler.cli import main

if __name__ == "__main__":
    sys.exit(main())
