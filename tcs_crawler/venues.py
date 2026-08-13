"""TCS 顶会在 DBLP 上的定义。

DBLP 的会议目录 key 与会议通称并不总是一致（ITCS -> conf/innovations，
EC -> conf/sigecom），这里做一次映射。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Venue:
    slug: str  # 命令行里用的短名，如 focs
    name: str  # 会议全称
    dblp_dir: str  # DBLP 目录，如 conf/focs
    toc_prefix: str  # 每届 proceedings 文件名前缀，如 focs2023.html -> focs
    first_year: int  # 已知的第一届年份，仅用于展示
    note: str = ""

    @property
    def index_url(self) -> str:
        return f"https://dblp.org/db/{self.dblp_dir}/index.html"


VENUES: dict[str, Venue] = {
    v.slug: v
    for v in [
        Venue(
            slug="focs",
            name="IEEE Symposium on Foundations of Computer Science",
            dblp_dir="conf/focs",
            toc_prefix="focs",
            first_year=1960,
            note="1960-1974 期间名为 SWAT",
        ),
        Venue(
            slug="stoc",
            name="ACM Symposium on Theory of Computing",
            dblp_dir="conf/stoc",
            toc_prefix="stoc",
            first_year=1969,
        ),
        Venue(
            slug="soda",
            name="ACM-SIAM Symposium on Discrete Algorithms",
            dblp_dir="conf/soda",
            toc_prefix="soda",
            first_year=1990,
        ),
        Venue(
            slug="itcs",
            name="Innovations in Theoretical Computer Science",
            dblp_dir="conf/innovations",
            toc_prefix="innovations",
            first_year=2010,
            note="2010-2011 名为 ICS；DBLP key 为 conf/innovations",
        ),
        Venue(
            slug="ec",
            name="ACM Conference on Economics and Computation",
            dblp_dir="conf/sigecom",
            toc_prefix="sigecom",
            first_year=1999,
            note="2014 年前名为 ACM Conference on Electronic Commerce",
        ),
    ]
}

DEFAULT_VENUES = ["focs", "stoc", "soda", "itcs", "ec"]


def get_venue(slug: str) -> Venue:
    key = slug.strip().lower()
    if key not in VENUES:
        raise KeyError(
            f"未知会议 {slug!r}，可选：{', '.join(sorted(VENUES))}"
        )
    return VENUES[key]
