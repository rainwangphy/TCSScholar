"""TCS 顶会论文元数据抓取工具（数据源：DBLP）。"""

from .models import Author, Paper
from .venues import DEFAULT_VENUES, VENUES, Venue, get_venue

__version__ = "0.1.0"
__all__ = ["Author", "Paper", "Venue", "VENUES", "DEFAULT_VENUES", "get_venue"]
