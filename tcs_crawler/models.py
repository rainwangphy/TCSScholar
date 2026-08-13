"""数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Author:
    name: str
    pid: str = ""  # DBLP 作者 id，可用于消歧（同名作者）

    @property
    def clean_name(self) -> str:
        """DBLP 会给重名作者加编号后缀，如 'Mohsen Ghaffari 0001'。"""
        parts = self.name.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
            return parts[0]
        return self.name


@dataclass
class Paper:
    venue: str  # 会议短名，如 focs
    venue_full: str  # 会议全称
    year: int
    title: str
    authors: list[Author] = field(default_factory=list)
    pages: str = ""
    doi: str = ""
    ee: str = ""  # 电子版链接（出版商页面）
    dblp_key: str = ""  # 如 conf/stoc/0001G23，全局唯一
    dblp_url: str = ""
    toc_key: str = ""  # 来源 proceedings，如 db/conf/stoc/stoc2023.bht
    type: str = ""
    access: str = ""  # open / closed

    @property
    def author_names(self) -> list[str]:
        return [a.clean_name for a in self.authors]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["authors"] = [{"name": a.clean_name, "raw_name": a.name, "pid": a.pid} for a in self.authors]
        return d

    def to_row(self) -> dict:
        """扁平化为 CSV 一行。"""
        return {
            "venue": self.venue,
            "year": self.year,
            "title": self.title,
            "authors": "; ".join(self.author_names),
            "num_authors": len(self.authors),
            "pages": self.pages,
            "doi": self.doi,
            "ee": self.ee,
            "dblp_key": self.dblp_key,
            "dblp_url": self.dblp_url,
            "type": self.type,
            "access": self.access,
            "venue_full": self.venue_full,
            "toc_key": self.toc_key,
        }
